"""Text protocol that lets Gemini on the web work with files.

The web app has no function calling, so the first message of each chat teaches Gemini to write action
lines such as `@@WRITE app.py` followed by a code block. gcode parses them from the rendered response,
runs them through ToolRunner, and sends the results back as the next message.
"""

import platform
import re
from dataclasses import dataclass, field
from pathlib import Path

from .tools import ToolError, read_file

MAX_RESULT_CHARS = 15_000
MAX_MESSAGE_CHARS = 40_000
PROJECT_INSTRUCTION_FILES = ("GEMINI.md", "AGENTS.md")

PROTOCOL = """[ข้อความระบบจาก gcode]
You are connected to gcode, a terminal app on the user's computer. You CAN read, create, edit and delete \
files on the user's computer and run commands, by writing ACTION lines. gcode runs them (asking the user \
for approval before changing anything) and sends the results back in the next message. Never tell the user \
you cannot access their local files: use the actions instead.

Working directory: {cwd}
OS: {os}

Actions, each on its own line starting with @@:
@@LIST <dir>              list a directory ("." = working directory)
@@FIND <glob>             find files, e.g. @@FIND **/*.py
@@GREP <regex> [path]     search file contents
@@READ <path>             read a file (returned with line numbers)
@@WRITE <path>            create or overwrite a file: put the COMPLETE file content in ONE code block right after the line
@@EDIT <path>             change part of a file: right after the line put TWO code blocks, first the exact current text, then the replacement
@@DELETE <path>           delete a file (moved to the Trash)
@@RUN <command>           run a shell command in the working directory (no interactive commands)

Rules:
- Paths can be relative to the working directory or absolute (such as a path the user pasted, even \
outside the working directory). Write them as plain text without backticks or quotes.
- Files the user mentions may already be attached below the request as @@RESULT READ; use them directly.
- Never use Canvas. File content always goes in a code block in the chat, even for plain text or a single line.
- Look before changing: LIST/FIND/READ first. EDIT's first code block must match the file exactly, including indentation.
- Prefer EDIT for small changes to existing files; WRITE for new files or full rewrites.
- You may write several actions in one message. After the actions, STOP and wait. Never invent results.
- Results arrive as lines starting with @@RESULT. The user may deny an action; then adjust or ask.
- When the task is done, reply with a short summary and no actions.
- If the request does not involve files, just answer normally without actions.
- Reply in the user's language.{project}

User's request:
{request}"""

RESULTS_FOOTER = (
    "Continue the task. Use more @@ actions if needed; otherwise reply with a short summary and no actions."
)
# Appended to every later user message so Gemini doesn't forget the protocol in long chats.
FOLLOW_UP_REMINDER = "\n\n(gcode: you can work with the user's files using @@ actions.)"
MAX_ATTACHED_FILE_CHARS = 20_000
# Quoted strings, or unquoted tokens where "\ " is an escaped space (as when dragging a file into Terminal).
PATH_CANDIDATES = re.compile(r"'([^'\n]+)'|\"([^\"\n]+)\"|((?:[^\s\\'\"]|\\.)+)")

ACTION_LINE = re.compile(r"^@@(LIST|FIND|GREP|READ|WRITE|EDIT|DELETE|RUN)\b[ \t]*(.*)$")
CODE_BLOCKS_NEEDED = {"WRITE": 1, "EDIT": 2}

# The newest response as an ordered list of blocks: code blocks verbatim, everything else as text + HTML.
RESPONSE_BLOCKS_JS = """m => {
  const root = m.querySelector('message-content .markdown') || m.querySelector('message-content');
  if (!root) return [];
  const out = [];
  const walk = n => {
    if (n.nodeType !== 1) {
      const t = (n.textContent || '').trim();
      if (t) out.push({type: 'text', text: t, html: t});
      return;
    }
    const tag = n.tagName.toLowerCase();
    if (tag === 'code-block') {
      out.push({type: 'code', lang: (n.querySelector('.code-block-decoration span')?.textContent || '').trim().toLowerCase(),
                code: n.querySelector('code')?.textContent ?? ''});
      return;
    }
    if (tag === 'generated-image' || n.classList.contains('attachment-container')) return;
    if (!n.querySelector('code-block')) {
      const t = (n.innerText || '').trim();
      if (t) {
        const clone = n.cloneNode(true);
        clone.querySelectorAll('button, mat-icon, sources-list').forEach(e => e.remove());
        out.push({type: 'text', text: t, html: clone.outerHTML});
      }
      return;
    }
    n.childNodes.forEach(walk);
  };
  root.childNodes.forEach(walk);
  return out;
}"""


@dataclass
class Action:
    kind: str
    arg: str
    code: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.kind} {self.arg}".strip()

    def to_tool_call(self) -> tuple[str, dict]:
        """Map to a tools.py call. Raises ValueError when the action is malformed."""
        needed = CODE_BLOCKS_NEEDED.get(self.kind, 0)
        if len(self.code) < needed:
            raise ValueError(f"{self.kind} needs {needed} code block(s) right after the action line")
        if self.kind not in ("LIST", "RUN") and not self.arg:
            raise ValueError(f"{self.kind} needs an argument")
        if self.kind == "LIST":
            return "list_dir", {"path": self.arg or "."}
        if self.kind == "FIND":
            return "glob_files", {"pattern": self.arg}
        if self.kind == "GREP":
            pattern, _, path = self.arg.rpartition(" ")
            if pattern and (Path.cwd() / path).expanduser().exists():
                return "grep", {"pattern": pattern, "path": path}
            return "grep", {"pattern": self.arg}
        if self.kind == "READ":
            return "read_file", {"path": self.arg}
        if self.kind == "WRITE":
            return "write_file", {"path": self.arg, "content": self.code[0]}
        if self.kind == "EDIT":
            # Code blocks end with a newline the model didn't mean as part of the snippet.
            old, new = (c.removesuffix("\n") for c in self.code[:2])
            return "edit_file", {"path": self.arg, "old_string": old, "new_string": new}
        if self.kind == "DELETE":
            return "delete_file", {"path": self.arg}
        if not self.arg:
            raise ValueError("RUN needs a command")
        return "run_shell", {"command": self.arg}


def find_mentioned_files(text: str) -> list[Path]:
    """Existing files the user referred to by path in their message."""
    found: list[Path] = []
    for match in PATH_CANDIDATES.finditer(text):
        raw = next(g for g in match.groups() if g is not None)
        raw = re.sub(r"\\(.)", r"\1", raw).rstrip(".,;:)!?")
        if "/" not in raw and "." not in raw:
            continue
        try:
            path = (Path.cwd() / Path(raw).expanduser()).resolve()
            if path.is_file() and path not in found:
                found.append(path)
        except (OSError, ValueError):
            continue
    return found


def attach_mentioned_files(text: str) -> str:
    """Read files mentioned in the message and attach them, so Gemini doesn't need a round trip to READ."""
    parts = []
    for path in find_mentioned_files(text):
        try:
            content = read_file(str(path))
        except ToolError as e:
            parts.append(f"@@RESULT READ {path}\n{_fenced(f'ERROR: {e}')}")
            continue
        if len(content) > MAX_ATTACHED_FILE_CHARS:
            note = f"File is large ({path.stat().st_size:,} bytes); showing the start. Use @@READ for the rest."
            content = f"{content[:MAX_ATTACHED_FILE_CHARS]}\n...[{note}]"
        parts.append(f"@@RESULT READ {path}\n{_fenced(content)}")
    if not parts:
        return text
    return text + "\n\n[gcode แนบไฟล์ที่ผู้ใช้อ้างถึงให้แล้ว]\n" + "\n\n".join(parts)


def build_follow_up_message(request: str) -> str:
    return attach_mentioned_files(request) + FOLLOW_UP_REMINDER


def build_first_message(request: str) -> str:
    request = attach_mentioned_files(request)
    cwd = Path.cwd()
    project = ""
    for name in PROJECT_INSTRUCTION_FILES:
        f = cwd / name
        if f.is_file():
            project += f"\n\nProject instructions from {name}:\n{f.read_text(encoding='utf-8', errors='replace')}"
    return PROTOCOL.format(cwd=cwd, os=f"{platform.system()} {platform.release()}", project=project, request=request)


def _clean_arg(arg: str) -> str:
    return arg.strip().strip("`'\"").strip()


def parse_response(blocks: list[dict]) -> tuple[list[Action], list[dict]]:
    """Split a response into actions and the blocks to display (action lines and their code removed)."""
    actions: list[Action] = []
    display: list[dict] = []
    i = 0
    while i < len(blocks):
        block = blocks[i]
        i += 1
        if block["type"] == "code":
            # Some replies put the action line inside the code block itself.
            first, _, rest = block["code"].partition("\n")
            match = ACTION_LINE.match(first.strip())
            if match and match.group(1) == "WRITE":
                actions.append(Action("WRITE", _clean_arg(match.group(2)), [rest]))
            else:
                display.append(block)
            continue

        found, kept = [], []
        for line in block["text"].splitlines():
            match = ACTION_LINE.match(line.strip())
            if match:
                found.append(Action(match.group(1), _clean_arg(match.group(2))))
            else:
                kept.append(line)
        if not found:
            display.append(block)
            continue
        if any(line.strip() for line in kept):
            display.append({"type": "text", "text": "\n".join(kept), "html": None})
        for action in found:
            while len(action.code) < CODE_BLOCKS_NEEDED.get(action.kind, 0) and i < len(blocks) \
                    and blocks[i]["type"] == "code":
                action.code.append(blocks[i]["code"])
                i += 1
            actions.append(action)
    return actions, display


def _fenced(text: str) -> str:
    fence = "````"
    while fence in text:
        fence += "`"
    return f"{fence}\n{text}\n{fence}"


def format_results(results: list[tuple[Action, dict]]) -> str:
    parts = []
    budget = MAX_MESSAGE_CHARS
    for action, result in results:
        if "error" in result:
            body = f"ERROR: {result['error']}"
        else:
            body = result["output"]
        if len(body) > MAX_RESULT_CHARS:
            body = body[:MAX_RESULT_CHARS] + f"\n...[truncated {len(body) - MAX_RESULT_CHARS} chars]"
        if len(body) > budget:
            body = body[:max(budget, 0)] + "\n...[truncated: message too long, read less at a time]"
        budget -= len(body)
        parts.append(f"@@RESULT {action.label}\n{_fenced(body)}")
    return "\n\n".join(parts) + f"\n\n{RESULTS_FOOTER}"
