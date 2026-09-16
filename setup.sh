#!/usr/bin/env bash
# Create the virtualenv outside Google Drive (so Drive doesn't sync thousands of files), install deps,
# link the Claude Code commands and add the shell function, so both /gcode and `gcode` work
# right after cloning this one repo.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="${GCODE_VENV:-$HOME/.venvs/gemini-code}"
CLAUDE_COMMANDS="${CLAUDE_COMMANDS_DIR:-$HOME/.claude/commands}"
SHELL_RC="${GCODE_SHELL_RC:-$HOME/.zshrc}"

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

# The `gcode` command in the terminal. A function and not an alias so that /close can close the
# window: exit code 99 from the app means "close this shell". Written between markers, so running
# setup.sh again after moving the repo rewrites the block instead of stacking a stale copy.
GCODE_FUNC="gcode() { GCODE_SHELL_FUNCTION=1 \"$APP_DIR/gcode\" \"\$@\"; local code=\$?; [ \$code -eq 99 ] && exit 0; return \$code; }"
BEGIN_MARK="# >>> gcode >>>"
END_MARK="# <<< gcode <<<"

echo
if [ -e "$SHELL_RC" ] && grep -qF "$BEGIN_MARK" "$SHELL_RC"; then
  tmp="$(mktemp)"
  awk -v begin="$BEGIN_MARK" -v end="$END_MARK" -v fn="$GCODE_FUNC" '
    $0 == begin { print; print fn; skip = 1; next }
    $0 == end   { print; skip = 0; next }
    !skip
  ' "$SHELL_RC" > "$tmp"
  cat "$tmp" > "$SHELL_RC"   # overwrite in place, so permissions and symlinks on the rc file survive
  rm -f "$tmp"
  echo "อัปเดตฟังก์ชัน gcode ใน $SHELL_RC แล้ว (ชี้มาที่ $APP_DIR)"
elif [ -e "$SHELL_RC" ] && grep -qE '^[[:space:]]*(gcode[[:space:]]*\(\)|alias[[:space:]]+gcode=)' "$SHELL_RC"; then
  echo "ข้าม $SHELL_RC — มี gcode ของเดิมอยู่ในไฟล์แล้ว ถ้าต้องการให้ชี้มาที่ repo นี้ ลบของเก่าแล้วใส่บรรทัดนี้แทน:"
  echo "  $GCODE_FUNC"
else
  printf '\n%s\n%s\n%s\n' "$BEGIN_MARK" "$GCODE_FUNC" "$END_MARK" >> "$SHELL_RC"
  echo "เพิ่มฟังก์ชัน gcode ใน $SHELL_RC แล้ว"
fi

echo
echo "ติดตั้งเสร็จ เปิด terminal ใหม่ (หรือ source $SHELL_RC) แล้วรัน gcode หนึ่งครั้ง"
echo "เพื่อลงชื่อเข้าใช้ Google ในหน้าต่าง Chrome ที่เปิดขึ้นมา"
