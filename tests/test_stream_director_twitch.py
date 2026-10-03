"""Stream Director Twitch transport with fakes: OAuth device flow, tokens,
Helix with refresh, EventSub WebSocket supervisor (welcome, subscriptions,
reconnect, keepalive loss, auth failure) and the domain integration."""

import asyncio
import importlib
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"
sys.path.insert(0, str(CORE_ROOT))
_ISOLATED = tempfile.TemporaryDirectory()
os.environ["DARKABYSS_DATA_DIR"] = str(Path(_ISOLATED.name) / "data")
for _name in ("stream_director", "stream_director_config", "stream_director_store", "stream_director_twitch"):
    sys.modules.pop(_name, None)
sd = importlib.import_module("stream_director")
sdc = importlib.import_module("stream_director_config")
sds = importlib.import_module("stream_director_store")
sdt = importlib.import_module("stream_director_twitch")

CLIENT_ID = "abcdefghijklmnopqrst123456"
ACCESS = "access-token-value-1"
REFRESH = "refresh-token-value-1"
NOW = 1_791_000_000.0


def body(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


class FakeHttp:
    """Scripted Twitch: (method, path prefix) -> list of (status, payload)."""

    def __init__(self):
        self.routes: dict[tuple[str, str], list] = {}
        self.calls: list[tuple[str, str, dict, bytes | None]] = []

    def on(self, method, path, *responses):
        self.routes.setdefault((method, path), []).extend(responses)

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append((method, url, dict(headers), data))
        parsed = urllib.parse.urlparse(url)
        key = (method, f"{parsed.netloc}{parsed.path}")
        queue = self.routes.get(key)
        if not queue:
            raise AssertionError(f"unexpected request {method} {url}")
        status, payload = queue.pop(0) if len(queue) > 1 else queue[0]
        return status, body(payload)

    def paths(self):
        return [urllib.parse.urlparse(url).path for _method, url, _headers, _data in self.calls]


def token(**changes):
    values = dict(
        access_token=ACCESS,
        refresh_token=REFRESH,
        expires_at=NOW + 3 * 3600,
        scopes=tuple(sdt.SCOPES),
        user_id="4242",
        login="streamer",
        display_name="Streamer",
        client_id=CLIENT_ID,
        generation="gen-1",
        validated_at=NOW,
    )
    values.update(changes)
    return sdt.TwitchToken(**values)


def notification(message_id, event_type, event, timestamp="2026-10-03T10:00:00.123456789Z"):
    return {
        "metadata": {"message_id": message_id, "message_type": "notification", "message_timestamp": timestamp, "subscription_type": event_type},
        "payload": {"subscription": {"type": event_type}, "event": event},
    }


class NormalizeTests(unittest.TestCase):
    def test_event_types(self):
        online = sdt.normalize_notification(notification("m1", "stream.online", {"id": "777", "type": "live", "started_at": "2026-10-03T09:00:00Z"}))
        self.assertEqual((online.kind, online.data["stream_id"]), ("online", "777"))
        self.assertEqual(online.data["started_at"], sdt.parse_time("2026-10-03T09:00:00Z"))
        self.assertIsNone(sdt.normalize_notification(notification("m2", "stream.online", {"id": "1", "type": "rerun"})))
        self.assertEqual(sdt.normalize_notification(notification("m3", "stream.offline", {})).kind, "offline")
        update = sdt.normalize_notification(notification("m4", "channel.update", {"title": "T", "category_name": "Celeste"}))
        self.assertEqual(update.data, {"title": "T", "category": "Celeste"})
        raid = sdt.normalize_notification(notification("m5", "channel.raid", {"from_broadcaster_user_name": "Pal", "viewers": 12}))
        self.assertEqual(raid.data, {"from_name": "Pal", "viewers": 12})
        self.assertIsNone(sdt.normalize_notification(notification("m6", "channel.subscribe", {"is_gift": True})))
        self.assertEqual(sdt.normalize_notification(notification("m7", "channel.subscription.gift", {"total": 5})).data, {"total": 5})
        chat = sdt.normalize_notification(
            notification("m8", "channel.chat.message", {"chatter_user_id": "9", "chatter_user_name": "Mod", "message": {"text": "!moment"}, "badges": [{"set_id": "moderator"}]})
        )
        self.assertEqual(chat.data, {"user_id": "9", "user_name": "Mod", "text": "!moment", "team": True})
        keepalive = {"metadata": {"message_id": "k", "message_type": "session_keepalive"}, "payload": {}}
        self.assertIsNone(sdt.normalize_notification(keepalive))

    def test_parse_time(self):
        self.assertAlmostEqual(sdt.parse_time("2026-10-03T10:00:00.123456789Z"), sdt.parse_time("2026-10-03T10:00:00Z") + 0.123456, places=5)
        self.assertIsNone(sdt.parse_time("yesterday"))


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = sdt.TokenStore(Path(self.temp.name) / "secrets")

    def test_round_trip_without_leaking(self):
        self.assertIsNone(self.store.load())
        self.store.save(token())
        loaded = self.store.load()
        self.assertEqual(loaded.access_token, ACCESS)
        self.assertNotIn(ACCESS, repr(loaded))
        self.assertNotIn(REFRESH, json.dumps(loaded.public()))
        self.assertEqual(loaded.public()["missing_scopes"], [])
        self.assertEqual(self.store.path.parent.name, "secrets")

    def test_refresh_does_not_overwrite_a_new_connection(self):
        self.store.save(token(generation="gen-2", access_token="from-manager"))
        self.assertFalse(self.store.save_if_current(token(generation="gen-1", access_token="refreshed-old")))
        self.assertEqual(self.store.load().access_token, "from-manager")
        self.assertTrue(self.store.save_if_current(token(generation="gen-2", access_token="refreshed-new")))
        self.store.clear()
        self.assertIsNone(self.store.load())

    def test_corrupt_token_file_means_not_connected(self):
        self.store.path.parent.mkdir(parents=True)
        self.store.path.write_text("{oops", encoding="utf-8")
        self.assertIsNone(self.store.load())


class DeviceFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = sdt.TokenStore(Path(self.temp.name) / "secrets")
        self.http = FakeHttp()
        self.http.on("POST", "id.twitch.tv/oauth2/device", (200, {"device_code": "dev", "user_code": "ABCD-EFGH", "verification_uri": "https://www.twitch.tv/activate?public=true&device-code=ABCD", "expires_in": 1800, "interval": 5}))

    def test_code_then_pending_then_token(self):
        device = sdt.start_device_flow(CLIENT_ID, self.http, clock=lambda: NOW)
        self.assertEqual((device.user_code, device.interval), ("ABCD-EFGH", 5))
        form = urllib.parse.parse_qs(self.http.calls[0][3].decode())
        self.assertEqual(form["client_id"], [CLIENT_ID])
        self.assertEqual(set(form["scopes"][0].split()), set(sdt.SCOPES))
        self.assertNotIn("client_secret", form)
        self.http.on(
            "POST",
            "id.twitch.tv/oauth2/token",
            (400, {"status": 400, "message": "authorization_pending"}),
            (200, {"access_token": ACCESS, "refresh_token": REFRESH, "expires_in": 14000, "scope": list(sdt.SCOPES)}),
        )
        self.http.on("GET", "id.twitch.tv/oauth2/validate", (200, {"client_id": CLIENT_ID, "login": "streamer", "user_id": "4242", "scopes": list(sdt.SCOPES), "expires_in": 14000}))
        sleeps = []
        result = sdt.complete_device_flow(CLIENT_ID, device, self.store, transport=self.http, clock=lambda: NOW, sleep=sleeps.append)
        self.assertEqual(sleeps, [5])
        self.assertEqual((result.user_id, result.login), ("4242", "streamer"))
        self.assertEqual(self.store.load().refresh_token, REFRESH)
        self.assertTrue(self.store.load().generation)

    def test_expired_cancelled_and_refused(self):
        device = sdt.start_device_flow(CLIENT_ID, self.http, clock=lambda: NOW)
        self.http.on("POST", "id.twitch.tv/oauth2/token", (400, {"status": 400, "message": "invalid device code"}))
        with self.assertRaises(sdt.TwitchAuthError):
            sdt.complete_device_flow(CLIENT_ID, device, self.store, transport=self.http, clock=lambda: NOW, sleep=lambda _s: None)
        with self.assertRaisesRegex(sdt.TwitchError, "cancelled"):
            sdt.complete_device_flow(CLIENT_ID, device, self.store, transport=self.http, clock=lambda: NOW, sleep=lambda _s: None, cancelled=lambda: True)
        bad = FakeHttp()
        bad.on("POST", "id.twitch.tv/oauth2/device", (400, {"message": "invalid client"}))
        with self.assertRaisesRegex(sdt.TwitchError, "Public client"):
            sdt.start_device_flow("nonexistentclient12345", bad)
        self.assertIsNone(self.store.load())

    def test_only_twitch_hosts(self):
        with self.assertRaises(sdt.TwitchError):
            sdt.urllib_transport("GET", "https://evil.example/helix/users", {}, None, 1)


class HelixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = sdt.TokenStore(Path(self.temp.name) / "secrets")
        self.store.save(token())
        self.http = FakeHttp()
        self.api = sdt.TwitchAPI(self.store, self.http, clock=lambda: NOW)

    def test_refresh_on_401_rotates_the_refresh_token(self):
        self.http.on("GET", "api.twitch.tv/helix/streams", (401, {"message": "Invalid OAuth token"}), (200, {"data": [{"id": "s1", "type": "live", "title": "Hi", "game_name": "Celeste", "viewer_count": 3, "started_at": "2026-10-03T09:00:00Z"}]}))
        self.http.on("POST", "id.twitch.tv/oauth2/token", (200, {"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 14000, "scope": list(sdt.SCOPES)}))
        live = self.api.live_stream("4242")
        self.assertEqual((live.stream_id, live.category, live.viewers), ("s1", "Celeste", 3))
        saved = self.store.load()
        self.assertEqual((saved.access_token, saved.refresh_token), ("access-2", "refresh-2"))
        headers = self.http.calls[-1][2]
        self.assertEqual(headers["Authorization"], "Bearer access-2")
        self.assertEqual(headers["Client-Id"], CLIENT_ID)
        form = urllib.parse.parse_qs(self.http.calls[1][3].decode())
        self.assertEqual(form["grant_type"], ["refresh_token"])
        self.assertNotIn("client_secret", form)

    def test_revoked_refresh_means_reconnect(self):
        self.http.on("GET", "api.twitch.tv/helix/streams", (401, {"message": "Invalid OAuth token"}))
        self.http.on("POST", "id.twitch.tv/oauth2/token", (400, {"message": "Invalid refresh token"}))
        with self.assertRaises(sdt.TwitchAuthError):
            self.api.live_stream("4242")

    def test_offline_followers_vod_and_subscription(self):
        self.http.on("GET", "api.twitch.tv/helix/streams", (200, {"data": []}))
        self.assertIsNone(self.api.live_stream("4242"))
        self.http.on("GET", "api.twitch.tv/helix/channels/followers", (200, {"total": 57, "data": []}))
        self.assertEqual(self.api.followers_total("4242"), 57)
        self.http.on("GET", "api.twitch.tv/helix/videos", (200, {"data": [{"id": "v1", "stream_id": "other", "url": "u1"}, {"id": "v2", "stream_id": "s1", "url": "https://www.twitch.tv/videos/v2"}]}))
        self.assertEqual(self.api.find_vod("4242", ["s0", "s1"]), ("v2", "https://www.twitch.tv/videos/v2"))
        self.http.on("POST", "api.twitch.tv/helix/eventsub/subscriptions", (202, {"data": [{}]}))
        self.api.subscribe("channel.chat.message", "4242", "session-1")
        payload = json.loads(self.http.calls[-1][3])
        self.assertEqual(payload["condition"], {"broadcaster_user_id": "4242", "user_id": "4242"})
        self.assertEqual(payload["transport"], {"method": "websocket", "session_id": "session-1"})
        self.api.subscribe("channel.raid", "4242", "session-1")
        self.assertEqual(json.loads(self.http.calls[-1][3])["condition"], {"to_broadcaster_user_id": "4242"})


# --------------------------------------------------------------------------
# EventSub supervisor
# --------------------------------------------------------------------------


def welcome(session_id="session-1", keepalive=10):
    return json.dumps({"metadata": {"message_id": "w", "message_type": "session_welcome"}, "payload": {"session": {"id": session_id, "keepalive_timeout_seconds": keepalive}}})


class FakeSocket:
    def __init__(self, url, messages):
        self.url = url
        self.messages = list(messages)
        self.closed = False

    async def recv(self, timeout):
        await asyncio.sleep(0)
        if not self.messages:
            raise asyncio.TimeoutError()
        item = self.messages.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self):
        self.closed = True


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = sdt.TokenStore(Path(self.temp.name) / "secrets")
        self.http = FakeHttp()
        self.http.on("POST", "api.twitch.tv/helix/eventsub/subscriptions", (202, {"data": [{}]}))
        self.events = []
        self.reconciled = []
        self.sockets = []
        self.scripts = []

    async def factory(self, url):
        socket_ = FakeSocket(url, self.scripts.pop(0) if self.scripts else [])
        self.sockets.append(socket_)
        return socket_

    def supervisor(self, client_id=CLIENT_ID, on_event=None):
        async def record(event):
            self.events.append(event)

        async def reconcile(live, followers):
            self.reconciled.append((live, followers))

        async def no_sleep(_seconds):
            await asyncio.sleep(0)

        supervisor = sdt.TwitchSupervisor(
            self.store,
            lambda: client_id,
            on_event or record,
            reconcile,
            transport=self.http,
            ws_factory=self.factory,
            clock=lambda: NOW,
            sleep=no_sleep,
            backoff=(0.01, 0.01),
            idle_wait=0.01,
        )
        return supervisor

    async def until(self, condition, timeout=5.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if condition():
                return True
            await asyncio.sleep(0.01)
        return False

    def subscription_types(self):
        return [json.loads(data)["type"] for method, url, _h, data in self.http.calls if url.endswith("/eventsub/subscriptions")]

    async def test_not_configured_and_not_connected(self):
        supervisor = self.supervisor(client_id="")
        self.assertFalse(await supervisor.poll_once())
        self.assertEqual(supervisor.status.state, "not_configured")
        supervisor = self.supervisor()
        self.assertFalse(await supervisor.poll_once())
        self.assertEqual(supervisor.status.state, "not_connected")
        self.store.save(token(client_id="otherclientid123456789"))
        self.assertFalse(await supervisor.poll_once())
        self.assertIn("Client ID changed", supervisor.status.detail)
        self.assertEqual(self.http.calls, [])

    async def test_welcome_subscribe_notify_reconnect_and_resubscribe(self):
        self.store.save(token(scopes=("user:read:chat",)))
        reconnect = json.dumps({"metadata": {"message_id": "r", "message_type": "session_reconnect"}, "payload": {"session": {"id": "session-1", "reconnect_url": "wss://eventsub.wss.twitch.tv/ws?reconnect=1"}}})
        raid = json.dumps(notification("n-raid", "channel.raid", {"from_broadcaster_user_name": "Pal", "viewers": 4}))
        self.scripts = [
            [welcome(), json.dumps({"metadata": {"message_type": "session_keepalive"}}), raid, reconnect],
            [welcome("session-1"), raid, None],  # same message again after the move; then Twitch closes
            [welcome("session-2")],
        ]
        supervisor = self.supervisor()
        task = asyncio.create_task(supervisor._eventsub_loop())
        self.assertTrue(await self.until(lambda: len(self.sockets) >= 3 and len(self.subscription_types()) >= 10))
        supervisor.stop()
        await asyncio.wait_for(task, 2)
        types = self.subscription_types()
        # Granted scope only: online/offline/update/raid + chat; no subs/cheers without their scopes.
        first = types[:5]
        self.assertEqual(set(first), {"stream.online", "stream.offline", "channel.update", "channel.raid", "channel.chat.message"})
        self.assertIn("channel.cheer", supervisor.status.failed_subscriptions)
        self.assertEqual(self.sockets[1].url, "wss://eventsub.wss.twitch.tv/ws?reconnect=1")
        self.assertTrue(self.sockets[0].closed)
        # The reconnect_url move did not resubscribe; the later fresh connection did.
        sessions = [json.loads(data)["transport"]["session_id"] for method, url, _h, data in self.http.calls if url.endswith("/eventsub/subscriptions")]
        self.assertEqual(set(sessions), {"session-1", "session-2"})
        self.assertEqual([event.message_id for event in self.events], ["n-raid", "n-raid"])
        status = json.dumps(supervisor.status.to_json())
        self.assertNotIn(ACCESS, status)
        self.assertNotIn(REFRESH, status)

    async def test_keepalive_loss_reconnects(self):
        self.store.save(token())
        self.scripts = [[welcome()], [welcome("session-2")]]
        supervisor = self.supervisor()
        task = asyncio.create_task(supervisor._eventsub_loop())
        self.assertTrue(await self.until(lambda: len(self.sockets) >= 2))
        supervisor.stop()
        await asyncio.wait_for(task, 2)
        self.assertGreaterEqual(len(self.sockets), 2)
        self.assertGreaterEqual(supervisor.status.reconnects, 1)

    async def test_auth_failure_waits_for_a_new_connection(self):
        self.store.save(token(expires_at=NOW - 10))  # expired -> refresh is attempted
        self.http.on("POST", "id.twitch.tv/oauth2/token", (400, {"message": "Invalid refresh token"}))
        supervisor = self.supervisor()
        self.assertFalse(await supervisor.poll_once())
        self.assertEqual(supervisor.status.state, "auth_failed")
        calls = len(self.http.calls)
        self.assertFalse(await supervisor.poll_once())  # same rejected token: no new attempts
        self.assertEqual(len(self.http.calls), calls)
        self.store.save(token(generation="gen-2"))  # connected again in the Manager
        self.http.on("GET", "api.twitch.tv/helix/streams", (200, {"data": []}))
        self.http.on("GET", "api.twitch.tv/helix/channels/followers", (200, {"total": 9}))
        self.assertTrue(await supervisor.poll_once())
        self.assertEqual(self.reconciled, [(None, 9)])
        self.assertFalse(supervisor.status.live)

    async def test_network_down_never_raises(self):
        self.store.save(token())

        def down(*_args):
            raise sdt.TwitchNetworkError("Twitch is not reachable right now.")

        supervisor = self.supervisor()
        supervisor.api.transport = down
        self.assertFalse(await supervisor.poll_once())
        self.assertEqual(supervisor.status.state, "error")

        async def refuse(url):
            raise sdt.TwitchNetworkError("Twitch EventSub is not reachable right now.")

        supervisor.ws_factory = refuse
        task = asyncio.create_task(supervisor._eventsub_loop())
        self.assertTrue(await self.until(lambda: supervisor.status.reconnects >= 2))
        supervisor.stop()
        await asyncio.wait_for(task, 2)
        self.assertEqual(supervisor.status.state, "error")

    async def test_events_reach_the_domain_once(self):
        self.store.save(token())
        temp = Path(self.temp.name) / "data"
        clock = lambda: sdt.parse_time("2026-10-03T10:00:01Z")
        director = sd.Director(sds.StateStore(temp), sdc.parse_config(sdc.normalize_config({})), clock)
        posts = []

        async def apply(event):
            posts.extend(director.handle_event(event))

        online = json.dumps(notification("n-on", "stream.online", {"id": "s1", "type": "live", "started_at": "2026-10-03T09:59:00Z"}, "2026-10-03T10:00:00Z"))
        raid = json.dumps(notification("n-raid", "channel.raid", {"from_broadcaster_user_name": "Pal", "viewers": 4}, "2026-10-03T10:00:00Z"))
        self.scripts = [[welcome(), online, raid, raid, online]]
        supervisor = self.supervisor(on_event=apply)
        task = asyncio.create_task(supervisor._eventsub_loop())
        self.assertTrue(await self.until(lambda: len(self.sockets) >= 2))
        supervisor.stop()
        await asyncio.wait_for(task, 2)
        self.assertEqual([effect.kind for effect in posts].count("session_started"), 1)
        self.assertEqual(len(director.session["stats"]["raids"]), 1)


if __name__ == "__main__":
    unittest.main()
