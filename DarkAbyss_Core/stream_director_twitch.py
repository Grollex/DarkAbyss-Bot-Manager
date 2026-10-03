"""Twitch transport for Stream Director: OAuth, Helix and EventSub (WebSocket).

Only official Twitch mechanisms:

* OAuth **Device Code Grant Flow** for a *public* Twitch application: the
  Manager shows a code, the streamer approves it on twitch.tv/activate; no
  client secret exists anywhere. Tokens are refreshed with the rotating
  refresh token and validated (``/oauth2/validate``) on connect and hourly.
* **EventSub over WebSocket** (wss://eventsub.wss.twitch.tv/ws): a desktop
  app cannot receive webhooks. Subscriptions are created with the session id
  after ``session_welcome``; ``session_reconnect`` is followed without
  resubscribing; any other disconnect reconnects with backoff and
  resubscribes.
* **Helix** polling every few minutes as a safety net (stream online/offline,
  title/category, viewers, follower total) and right after every
  (re)connect, so a missed event or a restart never loses the session state.

Secrets: the token file lives in the instance's secrets folder; tokens are
never logged, never part of status files, and never sent anywhere except
id.twitch.tv / api.twitch.tv. Network code is behind two small transports
(HTTP callable, WebSocket factory) so tests run with fakes.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

import stream_director as sd

OAUTH_BASE = "https://id.twitch.tv/oauth2"
HELIX_BASE = "https://api.twitch.tv/helix"
EVENTSUB_WS_URL = "wss://eventsub.wss.twitch.tv/ws?keepalive_timeout_seconds=30"
TOKEN_FILE_NAME = "twitch_oauth.json"
USER_AGENT = "DarkAbyssBotManager-StreamDirector/1"
TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 1024 * 1024
REFRESH_MARGIN_SECONDS = 300
VALIDATE_EVERY_SECONDS = 3600
POLL_EVERY_SECONDS = 120
WELCOME_TIMEOUT_SECONDS = 10
BACKOFF_SECONDS = (1, 2, 5, 10, 30, 60)
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# scope -> why Stream Director asks for it (shown in the Manager)
SCOPES = {
    "moderator:read:followers": "follower count for the recap",
    "channel:read:subscriptions": "subscriptions during the stream",
    "bits:read": "cheers during the stream",
    "user:read:chat": "chat commands (!moment, !challenge, !q …) and chat activity",
}

# EventSub type -> (version, condition builder, required scope or None, domain kind)
SUBSCRIPTIONS: dict[str, tuple[str, str, str | None]] = {
    "stream.online": ("1", "broadcaster", None),
    "stream.offline": ("1", "broadcaster", None),
    "channel.update": ("2", "broadcaster", None),
    "channel.raid": ("1", "to_broadcaster", None),
    "channel.subscribe": ("1", "broadcaster", "channel:read:subscriptions"),
    "channel.subscription.gift": ("1", "broadcaster", "channel:read:subscriptions"),
    "channel.cheer": ("1", "broadcaster", "bits:read"),
    "channel.chat.message": ("1", "chat", "user:read:chat"),
}


class TwitchError(RuntimeError):
    """A Twitch request failed (message is safe to show: no tokens)."""


class TwitchAuthError(TwitchError):
    """The Twitch authorization is gone (revoked, expired refresh token): reconnect in the Manager."""


class TwitchNetworkError(TwitchError):
    """Twitch is unreachable right now; retried automatically."""


# --------------------------------------------------------------------------
# HTTP transport
# --------------------------------------------------------------------------

HttpTransport = Callable[[str, str, dict[str, str], bytes | None, float], tuple[int, bytes]]


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> tuple[int, bytes]:
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in ("id.twitch.tv", "api.twitch.tv"):
        raise TwitchError(f"Refusing a request to an unexpected host: {host}")
    request = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT, **headers}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(MAX_RESPONSE_BYTES)
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise TwitchNetworkError("Twitch is not reachable right now.") from exc


def _json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, ValueError):
        return {}


def _error_text(status: int, body: bytes) -> str:
    payload = _json(body)
    message = payload.get("message") if isinstance(payload, dict) else None
    return f"HTTP {status}" + (f": {str(message)[:200]}" if message else "")


def _form(values: dict[str, str]) -> bytes:
    return urllib.parse.urlencode(values).encode("ascii")


# --------------------------------------------------------------------------
# tokens (instance secrets folder)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TwitchToken:
    access_token: str
    refresh_token: str
    expires_at: float
    scopes: tuple[str, ...]
    user_id: str
    login: str
    display_name: str
    client_id: str
    generation: str
    validated_at: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scopes": list(self.scopes),
            "user_id": self.user_id,
            "login": self.login,
            "display_name": self.display_name,
            "client_id": self.client_id,
            "generation": self.generation,
            "validated_at": self.validated_at,
        }

    def public(self) -> dict[str, Any]:
        """What may be shown or written to status files (no token values)."""
        return {
            "user_id": self.user_id,
            "login": self.login,
            "display_name": self.display_name,
            "scopes": sorted(self.scopes),
            "missing_scopes": sorted(set(SCOPES) - set(self.scopes)),
        }

    def __repr__(self) -> str:  # never print tokens by accident
        return f"TwitchToken(login={self.login!r}, scopes={sorted(self.scopes)!r})"


class TokenStore:
    def __init__(self, secrets_dir: Path | str) -> None:
        self.path = Path(secrets_dir) / TOKEN_FILE_NAME

    def load(self) -> TwitchToken | None:
        if not self.path.is_file() or self.path.is_symlink():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return TwitchToken(
                access_token=str(raw["access_token"]),
                refresh_token=str(raw["refresh_token"]),
                expires_at=float(raw["expires_at"]),
                scopes=tuple(str(item) for item in raw.get("scopes", [])),
                user_id=str(raw["user_id"]),
                login=str(raw["login"]),
                display_name=str(raw.get("display_name") or raw["login"]),
                client_id=str(raw["client_id"]),
                generation=str(raw.get("generation") or ""),
                validated_at=float(raw.get("validated_at") or 0),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, token: TwitchToken) -> None:
        if self.path.is_symlink():
            raise TwitchError("Refusing to write the Twitch token through a symlink.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(token.to_json(), indent=1), encoding="utf-8")
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def save_if_current(self, token: TwitchToken) -> bool:
        """Write a refreshed token only if nobody reconnected meanwhile (the
        Manager writes a new generation when the account is (re)connected)."""
        current = self.load()
        if current is not None and current.generation != token.generation:
            return False
        self.save(token)
        return True

    def clear(self) -> None:
        if self.path.is_file() and not self.path.is_symlink():
            self.path.unlink()


# --------------------------------------------------------------------------
# OAuth: device code flow, refresh, validate, revoke
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_at: float
    interval: int

    def __repr__(self) -> str:
        return f"DeviceCode(user_code={self.user_code!r})"


def start_device_flow(client_id: str, transport: HttpTransport = urllib_transport, clock: Callable[[], float] = time.time) -> DeviceCode:
    status, body = transport(
        "POST",
        f"{OAUTH_BASE}/device",
        {"Content-Type": "application/x-www-form-urlencoded"},
        _form({"client_id": client_id, "scopes": " ".join(SCOPES)}),
        TIMEOUT_SECONDS,
    )
    payload = _json(body)
    if status != 200 or not isinstance(payload, dict) or "device_code" not in payload:
        raise TwitchError(f"Twitch refused to start the login ({_error_text(status, body)}). Check the Client ID and that the application is a Public client.")
    return DeviceCode(
        device_code=str(payload["device_code"]),
        user_code=str(payload["user_code"]),
        verification_uri=str(payload["verification_uri"]),
        expires_at=clock() + float(payload.get("expires_in", 1800)),
        interval=max(1, int(payload.get("interval", 5))),
    )


def exchange_device_code(client_id: str, device: DeviceCode, transport: HttpTransport = urllib_transport) -> dict[str, Any] | None:
    """Token payload once the streamer approved, None while still pending."""
    status, body = transport(
        "POST",
        f"{OAUTH_BASE}/token",
        {"Content-Type": "application/x-www-form-urlencoded"},
        _form({"client_id": client_id, "scopes": " ".join(SCOPES), "device_code": device.device_code, "grant_type": DEVICE_GRANT}),
        TIMEOUT_SECONDS,
    )
    payload = _json(body)
    if status == 200 and isinstance(payload, dict) and payload.get("access_token"):
        return payload
    message = str(payload.get("message", "")) if isinstance(payload, dict) else ""
    if status == 400 and message in ("authorization_pending", "slow_down"):
        return None
    if status == 400 and "invalid device code" in message.lower():
        raise TwitchAuthError("The Twitch login code expired. Start the connection again.")
    raise TwitchAuthError(f"Twitch did not authorize the connection ({_error_text(status, body)}).")


def validate(access_token: str, transport: HttpTransport = urllib_transport) -> dict[str, Any]:
    status, body = transport("GET", f"{OAUTH_BASE}/validate", {"Authorization": f"OAuth {access_token}"}, None, TIMEOUT_SECONDS)
    payload = _json(body)
    if status == 401:
        raise TwitchAuthError("The Twitch token is no longer valid.")
    if status != 200 or not isinstance(payload, dict) or not payload.get("user_id"):
        raise TwitchError(f"Twitch token validation failed ({_error_text(status, body)}).")
    return payload


def token_from_payload(client_id: str, payload: dict[str, Any], identity: dict[str, Any], generation: str, clock: Callable[[], float] = time.time) -> TwitchToken:
    now = clock()
    return TwitchToken(
        access_token=str(payload["access_token"]),
        refresh_token=str(payload.get("refresh_token") or ""),
        expires_at=now + float(payload.get("expires_in") or identity.get("expires_in") or 3600),
        scopes=tuple(str(item) for item in (payload.get("scope") or identity.get("scopes") or [])),
        user_id=str(identity["user_id"]),
        login=str(identity.get("login") or ""),
        display_name=str(identity.get("display_name") or identity.get("login") or ""),
        client_id=client_id,
        generation=generation,
        validated_at=now,
    )


def complete_device_flow(
    client_id: str,
    device: DeviceCode,
    store: TokenStore,
    *,
    transport: HttpTransport = urllib_transport,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    cancelled: Callable[[], bool] = lambda: False,
) -> TwitchToken:
    """Poll until the streamer approved (Manager worker thread), then store the token."""
    while clock() < device.expires_at:
        if cancelled():
            raise TwitchError("Twitch connection cancelled.")
        payload = exchange_device_code(client_id, device, transport)
        if payload is not None:
            identity = validate(str(payload["access_token"]), transport)
            token = token_from_payload(client_id, payload, identity, uuid.uuid4().hex, clock)
            store.save(token)
            return token
        sleep(device.interval)
    raise TwitchAuthError("The Twitch login code expired. Start the connection again.")


def refresh(token: TwitchToken, transport: HttpTransport = urllib_transport, clock: Callable[[], float] = time.time) -> TwitchToken:
    status, body = transport(
        "POST",
        f"{OAUTH_BASE}/token",
        {"Content-Type": "application/x-www-form-urlencoded"},
        _form({"client_id": token.client_id, "grant_type": "refresh_token", "refresh_token": token.refresh_token}),
        TIMEOUT_SECONDS,
    )
    payload = _json(body)
    if status in (400, 401) or not isinstance(payload, dict) or not payload.get("access_token"):
        raise TwitchAuthError("Twitch did not renew the connection; connect the Twitch account again in the Manager.")
    return TwitchToken(
        access_token=str(payload["access_token"]),
        refresh_token=str(payload.get("refresh_token") or token.refresh_token),
        expires_at=clock() + float(payload.get("expires_in") or 3600),
        scopes=tuple(str(item) for item in (payload.get("scope") or token.scopes)),
        user_id=token.user_id,
        login=token.login,
        display_name=token.display_name,
        client_id=token.client_id,
        generation=token.generation,
        validated_at=token.validated_at,
    )


def revoke(token: TwitchToken, transport: HttpTransport = urllib_transport) -> None:
    try:
        transport(
            "POST",
            f"{OAUTH_BASE}/revoke",
            {"Content-Type": "application/x-www-form-urlencoded"},
            _form({"client_id": token.client_id, "token": token.access_token}),
            TIMEOUT_SECONDS,
        )
    except TwitchError:
        pass  # disconnecting locally works offline too


# --------------------------------------------------------------------------
# Helix
# --------------------------------------------------------------------------


def parse_time(value: Any) -> float | None:
    """RFC 3339 from Twitch (also with nanoseconds) -> epoch seconds."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    if "." in text:
        head, _, rest = text.partition(".")
        digits = "".join(ch for ch in rest if ch.isdigit())
        zone = rest[len(digits):]
        text = f"{head}.{digits[:6]}{zone}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class TwitchAPI:
    """Helix calls with the stored user token; refreshes once on 401."""

    def __init__(self, store: TokenStore, transport: HttpTransport = urllib_transport, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.transport = transport
        self.clock = clock

    def token(self) -> TwitchToken:
        token = self.store.load()
        if token is None:
            raise TwitchAuthError("No Twitch account is connected.")
        if token.expires_at - self.clock() < REFRESH_MARGIN_SECONDS:
            token = self._refresh(token)
        return token

    def _refresh(self, token: TwitchToken) -> TwitchToken:
        renewed = refresh(token, self.transport, self.clock)
        if not self.store.save_if_current(renewed):
            fresh = self.store.load()  # reconnected in the Manager meanwhile
            if fresh is None:
                raise TwitchAuthError("No Twitch account is connected.")
            return fresh
        return renewed

    def ensure_valid(self) -> TwitchToken:
        """Validate (Twitch asks apps to do this hourly); updates scopes/login."""
        token = self.token()
        if self.clock() - token.validated_at < VALIDATE_EVERY_SECONDS:
            return token
        try:
            identity = validate(token.access_token, self.transport)
        except TwitchAuthError:
            token = self._refresh(token)
            identity = validate(token.access_token, self.transport)
        updated = TwitchToken(
            **{
                **token.to_json(),
                "scopes": tuple(identity.get("scopes") or token.scopes),
                "login": str(identity.get("login") or token.login),
                "validated_at": self.clock(),
            }
        )
        self.store.save_if_current(updated)
        return updated

    def request(self, method: str, path: str, params: dict[str, Any] | None = None, payload: dict[str, Any] | None = None) -> Any:
        token = self.token()
        for attempt in (0, 1):
            query = f"?{urllib.parse.urlencode(params, doseq=True)}" if params else ""
            headers = {"Authorization": f"Bearer {token.access_token}", "Client-Id": token.client_id}
            body = None
            if payload is not None:
                headers["Content-Type"] = "application/json"
                body = json.dumps(payload).encode("utf-8")
            status, data = self.transport(method, f"{HELIX_BASE}/{path}{query}", headers, body, TIMEOUT_SECONDS)
            if status == 401 and attempt == 0:
                token = self._refresh(token)
                continue
            if status == 401:
                raise TwitchAuthError("Twitch rejected the connection; connect the Twitch account again in the Manager.")
            if status >= 400:
                raise TwitchError(f"Twitch {path} failed ({_error_text(status, data)}).")
            return _json(data)
        raise TwitchError("Twitch request failed.")

    def live_stream(self, user_id: str) -> sd.LiveStream | None:
        data = (self.request("GET", "streams", {"user_id": user_id}) or {}).get("data") or []
        for item in data:
            if item.get("type") == "live":
                return sd.LiveStream(
                    stream_id=str(item.get("id")),
                    title=str(item.get("title") or ""),
                    category=str(item.get("game_name") or ""),
                    started_at=parse_time(item.get("started_at")),
                    viewers=int(item["viewer_count"]) if isinstance(item.get("viewer_count"), int) else None,
                )
        return None

    def followers_total(self, user_id: str) -> int | None:
        try:
            payload = self.request("GET", "channels/followers", {"broadcaster_id": user_id, "first": 1})
        except TwitchAuthError:
            raise
        except TwitchError:
            return None
        total = (payload or {}).get("total")
        return int(total) if isinstance(total, int) else None

    def find_vod(self, user_id: str, stream_ids: list[str]) -> tuple[str, str] | None:
        """The past-broadcast video of this session (if the streamer keeps VODs)."""
        payload = self.request("GET", "videos", {"user_id": user_id, "type": "archive", "first": 5})
        for item in (payload or {}).get("data") or []:
            if str(item.get("stream_id")) in stream_ids and item.get("id") and item.get("url"):
                return str(item["id"]), str(item["url"])
        return None

    def subscribe(self, event_type: str, user_id: str, session_id: str) -> None:
        version, condition_kind, _scope = SUBSCRIPTIONS[event_type]
        if condition_kind == "to_broadcaster":
            condition = {"to_broadcaster_user_id": user_id}
        elif condition_kind == "chat":
            condition = {"broadcaster_user_id": user_id, "user_id": user_id}
        else:
            condition = {"broadcaster_user_id": user_id}
        self.request(
            "POST",
            "eventsub/subscriptions",
            payload={"type": event_type, "version": version, "condition": condition, "transport": {"method": "websocket", "session_id": session_id}},
        )


# --------------------------------------------------------------------------
# EventSub notifications -> domain events
# --------------------------------------------------------------------------


def normalize_notification(message: dict[str, Any]) -> sd.StreamEvent | None:
    metadata = message.get("metadata") or {}
    payload = message.get("payload") or {}
    if metadata.get("message_type") != "notification":
        return None
    event_type = metadata.get("subscription_type") or (payload.get("subscription") or {}).get("type")
    event = payload.get("event") or {}
    message_id = str(metadata.get("message_id") or "")
    at = parse_time(metadata.get("message_timestamp")) or time.time()
    if not message_id or not isinstance(event, dict):
        return None
    if event_type == "stream.online":
        if event.get("type", "live") != "live":
            return None
        return sd.StreamEvent("online", message_id, at, {"stream_id": str(event.get("id") or ""), "started_at": parse_time(event.get("started_at"))})
    if event_type == "stream.offline":
        return sd.StreamEvent("offline", message_id, at, {})
    if event_type == "channel.update":
        return sd.StreamEvent("update", message_id, at, {"title": event.get("title") or "", "category": event.get("category_name") or ""})
    if event_type == "channel.raid":
        return sd.StreamEvent(
            "raid", message_id, at, {"from_name": event.get("from_broadcaster_user_name") or event.get("from_broadcaster_user_login") or "", "viewers": event.get("viewers") or 0}
        )
    if event_type == "channel.subscribe":
        if event.get("is_gift"):
            return None  # counted once through channel.subscription.gift
        return sd.StreamEvent("sub", message_id, at, {"user_name": event.get("user_name") or ""})
    if event_type == "channel.subscription.gift":
        return sd.StreamEvent("gift", message_id, at, {"total": event.get("total") or 1})
    if event_type == "channel.cheer":
        return sd.StreamEvent("cheer", message_id, at, {"bits": event.get("bits") or 0})
    if event_type == "channel.chat.message":
        badges = {str(item.get("set_id")) for item in event.get("badges") or [] if isinstance(item, dict)}
        return sd.StreamEvent(
            "chat",
            message_id,
            at,
            {
                "user_id": str(event.get("chatter_user_id") or ""),
                "user_name": event.get("chatter_user_name") or event.get("chatter_user_login") or "",
                "text": ((event.get("message") or {}).get("text") or "") if isinstance(event.get("message"), dict) else "",
                "team": bool(badges & {"broadcaster", "moderator"}),
            },
        )
    return None


# --------------------------------------------------------------------------
# EventSub WebSocket client and the supervisor
# --------------------------------------------------------------------------


class WebSocket(Protocol):
    async def recv(self, timeout: float) -> str | None: ...  # None = closed

    async def close(self) -> None: ...


WebSocketFactory = Callable[[str], Awaitable[WebSocket]]


class AiohttpWebSocket:
    """aiohttp (bundled with discord.py) behind the small WebSocket protocol."""

    def __init__(self, session: Any, socket_: Any) -> None:
        self._session = session
        self._socket = socket_

    @classmethod
    async def connect(cls, url: str) -> "AiohttpWebSocket":
        import aiohttp

        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "wss" or not (parsed.hostname or "").endswith(".twitch.tv"):
            raise TwitchError("Unexpected EventSub address.")
        session = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
        try:
            ws_timeout = aiohttp.ClientWSTimeout(ws_close=10) if hasattr(aiohttp, "ClientWSTimeout") else 10.0  # aiohttp < 3.10
            socket_ = await session.ws_connect(url, heartbeat=None, timeout=ws_timeout)
        except Exception as exc:
            await session.close()
            raise TwitchNetworkError("Twitch EventSub is not reachable right now.") from exc
        return cls(session, socket_)

    async def recv(self, timeout: float) -> str | None:
        import aiohttp

        try:
            message = await self._socket.receive(timeout=timeout)
        except asyncio.TimeoutError:
            raise
        if message.type == aiohttp.WSMsgType.TEXT:
            return str(message.data)
        if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
            return None
        return ""

    async def close(self) -> None:
        try:
            await self._socket.close()
        finally:
            await self._session.close()


@dataclass
class TwitchStatus:
    state: str = "not_configured"  # not_configured | not_connected | connecting | connected | auth_failed | error
    detail: str = ""
    account: dict[str, Any] | None = None
    subscriptions: list[str] = field(default_factory=list)
    failed_subscriptions: list[str] = field(default_factory=list)
    live: bool | None = None
    last_event_at: float | None = None
    last_poll_at: float | None = None
    reconnects: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "detail": self.detail,
            "account": self.account,
            "subscriptions": list(self.subscriptions),
            "failed_subscriptions": list(self.failed_subscriptions),
            "live": self.live,
            "last_event_at": self.last_event_at,
            "last_poll_at": self.last_poll_at,
            "reconnects": self.reconnects,
        }


EventHandler = Callable[[sd.StreamEvent], Awaitable[None]]
ReconcileHandler = Callable[[sd.LiveStream | None, int | None], Awaitable[None]]


class TwitchSupervisor:
    """Keeps EventSub connected and polls Helix; never raises out of run().

    The bot passes two callbacks: ``on_event`` for notifications and
    ``on_reconcile`` for the polled channel state. A missing Client ID or
    account only changes ``status``; Discord features keep working.
    """

    def __init__(
        self,
        store: TokenStore,
        client_id: Callable[[], str],
        on_event: EventHandler,
        on_reconcile: ReconcileHandler,
        *,
        transport: HttpTransport = urllib_transport,
        ws_factory: WebSocketFactory = AiohttpWebSocket.connect,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_every: float = POLL_EVERY_SECONDS,
        url: str = EVENTSUB_WS_URL,
        backoff: tuple[float, ...] = BACKOFF_SECONDS,
        idle_wait: float = 10.0,
    ) -> None:
        self.store = store
        self.client_id = client_id
        self.on_event = on_event
        self.on_reconcile = on_reconcile
        self.api = TwitchAPI(store, transport, clock)
        self.ws_factory = ws_factory
        self.clock = clock
        self.sleep = sleep
        self.poll_every = poll_every
        self.url = url
        self.backoff = backoff
        self.idle_wait = idle_wait
        self.status = TwitchStatus()
        self._stop = asyncio.Event()
        self._poll_now = asyncio.Event()
        self._user_id: str | None = None
        self._failed_token: TwitchToken | None = None  # rejected; wait for a reconnect in the Manager

    def stop(self) -> None:
        self._stop.set()

    def request_poll(self) -> None:
        self._poll_now.set()

    async def _blocking(self, function: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.to_thread(function, *args)

    def _ready(self) -> TwitchToken | None:
        client_id = (self.client_id() or "").strip()
        if not client_id:
            self.status.state, self.status.detail, self.status.account = "not_configured", "Add the Twitch Client ID in the Manager.", None
            return None
        token = self.store.load()
        if token is None:
            self.status.state, self.status.detail, self.status.account = "not_connected", "Connect the Twitch account in the Manager.", None
            return None
        if token.client_id != client_id:
            self.status.state, self.status.detail = "not_connected", "The Twitch Client ID changed; connect the Twitch account again."
            self.status.account = None
            return None
        if self._failed_token is not None:
            if token == self._failed_token:
                return None  # still the rejected authorization
            self._failed_token = None
        return token

    def _auth_failed(self, token: TwitchToken, exc: Exception) -> None:
        self.status.state, self.status.detail = "auth_failed", str(exc)
        self._failed_token = self.store.load() or token

    # -- the loops ----------------------------------------------------------------------

    async def run(self) -> None:
        await asyncio.gather(self._eventsub_loop(), self._poll_loop())

    async def _poll_loop(self) -> None:
        while not self._stop.is_set():
            await self.poll_once()
            self._poll_now.clear()
            try:
                await asyncio.wait_for(self._poll_now.wait(), timeout=self.poll_every)
            except asyncio.TimeoutError:
                pass

    async def poll_once(self) -> bool:
        token = self._ready()
        if token is None:
            return False
        try:
            token = await self._blocking(self.api.ensure_valid)
            self.status.account = token.public()
            self._user_id = token.user_id
            live = await self._blocking(self.api.live_stream, token.user_id)
            followers = await self._blocking(self.api.followers_total, token.user_id) if "moderator:read:followers" in token.scopes else None
        except TwitchAuthError as exc:
            self._auth_failed(token, exc)
            return False
        except TwitchError as exc:
            if self.status.state != "connected":
                self.status.state = "error"
            self.status.detail = str(exc)
            return False
        self.status.live = live is not None
        self.status.last_poll_at = self.clock()
        try:
            await self.on_reconcile(live, followers)
        except Exception as exc:  # the domain reports its own problems
            self.status.detail = f"Session update failed: {type(exc).__name__}"
        return True

    async def _eventsub_loop(self) -> None:
        failures = 0
        while not self._stop.is_set():
            token = self._ready()
            if token is None:
                await self._wait(self.idle_wait)
                continue
            self.status.state = "connecting"
            try:
                await self._connection(token)
                failures = 0
            except TwitchAuthError as exc:
                self._auth_failed(token, exc)
                failures += 1
            except (TwitchError, OSError, asyncio.TimeoutError, ValueError) as exc:
                self.status.state = "error"
                self.status.detail = str(exc) if isinstance(exc, TwitchError) else f"EventSub connection lost ({type(exc).__name__}); reconnecting."
                failures += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let the bot die because of Twitch
                self.status.state = "error"
                self.status.detail = f"EventSub error ({type(exc).__name__}); reconnecting."
                failures += 1
            if not self._stop.is_set():
                self.status.reconnects += 1
                await self._wait(self.backoff[min(failures, len(self.backoff) - 1)] if failures else self.backoff[0])

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _connection(self, token: TwitchToken) -> None:
        socket_ = await self.ws_factory(self.url)
        try:
            session = await self._welcome(socket_)
            token = await self._blocking(self.api.ensure_valid)
            self.status.account = token.public()
            self._user_id = token.user_id
            await self._subscribe_all(token, session["id"])
            self.status.state, self.status.detail = "connected", ""
            self.request_poll()  # catch up on anything missed while disconnected
            keepalive = float(session.get("keepalive_timeout_seconds") or 30)
            while not self._stop.is_set():
                text = await socket_.recv(timeout=keepalive + 10)
                if text is None:
                    raise TwitchNetworkError("Twitch closed the EventSub connection; reconnecting.")
                if not text:
                    continue
                message = json.loads(text)
                kind = (message.get("metadata") or {}).get("message_type")
                if kind == "session_reconnect":
                    new_url = ((message.get("payload") or {}).get("session") or {}).get("reconnect_url")
                    if not isinstance(new_url, str):
                        raise TwitchNetworkError("Twitch asked to reconnect without an address.")
                    replacement = await self.ws_factory(new_url)
                    await self._welcome(replacement)  # subscriptions move with the session
                    await socket_.close()
                    socket_ = replacement
                    self.status.reconnects += 1
                elif kind == "revocation":
                    revoked = ((message.get("payload") or {}).get("subscription") or {}).get("type")
                    self.status.failed_subscriptions = sorted(set(self.status.failed_subscriptions) | {str(revoked)})
                    if revoked in ("stream.online", "stream.offline"):
                        raise TwitchAuthError("Twitch revoked the stream subscriptions; connect the Twitch account again in the Manager.")
                elif kind == "notification":
                    event = normalize_notification(message)
                    self.status.last_event_at = self.clock()
                    if event is not None:
                        if event.kind in ("online", "offline"):
                            self.request_poll()
                        await self.on_event(event)
        finally:
            await socket_.close()

    async def _welcome(self, socket_: WebSocket) -> dict[str, Any]:
        text = await socket_.recv(timeout=WELCOME_TIMEOUT_SECONDS)
        message = json.loads(text or "{}")
        if (message.get("metadata") or {}).get("message_type") != "session_welcome":
            raise TwitchNetworkError("Twitch EventSub did not greet the connection.")
        session = (message.get("payload") or {}).get("session") or {}
        if not session.get("id"):
            raise TwitchNetworkError("Twitch EventSub sent no session id.")
        return session

    async def _subscribe_all(self, token: TwitchToken, session_id: str) -> None:
        created, failed = [], []
        for event_type, (_version, _condition, scope) in SUBSCRIPTIONS.items():
            if scope is not None and scope not in token.scopes:
                failed.append(event_type)
                continue
            try:
                await self._blocking(self.api.subscribe, event_type, token.user_id, session_id)
                created.append(event_type)
            except TwitchAuthError:
                raise
            except TwitchError:
                failed.append(event_type)
        if "stream.online" not in created:
            raise TwitchError("Twitch did not accept the stream subscriptions; retrying.")
        self.status.subscriptions = created
        self.status.failed_subscriptions = failed

    async def vod_for(self, stream_ids: list[str]) -> tuple[str, str] | None:
        if not stream_ids or self._user_id is None:
            return None
        try:
            return await self._blocking(self.api.find_vod, self._user_id, stream_ids)
        except TwitchError:
            return None
