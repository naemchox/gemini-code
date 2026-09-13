"""Tools the model can call: read/write/edit files, search, and run shell commands."""

import difflib
import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from google.genai import types

MAX_OUTPUT_CHARS = 30_000
MAX_SEARCH_RESULTS = 200
MAX_GREP_FILE_BYTES = 2_000_000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tmp", "dist", "build", ".next"}


class ToolError(Exception):
    """An expected failure whose message is sent back to the model."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable[..., str]
    needs_approval: bool = False
    preview: Callable[..., str] | None = None


def _resolve(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.resolve()


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n...[truncated {len(text) - MAX_OUTPUT_CHARS} chars]"


def _is_binary(path: Path) -> bool:
    with path.open("rb") as f:
        return b"\0" in f.read(8192)


def _read_text(path: Path) -> str:
    if not path.exists():
        raise ToolError(f"File not found: {path}")
    if path.is_dir():
        raise ToolError(f"{path} is a directory; use list_dir instead")
    if _is_binary(path):
        raise ToolError(f"{path} looks like a binary file")
    return path.read_text(encoding="utf-8", errors="replace")


def _display(path: Path) -> str:
    return str(path.relative_to(Path.cwd())) if path.is_relative_to(Path.cwd()) else str(path)


def _diff(path: Path, old: str, new: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=_display(path), tofile=_display(path),
    )
    return "".join(lines) or "(no changes)"


def _walk_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


# --- read-only tools ---------------------------------------------------------

def read_file(path: str, offset: int = 1, limit: int = 2000) -> str:
    lines = _read_text(_resolve(path)).splitlines()
    start = max(int(offset), 1) - 1
    chunk = lines[start:start + int(limit)]
    out = "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(chunk, start + 1))
    if start + len(chunk) < len(lines):
        out += f"\n...[file has {len(lines)} lines; use offset to read more]"
    return _truncate(out or "(empty file)")


def list_dir(path: str = ".") -> str:
    p = _resolve(path)
    if not p.is_dir():
        raise ToolError(f"Not a directory: {p}")
    entries = []
    for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
        if child.is_dir():
            entries.append(f"{child.name}/")
        else:
            entries.append(f"{child.name}  ({child.stat().st_size} bytes)")
    return _truncate("\n".join(entries) or "(empty directory)")


def glob_files(pattern: str, path: str = ".") -> str:
    root = _resolve(path)
    matches = [
        m for m in root.glob(pattern)
        if m.is_file() and not SKIP_DIRS.intersection(m.relative_to(root).parts)
    ]
    matches.sort(key=lambda m: m.stat().st_mtime, reverse=True)
    lines = [str(m.relative_to(root)) for m in matches[:MAX_SEARCH_RESULTS]]
    if len(matches) > MAX_SEARCH_RESULTS:
        lines.append(f"...[{len(matches) - MAX_SEARCH_RESULTS} more]")
    return "\n".join(lines) or "No files matched."


def grep(pattern: str, path: str = ".", include: str = "") -> str:
    try:
        regex = re.compile(pattern)
    except re.error as e:
        raise ToolError(f"Invalid regex: {e}")
    root = _resolve(path)
    files = [root] if root.is_file() else _walk_files(root)
    results = []
    for file in files:
        if include and not fnmatch.fnmatch(file.name, include):
            continue
        try:
            if file.stat().st_size > MAX_GREP_FILE_BYTES or _is_binary(file):
                continue
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                shown = file.relative_to(root) if file != root else file.name
                results.append(f"{shown}:{lineno}: {line.strip()[:300]}")
                if len(results) >= MAX_SEARCH_RESULTS:
                    results.append("...[result limit reached; narrow the search]")
                    return "\n".join(results)
    return "\n".join(results) or "No matches."


# --- tools that change things (need approval) --------------------------------

def _plan_write(path: str, content: str) -> tuple[Path, str, str]:
    p = _resolve(path)
    old = _read_text(p) if p.exists() else ""
    return p, old, content


def preview_write(path: str, content: str) -> str:
    p, old, new = _plan_write(path, content)
    if not p.exists():
        return f"(new file) {_display(p)}\n" + "".join(f"+{line}\n" for line in new.splitlines())
    return _diff(p, old, new)


def write_file(path: str, content: str) -> str:
    p, _, new = _plan_write(path, content)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new, encoding="utf-8")
    return f"Wrote {len(new.splitlines())} lines to {p}"


def _plan_edit(path: str, old_string: str, new_string: str, replace_all: bool = False) -> tuple[Path, str, str]:
    p = _resolve(path)
    old = _read_text(p)
    count = old.count(old_string)
    if not old_string or count == 0:
        raise ToolError("old_string not found in file. Read the file again and copy the text exactly.")
    if count > 1 and not replace_all:
        raise ToolError(f"old_string appears {count} times. Include more surrounding context or set replace_all=true.")
    new = old.replace(old_string, new_string) if replace_all else old.replace(old_string, new_string, 1)
    return p, old, new


def preview_edit(**kwargs) -> str:
    p, old, new = _plan_edit(**kwargs)
    return _diff(p, old, new)


def edit_file(**kwargs) -> str:
    p, old, new = _plan_edit(**kwargs)
    p.write_text(new, encoding="utf-8")
    return f"Edited {p}"


def run_shell(command: str, timeout: int = 120) -> str:
    try:
        proc = subprocess.run(
            command, shell=True, cwd=Path.cwd(), capture_output=True,
            text=True, errors="replace", timeout=int(timeout),
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"Command timed out after {timeout}s")
    out = proc.stdout
    if proc.stderr:
        out += ("\n" if out else "") + "[stderr]\n" + proc.stderr
    return _truncate(f"{out}\n[exit code {proc.returncode}]".strip())


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool(
        name="read_file",
        description="Read a text file. Returns lines prefixed with line numbers. Use offset/limit for large files.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path, absolute or relative to the working directory"},
                "offset": {"type": "integer", "description": "1-based line to start from (default 1)"},
                "limit": {"type": "integer", "description": "Max lines to read (default 2000)"},
            },
            "required": ["path"],
        },
        func=read_file,
    ),
    Tool(
        name="list_dir",
        description="List files and subdirectories in a directory.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory path (default: working directory)"}},
        },
        func=list_dir,
    ),
    Tool(
        name="glob_files",
        description="Find files by glob pattern, e.g. '**/*.py'. Results sorted by most recently modified.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern"},
                "path": {"type": "string", "description": "Directory to search in (default: working directory)"},
            },
            "required": ["pattern"],
        },
        func=glob_files,
    ),
    Tool(
        name="grep",
        description="Search file contents with a Python regular expression. Returns file:line: text.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression"},
                "path": {"type": "string", "description": "File or directory to search (default: working directory)"},
                "include": {"type": "string", "description": "Only search file names matching this glob, e.g. '*.py'"},
            },
            "required": ["pattern"],
        },
        func=grep,
    ),
    Tool(
        name="write_file",
        description="Create a new file or completely overwrite an existing one. Prefer edit_file for changes to existing files.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "content": {"type": "string", "description": "Full file content"},
            },
            "required": ["path", "content"],
        },
        func=write_file,
        needs_approval=True,
        preview=preview_write,
    ),
    Tool(
        name="edit_file",
        description=(
            "Replace exact text in a file. old_string must match the file exactly (including indentation) "
            "and be unique unless replace_all is true. Read the file first."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "old_string": {"type": "string", "description": "Exact text to replace"},
                "new_string": {"type": "string", "description": "Replacement text"},
                "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        func=edit_file,
        needs_approval=True,
        preview=preview_edit,
    ),
    Tool(
        name="run_shell",
        description=(
            "Run a shell command in the working directory and return stdout, stderr and exit code. "
            "Each call runs in a fresh shell, so `cd` does not persist. Avoid interactive commands."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 120)"},
            },
            "required": ["command"],
        },
        func=run_shell,
        needs_approval=True,
    ),
]}

TOOL_DECLARATIONS = [
    types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
    for t in TOOLS.values()
]
