"""Tolerant parsing of MCP tool results.

Server builds differ in how they wrap payloads (``structuredContent`` vs a JSON
string in a text block) and in the exact key names inside them.  Rather than
pinning one shape, every accessor here searches a set of aliases and gives up
gracefully, so a cosmetic rename upstream degrades one field instead of crashing
the bot.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from mcp_client.errors import ToolCallError

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm(key: str) -> str:
    return _NON_ALNUM.sub("", str(key).lower())


def extract_payload(result: Any) -> Any:
    """Turn a ``CallToolResult`` into plain Python data.

    Raises :class:`ToolCallError` when the server flagged the call as an error -
    those must never be mistaken for empty-but-successful results (an empty
    position list and a failed position query mean opposite things to the risk
    guards).
    """
    if result is None:
        return None

    is_error = getattr(result, "is_error", None)
    if is_error is None:
        is_error = getattr(result, "isError", None)

    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)

    texts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            texts.append(text)

    if is_error:
        detail = " ".join(texts) or str(structured) or "unknown tool error"
        raise ToolCallError(detail[:500])

    if structured is not None:
        # Some servers wrap everything in {"result": ...}.
        if isinstance(structured, Mapping) and set(structured.keys()) == {"result"}:
            return structured["result"]
        return structured

    if texts:
        blob = "\n".join(texts).strip()
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            return blob
    return None


def pick(source: Any, *aliases: str, default: Any = None) -> Any:
    """Fetch the first matching key from a mapping, comparing loosely."""
    if not isinstance(source, Mapping):
        return default
    normalized = {_norm(k): v for k, v in source.items()}
    for alias in aliases:
        value = normalized.get(_norm(alias))
        if value is not None:
            return value
    return default


def find_list(payload: Any, *keys: str) -> list[Any]:
    """Locate the list of records inside a payload of unknown shape."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in keys:
            value = pick(payload, key)
            if isinstance(value, list):
                return value
        # Fall back to the first list-valued entry (e.g. {"data": [...]}).
        for value in payload.values():
            if isinstance(value, list):
                return value
        # A single record returned bare.
        return [payload]
    return []


def as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        try:
            return float(cleaned)
        except ValueError:
            return default
    return default


def as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    parsed = as_float(value, None)
    return int(parsed) if parsed is not None else default


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Accept epoch ms, epoch s, epoch minutes or ISO-8601."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    number = as_float(value, None)
    if number is not None and not (isinstance(value, str) and not value.strip().isdigit()):
        magnitude = abs(number)
        if magnitude >= 1e17:          # nanoseconds
            number /= 1e9
        elif magnitude >= 1e14:        # microseconds
            number /= 1e6
        elif magnitude >= 1e11:        # milliseconds
            number /= 1e3
        elif magnitude < 1e8:          # minutes since epoch (cTrader trendbars)
            number *= 60
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def normalize_side(value: Any) -> str:
    """Map any of BUY/1/LONG/buy onto canonical ``"BUY"``/``"SELL"``."""
    if value is None:
        return ""
    text = str(value).strip().upper()
    if text in {"BUY", "LONG", "B", "1", "BID"}:
        return "BUY"
    if text in {"SELL", "SHORT", "S", "2", "ASK"}:
        return "SELL"
    return text


def collect_numbers(records: Iterable[Any], keys: Sequence[str], limit: int = 8) -> list[float]:
    """Sample numeric values from records - used to auto-detect price scaling."""
    out: list[float] = []
    for record in records:
        for key in keys:
            value = as_float(pick(record, key), None)
            if value:
                out.append(value)
        if len(out) >= limit:
            break
    return out[:limit]
