"""AI usage statistics, built only from what providers report.

Each bot keeps its own file ``instances/<id>/data/ai_usage.json`` (via
``ai_storage.InstanceAIStores.usage``); Manager Test Connection calls go to
``config/ai_usage_manager.json``. Schema (version 2):

    {"version": 2,
     "days": {"2026-10-02": {"groq": {"groq-main": {"openai/gpt-oss-120b":
         {"requests": 18, "failed_requests": 2, "input_tokens": 34000,
          "output_tokens": 6800, "thinking_tokens": 0, "total_tokens": 41000}}}}},
     "rate_limits": {"groq": {"groq-main": {"openai/gpt-oss-120b":
         {"captured_at": 1790000000.0, "remaining_tokens": 6100, ...}}}}}

Level 3 is the connection (= credential reference): rate limits belong to one
API key, and several connections may use the same provider and model.
Version 1 files (provider -> model) are read as connection ``<provider>-default``.

* Every provider HTTP response the adapter actually received is one request:
  retries, 429/5xx answers and Manager "Test Connection" calls included.
  ``failed_requests`` counts the non-2xx ones. A network failure without any
  response is not counted (nothing was received).
* Tokens come only from the response itself (Groq ``usage``, Gemini
  ``usageMetadata``), for failed responses too if the provider reported them;
  nothing is estimated.
* Remaining/limit/reset come from Groq ``x-ratelimit-*`` headers of the most
  recent response that carried them (intermediate 429s included). Gemini sends
  no such headers, so nothing is shown for it.
* No prompts, responses, keys or other request content are stored.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

USAGE_FILE_NAME = "ai_usage.json"
STORE_VERSION = 2
RETENTION_DAYS = 31
MAX_MODEL_ID = 128
LOCK_TIMEOUT_SECONDS = 2.0
UNKNOWN_CONNECTION = "unknown"
TOKEN_FIELDS = ("input_tokens", "output_tokens", "thinking_tokens", "total_tokens")
COUNT_FIELDS = ("requests", "failed_requests", *TOKEN_FIELDS)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")

# Groq rate-limit headers (lower-case) -> stored field.
GROQ_RATE_LIMIT_HEADERS = {
    "x-ratelimit-limit-requests": "limit_requests",
    "x-ratelimit-limit-tokens": "limit_tokens",
    "x-ratelimit-remaining-requests": "remaining_requests",
    "x-ratelimit-remaining-tokens": "remaining_tokens",
    "x-ratelimit-reset-requests": "reset_requests_seconds",
    "x-ratelimit-reset-tokens": "reset_tokens_seconds",
}


@dataclass(frozen=True)
class UsageEvent:
    """One received provider HTTP response (``success`` = 2xx)."""

    provider_id: str
    model_id: str
    success: bool = True
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    total_tokens: int | None = None
    rate_limits: Mapping[str, float] = field(default_factory=dict)
    connection_id: str | None = None  # credential reference of the key used


@dataclass(frozen=True)
class ModelUsage:
    provider_id: str
    model_id: str
    requests: int
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    total_tokens: int
    rate_limits: Mapping[str, float]
    failed_requests: int = 0
    connection_id: str = UNKNOWN_CONNECTION


# --------------------------------------------------------------------------
# parsing provider data (no content, numbers only)
# --------------------------------------------------------------------------


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(value)


def parse_duration_seconds(text: Any) -> float | None:
    """Groq reset values such as "7.66s", "750ms", "2m59.56s", "1h2m3s"."""
    if not isinstance(text, str) or not text.strip() or len(text) > 32:
        return None
    value = text.strip().lower()
    parts = _DURATION_PART.findall(value)
    if not parts or "".join(number + unit for number, unit in parts) != value:
        return None
    scale = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    return sum(float(number) * scale[unit] for number, unit in parts)


def groq_rate_limits(headers: Mapping[str, Any] | None) -> dict[str, float]:
    if not headers:
        return {}
    lowered = {str(key).lower(): value for key, value in headers.items()}
    limits: dict[str, float] = {}
    for header, name in GROQ_RATE_LIMIT_HEADERS.items():
        raw = lowered.get(header)
        if raw is None:
            continue
        if name.endswith("_seconds"):
            seconds = parse_duration_seconds(str(raw))
            if seconds is not None:
                limits[name] = seconds
        else:
            try:
                number = int(str(raw).strip())
            except ValueError:
                continue
            if number >= 0:
                limits[name] = number
    return limits


def _json_object(response_bytes: bytes | None, key: str) -> Mapping[str, Any]:
    """``payload[key]`` when the body is a JSON object carrying it, else {}."""
    if not response_bytes:
        return {}
    try:
        payload = json.loads(response_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    value = payload.get(key) if isinstance(payload, dict) else None
    return value if isinstance(value, dict) else {}


def groq_event(
    model_id: str,
    status: int,
    response_bytes: bytes | None,
    headers: Mapping[str, Any] | None,
    connection_id: str | None = None,
) -> UsageEvent:
    usage = _json_object(response_bytes, "usage")
    return UsageEvent(
        provider_id="groq",
        model_id=model_id,
        success=200 <= int(status) < 300,
        input_tokens=_count(usage.get("prompt_tokens")),
        output_tokens=_count(usage.get("completion_tokens")),
        total_tokens=_count(usage.get("total_tokens")),
        rate_limits=groq_rate_limits(headers),
        connection_id=connection_id,
    )


def gemini_event(model_id: str, status: int, response_bytes: bytes | None, connection_id: str | None = None) -> UsageEvent:
    usage = _json_object(response_bytes, "usageMetadata")
    return UsageEvent(
        provider_id="gemini",
        model_id=model_id,
        success=200 <= int(status) < 300,
        input_tokens=_count(usage.get("promptTokenCount")),
        output_tokens=_count(usage.get("candidatesTokenCount")),
        thinking_tokens=_count(usage.get("thoughtsTokenCount")),
        total_tokens=_count(usage.get("totalTokenCount")),
        connection_id=connection_id,
    )


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------


def _day_key(now: float) -> str:
    return datetime.fromtimestamp(now).strftime("%Y-%m-%d")


def _empty() -> dict[str, Any]:
    return {"version": STORE_VERSION, "days": {}, "rate_limits": {}}


def _upgrade_v1(raw: dict[str, Any]) -> dict[str, Any]:
    """provider -> model  becomes  provider -> "<provider>-default" -> model."""
    days = {}
    for day, providers in (raw.get("days") or {}).items():
        if isinstance(providers, dict):
            days[day] = {
                provider: {f"{provider}-default": models}
                for provider, models in providers.items()
                if isinstance(models, dict)
            }
    limits = {
        provider: {f"{provider}-default": models}
        for provider, models in (raw.get("rate_limits") or {}).items()
        if isinstance(models, dict)
    }
    return {"version": STORE_VERSION, "days": days, "rate_limits": limits}


@contextmanager
def _interprocess_lock(lock_path: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Serialize writers across processes (a bot, the Manager)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    locked = False
    try:
        try:
            import msvcrt
        except ImportError:  # pragma: no cover - non-Windows development
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
            yield
            return
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("AI usage file is busy.")
                time.sleep(0.02)
        yield
    finally:
        if locked:
            try:
                try:
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except ImportError:  # pragma: no cover
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


class AIUsageStore:
    """Aggregated usage: provider -> connection -> model -> day."""

    def __init__(self, path: Path | str) -> None:
        if path is None or (isinstance(path, str) and not path.strip()):
            raise ValueError("AI usage path is required.")
        self.path = Path(path).resolve()
        self._lock = threading.Lock()

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.lock")

    # -- file ---------------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty()
        raw = json.loads(self.path.read_text(encoding="utf-8"))  # ValueError/OSError: unreadable
        if not isinstance(raw, dict) or not isinstance(raw.get("days", {}), dict) or not isinstance(raw.get("rate_limits", {}), dict):
            raise ValueError("AI usage file has an invalid shape.")
        if raw.get("version", 1) == 1:
            return _upgrade_v1(raw)
        return {"version": STORE_VERSION, "days": raw.get("days", {}), "rate_limits": raw.get("rate_limits", {})}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        temp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, self.path)

    def load(self) -> dict[str, Any]:
        """Read-only view. Raises ValueError/OSError for an unreadable file."""
        with self._lock:
            return self._read()

    # -- recording -------------------------------------------------------------

    def record(self, event: UsageEvent, now: float | None = None) -> None:
        if not isinstance(event, UsageEvent) or not _SAFE_ID.fullmatch(str(event.provider_id)):
            return
        model_id = str(event.model_id or "")[:MAX_MODEL_ID]
        connection_id = str(event.connection_id or UNKNOWN_CONNECTION)
        if not model_id or not _SAFE_ID.fullmatch(connection_id):
            return
        now = time.time() if now is None else float(now)
        with self._lock, _interprocess_lock(self.lock_path):
            try:
                data = self._read()
            except (OSError, ValueError):
                # Keep the unreadable file for inspection; statistics start over.
                try:
                    self.path.replace(self.path.with_name(f"{self.path.stem}.corrupt-{int(now)}.json"))
                except OSError:
                    return
                data = _empty()
            day = data["days"].setdefault(_day_key(now), {})
            entry = day.setdefault(event.provider_id, {}).setdefault(connection_id, {}).setdefault(model_id, {})
            entry["requests"] = int(entry.get("requests", 0)) + 1
            entry["failed_requests"] = int(entry.get("failed_requests", 0)) + (0 if event.success else 1)
            for name in TOKEN_FIELDS:
                entry[name] = int(entry.get(name, 0)) + int(getattr(event, name) or 0)
            if event.rate_limits:
                data["rate_limits"].setdefault(event.provider_id, {}).setdefault(connection_id, {})[model_id] = {
                    "captured_at": now,
                    **{key: value for key, value in event.rate_limits.items()},
                }
            cutoff = _day_key(now - RETENTION_DAYS * 86400)
            data["days"] = {key: value for key, value in data["days"].items() if key >= cutoff}
            try:
                self._write(data)
            except OSError:
                pass

    def recorder(self):
        """Callable for provider adapters; never raises into a request."""

        def record(event: UsageEvent) -> None:
            try:
                self.record(event)
            except Exception:
                pass

        return record

    # -- reading ---------------------------------------------------------------------

    def day_usage(self, provider_id: str, now: float | None = None, connection_id: str | None = None) -> list[ModelUsage]:
        """Today's (local day) usage of one provider (optionally one connection), busiest first."""
        now = time.time() if now is None else float(now)
        data = self.load()
        connections = (data["days"].get(_day_key(now)) or {}).get(provider_id) or {}
        all_limits = data["rate_limits"].get(provider_id) if isinstance(data["rate_limits"].get(provider_id), dict) else {}
        rows = []
        seen = set()
        for conn_id, models in connections.items():
            if connection_id is not None and conn_id != connection_id or not isinstance(models, dict):
                continue
            limits = all_limits.get(conn_id) if isinstance(all_limits.get(conn_id), dict) else {}
            for model_id, entry in models.items():
                if not isinstance(entry, dict):
                    continue
                counts = {name: _count(entry.get(name)) or 0 for name in COUNT_FIELDS}
                seen.add((conn_id, model_id))
                rows.append(
                    ModelUsage(
                        provider_id=provider_id,
                        model_id=model_id,
                        requests=counts["requests"],
                        failed_requests=counts["failed_requests"],
                        input_tokens=counts["input_tokens"],
                        output_tokens=counts["output_tokens"],
                        thinking_tokens=counts["thinking_tokens"],
                        total_tokens=counts["total_tokens"],
                        rate_limits=dict(limits.get(model_id) or {}),
                        connection_id=conn_id,
                    )
                )
        for conn_id, models in all_limits.items():
            if connection_id is not None and conn_id != connection_id or not isinstance(models, dict):
                continue
            for model_id, snapshot in models.items():
                if (conn_id, model_id) not in seen and isinstance(snapshot, dict):
                    rows.append(ModelUsage(provider_id, model_id, 0, 0, 0, 0, 0, dict(snapshot), 0, conn_id))
        rows.sort(key=lambda row: (-row.requests, row.connection_id, row.model_id))
        return rows


def merged_usage(
    stores: Iterable[AIUsageStore],
    provider_id: str,
    connection_id: str,
    now: float | None = None,
) -> list[ModelUsage]:
    """One connection's usage summed over several stores (all bots + Manager):
    counts add up, the freshest rate-limit snapshot wins (limits belong to the key).
    Unreadable stores are skipped."""
    merged: dict[str, dict[str, Any]] = {}
    for store in stores:
        try:
            rows = store.day_usage(provider_id, now, connection_id)
        except (OSError, ValueError):
            continue
        for row in rows:
            item = merged.setdefault(row.model_id, {name: 0 for name in COUNT_FIELDS} | {"rate_limits": {}})
            for name in COUNT_FIELDS:
                item[name] += getattr(row, name)
            if float(row.rate_limits.get("captured_at", 0) or 0) > float(item["rate_limits"].get("captured_at", 0) or 0):
                item["rate_limits"] = dict(row.rate_limits)
    rows = [
        ModelUsage(
            provider_id,
            model_id,
            item["requests"],
            item["input_tokens"],
            item["output_tokens"],
            item["thinking_tokens"],
            item["total_tokens"],
            item["rate_limits"],
            item["failed_requests"],
            connection_id,
        )
        for model_id, item in merged.items()
    ]
    rows.sort(key=lambda row: (-row.requests, row.model_id))
    return rows


# --------------------------------------------------------------------------
# compact text for the Manager
# --------------------------------------------------------------------------


def compact_number(value: int | float) -> str:
    value = float(value)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 10_000:
        return f"{round(value / 1000):d}k"
    if value >= 1000:
        return f"{value / 1000:.1f}k".replace(".0k", "k")
    return str(int(value))


def _current_limit(limits: Mapping[str, Any], remaining_key: str, reset_key: str, now: float) -> tuple[float, float | None] | None:
    """(remaining, reset timestamp) while the provider's window is still open."""
    remaining = limits.get(remaining_key)
    captured = limits.get("captured_at")
    if not isinstance(remaining, (int, float)) or isinstance(remaining, bool):
        return None
    reset = limits.get(reset_key)
    if isinstance(reset, (int, float)) and isinstance(captured, (int, float)):
        reset_at = float(captured) + float(reset)
        if reset_at <= now:
            return None  # window already reset: the stored value is outdated
        return float(remaining), reset_at
    return float(remaining), None


def usage_lines(row: ModelUsage, display_name: str | None = None, now: float | None = None) -> list[str]:
    """Two compact lines, e.g.
    "GPT-OSS 120B · 18 req · 41k tok" / "34k in / 6.8k out · TPM 6.1k left"."""
    now = time.time() if now is None else float(now)
    name = display_name or row.model_id
    tokens_left = _current_limit(row.rate_limits, "remaining_tokens", "reset_tokens_seconds", now)
    requests_left = _current_limit(row.rate_limits, "remaining_requests", "reset_requests_seconds", now)
    if not row.requests and tokens_left is None and requests_left is None:
        return []  # only an outdated rate-limit snapshot: nothing real to show
    total = row.total_tokens or (row.input_tokens + row.output_tokens + row.thinking_tokens)
    failed = f" ({row.failed_requests} failed)" if row.failed_requests else ""
    first = f"{name} · {row.requests} req{failed} · {compact_number(total)} tok"
    details = []
    if row.input_tokens or row.output_tokens or row.thinking_tokens:
        tokens = f"{compact_number(row.input_tokens)} in / {compact_number(row.output_tokens)} out"
        if row.thinking_tokens:
            tokens += f" / {compact_number(row.thinking_tokens)} think"
        details.append(tokens)
    if tokens_left is not None:
        details.append(f"TPM {compact_number(tokens_left[0])} left")
    if requests_left is not None:
        text = f"RPD {compact_number(requests_left[0])} left"
        if requests_left[1] is not None:
            text += f" (resets {datetime.fromtimestamp(requests_left[1]).strftime('%H:%M')})"
        details.append(text)
    return [first, " · ".join(details)] if details else [first]


def rows_text(
    rows: Iterable[ModelUsage],
    display_names: Mapping[str, str] | None = None,
    now: float | None = None,
    max_models: int = 2,
) -> str:
    lines: list[str] = []
    shown = 0
    for row in rows:
        row_lines = usage_lines(row, (display_names or {}).get(row.model_id), now)
        if row_lines and shown < max_models:
            lines.extend(row_lines)
            shown += 1
    return "\n".join(lines) if lines else "No usage yet"


def provider_usage_text(
    store: AIUsageStore | None,
    provider_id: str,
    display_names: Mapping[str, str] | None = None,
    now: float | None = None,
    max_models: int = 2,
    connection_id: str | None = None,
) -> str:
    if store is None:
        return "No usage yet"
    try:
        rows = store.day_usage(provider_id, now, connection_id)
    except (OSError, ValueError):
        return "Usage data unreadable"
    return rows_text(rows, display_names, now, max_models)
