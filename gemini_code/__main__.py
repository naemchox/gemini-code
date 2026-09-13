"""Command-line entry point: `python -m gemini_code` or the `gcode` launcher."""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import errors
from rich.console import Console

from . import __version__
from .agent import Agent
from .prompt import enable_line_editing, read_prompt
from .usage import time_until_reset

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "gemini-3-flash-preview"

HELP = """[bold]คำสั่ง[/bold]
  /help            แสดงวิธีใช้
  /clear           เริ่มบทสนทนาใหม่
  /model [ชื่อ]     ดูหรือเปลี่ยนโมเดล
  /models          แสดงรายชื่อโมเดลที่ key นี้ใช้ได้
  /usage           ดูปริมาณการใช้ (เซสชันนี้และวันนี้)
  /exit            ออก (หรือกด Ctrl+D)

พิมพ์ \\ ท้ายบรรทัดเพื่อขึ้นบรรทัดใหม่  กด Ctrl+C เพื่อยกเลิกงานที่กำลังทำ"""


def show_usage(agent: Agent, console: Console) -> None:
    u = agent.usage
    console.print("[bold]เซสชันนี้[/bold]")
    console.print(
        f"  คำขอ {u.requests:,} ครั้ง (นาทีล่าสุด {u.requests_last_minute()})\n"
        f"  input {u.prompt_tokens:,} · output {u.output_tokens:,} · thinking {u.thinking_tokens:,} tokens",
        highlight=False,
    )
    if u.last:
        console.print(
            f"  คำขอล่าสุด: {u.last.total_token_count or 0:,} tokens "
            f"(ขนาดบทสนทนาตอนนี้ ~{u.last.prompt_token_count or 0:,} tokens)",
            highlight=False,
        )

    today = u.today()
    console.print("\n[bold]วันนี้ (นับตามเวลาแปซิฟิกที่โควตารีเซ็ต)[/bold]")
    if not today:
        console.print("  ยังไม่มีคำขอ")
    for model, stats in sorted(today.items()):
        console.print(f"  {model}: {stats['requests']:,} คำขอ · {stats['tokens']:,} tokens", highlight=False)

    limits = u.limits(agent.model)
    reset_minutes = int(time_until_reset().total_seconds() // 60)
    console.print(f"\n[bold]โควตาที่เหลือ ({agent.model})[/bold]", highlight=False)
    for kind, label, used in (
        ("rpm", "ต่อนาที", u.requests_last_minute(agent.model)),
        ("rpd", "ต่อวัน ", today.get(agent.model, {}).get("requests", 0)),
    ):
        if kind not in limits:
            console.print(f"  {label}: ใช้ {used:,} · [dim]ไม่ทราบเพดาน (ใส่ได้ใน limits.json)[/dim]", highlight=False)
            continue
        limit, source = limits[kind]
        left = max(limit - used, 0)
        color = "red" if left == 0 else "yellow" if left <= limit * 0.2 else "green"
        note = "ค่าจริงจาก Google" if source == "google" else "ค่าประมาณ"
        console.print(
            f"  {label}: เหลือ [{color}]{left:,}[/{color}] จาก {limit:,} [dim]({note})[/dim]", highlight=False
        )
    console.print(f"  โควตารายวันรีเซ็ตในอีก {reset_minutes // 60} ชม. {reset_minutes % 60} นาที")
    console.print(
        "\n[dim]นับเฉพาะคำขอที่สำเร็จจากแอปนี้ในเครื่องนี้ "
        "ตัวเลขจริงดูได้ที่ https://aistudio.google.com/usage[/dim]"
    )


def handle_command(command: str, agent: Agent, console: Console) -> bool:
    """Run a slash command. Return False when the app should exit."""
    name, _, arg = command.partition(" ")
    arg = arg.strip()
    if name in ("/exit", "/quit"):
        return False
    if name == "/help":
        console.print(HELP)
    elif name == "/clear":
        agent.reset()
        console.print("[green]เริ่มบทสนทนาใหม่แล้ว[/green]")
    elif name == "/model":
        if arg:
            agent.model = arg
        console.print(f"โมเดล: [bold]{agent.model}[/bold]")
    elif name == "/models":
        try:
            for m in agent.client.models.list():
                if "generateContent" in (m.supported_actions or []):
                    console.print(f"  {m.name.removeprefix('models/')}", highlight=False)
        except errors.APIError as e:
            console.print(f"[red]API error {e.code}: {e.message}[/red]")
    elif name == "/usage":
        show_usage(agent, console)
    else:
        console.print(f"[red]ไม่รู้จักคำสั่ง {name}[/red] พิมพ์ /help")
    return True


def main() -> None:
    load_dotenv(APP_DIR / ".env")
    parser = argparse.ArgumentParser(prog="gcode", description="Gemini in your terminal")
    parser.add_argument("prompt", nargs="*", help="ถ้าใส่ข้อความ จะทำงานนั้นครั้งเดียวแล้วออก")
    parser.add_argument("--api", action="store_true", help="ใช้โหมด API (agent แก้ไฟล์ได้) แทนโหมดเว็บ")
    parser.add_argument("-m", "--model", default=os.getenv("GEMINI_MODEL", DEFAULT_MODEL), help="โมเดล (โหมด API)")
    parser.add_argument("--yolo", action="store_true", help="โหมด API: อนุมัติทุกการแก้ไฟล์และรันคำสั่งอัตโนมัติ (ระวัง)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()

    console = Console()
    if not args.api:
        from .web import run_web

        try:
            run_web(" ".join(args.prompt), console)
        except KeyboardInterrupt:
            pass
        return

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        console.print(
            "[red]ไม่พบ GEMINI_API_KEY[/red]\n"
            f"สร้าง key ฟรีที่ https://aistudio.google.com/apikey แล้วใส่ใน {APP_DIR / '.env'}"
        )
        sys.exit(1)

    agent = Agent(genai.Client(api_key=api_key), args.model, console, yolo=args.yolo)

    if args.prompt:
        agent.run_turn(" ".join(args.prompt))
        return

    enable_line_editing()
    console.print(f"[bold cyan]Gemini Code[/bold cyan] v{__version__} · โหมด API · {agent.model} · {Path.cwd()}")
    if args.yolo:
        console.print("[bold red]โหมด --yolo: อนุมัติทุกอย่างอัตโนมัติ[/bold red]")
    console.print("[dim]พิมพ์ /help เพื่อดูคำสั่ง[/dim]\n")

    while True:
        try:
            text = read_prompt()
        except KeyboardInterrupt:
            console.print()
            continue
        except EOFError:
            break
        if not text:
            continue
        if text.startswith("/"):
            if not handle_command(text, agent, console):
                break
            continue
        agent.run_turn(text)
        console.print()


if __name__ == "__main__":
    main()
