#!/usr/bin/env bash
# Create the virtualenv outside Google Drive (so Drive doesn't sync thousands of files), install deps,
# and link the Claude Code commands so /gcode works right after cloning this one repo.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="${GCODE_VENV:-$HOME/.venvs/gemini-code}"
CLAUDE_COMMANDS="${CLAUDE_COMMANDS_DIR:-$HOME/.claude/commands}"

python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chmod +x "$APP_DIR/gcode"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "สร้างไฟล์ $APP_DIR/.env แล้ว ใส่ GEMINI_API_KEY ในไฟล์นี้ (เฉพาะถ้าจะใช้ gcode --api)"
fi

# Web mode drives the real Google Chrome over CDP, so it has to be installed. Playwright's own
# browsers are never used, which is why `playwright install` is not needed here.
if [ ! -d "/Applications/Google Chrome.app" ]; then
  echo "เตือน: ไม่พบ Google Chrome ใน /Applications โหมดเว็บต้องใช้ Chrome ติดตั้งก่อนที่ https://google.com/chrome"
fi

# /gcode and /gcodeclose in Claude Code. Symlinks, so `git pull` here updates the commands too.
mkdir -p "$CLAUDE_COMMANDS"
for name in gcode gcodeclose; do
  src="$APP_DIR/commands/$name.md"
  dest="$CLAUDE_COMMANDS/$name.md"
  if [ -L "$dest" ] && [ "$(readlink "$dest")" = "$src" ]; then
    echo "  /$name ลิงก์ไว้แล้ว"
  elif [ -e "$dest" ] || [ -L "$dest" ]; then
    echo "  ข้าม /$name — มีไฟล์อื่นอยู่ที่ $dest แล้ว (ลบเองก่อนถ้าต้องการให้ชี้มาที่ repo นี้)"
  else
    ln -s "$src" "$dest"
    echo "  ลิงก์ /$name แล้ว"
  fi
done

echo
echo "ติดตั้งเสร็จ เพิ่มบรรทัดนี้ใน ~/.zshrc เพื่อเรียกใช้ด้วยคำสั่ง gcode ได้ทุกที่:"
echo "(เป็นฟังก์ชันแทน alias เพื่อให้ /close ปิด terminal ได้: exit code 99 = ให้ปิด shell)"
echo "  gcode() { GCODE_SHELL_FUNCTION=1 \"$APP_DIR/gcode\" \"\$@\"; local code=\$?; [ \$code -eq 99 ] && exit 0; return \$code; }"
echo
echo "จากนั้นเปิด terminal ใหม่ แล้วรัน gcode หนึ่งครั้งเพื่อลงชื่อเข้าใช้ Google ในหน้าต่าง Chrome ที่เปิดขึ้นมา"
