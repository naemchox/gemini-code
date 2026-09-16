"""A log of the Gemini chats each caller-defined session created, so another process can delete them.

Web mode keeps chat ids in memory, which is enough for the interactive `/close`. A one-shot run
(`gcode "..."`) exits immediately, so anything that wants to reuse or delete that chat later --
the /gcode and /gcodeclose commands in Claude Code -- needs the ids on disk.

One file per session key (Claude Code passes its session id), holding the chats in creation order
with the prompts that were sent to each, so a menu can name a chat by what was asked of it.
"""

import json
import re
from datetime import datetime
from pathlib import Path

SESSION_DIR = Path.home() / ".gemini-code" / "sessions"
UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
MAX_PROMPTS = 50  # keep the file small; the first prompt is the one a menu shows


def path(key: str) -> Path:
    return SESSION_DIR / f"{UNSAFE.sub('_', key)[:120]}.json"


def load(key: str) -> list[dict]:
    """Every chat this session created, oldest first. Missing or corrupt file means no chats."""
    try:
        data = json.loads(path(key).read_text())
    except (OSError, ValueError):
        return []
    chats = data.get("chats")
    return chats if isinstance(chats, list) else []


def _save(key: str, chats: list[dict]) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    file = path(key)
    file.write_text(json.dumps({"chats": chats}, ensure_ascii=False, indent=2))


def record(key: str, chat_id: str, prompt: str) -> None:
    """Note that `prompt` was sent to `chat_id` in this session."""
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    chats = load(key)
    for chat in chats:
        if chat.get("id") == chat_id:
            chat["prompts"] = (chat.get("prompts") or [])[: MAX_PROMPTS - 1] + [prompt]
            chat["updated_at"] = now
            break
    else:
        chats.append({"id": chat_id, "prompts": [prompt], "created_at": now, "updated_at": now})
    _save(key, chats)


def forget(key: str, chat_id: str) -> None:
    """Drop a chat from the log (it was deleted, or it no longer exists in Gemini)."""
    chats = [c for c in load(key) if c.get("id") != chat_id]
    if chats:
        _save(key, chats)
    else:
        path(key).unlink(missing_ok=True)


def latest(key: str) -> str | None:
    """The id of the most recent chat, i.e. the one a follow-up should continue."""
    chats = load(key)
    return chats[-1].get("id") if chats else None
