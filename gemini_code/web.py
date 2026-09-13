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

from .prompt import enable_line_editing, read_prompt

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
MODE_NAME = re.compile(r"^\d+(\.\d+)?\s+(Flash-Lite|Flash|Pro|Ultra)\b")

RESPONSE_START_TIMEOUT = 60
RESPONSE_TIMEOUT = 900
STABLE_SECONDS = 1.5
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
  /exit            ออก (หรือกด Ctrl+D)

พิมพ์ \\ ท้ายบรรทัดเพื่อขึ้นบรรทัดใหม่  กด Ctrl+C ระหว่างรอเพื่อหยุดคำตอบ
รูปที่ Gemini สร้างจะถูกเซฟในโฟลเดอร์ที่เรียก gcode"""


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
    def __init__(self, page: Page, console: Console, save_dir: Path):
        self.page = page
        self.console = console
        self.save_dir = save_dir

    def open_new_chat(self) -> None:
        self.page.goto(GEMINI_URL)
        self.page.locator(EDITOR).first.wait_for(timeout=30_000)
        while self.page.locator(SIGN_IN).count():
            show_chrome(self.page)
            self.console.input(
                "[yellow]ยังไม่ได้ลงชื่อเข้าใช้ Google[/yellow] ลงชื่อเข้าใช้ในหน้าต่าง Chrome แล้วกด Enter: "
            )
            self.page.goto(GEMINI_URL)
            self.page.locator(EDITOR).first.wait_for(timeout=30_000)

    # --- chatting ---------------------------------------------------------------

    def ask(self, text: str) -> None:
        responses = self.page.locator(RESPONSE)
        before = responses.count()
        editor = self.page.locator(EDITOR).first
        editor.click()
        editor.press("Meta+A")
        editor.press("Backspace")
        self.page.keyboard.insert_text(text)
        self.page.locator(SEND_BUTTON).first.click(timeout=10_000)

        try:
            self._wait_for_response(before)
        except KeyboardInterrupt:
            stop = self.page.locator(STOP_BUTTON)
            if stop.count():
                stop.first.click()
            self.console.print("[yellow]หยุดคำตอบแล้ว[/yellow]")
        if responses.count() <= before:
            return
        response = responses.last
        markdown = html_to_markdown(response.evaluate(RESPONSE_HTML_JS))
        if markdown:
            self.console.print(Markdown(markdown))
        self._save_images(response)

    def _wait_for_response(self, before: int) -> None:
        start = time.monotonic()
        with self.console.status("[cyan]ส่งแล้ว รอ Gemini...[/cyan]") as status:
            while self.page.locator(RESPONSE).count() <= before:
                if time.monotonic() - start > RESPONSE_START_TIMEOUT:
                    raise TimeoutError("Gemini ไม่ตอบกลับ ลองดูในหน้าต่าง Chrome ด้วย /open")
                time.sleep(0.3)
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


def handle_command(command: str, gemini: GeminiWeb, console: Console) -> bool:
    """Run a slash command. Return False when the app should exit."""
    name, _, arg = command.partition(" ")
    arg = arg.strip()
    if name in ("/exit", "/quit"):
        return False
    if name == "/help":
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
    return True


def run_web(prompt: str, console: Console) -> None:
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
        gemini = GeminiWeb(page, console, Path.cwd())
        with console.status("[cyan]กำลังเปิด Gemini...[/cyan]"):
            gemini.open_new_chat()

        if prompt:
            gemini.ask(prompt)
            return

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
                break
            if not text:
                continue
            try:
                if text.startswith("/"):
                    if not handle_command(text, gemini, console):
                        break
                else:
                    gemini.ask(text)
            except (PlaywrightError, TimeoutError) as e:
                console.print(f"[red]เกิดข้อผิดพลาด: {str(e).splitlines()[0]}[/red]")
            console.print()
    finally:
        if page is not None:
            try:
                page.close()
            except PlaywrightError:
                pass
        playwright.stop()
