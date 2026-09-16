"""Agent loop: send the conversation to Gemini, run the tools it asks for, repeat."""

import platform
import re
import time
from datetime import date
from pathlib import Path

from google import genai
from google.genai import errors, types
from rich.console import Console
from rich.markdown import Markdown

from .runner import ToolRunner
from .tools import TOOL_DECLARATIONS
from .usage import UsageTracker

MAX_STEPS = 60
MAX_RETRIES = 6
MAX_RETRY_WAIT = 90
RETRYABLE_CODES = (429, 500, 503, 504)
PROJECT_INSTRUCTION_FILES = ("GEMINI.md", "AGENTS.md")

SYSTEM_PROMPT = """You are Gemini Code, a coding agent running in the user's terminal. You help with \
software engineering tasks by reading, searching and editing files and running shell commands through \
the provided tools.

Environment:
- Working directory: {cwd}
- OS: {os}
- Date: {today}

Guidelines:
- Explore before changing: use list_dir, glob_files, grep and read_file to understand the code. \
Never edit a file you have not read in this conversation.
- Prefer edit_file for targeted changes; use write_file only for new files or full rewrites.
- Match the existing code style. Keep changes focused on what the user asked for.
- Requests are rate limited: call several independent tools in one turn instead of one at a time, \
and avoid unnecessary calls.
- After changing code, verify when practical (run the tests, a linter, or the program).
- The user may deny a tool call. Do not retry the same action; adjust your approach or ask.
- Never run destructive commands (rm -rf, git reset --hard, force pushes, ...) unless the user explicitly asked.
- Be concise. Reply in the language the user writes in. Reference code as path:line."""


def build_system_prompt() -> str:
    cwd = Path.cwd()
    prompt = SYSTEM_PROMPT.format(cwd=cwd, os=f"{platform.system()} {platform.release()}", today=date.today())
    for name in PROJECT_INSTRUCTION_FILES:
        f = cwd / name
        if f.is_file():
            prompt += f"\n\nProject instructions from {name}:\n{f.read_text(encoding='utf-8', errors='replace')}"
    return prompt


def _retry_delay(err: errors.APIError) -> float | None:
    """Read the server-suggested wait (e.g. retryDelay: '37s') from a 429 response."""
    match = re.search(r"retryDelay'?\"?:\s*'?\"?(\d+(?:\.\d+)?)s", str(err.details))
    return float(match.group(1)) + 1 if match else None


class Agent:
    def __init__(self, client: genai.Client, model: str, console: Console, yolo: bool = False):
        self.client = client
        self.model = model
        self.console = console
        self.runner = ToolRunner(console, yolo=yolo)
        self.history: list[types.Content] = []
        self.usage = UsageTracker()
        self.reset()

    def reset(self) -> None:
        self.history.clear()
        self.config = types.GenerateContentConfig(
            system_instruction=build_system_prompt(),
            tools=[types.Tool(function_declarations=TOOL_DECLARATIONS)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    def run_turn(self, user_text: str) -> None:
        self.history.append(types.Content(role="user", parts=[types.Part(text=user_text)]))
        try:
            for _ in range(MAX_STEPS):
                response = self._generate()
                candidate = response.candidates[0] if response.candidates else None
                if not candidate or not candidate.content or not candidate.content.parts:
                    reason = candidate.finish_reason if candidate else response.prompt_feedback
                    self.console.print(f"[yellow]Gemini ไม่ได้ตอบกลับ ({reason})[/yellow]")
                    return
                self.history.append(candidate.content)
                self._print_text(candidate.content)
                calls = [p.function_call for p in candidate.content.parts if p.function_call]
                if not calls:
                    return
                results = [self._run_tool(call) for call in calls]
                self.history.append(types.Content(role="user", parts=results))
            self.console.print(f"[yellow]หยุดหลังจากทำงานครบ {MAX_STEPS} รอบ พิมพ์ต่อเพื่อให้ทำต่อ[/yellow]")
        except KeyboardInterrupt:
            self._drop_dangling_calls()
            self.console.print("\n[yellow]ยกเลิกแล้ว[/yellow]")
        except errors.APIError as e:
            self._drop_dangling_calls()
            self.console.print(f"[red]API error {e.code}: {e.message}[/red]")

    def _drop_dangling_calls(self) -> None:
        """A model turn with function calls must be followed by their results, or the next request fails."""
        last = self.history[-1] if self.history else None
        if last and last.role == "model" and any(p.function_call for p in last.parts or []):
            self.history.pop()

    def _generate(self) -> types.GenerateContentResponse:
        for attempt in range(MAX_RETRIES):
            try:
                with self.console.status("[cyan]กำลังคิด...[/cyan]"):
                    response = self.client.models.generate_content(
                        model=self.model, contents=self.history, config=self.config,
                    )
                self.usage.record(self.model, response.usage_metadata)
                return response
            except errors.APIError as e:
                if e.code == 429:
                    self.usage.learn_limits(self.model, e.details)
                if e.code not in RETRYABLE_CODES or attempt == MAX_RETRIES - 1:
                    raise
                wait = _retry_delay(e) or min(5 * 2 ** attempt, 60)
                if wait > MAX_RETRY_WAIT:
                    raise
                self.console.print(
                    f"[yellow]API {e.code} (อาจติดโควตาฟรี) รอ {wait:.0f} วินาทีแล้วลองใหม่ "
                    f"({attempt + 1}/{MAX_RETRIES - 1})[/yellow]"
                )
                time.sleep(wait)
        raise AssertionError("unreachable")

    def _print_text(self, content: types.Content) -> None:
        text = "".join(p.text for p in content.parts if p.text and not p.thought)
        if text.strip():
            self.console.print(Markdown(text))

    def _run_tool(self, call: types.FunctionCall) -> types.Part:
        result = self.runner.run(call.name, dict(call.args or {}))
        return types.Part(function_response=types.FunctionResponse(id=call.id, name=call.name, response=result))
