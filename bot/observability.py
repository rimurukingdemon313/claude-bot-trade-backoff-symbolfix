"""Structured logging + event IDs.

Every important operation gets an event id, a timestamp, a severity, the
symbol where applicable, and the strategy version stamp. Logs are emitted
as one JSON object per line so Railway's log viewer and any downstream
collector can parse them without a custom grammar.

Secret redaction is applied at the sink, not at each call site: a value
that matches a known-secret env var is replaced before it is ever
written. That is deliberate — the old diagnostic script printed a login
response body straight to the logs, which leaked an access token.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from collections import deque
from typing import Any, Deque, Iterable

from .clock import utc_now
from .version import version_stamp

SECRET_ENV_KEYS = (
    "TRADELOCKER_PASSWORD",
    "TRADELOCKER_EMAIL",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "DASHBOARD_TOKEN",
    "DATABASE_URL",
)

_SECRET_PLACEHOLDER = "***redacted***"
_lock = threading.Lock()
_recent: Deque[dict[str, Any]] = deque(maxlen=500)


def new_event_id() -> str:
    return uuid.uuid4().hex[:16]


def _secret_values() -> list[str]:
    values = []
    for key in SECRET_ENV_KEYS:
        value = os.environ.get(key)
        if value and len(value) >= 6:
            values.append(value)
    return values


def redact(text: str) -> str:
    """Replace any configured secret literal appearing in `text`.

    Also masks bearer/access tokens by shape, since those are minted at
    runtime and never appear in the environment.
    """

    cleaned = text
    for secret in _secret_values():
        cleaned = cleaned.replace(secret, _SECRET_PLACEHOLDER)
    for marker in ('"accessToken":', '"refreshToken":', '"access_token":'):
        index = cleaned.find(marker)
        while index != -1:
            start = index + len(marker)
            end = cleaned.find(",", start)
            end = len(cleaned) if end == -1 else end
            cleaned = cleaned[:start] + f' "{_SECRET_PLACEHOLDER}"' + cleaned[end:]
            index = cleaned.find(marker, start + 20)
    return cleaned


def _coerce(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _coerce(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce(item) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value))


def log_event(
    stage: str,
    message: str,
    *,
    severity: str = "info",
    symbol: str | None = None,
    event_id: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Emit one structured event and return it (callers persist some).

    `stage` traces the pipeline: SCAN, DATA, SMC, SCORE, AI, RISK, PLAN,
    ORDER, FILL, POSITION, EXIT, RECONCILE, HEALTH.
    """

    record = {
        "event_id": event_id or new_event_id(),
        "ts": utc_now().isoformat(timespec="milliseconds"),
        "stage": stage,
        "severity": severity,
        "symbol": symbol,
        "message": redact(message),
        "versions": version_stamp(),
        **_coerce(fields),
    }
    line = json.dumps(record, separators=(",", ":"), default=str)
    with _lock:
        _recent.append(record)
        stream = sys.stderr if severity in ("error", "critical") else sys.stdout
        print(line, file=stream, flush=True)
    return record


def recent_events(limit: int = 100, severities: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """In-memory ring buffer used by the dashboard's health panel.

    This is a convenience view only. Anything that must survive a restart
    is written to the database, never read back from here.
    """

    wanted = set(severities) if severities else None
    with _lock:
        items = list(_recent)
    if wanted is not None:
        items = [item for item in items if item["severity"] in wanted]
    return items[-limit:]
