"""Web mode: drive gemini.google.com in a dedicated Chrome profile and relay the chat to the terminal.

Chrome runs with a remote-debugging port and its own profile (so the user's normal Chrome is untouched,
and the Google login is kept between runs). Each gcode session opens its own tab, which is a new chat.
"""

import base64
import re
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from markdownify import markdownify
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Browser, BrowserContext, Locator, Page, sync_playwright
from rich.console import Console
from rich.markdown import Markdown

from .actions import (
    RESPONSE_BLOCKS_JS,
    Action,
    build_first_message,
    build_follow_up_message,
    format_results,
    parse_response,
)
from .prompt import enable_line_editing, read_prompt
from .runner import ToolRunner
from . import sessions

GEMINI_URL = "https://gemini.google.com/app?hl=th"
CDP_PORT = 9333
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
PROFILE_DIR = Path.home() / ".gemini-code" / "chrome-profile"
CHROME_APP = "Google Chrome"

# Selectors for the Gemini web UI (Thai locale, Sep 2026). Update these when Google changes the page.
EDITOR = "div.ql-editor[contenteditable=true]"
SEND_BUTTON = 'button[aria-label="ส่งข้อความ"], button[aria-label="Send message"]'
STOP_BUTTON = 'button[aria-label="หยุดคำตอบ"], button[aria-label="Stop response"]'
MODE_BUTTON = 'button[aria-label^="เปิดตัวเลือกโหมด"], button[aria-label^="Open mode picker"]'
SIGN_IN = 'a:has-text("ลงชื่อเข้าใช้"), button:has-text("ลงชื่อเข้าใช้"), a:has-text("Sign in")'
RESPONSE = "model-response"
GENERATED_IMAGE = "generated-image"
IMAGE_DOWNLOAD_BUTTON = "download-generated-image-button button"
CHAT_ACTIONS_BUTTON = (
    'button[aria-label="เปิดเมนูสําหรับการดําเนินการที่เกี่ยวกับการสนทนา"], '
    'button[aria-label="Open menu for conversation actions"]'
)
DELETE_MENU_ITEM = '[data-test-id="delete-button"]'
DIALOG = "mat-dialog-container, [role=dialog]"
DIALOG_CONFIRM_DELETE = re.compile(r"^\s*(ลบ|Delete)\s*$")
CHAT_NOT_FOUND_TEXT = re.compile(r"ถูกลบไปแล้ว|doesn't exist|deleted")
CHAT_ID_IN_URL = re.compile(r"/app/([0-9a-f]+)")
GEMINI_ERROR_REPLY = re.compile(r"encountering an error|Something went wrong|เกิดข้อผิดพลาดบางอย่าง")
MODE_NAME = re.compile(r"^\d+(\.\d+)?\s+(Flash-Lite|Flash|Pro|Ultra)\b")

RESPONSE_START_TIMEOUT = 120
RESPONSE_TIMEOUT = 900
STABLE_SECONDS = 1.5
MAX_ACTION_STEPS = 25
DELETE_CONFIRM_SECONDS = 8
DELETE_PAUSE_SECONDS = 3
BETWEEN_DELETES_SECONDS = 8  # a person doesn't delete chats back to back; see CLAUDE.md
RESUME_SETTLE_SECONDS = 2
RESULTS_PAUSE_SECONDS = 2
BLOCKED_URL = "google.com/sorry"


class BlockedByGoogle(RuntimeError):
    """Google showed its automated-traffic check (CAPTCHA). The user has to solve it themselves."""
IMAGE_DOWNLOAD_TIMEOUT_MS = 60_000

# Snapshot of the newest response: still streaming? finished? how much text? images still loading?
RESPONSE_STATE_JS = f"""() => {{
  const m = [...document.querySelectorAll('{RESPONSE}')].pop();
  return {{
    stop: !!document.querySelector('{STOP_BUTTON}'),
    done: !!m?.querySelector('message-actions'),
    length: (m?.querySelector('message-content')?.innerText || '').length,
    pendingImages: m ? [...m.querySelectorAll('{GENERATED_IMAGE}')].filter(g => !g.querySelector('img.loaded')).length : 0,
  }};
}}"""

# Clean copy of a response's HTML: code blocks become <pre><code class="language-x">, UI chrome removed.
RESPONSE_HTML_JS = """m => {
  const root = m.querySelector('message-content .markdown') || m.querySelector('message-content');
  if (!root) return '';
  const clone = root.cloneNode(true);
  clone.querySelectorAll('code-block').forEach(cb => {
    const lang = (cb.querySelector('.code-block-decoration span')?.textContent || '').trim().toLowerCase();
    const pre = document.createElement('pre');
    const code = document.createElement('code');
    code.textContent = cb.querySelector('code')?.textContent ?? cb.textContent;
    if (lang) code.className = 'language-' + lang;
    pre.appendChild(code);
    cb.replaceWith(pre);
  });
  clone.querySelectorAll('generated-image, .attachment-container, button, mat-icon, sources-list').forEach(e => e.remove());
  return clone.innerHTML;
}"""

CANVAS_JS = """img => {
  const c = document.createElement('canvas');
  c.width = img.naturalWidth; c.height = img.naturalHeight;
  c.getContext('2d').drawImage(img, 0, 0);
  return c.toDataURL('image/png');
}"""

VISIBLE_TEXTS_JS = """() => [...document.querySelectorAll('body *')]
  .filter(e => e.offsetParent !== null && e.children.length === 0 && (e.innerText || '').trim())
  .map(e => e.innerText.trim())"""

HELP = """[bold]คำสั่ง (โหมดเว็บ)[/bold]
  /help            แสดงวิธีใช้
  /new             เริ่มแชทใหม่
  /mode [ชื่อ]      ดูหรือเปลี่ยนโมเดล เช่น /mode 3.1 Pro
  /open            สลับไปดูแท็บนี้ใน Chrome
  /close           ลบแชทของ terminal นี้ออกจาก Gemini แล้วปิด terminal
  /exit            ออก โดยเก็บแชทไว้ (หรือกด Ctrl+D)

พิมพ์ \\ ท้ายบรรทัดเพื่อขึ้นบรรทัดใหม่  กด Ctrl+C ระหว่างรอเพื่อหยุดคำตอบ
Gemini อ่าน/เขียน/แก้/ลบไฟล์ และรันคำสั่งในโฟลเดอร์ที่เรียก gcode ได้ (ถามก่อนทุกครั้งที่เปลี่ยนแปลง)
รูปที่ Gemini สร้างจะถูกเซฟในโฟลเดอร์เดียวกัน"""


def _code_language(pre) -> str | None:
    code = pre.find("code")
    for cls in (code.get("class") or []) if code else []:
        if cls.startswith("language-"):
            return cls.removeprefix("language-")
    return None


def html_to_markdown(html: str) -> str:
    md = markdownify(html, heading_style="ATX", bullets="-", code_language_callback=_code_language)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def _cdp_ready() -> bool:
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=1):
            return True
    except OSError:
        return False


def ensure_chrome(console: Console) -> bool:
    """Start the gcode Chrome if needed. Return True if it was just launched."""
    if _cdp_ready():
        return False
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    # -g: don't bring Chrome to the foreground, -j: launch hidden.
    # The --disable-* flags stop Chrome from slowing down pages that sit in a hidden window.
    subprocess.run(
        ["open", "-gj", "-na", CHROME_APP, "--args", f"--remote-debugging-port={CDP_PORT}",
         f"--user-data-dir={PROFILE_DIR}", "--no-first-run", "--no-default-browser-check",
         "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
         "--disable-backgrounding-occluded-windows"],
        check=True,
    )
    with console.status("[cyan]กำลังเปิด Chrome...[/cyan]"):
        for _ in range(60):
            _chrome_window("set visible to false")  # Chrome shows itself while starting up; hide ASAP
            if _cdp_ready():
                return True
            time.sleep(0.25)
    raise RuntimeError("เปิด Chrome ไม่สำเร็จ")


def _chrome_window(applescript: str) -> str:
    """Run a System Events command on the gcode Chrome process (not the user's normal Chrome)."""
    pid = subprocess.run(
        ["lsof", "-ti", f"tcp:{CDP_PORT}", "-sTCP:LISTEN"], capture_output=True, text=True
    ).stdout.split()
    if not pid:
        return ""
    return subprocess.run(
        ["osascript", "-e",
         f'tell application "System Events" to tell (first process whose unix id is {pid[0]}) to {applescript}'],
        capture_output=True, text=True,
    ).stdout.strip()


def show_chrome(page: Page) -> None:
    """Switch to this tab and bring the hidden gcode Chrome window to the front."""
    page.bring_to_front()
    _chrome_window("set {visible, frontmost} to {true, true}")


def open_background_tab(browser: Browser, context: BrowserContext, keep_hidden: bool) -> Page:
    """Open a tab without switching to it. Creating a tab un-hides Chrome on macOS, so hide it again."""
    keep_hidden = keep_hidden or _chrome_window("get visible") == "false"
    session = browser.new_browser_cdp_session()
    with context.expect_page() as info:
        session.send("Target.createTarget", {"url": "about:blank", "background": True})
    session.detach()
    if keep_hidden:
        _chrome_window("set visible to false")
    return info.value


class GeminiWeb:
    def __init__(self, page: Page, console: Console, save_dir: Path, yolo: bool = False):
        self.page = page
        self.console = console
        self.save_dir = save_dir
        self.runner = ToolRunner(console, yolo=yolo)
        self.chat_ids: list[str] = []  # every Gemini chat this terminal session created
        self.needs_protocol = True  # the first message of each chat teaches Gemini the @@ actions

    def check_not_blocked(self) -> None:
        """Stop if Google flagged automated traffic. Never try to get past the check automatically."""
        if BLOCKED_URL in self.page.url:
            show_chrome(self.page)
            raise BlockedByGoogle(
                "Google ตรวจพบการใช้งานอัตโนมัติและขอให้ยืนยันตัวตน "
                "ยืนยันเองในหน้าต่าง Chrome ที่เปิดขึ้นมา แล้วพักการใช้งานสักครู่ก่อนเรียก gcode ใหม่"
            )

    def open_new_chat(self) -> None:
        self.needs_protocol = True
        self.page.goto(GEMINI_URL)
        self.check_not_blocked()
        self.page.locator(EDITOR).first.wait_for(timeout=30_000)
        while self.page.locator(SIGN_IN).count():
            show_chrome(self.page)
            self.console.input(
                "[yellow]ยังไม่ได้ลงชื่อเข้าใช้ Google[/yellow] ลงชื่อเข้าใช้ในหน้าต่าง Chrome แล้วกด Enter: "
            )
            self.page.goto(GEMINI_URL)
            self.page.locator(EDITOR).first.wait_for(timeout=30_000)

    def open_chat(self, chat_id: str) -> bool:
        """Continue an existing chat. Return False (and open a new chat instead) if it is gone.

        A deleted chat redirects to a fresh chat, so trust the URL only after it has settled.
        """
        self.page.goto(f"https://gemini.google.com/app/{chat_id}?hl=th")
        self.check_not_blocked()
        self.page.locator(EDITOR).first.wait_for(timeout=30_000)
        time.sleep(RESUME_SETTLE_SECONDS)
        if self.page.locator(SIGN_IN).count() or chat_id not in self.page.url:
            self.open_new_chat()
            return False
        self.needs_protocol = False  # this chat already learned the @@ protocol
        if chat_id not in self.chat_ids:
            self.chat_ids.append(chat_id)
        return True

    # --- chatting ---------------------------------------------------------------

    def ask(self, text: str) -> None:
        """Send a request, then keep running Gemini's @@ actions and sending results until it is done."""
        message = build_first_message(text) if self.needs_protocol else build_follow_up_message(text)
        for _ in range(MAX_ACTION_STEPS):
            response, interrupted = self._send(message)
            self.needs_protocol = False
            if response is not None and not interrupted and GEMINI_ERROR_REPLY.search(response.inner_text()[:300]):
                self.console.print("[yellow]Gemini ตอบว่าเกิดข้อผิดพลาด กำลังส่งใหม่อีกครั้ง...[/yellow]")
                time.sleep(3)
                response, interrupted = self._send(message)
            if response is None:
                return
            actions, display = parse_response(response.evaluate(RESPONSE_BLOCKS_JS))
            if actions:
                self._print_blocks(display)
            else:
                self._print_markdown(response)
            self._save_images(response)
            if interrupted or not actions:
                return
            try:
                results = [(action, self._run_action(action)) for action in actions]
            except KeyboardInterrupt:
                self.console.print("\n[yellow]ยกเลิกแล้ว ยังไม่ได้ส่งผลลัพธ์ให้ Gemini[/yellow]")
                return
            message = format_results(results)
            time.sleep(RESULTS_PAUSE_SECONDS)  # pace follow-up messages like a person would
        self.console.print(f"[yellow]หยุดหลังจากทำงานครบ {MAX_ACTION_STEPS} รอบ พิมพ์ต่อเพื่อให้ทำต่อ[/yellow]")

    def _run_action(self, action: Action) -> dict:
        try:
            name, args = action.to_tool_call()
        except ValueError as e:
            self.console.print(f"[red]● @@{action.label}: {e}[/red]", highlight=False)
            return {"error": str(e)}
        return self.runner.run(name, args)

    def _send(self, text: str) -> tuple[Locator | None, bool]:
        """Send one message and wait for the reply. Return (response, interrupted by Ctrl+C)."""
        responses = self.page.locator(RESPONSE)
        before = responses.count()
        editor = self.page.locator(EDITOR).first
        editor.click()
        editor.press("Meta+A")
        editor.press("Backspace")
        self.page.keyboard.insert_text(text)
        self.page.locator(SEND_BUTTON).first.click(timeout=10_000)

        interrupted = False
        try:
            self._wait_for_response(before)
        except KeyboardInterrupt:
            interrupted = True
            stop = self.page.locator(STOP_BUTTON)
            if stop.count():
                stop.first.click()
            self.console.print("[yellow]หยุดคำตอบแล้ว[/yellow]")
        finally:
            self._remember_chat()
        if responses.count() <= before:
            return None, interrupted
        return responses.last, interrupted

    def _remember_chat(self) -> None:
        """Record the chat id once Gemini assigns one (the URL changes to /app/<id>), so /close can delete it."""
        match = CHAT_ID_IN_URL.search(self.page.url)
        if match and match.group(1) not in self.chat_ids:
            self.chat_ids.append(match.group(1))

    def _print_markdown(self, response: Locator) -> None:
        markdown = html_to_markdown(response.evaluate(RESPONSE_HTML_JS))
        if markdown:
            self.console.print(Markdown(markdown))

    def _print_blocks(self, blocks: list[dict]) -> None:
        """Print a response whose action lines and file contents were removed (shown as tool calls instead)."""
        parts = []
        for block in blocks:
            if block["type"] == "code":
                parts.append(f"```{block['lang']}\n{block['code'].rstrip()}\n```")
            elif block["html"]:
                parts.append(html_to_markdown(block["html"]))
            else:
                parts.append(block["text"])
        markdown = "\n\n".join(p for p in parts if p.strip())
        if markdown:
            self.console.print(Markdown(markdown))

    def _wait_for_response(self, before: int) -> None:
        start = time.monotonic()
        with self.console.status("[cyan]ส่งแล้ว รอ Gemini...[/cyan]") as status:
            while self.page.locator(RESPONSE).count() <= before:
                self._remember_chat()
                # Long prompts can think for minutes before the reply appears; the stop button means "still working".
                working = self.page.locator(STOP_BUTTON).count() > 0
                if not working and time.monotonic() - start > RESPONSE_START_TIMEOUT:
                    raise TimeoutError("Gemini ไม่ตอบกลับ ลองดูในหน้าต่าง Chrome ด้วย /open")
                if time.monotonic() - start > RESPONSE_TIMEOUT:
                    raise TimeoutError("รอคำตอบนานเกินไป")
                status.update(f"[cyan]Gemini กำลังคิด... {int(time.monotonic() - start)} วินาที[/cyan]")
                time.sleep(0.5)
            last_length, stable_since = -1, time.monotonic()
            while True:
                state = self.page.evaluate(RESPONSE_STATE_JS)
                if state["length"] != last_length:
                    last_length, stable_since = state["length"], time.monotonic()
                    status.update(f"[cyan]Gemini กำลังตอบ... {last_length:,} ตัวอักษร[/cyan]")
                if state["pendingImages"]:
                    status.update("[cyan]Gemini กำลังสร้างรูป...[/cyan]")
                finished = state["done"] and not state["stop"] and not state["pendingImages"]
                if finished and time.monotonic() - stable_since >= STABLE_SECONDS:
                    return
                if time.monotonic() - start > RESPONSE_TIMEOUT:
                    raise TimeoutError("รอคำตอบนานเกินไป")
                time.sleep(0.5)

    # --- images -------------------------------------------------------------------

    def _new_image_path(self, index: int, suffix: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.save_dir / f"gemini_{stamp}_{index}{suffix}"
        n = 2
        while path.exists():
            path = self.save_dir / f"gemini_{stamp}_{index}_{n}{suffix}"
            n += 1
        return path

    def _save_images(self, response: Locator) -> None:
        images = response.locator(GENERATED_IMAGE)
        count = images.count()
        if not count:
            return
        saved = []
        with self.console.status("[cyan]กำลังเซฟรูป...[/cyan]"):
            for i in range(count):
                image = images.nth(i)
                try:
                    saved.append((self._download_full_size(image, i + 1), "ขนาดเต็ม"))
                except PlaywrightError:
                    try:
                        saved.append((self._save_preview(image, i + 1), "ขนาดพรีวิว ดาวน์โหลดขนาดเต็มไม่สำเร็จ"))
                    except (PlaywrightError, ValueError) as e:
                        self.console.print(f"[red]เซฟรูปที่ {i + 1} ไม่สำเร็จ: {e}[/red]")

        if saved:
            self.console.print(f"\n[bold green]🖼  เซฟรูปให้แล้ว {len(saved)} รูป[/bold green] ในโฟลเดอร์ {self.save_dir}")
            for path, note in saved:
                size_kb = path.stat().st_size / 1024
                self.console.print(f"   • {path.name}  [dim]({size_kb:,.0f} KB, {note})[/dim]", highlight=False)

    def _download_full_size(self, image: Locator, index: int) -> Path:
        image.scroll_into_view_if_needed()
        image.hover()
        with self.page.expect_download(timeout=IMAGE_DOWNLOAD_TIMEOUT_MS) as info:
            image.locator(IMAGE_DOWNLOAD_BUTTON).first.click(timeout=10_000)
        download = info.value
        path = self._new_image_path(index, Path(download.suggested_filename).suffix or ".png")
        download.save_as(path)
        return path

    def _save_preview(self, image: Locator, index: int) -> Path:
        data_url = image.locator("img").first.evaluate(CANVAS_JS)
        path = self._new_image_path(index, ".png")
        path.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
        return path

    # --- deleting chats -----------------------------------------------------------

    def delete_session_chats(self) -> bool:
        """Delete every chat this session created. Return True only if all of them are gone.

        Only ids recorded while sending messages are deleted, never whatever chat the tab happens to show.
        """
        if not self.chat_ids:
            self.console.print("[dim]terminal นี้ยังไม่ได้เริ่มแชทใน Gemini ไม่มีแชทต้องลบ[/dim]")
            return True
        total = len(self.chat_ids)
        with self.console.status("[cyan]กำลังลบแชทใน Gemini...[/cyan]"):
            for chat_id in list(self.chat_ids):
                try:
                    self._delete_chat(chat_id)
                except PlaywrightError as e:
                    self.console.print(f"[red]ลบแชท {chat_id} ไม่สำเร็จ: {str(e).splitlines()[0]}[/red]")
                    return False
                self.chat_ids.remove(chat_id)
        self.console.print(f"[green]ลบแชทใน Gemini แล้ว {total} แชท[/green]")
        return True

    def _delete_chat(self, chat_id: str) -> None:
        self.page.goto(f"https://gemini.google.com/app/{chat_id}?hl=th")
        self.check_not_blocked()
        self.page.locator(EDITOR).first.wait_for(timeout=30_000)
        self.page.locator(CHAT_ACTIONS_BUTTON).first.click(timeout=15_000)
        self.page.locator(DELETE_MENU_ITEM).first.click(timeout=10_000)
        dialog = self.page.locator(DIALOG).last
        dialog.locator("button").filter(has_text=DIALOG_CONFIRM_DELETE).last.click(timeout=10_000)
        dialog.wait_for(state="detached", timeout=15_000)

        # Confirm on the server side: reopening a deleted chat redirects to a new chat (with a brief
        # "not found" toast). It may flash cached messages first, so only trust "still there" once the
        # messages have stayed on the chat's URL for a while.
        time.sleep(DELETE_PAUSE_SECONDS)  # don't hammer the site; see CLAUDE.md
        self.page.goto(f"https://gemini.google.com/app/{chat_id}?hl=th")
        self.check_not_blocked()
        deadline = time.monotonic() + 25
        messages_seen_at = None
        while time.monotonic() < deadline:
            if chat_id not in self.page.url or self.page.get_by_text(CHAT_NOT_FOUND_TEXT).count():
                return
            if self.page.locator("user-query").count():
                messages_seen_at = messages_seen_at or time.monotonic()
                if time.monotonic() - messages_seen_at > DELETE_CONFIRM_SECONDS:
                    raise PlaywrightError("แชทยังอยู่หลังจากกดลบ")
            time.sleep(0.3)
        raise PlaywrightError("ยืนยันไม่ได้ว่าแชทถูกลบแล้ว")

    # --- modes --------------------------------------------------------------------

    def current_mode(self) -> str:
        button = self.page.locator(MODE_BUTTON).first
        try:
            button.wait_for(timeout=10_000)  # renders a moment after the editor
        except PlaywrightError:
            return "ไม่ทราบ"
        label = button.get_attribute("aria-label") or ""
        return re.sub(r"^.*(โหมด|mode)\s*", "", label).strip() or "ไม่ทราบ"

    def list_modes(self) -> list[str]:
        self.page.locator(MODE_BUTTON).first.click()
        time.sleep(1)
        names = [t for t in self.page.evaluate(VISIBLE_TEXTS_JS) if MODE_NAME.match(t)]
        self.page.keyboard.press("Escape")
        return list(dict.fromkeys(names))

    def set_mode(self, name: str) -> bool:
        self.page.locator(MODE_BUTTON).first.click()
        time.sleep(1)
        names = [t for t in self.page.evaluate(VISIBLE_TEXTS_JS) if MODE_NAME.match(t)]
        wanted = name.lower()
        # "3.6 Flash" exact, then family name ("flash" must not pick "Flash-Lite"), then substring
        match = (
            next((n for n in names if n.lower() == wanted), None)
            or next((n for n in names if n.split(" ", 1)[-1].lower() == wanted), None)
            or next((n for n in names if wanted in n.lower()), None)
        )
        if not match:
            self.page.keyboard.press("Escape")
            return False
        self.page.get_by_text(match, exact=True).first.click()
        time.sleep(0.5)
        return True


def handle_command(command: str, gemini: GeminiWeb, console: Console) -> str | None:
    """Run a slash command. Return "exit" or "close" when the app should stop."""
    name, _, arg = command.partition(" ")
    arg = arg.strip()
    if name in ("/exit", "/quit"):
        return "exit"
    if name == "/close":
        # Gemini first: only close the terminal once the chats are really deleted.
        if gemini.delete_session_chats():
            return "close"
        console.print("[yellow]ยังไม่ปิด terminal เพราะลบแชทไม่สำเร็จ ลองใหม่อีกครั้ง หรือดูด้วย /open[/yellow]")
    elif name == "/help":
        console.print(HELP)
    elif name == "/new":
        gemini.open_new_chat()
        console.print("[green]เริ่มแชทใหม่แล้ว[/green]")
    elif name == "/mode":
        if arg and not gemini.set_mode(arg):
            console.print(f"[red]ไม่พบโหมด {arg}[/red]")
        console.print(f"โหมดตอนนี้: [bold]{gemini.current_mode()}[/bold]")
        if not arg:
            console.print("โหมดที่เลือกได้: " + ", ".join(gemini.list_modes()), highlight=False)
    elif name == "/open":
        show_chrome(gemini.page)
    else:
        console.print(f"[red]ไม่รู้จักคำสั่ง {name}[/red] พิมพ์ /help")
    return None


def run_web(
    prompt: str, console: Console, yolo: bool = False, session: str | None = None, resume: str | None = None
) -> bool:
    """Run the web-mode session. Return True if the user asked to close the terminal (/close).

    With `session`, every chat this run creates is written to the session log so a later process
    (`--continue`, `--list-chats`, `--delete`) can find it. With `resume`, keep using that chat.
    """
    just_launched = ensure_chrome(console)
    playwright = sync_playwright().start()
    page = None
    try:
        browser = playwright.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = open_background_tab(browser, context, keep_hidden=just_launched)
        for other in context.pages:
            if other is not page and other.url in ("about:blank", "chrome://newtab/"):
                other.close()
        gemini = GeminiWeb(page, console, Path.cwd(), yolo=yolo)
        with console.status("[cyan]กำลังเปิด Gemini...[/cyan]"):
            if not resume:
                gemini.open_new_chat()
            elif not gemini.open_chat(resume):
                console.print(f"[dim]แชท {resume} ไม่อยู่แล้ว เริ่มแชทใหม่ให้แทน[/dim]")
                if session:
                    sessions.forget(session, resume)

        if prompt:
            try:
                gemini.ask(prompt)
            finally:
                for chat_id in gemini.chat_ids:
                    if session:
                        sessions.record(session, chat_id, prompt)
            return False

        enable_line_editing()
        console.print(f"[bold cyan]Gemini Code[/bold cyan] · โหมดเว็บ · {gemini.current_mode()} · {Path.cwd()}")
        console.print("[dim]แชทใหม่ใน gemini.google.com · พิมพ์ /help เพื่อดูคำสั่ง[/dim]\n")
        while True:
            try:
                text = read_prompt()
            except KeyboardInterrupt:
                console.print()
                continue
            except EOFError:
                return False
            if not text:
                continue
            try:
                if text.startswith("/"):
                    action = handle_command(text, gemini, console)
                    if action:
                        return action == "close"
                else:
                    gemini.ask(text)
            except (PlaywrightError, TimeoutError) as e:
                console.print(f"[red]เกิดข้อผิดพลาด: {str(e).splitlines()[0]}[/red]")
            console.print()
    except BlockedByGoogle as e:
        console.print(f"[bold red]หยุดทำงาน:[/bold red] {e}")
        return False
    finally:
        if page is not None:
            try:
                page.close()
            except PlaywrightError:
                pass
        playwright.stop()


def run_delete(chat_ids: list[str], console: Console, session: str | None = None) -> list[str]:
    """Delete the given chats, pacing the requests like a person. Return the ids that are gone.

    Only the ids passed in are touched -- never whatever chat the tab happens to be showing.
    """
    ensure_chrome(console)
    playwright = sync_playwright().start()
    page = None
    deleted: list[str] = []
    try:
        browser = playwright.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = open_background_tab(browser, context, keep_hidden=True)
        gemini = GeminiWeb(page, console, Path.cwd())
        for index, chat_id in enumerate(chat_ids):
            if index:
                time.sleep(BETWEEN_DELETES_SECONDS)
            try:
                with console.status(f"[cyan]กำลังลบแชท {chat_id}...[/cyan]"):
                    gemini._delete_chat(chat_id)
            except PlaywrightError as e:
                console.print(f"[red]ลบแชท {chat_id} ไม่สำเร็จ: {str(e).splitlines()[0]}[/red]")
                continue
            deleted.append(chat_id)
            if session:
                sessions.forget(session, chat_id)
            console.print(f"[green]ลบแชท {chat_id} แล้ว[/green]")
    except BlockedByGoogle as e:
        console.print(f"[bold red]หยุดทำงาน:[/bold red] {e}")
    finally:
        if page is not None:
            try:
                page.close()
            except PlaywrightError:
                pass
        playwright.stop()
    return deleted
