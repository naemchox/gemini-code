"""Run a tool on the model's behalf: show the call, preview changes, ask for approval, execute, show the result.

Shared by the API agent (function calling) and web mode (@@ action lines).
"""

from rich.console import Console
from rich.syntax import Syntax
from rich.text import Text

from .tools import TOOLS, ToolError


def _short(value, limit: int = 80) -> str:
    text = str(value)
    if "\n" in text:
        return f"<{len(text.splitlines())} lines>"
    return text if len(text) <= limit else text[:limit] + "…"


class ToolRunner:
    def __init__(self, console: Console, yolo: bool = False):
        self.console = console
        self.yolo = yolo
        self.always_allowed: set[str] = set()

    def run(self, name: str, args: dict) -> dict:
        """Return {"output": str} on success or {"error": str} (also for a denied call)."""
        arg_summary = ", ".join(f"{k}={_short(v)}" for k, v in args.items())
        self.console.print(Text(f"● {name}({arg_summary})", style="bold magenta"))

        tool = TOOLS.get(name)
        try:
            if tool is None:
                raise ToolError(f"Unknown tool: {name}")
            if tool.needs_approval and not self.yolo and name not in self.always_allowed:
                if tool.preview:
                    self.console.print(Syntax(tool.preview(**args), "diff", theme="ansi_dark", word_wrap=True))
                denial = self._ask_permission(name)
                if denial:
                    raise ToolError(denial)
            result = {"output": tool.func(**args)}
            self._print_result(result["output"], style="dim")
        except ToolError as e:
            result = {"error": str(e)}
        except (TypeError, ValueError) as e:
            result = {"error": f"Invalid arguments: {e}"}
        except OSError as e:
            result = {"error": f"{type(e).__name__}: {e}"}
        if "error" in result:
            self._print_result(result["error"], style="red")
        return result

    def _print_result(self, text: str, style: str, max_lines: int = 6) -> None:
        lines = text.splitlines() or [""]
        shown = "\n".join(f"  ⎿ {line}" if i == 0 else f"    {line}" for i, line in enumerate(lines[:max_lines]))
        if len(lines) > max_lines:
            shown += f"\n    … (+{len(lines) - max_lines} บรรทัด)"
        self.console.print(Text(shown, style=style))

    def _ask_permission(self, tool_name: str) -> str | None:
        """Return None if allowed, otherwise a denial message for the model."""
        while True:
            try:
                answer = self.console.input(
                    f"[bold]อนุญาตไหม?[/bold] [green]y[/green]=ใช่  "
                    f"[green]a[/green]=ใช่ทุกครั้งสำหรับ {tool_name} ในเซสชันนี้  [red]n[/red]=ไม่: "
                ).strip().lower()
            except EOFError:  # no one to answer (input closed): never approve by default
                self.console.print()
                return "The user denied this tool call (no answer)."
            if answer in ("y", "yes"):
                return None
            if answer in ("a", "always"):
                self.always_allowed.add(tool_name)
                return None
            if answer in ("n", "no"):
                try:
                    feedback = self.console.input("บอก Gemini ว่าควรทำอะไรแทน (Enter เพื่อข้าม): ").strip()
                except EOFError:
                    feedback = ""
                message = "The user denied this tool call."
                return f"{message} User feedback: {feedback}" if feedback else message
