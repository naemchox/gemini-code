#!/usr/bin/env bash
# Create the virtualenv outside Google Drive (so Drive doesn't sync thousands of files) and install deps.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="${GCODE_VENV:-$HOME/.venvs/gemini-code}"

python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chmod +x "$APP_DIR/gcode"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "สร้างไฟล์ $APP_DIR/.env แล้ว ใส่ GEMINI_API_KEY ในไฟล์นี้"
fi

echo
echo "ติดตั้งเสร็จ เพิ่มบรรทัดนี้ใน ~/.zshrc เพื่อเรียกใช้ด้วยคำสั่ง gcode ได้ทุกที่:"
echo "(เป็นฟังก์ชันแทน alias เพื่อให้ /close ปิด terminal ได้: exit code 99 = ให้ปิด shell)"
echo "  gcode() { GCODE_SHELL_FUNCTION=1 \"$APP_DIR/gcode\" \"\$@\"; local code=\$?; [ \$code -eq 99 ] && exit 0; return \$code; }"
