"""Track API usage and estimate remaining free-tier quota.

The Gemini API does not report remaining quota, so we count requests locally and compare them with
per-model limits: estimates from limits.json, overridden by real limits parsed from 429 errors.
"""

import json
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from google.genai import types

STATE_DIR = Path.home() / ".gemini-code"
USAGE_FILE = STATE_DIR / "usage.json"
LEARNED_LIMITS_FILE = STATE_DIR / "limits.json"
ESTIMATED_LIMITS_FILE = Path(__file__).resolve().parent.parent / "limits.json"
# Gemini API daily quotas reset at midnight Pacific time.
QUOTA_TZ = ZoneInfo("America/Los_Angeles")
KEEP_DAYS = 30


def _quota_day() -> str:
    return datetime.now(QUOTA_TZ).date().isoformat()


def time_until_reset() -> timedelta:
    now = datetime.now(QUOTA_TZ)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - now


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # usage stats are best-effort; never break the session over them


def _find_quota_violations(node) -> list[dict]:
    """Collect every {quotaId, quotaValue, ...} object nested anywhere in a 429 error body."""
    if isinstance(node, dict):
        found = [node] if "quotaId" in node and "quotaValue" in node else []
        return found + [v for child in node.values() for v in _find_quota_violations(child)]
    if isinstance(node, list):
        return [v for child in node for v in _find_quota_violations(child)]
    return []


class UsageTracker:
    def __init__(self):
        self.requests = 0
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.thinking_tokens = 0
        self.last: types.GenerateContentResponseUsageMetadata | None = None
        self._recent: deque[tuple[float, str]] = deque()

    def record(self, model: str, usage: types.GenerateContentResponseUsageMetadata | None) -> None:
        self.requests += 1
        self._recent.append((time.monotonic(), model))
        if usage:
            self.last = usage
            self.prompt_tokens += usage.prompt_token_count or 0
            self.output_tokens += usage.candidates_token_count or 0
            self.thinking_tokens += usage.thoughts_token_count or 0
        self._save_daily(model, usage)

    def requests_last_minute(self, model: str | None = None) -> int:
        cutoff = time.monotonic() - 60
        while self._recent and self._recent[0][0] < cutoff:
            self._recent.popleft()
        return sum(1 for _, m in self._recent if model is None or m == model)

    def today(self) -> dict[str, dict[str, int]]:
        return _read_json(USAGE_FILE).get(_quota_day(), {})

    def limits(self, model: str) -> dict[str, tuple[int, str]]:
        """Return {"rpm"|"rpd": (limit, source)} where source is "google" or "estimate"."""
        result = {}
        for source, path in (("estimate", ESTIMATED_LIMITS_FILE), ("google", LEARNED_LIMITS_FILE)):
            for kind, value in _read_json(path).get(model, {}).items():
                if kind in ("rpm", "rpd") and isinstance(value, int):
                    result[kind] = (value, source)
        return result

    def learn_limits(self, model: str, error_details) -> None:
        """Save the real request limits that Google includes in a 429 error."""
        learned = _read_json(LEARNED_LIMITS_FILE)
        changed = False
        for violation in _find_quota_violations(error_details):
            quota_id = str(violation["quotaId"])
            if "Token" in quota_id:
                continue
            kind = "rpd" if "PerDay" in quota_id else "rpm" if "PerMinute" in quota_id else None
            try:
                value = int(violation["quotaValue"])
            except (TypeError, ValueError):
                continue
            if kind:
                target = (violation.get("quotaDimensions") or {}).get("model", model)
                learned.setdefault(target, {})[kind] = value
                changed = True
        if changed:
            _write_json(LEARNED_LIMITS_FILE, learned)

    def _save_daily(self, model: str, usage: types.GenerateContentResponseUsageMetadata | None) -> None:
        data = _read_json(USAGE_FILE)
        entry = data.setdefault(_quota_day(), {}).setdefault(model, {"requests": 0, "tokens": 0})
        entry["requests"] += 1
        entry["tokens"] += (usage.total_token_count or 0) if usage else 0
        for day in sorted(data)[:-KEEP_DAYS]:
            del data[day]
        _write_json(USAGE_FILE, data)
