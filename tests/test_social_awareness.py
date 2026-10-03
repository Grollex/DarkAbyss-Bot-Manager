"""Kairo Social Awareness (social_awareness.py) and the bot event bus (bot_events.py)."""

import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parents[1] / "DarkAbyss_Core"
sys.path.insert(0, str(CORE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # test helpers of other test modules

import bot_events  # noqa: E402
import bot_i18n  # noqa: E402
import social_awareness as sa  # noqa: E402
import social_memory as sm  # noqa: E402

GUILD = 1
GAMES = 10
OTHER = 11
KAIRO = 900
ALICE, BOB, CARL = 101, 102, 103


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def result(content, status="COMPLETED"):
    return types.SimpleNamespace(status=types.SimpleNamespace(value=status), content=content)


class FakeOrchestrator:
    """Answers analysis requests (PLANNER) and compose requests (ROUTINE) from scripts."""

    def __init__(self, decisions=(), replies=()):
        self.decisions = list(decisions)
        self.replies = list(replies)
        self.requests = []

    async def orchestrate(self, request, **kwargs):
        self.requests.append((request, kwargs))
        if request.task_class.value == "PLANNER":
            item = self.decisions.pop(0) if self.decisions else {"decision": "ignore", "confidence": 0.9, "about": "", "intent": ""}
        else:
            item = self.replies.pop(0) if self.replies else "ok"
        if isinstance(item, Exception):
            raise item
        return result(item if isinstance(item, str) else json.dumps(item))

    def analyses(self):
        return [request for request, _ in self.requests if request.task_class.value == "PLANNER"]

    def composes(self):
        return [request for request, _ in self.requests if request.task_class.value == "ROUTINE"]


class Channel:
    def __init__(self, channel_id, parent_id=None):
        self.id = channel_id
        self.parent_id = parent_id


def message(message_id, author_id, text, channel=GAMES, name=None, bot=False, reply_to=None, guild=GUILD, mentions=(), manager=False):
    return types.SimpleNamespace(
        id=message_id,
        guild=types.SimpleNamespace(id=guild),
        channel=Channel(channel),
        author=types.SimpleNamespace(
            id=author_id,
            bot=bot,
            display_name=name or f"user{author_id}",
            guild_permissions=types.SimpleNamespace(manage_guild=manager, administrator=False, manage_messages=False, manage_channels=False),
        ),
        clean_content=text,
        content=text,
        reference=types.SimpleNamespace(message_id=reply_to) if reply_to else None,
        webhook_id=None,
        raw_mentions=list(mentions),
    )


class Harness:
    def __init__(
        self,
        config=None,
        decisions=(),
        replies=(),
        message_content=True,
        events=None,
        ai_request=None,
        knows=None,
        memory_path=None,
        instances=None,
        clock=None,
    ):
        self.clock = clock or Clock()
        self.orchestrator = FakeOrchestrator(decisions, replies)
        self.sent = []
        self.reacted = []
        self.statuses = []
        names = {ALICE: "Alice", BOB: "Bob", CARL: "Carl"}

        async def send(guild_id, channel_id, text, reply_to):
            self.sent.append((guild_id, channel_id, text, reply_to))
            return 5000 + len(self.sent)

        async def react(guild_id, channel_id, message_id, emoji):
            self.reacted.append((guild_id, channel_id, message_id, emoji))
            return True

        self.memory = sm.SocialMemory(memory_path, clock=self.clock) if memory_path is not None else None
        self.social = sa.SocialAwareness(
            get_orchestrator=lambda: self.orchestrator,
            send=send,
            react=react,
            memory=self.memory,
            instances=instances,
            own_instance_id="admin-main",
            clock=self.clock,
            events=events,
            is_ai_request=ai_request,
            resolve_name=lambda guild_id, user_id: names.get(user_id),
            describe_place=lambda guild_id, channel_id: ("Null", "#games"),
            knows_guild=knows,
            message_content=message_content,
            runtime_dir="runtime",
            write_status=lambda runtime, name, payload: self.statuses.append((name, payload)),
        )
        self.social.apply_config({"social_awareness_enabled": True, "language": "en", **(config or {})})
        self.social.set_identity(KAIRO, ["Kairo"])

    def say(self, *args, **kwargs):
        return self.social.observe_message(message(*args, **kwargs))

    async def run(self, seconds, step=5):
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.clock.advance(step)
            await self.social.tick()


def decision(kind, intent="", confidence=0.9, reply_to=None, wait=None, about="the message refers to the Group Up invite"):
    return {"decision": kind, "confidence": confidence, "about": about, "reply_to": reply_to, "intent": intent, "wait_seconds": wait}


class SettingsTests(unittest.TestCase):
    def test_never_chosen_is_off_and_old_configs_work(self):
        self.assertEqual((sa.Settings.from_config({}).enabled, sa.Settings.from_config({}).chosen), (False, False))
        self.assertEqual(sa.Settings.from_config({"social_awareness_enabled": None}).chosen, False)
        self.assertEqual((sa.Settings.from_config({"social_awareness_enabled": False}).enabled, sa.Settings.from_config({"social_awareness_enabled": False}).chosen), (False, True))
        on = sa.Settings.from_config({"social_awareness_enabled": True, "social_awareness_channel_ids": ["10"], "language": "ru"})
        self.assertEqual((on.enabled, on.chosen, on.channel_ids, on.language), (True, True, frozenset({10}), "ru"))
        self.assertTrue(on.watches(10) and on.watches(55, parent_id=10) and not on.watches(11))
        self.assertTrue(sa.Settings.from_config({"social_awareness_enabled": True}).watches(11))  # empty list = every channel

    def test_validation_fails_closed_in_the_bot_language(self):
        def ids(value, name):
            return [int(item) for item in value]

        config = {}
        sa.validate_config_fields(config, ids)
        self.assertEqual(
            config,
            {"social_awareness_enabled": None, "social_awareness_channel_ids": [], "social_awareness_replies_per_hour": 4, "social_awareness_lore_enabled": True},
        )
        for bad in ({"social_awareness_enabled": "yes"}, {"social_awareness_replies_per_hour": 0}, {"social_awareness_replies_per_hour": True}, {"social_awareness_lore_enabled": "no"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sa.validate_config_fields(dict(bad), ids)
        bot_i18n.set_bot_language("ru")
        self.addCleanup(bot_i18n.set_bot_language, "en")
        with self.assertRaisesRegex(ValueError, "должно быть"):
            sa.validate_config_fields({"social_awareness_enabled": "yes"}, ids)


class DecisionParsingTests(unittest.TestCase):
    def test_parse_decision(self):
        parsed = sa.parse_decision('```json\n{"decision": "wait", "confidence": 1.7, "reply_to": "m3", "intent": "x", "wait_seconds": 120}\n```')
        self.assertEqual((parsed.decision, parsed.confidence, parsed.reply_to_ref, parsed.wait_seconds), ("wait", 1.0, "m3", 120))
        self.assertIsNone(sa.parse_decision("I think Kairo should answer"))
        self.assertIsNone(sa.parse_decision('{"decision": "ban_everyone"}'))
        self.assertIsNone(sa.parse_decision('{"decision": "reply_now", "reply_to": "<@1>"}').reply_to_ref)

    def test_reply_text_never_pings_or_links(self):
        self.assertEqual(sa.sanitize_reply('Kairo: "Hi <@123> and <@&5>, see https://x.y"'), "Hi and , see")
        self.assertNotIn("@everyone", sa.sanitize_reply("@everyone go").replace("@​everyone", ""))
        self.assertIsNone(sa.sanitize_reply("   "))
        self.assertLessEqual(len(sa.sanitize_reply("a" * 900)), sa.MAX_REPLY_CHARS)


class SocialAwarenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordinary_chat_costs_nothing_and_kairo_stays_silent(self):
        h = Harness()
        for index in range(4):
            self.assertIsNone(h.say(index + 1, ALICE if index % 2 else BOB, f"just chatting {index}"))
        await h.run(120)
        self.assertEqual(h.orchestrator.requests, [])
        self.assertEqual(h.sent, [])

    async def test_named_without_mention_is_analysed_with_high_effort_and_ignore_means_silence(self):
        h = Harness(decisions=[decision("ignore", about="Alice talks about Kairo with Bob, not to it")])
        self.assertEqual(h.say(1, ALICE, "kairo is weird today lol"), "named")
        await h.run(3)
        self.assertEqual(h.orchestrator.requests, [])  # not instant: a short debounce first
        await h.run(10)
        [analysis] = h.orchestrator.analyses()
        self.assertEqual(analysis.reasoning_effort, "high")
        self.assertEqual(analysis.allowed_tool_names, ())
        self.assertEqual(analysis.response_language, "en")
        payload = json.loads(analysis.messages[1].content)
        self.assertEqual(payload["timeline_oldest_first"][-1]["text"], "kairo is weird today lol")
        self.assertEqual(analysis.messages[1].role.value, "user")  # member text is data, never the system prompt
        self.assertNotIn("weird", analysis.messages[0].content)
        self.assertEqual(h.sent, [])
        self.assertEqual(h.social.counters["ignored"], 1)

    async def test_reply_now_is_written_at_normal_effort_and_sent_without_mentions(self):
        h = Harness(
            config={"language": "ru"},
            decisions=[decision("reply_now", "Say hi back and ask what they play", reply_to="m1")],
            replies=["Привет, <@102>! Во что играете? @everyone"],
        )
        h.say(1, ALICE, "Кайро, привет")
        await h.run(15)
        [compose] = h.orchestrator.composes()
        self.assertIsNone(compose.reasoning_effort)  # the reply uses the profile's own effort
        self.assertEqual(compose.allowed_tool_names, ())
        self.assertEqual((compose.response_language, h.orchestrator.analyses()[0].response_language), ("ru", "ru"))
        [(guild, channel, text, reply_to)] = h.sent
        self.assertEqual((guild, channel, reply_to), (GUILD, GAMES, 1))
        self.assertNotIn("<@", text)
        self.assertNotIn("@everyone", text.replace("@​everyone", ""))

    async def test_low_confidence_reply_is_not_sent(self):
        h = Harness(decisions=[decision("reply_now", "say something", confidence=0.4)])
        h.say(1, ALICE, "kairo?")
        await h.run(20)
        self.assertEqual(h.sent, [])
        self.assertEqual(h.orchestrator.composes(), [])

    async def test_group_up_event_links_a_message_without_reply_or_mention(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        h = Harness(decisions=[decision("wait", "Tell Bob the others are waiting in Gaming", wait=120)])
        publisher = bot_events.EventPublisher("gp-main", "game_presence", Path(temp.name), clock=h.clock)
        h.social._events = bot_events.EventReader("admin-main", Path(temp.name), clock=h.clock)
        publisher.publish(
            "group_up.suggested",
            GUILD,
            channel_id=GAMES,
            message_id=777,
            data={"kind": "join", "game": "Overwatch 2", "invited_user_ids": [str(BOB)], "voice_player_ids": [str(ALICE)], "voice_crew_ids": [str(CARL)], "voice_channel_name": "Gaming"},
        )
        await h.run(5)
        # Bob answers in ANOTHER channel, no reply, no mention: still linked to the invite.
        self.assertEqual(h.say(2, BOB, "give me 5 min, finishing dinner", channel=OTHER), "after_event")
        await h.run(30)
        [analysis] = h.orchestrator.analyses()
        payload = json.loads(analysis.messages[1].content)
        texts = [entry["text"] for entry in payload["timeline_oldest_first"]]
        self.assertIn("Group Up invited Bob to join Alice in voice channel 'Gaming' to play Overwatch 2. It also told the others in that voice channel: Carl.", texts)
        self.assertEqual(h.sent, [])  # "wait": not now
        self.assertEqual(len(h.social.thoughts), 1)

    async def test_waiting_thought_is_dropped_when_the_context_changes(self):
        h = Harness(decisions=[decision("wait", "Ask whether they found a group", wait=120)])
        h.say(1, ALICE, "kairo, do you think anyone wants to play later?")
        await h.run(15)
        self.assertEqual(len(h.social.thoughts), 1)
        h.say(2, BOB, "yeah I'm in at 9")  # someone answered in the meantime
        self.assertEqual(h.social.thoughts, {})
        await h.run(200)
        self.assertEqual(h.sent, [])
        self.assertGreaterEqual(h.social.counters["dropped_stale"], 1)

    async def test_waiting_thought_is_said_later_when_nothing_changed(self):
        h = Harness(decisions=[decision("wait", "Check back whether they found a group", wait=90)], replies=["Did you find a group in the end?"])
        h.say(1, ALICE, "kairo, nobody wants to play...")
        await h.run(15)
        self.assertEqual(h.sent, [])
        await h.run(60)
        self.assertEqual(h.sent, [])  # not before the chosen time
        await h.run(40)
        self.assertEqual([text for _g, _c, text, _r in h.sent], ["Did you find a group in the end?"])

    async def test_normal_ai_requests_and_bots_never_trigger(self):
        h = Harness(ai_request=lambda msg: "@kairo" in msg.content, config={"ai_control_channel_id": OTHER})
        self.assertIsNone(h.say(1, ALICE, "@kairo kairo create a channel"))  # the normal AI flow answers it
        self.assertIsNone(h.say(2, ALICE, "kairo hello", channel=OTHER))  # control channel
        self.assertIsNone(h.say(3, 555, "kairo, Group Up here", bot=True))
        self.assertIsNone(h.say(4, KAIRO, "Kairo says hi"))
        await h.run(60)
        self.assertEqual(h.orchestrator.requests, [])

    async def test_replies_to_kairo_and_follow_ups_are_noticed(self):
        h = Harness(decisions=[decision("reply_now", "answer", reply_to="m1")], replies=["Sure!"])
        h.say(1, ALICE, "kairo can you hear me")
        await h.run(15)
        kairo_message = 5000 + len(h.sent)
        h.say(kairo_message, KAIRO, "Sure!")
        self.assertEqual(h.say(3, BOB, "it answered lol", reply_to=kairo_message), "reply_to_kairo")
        h.clock.advance(sa.REASON_GAP["continuation"] + 1)
        self.assertIn(h.say(4, ALICE, "nice, so what now"), ("continuation", "reply_to_kairo"))

    async def test_budgets_cap_replies_and_channel_gaps(self):
        h = Harness(config={"social_awareness_replies_per_hour": 1}, decisions=[decision("reply_now", "hi", reply_to="m1")] * 3, replies=["one", "two", "three"])
        h.say(1, ALICE, "kairo hi")
        await h.run(15)
        h.clock.advance(sa.REPLY_CHANNEL_GAP + 1)
        h.say(2, BOB, "kairo hello?")
        await h.run(15)
        self.assertEqual([text for _g, _c, text, _r in h.sent], ["one"])  # 1 per hour

    async def test_generic_bot_names_do_not_count_as_being_named(self):
        h = Harness()
        h.social.set_identity(KAIRO, ["Admin", "Kairo Prime"])
        self.assertIsNone(h.say(1, ALICE, "ask an admin about it"))
        self.assertEqual(h.say(2, BOB, "kairo prime, you there?"), "named")

    async def test_disabled_or_without_intent_reads_nothing_and_reports(self):
        h = Harness()
        h.say(1, BOB, "hello there")
        self.assertIn(GUILD, h.social.timelines)
        h.social.apply_config({"social_awareness_enabled": False})
        self.assertEqual(h.social.timelines, {})  # switched off: the conversation is forgotten at once
        self.assertIsNone(h.say(1, ALICE, "kairo hi"))
        self.assertEqual(h.social.timelines, {})  # privacy: nothing kept while off
        h = Harness(message_content=False)
        self.assertIsNone(h.say(1, ALICE, "kairo hi"))
        await h.social.tick()
        self.assertIn("Message Content Intent", h.social.status()["problem"])
        self.assertEqual(h.orchestrator.requests, [])

    async def test_ai_failure_backs_off_silently(self):
        h = Harness(decisions=[RuntimeError("provider down"), decision("reply_now", "x")])
        h.say(1, ALICE, "kairo?")
        await h.run(15)
        self.assertIn("analysis failed", h.social.status()["problem"])
        h.clock.advance(sa.REASON_GAP["named"] + 1)
        h.say(2, ALICE, "kairo??")
        await h.run(30)
        self.assertEqual(len(h.orchestrator.analyses()), 1)  # backing off
        self.assertEqual(h.sent, [])

    async def test_events_of_other_servers_are_ignored(self):
        h = Harness(knows=lambda guild_id: guild_id == GUILD)
        h.social.observe_event(bot_events.BotEvent("gp:1", h.clock(), "game_presence", "gp", "group_up.suggested", 999, data={}))
        self.assertNotIn(999, h.social.timelines)

    async def test_status_reports_counts_and_last_decision(self):
        h = Harness(decisions=[decision("ignore", about="small talk")])
        h.say(1, ALICE, "kairo lol")
        await h.run(40)
        name, status = h.statuses[-1]
        self.assertEqual(name, sa.STATUS_FILE_NAME)
        self.assertEqual((status["enabled"], status["active"], status["analyses_last_hour"]), (True, True, 1))
        self.assertEqual(status["last_decision"]["decision"], "ignore")


class BotEventsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()

    def test_round_trip_skips_own_file_and_sees_each_event_once(self):
        gp = bot_events.EventPublisher("gp-main", "game_presence", self.root, clock=self.clock)
        own = bot_events.EventPublisher("admin-main", "admin", self.root, clock=self.clock)
        reader = bot_events.EventReader("admin-main", self.root, clock=self.clock)
        gp.publish("group_up.suggested", 1, channel_id="10", message_id=777, data={"game": "OW", "invited_user_ids": ["5"]})
        own.publish("kairo.said", 1)
        [event] = reader.poll()
        self.assertEqual((event.kind, event.guild_id, event.channel_id, event.message_id, event.source_type), ("group_up.suggested", 1, 10, 777, "game_presence"))
        self.assertEqual(reader.poll(), [])
        self.clock.advance(1)
        gp.publish("group_up.suggested", 1)
        self.assertEqual(len(reader.poll()), 1)

    def test_files_are_bounded_validated_and_cannot_impersonate(self):
        gp = bot_events.EventPublisher("gp-main", "game_presence", self.root, clock=self.clock)
        for index in range(bot_events.MAX_EVENTS_PER_PRODUCER + 10):
            gp.publish("group_up.suggested", 1, data={"game": "x" * 500, "nested": {"a": 1}, "Bad Key": 1})
        events = bot_events.read_file(self.root / "gp-main.json")
        self.assertEqual(len(events), bot_events.MAX_EVENTS_PER_PRODUCER)
        self.assertLessEqual(len(events[0].data["game"]), bot_events.MAX_TEXT)
        self.assertNotIn("Bad Key", events[0].data)
        self.assertIsNone(events[0].data["nested"])
        # A file cannot speak for another instance, and junk is skipped.
        raw = json.loads((self.root / "gp-main.json").read_text(encoding="utf-8"))
        (self.root / "evil.json").write_text(json.dumps({**raw, "source_instance": "gp-main"}), encoding="utf-8")
        self.assertEqual(bot_events.read_file(self.root / "evil.json"), [])
        (self.root / "junk.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(bot_events.read_file(self.root / "junk.json"), [])
        with self.assertRaises(ValueError):
            bot_events.EventPublisher("../evil", "game_presence", self.root)
        self.assertIsNone(gp.publish("Bad Kind!", 1))
        self.assertIsNone(gp.publish("group_up.suggested", "not-an-id"))

    def test_old_events_expire(self):
        gp = bot_events.EventPublisher("gp-main", "game_presence", self.root, clock=self.clock)
        gp.publish("group_up.suggested", 1)
        self.clock.advance(bot_events.EVENT_TTL_SECONDS + 1)
        self.assertEqual(bot_events.EventReader("admin-main", self.root, clock=self.clock).poll(), [])
        gp.publish("group_up.suggested", 1)
        self.assertEqual(len(bot_events.read_file(self.root / "gp-main.json")), 1)  # the old one was dropped from the file


# --------------------------------------------------------------------------
# orchestrator: high reasoning effort for one request only
# --------------------------------------------------------------------------


class ReasoningEffortTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import ai_orchestrator
        from test_ai_orchestrator import FakeProvider

        ai_platform = ai_orchestrator.ai_platform  # other tests may have re-imported ai_platform

        self.ai_orchestrator, self.ai_platform = ai_orchestrator, ai_platform
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.providers = {name: FakeProvider(ai_platform, provider_id=name) for name in ("groq", "fake")}
        store = ai_platform.AISettingsStore(Path(temp.name) / "ai.json")
        store.save(
            ai_platform.AISettings(
                profiles=(
                    ai_platform.AIProfile("groq-p", "groq", "fake-model", options={"reasoning_effort": "medium"}),
                    ai_platform.AIProfile("fake-p", "fake", "fake-model", options={"reasoning_effort": "low"}),
                ),
                routing=ai_platform.RoutingConfig(routine_profile_id="groq-p", planner_profile_id="groq-p", creative_profile_id="fake-p"),
            )
        )
        self.orchestrator = ai_orchestrator.AIOrchestrator(
            settings_store=store,
            provider_registry=ai_platform.ProviderRegistry(self.providers),
            credential_store=ai_platform.CredentialStore(Path(temp.name) / "secrets"),
        )

    async def ask(self, task, effort=None):
        for provider in self.providers.values():
            provider.responses = [self.ai_platform.AIResponse(content="ok")]
        request = self.ai_orchestrator.OrchestratorRequest(
            messages=(self.ai_platform.AIMessage(role=self.ai_platform.MessageRole.USER, content="hi"),),
            task_class=task,
            allowed_tool_names=(),
            reasoning_effort=effort,
        )
        result = await self.orchestrator.orchestrate(request)
        self.assertEqual(result.status.value, "COMPLETED")
        provider = self.providers["fake" if task == "CREATIVE" else "groq"]
        return dict(provider.requests[-1][0].options)

    async def test_override_applies_only_to_that_request_and_supported_providers(self):
        self.assertEqual((await self.ask("PLANNER", "high"))["reasoning_effort"], "high")
        self.assertEqual((await self.ask("PLANNER"))["reasoning_effort"], "medium")  # the profile itself is unchanged
        self.assertEqual((await self.ask("ROUTINE"))["reasoning_effort"], "medium")
        # A provider the catalog does not know keeps its own options.
        self.assertEqual((await self.ask("CREATIVE", "high"))["reasoning_effort"], "low")
        with self.assertRaises(ValueError):
            self.ai_orchestrator.OrchestratorRequest(
                messages=(self.ai_platform.AIMessage(role=self.ai_platform.MessageRole.USER, content="hi"),), task_class="PLANNER", reasoning_effort="max"
            )


# --------------------------------------------------------------------------
# Admin (Kairo) wiring
# --------------------------------------------------------------------------


def import_admin():
    import importlib

    try:
        import msvcrt  # noqa: F401
    except ImportError:
        sys.modules["msvcrt"] = types.SimpleNamespace(LK_NBLCK=2, locking=lambda *args: None)
    return importlib.import_module("Admin")


class AdminWiringTests(unittest.IsolatedAsyncioTestCase):
    def base(self, **extra):
        return {"allow_server_administrators": True, "allowed_user_ids": [], "allowed_role_ids": [], "audit_channel_id": None, **extra}

    def test_old_configs_validate_and_social_awareness_needs_message_content(self):
        admin = import_admin()
        validated = admin.validate_config(self.base())
        self.assertEqual(
            (validated["social_awareness_enabled"], validated["social_awareness_channel_ids"], validated["social_awareness_replies_per_hour"]),
            (None, [], 4),
        )
        self.assertFalse(admin.message_content_requested(validated))
        self.assertTrue(admin.message_content_requested(admin.validate_config(self.base(social_awareness_enabled=True))))
        self.assertFalse(admin.message_content_requested(admin.validate_config(self.base(social_awareness_enabled=False))))
        with self.assertRaises(ValueError):
            admin.validate_config(self.base(social_awareness_channel_ids=["general"]))
        self.assertIn("Social Awareness", admin.PRIVILEGED_INTENTS_HELP)

    def test_live_refresh_applies_language_and_fails_closed(self):
        admin = import_admin()
        h = Harness()
        original_social, original_load = admin.social, admin.load_config
        self.addCleanup(lambda: (setattr(admin, "social", original_social), setattr(admin, "load_config", original_load), bot_i18n.set_bot_language("en")))
        admin.social = h.social

        def load(runtime=None):
            config = admin.validate_config(self.base(language="ru", social_awareness_enabled=True))
            bot_i18n.set_bot_language(config["language"])
            return config

        admin.load_config = load
        admin.refresh_live_config()
        self.assertEqual(bot_i18n.bot_language(), "ru")
        self.assertTrue(h.social.settings.enabled)

        def broken(runtime=None):
            raise RuntimeError("Invalid admin config")

        admin.load_config = broken
        admin.refresh_live_config()
        self.assertFalse(h.social.settings.enabled)  # an unreadable config switches it off
        self.assertEqual(bot_i18n.bot_language(), "ru")  # the language stays as it was

    async def test_social_messages_never_ping_and_respect_permissions(self):
        import discord
        from unittest import mock

        admin = import_admin()
        sent = []

        class Perms:
            view_channel = True
            send_messages = True
            send_messages_in_threads = True

        class TextChannel:
            id = GAMES
            name = "games"

            def permissions_for(self, member):
                return self.perms

            async def send(self, content, **kwargs):
                sent.append((content, kwargs))
                return types.SimpleNamespace(id=4242)

        channel = TextChannel()
        channel.perms = Perms()
        guild = types.SimpleNamespace(id=GUILD, name="Null", me=object(), get_channel_or_thread=lambda channel_id: channel if channel_id == GAMES else None)
        with mock.patch.object(admin.bot, "get_guild", lambda guild_id: guild if guild_id == GUILD else None):
            self.assertEqual(await admin.social_send(GUILD, GAMES, "hello", 77), 4242)
            content, kwargs = sent[0]
            mentions = kwargs["allowed_mentions"]
            self.assertEqual((mentions.everyone, mentions.users, mentions.roles, mentions.replied_user), (False, False, False, False))
            self.assertEqual((kwargs["reference"].message_id, kwargs["mention_author"]), (77, False))
            self.assertIsInstance(kwargs["reference"], discord.MessageReference)
            channel.perms.send_messages = False
            self.assertIsNone(await admin.social_send(GUILD, GAMES, "hello", None))
            self.assertIsNone(await admin.social_send(GUILD, 999, "hello", None))
            self.assertEqual(admin._place(GUILD, GAMES), ("Null", "#games"))

    def test_normal_ai_requests_are_left_to_the_normal_flow(self):
        admin = import_admin()
        transport = admin.ai_transport
        self.assertIsNotNone(transport)
        original = (transport.control_channel_id, transport.mention_enabled)
        self.addCleanup(lambda: (setattr(transport, "control_channel_id", original[0]), setattr(transport, "mention_enabled", original[1])))
        transport.control_channel_id = OTHER
        transport.mention_enabled = False
        self.assertTrue(admin.is_normal_ai_request(message(1, ALICE, "hi", channel=OTHER)))
        self.assertFalse(admin.is_normal_ai_request(message(1, ALICE, "hi", channel=GAMES)))

    def test_main_builds_social_awareness_with_the_message_content_decision(self):
        admin = import_admin()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = types.SimpleNamespace(instance_id="admin-main", config_path=Path("x"), token_path=Path("y"), lock_path=Path(temp.name) / "admin_bot.lock", data_dir=None)
        seen = {}

        def fake_run(token):
            seen["message_content"] = admin.bot._connection._intents.message_content
            seen["social"] = admin.social

        patches = {
            "resolve_runtime": lambda instance_id: runtime,
            "load_config": lambda runtime=None: admin.validate_config(self.base(social_awareness_enabled=True)),
            "load_token": lambda runtime=None: "not-a-real-token",
            "acquire_single_instance_lock": lambda runtime=None: True,
            "resolve_ai_stores": lambda runtime=None: None,
        }
        originals = {name: getattr(admin, name) for name in patches}
        original_run, original_social = admin.bot.run, admin.social
        for name, value in patches.items():
            setattr(admin, name, value)
        admin.bot.run = fake_run
        try:
            self.assertEqual(admin.main(["--instance", "admin-main"]), 0)
        finally:
            for name, value in originals.items():
                setattr(admin, name, value)
            admin.bot.run = original_run
            admin.social = original_social
            admin.configure_message_content_intent(admin.bot, False)
        self.assertTrue(seen["message_content"])
        self.assertTrue(seen["social"].active)
        self.assertEqual(seen["social"].settings.replies_per_hour, 4)


# --------------------------------------------------------------------------
# reactions, quiet wishes, feedback, Server Lore, the DarkAbyss bots
# --------------------------------------------------------------------------


class MemoryHarnessCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.memory_path = Path(self.temp.name) / sm.FILE_NAME

    def harness(self, **kwargs):
        return Harness(memory_path=self.memory_path, **kwargs)


def react_decision(ref, emoji="😂", confidence=0.9):
    return {"decision": "react", "confidence": confidence, "about": "a joke", "reply_to": ref, "emoji": emoji, "intent": ""}


class ReactionTests(MemoryHarnessCase):
    async def test_a_reaction_instead_of_words(self):
        h = self.harness(decisions=[react_decision("m1")])
        h.say(1, ALICE, "kairo, I just respawned inside a wall again")
        await h.run(15)
        self.assertEqual(h.reacted, [(GUILD, GAMES, 1, "😂")])
        self.assertEqual((h.sent, h.orchestrator.composes()), ([], []))  # no text, no second AI call
        self.assertEqual(h.social.counters["reacted"], 1)
        self.assertIn("you_may_react_with", json.loads(h.orchestrator.analyses()[0].messages[1].content))

    async def test_reactions_are_never_random_spam(self):
        # Not an allowed emoji / a bot's message / the same message twice: nothing.
        h = self.harness(decisions=[react_decision("m2", emoji="🍆"), react_decision("m1"), react_decision("m2"), react_decision("m2")])
        h.say(1, 555, "Group Up: you two, play together!", bot=True)
        h.say(2, ALICE, "kairo lol")
        await h.run(15)
        self.assertEqual(h.reacted, [])
        h.clock.advance(sa.REASON_GAP["named"] + 1)
        h.say(3, ALICE, "kairo??")
        await h.run(15)
        self.assertEqual(h.reacted, [])  # m1 is the bot message: never reacted to
        h.clock.advance(sa.REACT_CHANNEL_GAP + sa.REASON_GAP["named"])
        h.say(4, BOB, "kairo, that was funny")
        await h.run(15)
        self.assertEqual(len(h.reacted), 1)
        h.clock.advance(sa.REACT_CHANNEL_GAP + sa.REASON_GAP["named"])
        h.say(5, BOB, "kairo, again")
        await h.run(15)
        self.assertEqual(len(h.reacted), 1)  # the same message is never reacted to twice

    async def test_people_joining_a_reaction_is_positive_feedback(self):
        h = self.harness(decisions=[react_decision("m1")])
        h.say(1, ALICE, "kairo, we finally won a match")
        await h.run(15)
        h.social.observe_reaction(GUILD, GAMES, 1, BOB, "😂")
        await h.run(sa.FEEDBACK_WINDOW + 10)
        self.assertEqual([outcome.result for outcome in h.memory.guild(GUILD).outcomes], ["positive"])


class QuietWishTests(MemoryHarnessCase):
    async def test_be_quiet_is_kept_acknowledged_survives_restart_and_can_be_lifted(self):
        h = self.harness()
        self.assertEqual(h.say(1, ALICE, "Кайро, помолчи"), "quiet")
        await h.run(5)
        self.assertEqual(h.reacted, [(GUILD, GAMES, 1, "🤐")])  # a nod, not a message
        self.assertIsNone(h.say(2, ALICE, "kairo?"))
        await h.run(60)
        self.assertEqual(h.orchestrator.requests, [])
        # A restarted bot (same memory file) still keeps quiet.
        again = Harness(memory_path=self.memory_path, clock=h.clock)
        self.assertIsNone(again.say(3, BOB, "kairo, are you there?"))
        # Someone else cannot lift Alice's wish; Alice (or a server manager) can.
        self.assertEqual(again.say(4, BOB, "кайро, можешь снова говорить"), "resume")
        self.assertIsNone(again.say(5, BOB, "kairo?"))
        self.assertEqual(again.say(6, ALICE, "Кайро, можешь снова говорить"), "resume")
        await again.run(5)
        self.assertEqual(again.reacted, [(GUILD, GAMES, 6, "👌")])
        self.assertEqual(again.say(7, BOB, "kairo?"), "named")

    async def test_quiet_for_a_while_ends_by_itself(self):
        h = self.harness()
        h.say(1, ALICE, "кайро, заткнись на 10 минут")
        self.assertIsNone(h.say(2, BOB, "kairo?"))
        h.clock.advance(11 * 60)
        self.assertEqual(h.say(3, BOB, "kairo?"), "named")

    async def test_a_person_can_ask_not_to_be_answered_and_others_still_can_talk(self):
        h = self.harness()
        self.assertEqual(h.say(1, ALICE, "kairo, don't reply to me"), "quiet")
        self.assertIsNone(h.say(2, ALICE, "kairo what do you think?"))
        self.assertEqual(h.say(3, BOB, "kairo what do you think?"), "named")
        h.social.pending.clear()
        self.assertEqual(h.say(4, BOB, "kairo you can talk to me again"), "resume")
        self.assertIsNone(h.say(5, ALICE, "kairo hm?"))  # Bob cannot lift Alice's wish
        manager = message(6, CARL, "kairo be quiet everywhere", manager=True)
        self.assertEqual(h.social.observe_message(manager), "quiet")
        self.assertIsNone(h.say(7, BOB, "kairo?", channel=OTHER))  # the whole server

    async def test_right_after_kairo_spoke_a_bare_shut_up_is_for_kairo(self):
        h = self.harness(decisions=[decision("reply_now", "chime in", reply_to="m1")], replies=["Overwatch? Classic."])
        self.assertIsNone(h.say(1, BOB, "заткнись, Боб"))  # between members, Kairo has not said anything: not a wish
        h.say(2, ALICE, "kairo, guess what we play")
        await h.run(15)
        self.assertEqual(h.say(3, BOB, "не лезь сюда"), "quiet")
        self.assertIsNotNone(h.memory.quiet_for(GUILD, GAMES))
        self.assertEqual([outcome.result for outcome in h.memory.guild(GUILD).outcomes], ["negative"])  # and it counts as feedback
        self.assertIsNone(h.say(4, CARL, "заткнись на час", mentions=[BOB]))  # addressed to someone else

    async def test_a_repeated_wish_gets_one_nod(self):
        h = self.harness()
        h.say(1, ALICE, "kairo shut up")
        h.say(2, ALICE, "kairo SHUT UP")
        await h.run(5)
        self.assertEqual(len(h.reacted), 1)

    async def test_quiet_wishes_never_switch_off_the_normal_ai_flow(self):
        h = self.harness(ai_request=lambda msg: msg.content.startswith("@kairo"))
        self.assertEqual(h.say(1, ALICE, "@kairo помолчи"), "quiet")
        await h.run(5)
        self.assertEqual(h.reacted, [])  # the normal AI flow answers that message itself
        self.assertIsNone(h.say(2, ALICE, "@kairo создай канал"))  # left to the normal flow, as always
        self.assertIsNotNone(h.memory.quiet_for(GUILD, GAMES))

    async def test_unreadable_memory_means_silence(self):
        self.memory_path.write_text("{broken", encoding="utf-8")
        h = self.harness()
        self.assertIsNone(h.say(1, ALICE, "kairo, hello"))
        await h.run(30)
        self.assertEqual(h.orchestrator.requests, [])
        self.assertIn("unreadable", h.social.status()["problem"])


class FeedbackTests(MemoryHarnessCase):
    async def reply_once(self, h, message_id, text="kairo?"):
        h.say(message_id, ALICE, text)
        await h.run(15)
        kairo_id = 5000 + len(h.sent)
        h.say(kairo_id, KAIRO, h.sent[-1][2])
        return kairo_id

    async def test_engaged_and_ignored_outcomes(self):
        h = self.harness(decisions=[decision("reply_now", "answer", reply_to="m1")] * 2, replies=["Here!", "Still here"])
        kairo_id = await self.reply_once(h, 1)
        h.say(20, BOB, "haha it answered", reply_to=kairo_id)
        await h.run(sa.FEEDBACK_WINDOW + 10)
        h.clock.advance(sa.REPLY_CHANNEL_GAP)
        await self.reply_once(h, 30, "kairo, again?")
        for index in range(sa.IGNORED_AFTER_MESSAGES):
            h.say(40 + index, BOB, f"anyway, back to the match {index}")
        await h.run(sa.FEEDBACK_WINDOW + 10)
        self.assertEqual([outcome.result for outcome in h.memory.guild(GUILD).outcomes], ["engaged", "ignored"])

    async def test_a_bad_joke_raises_the_bar_twice_makes_kairo_step_back(self):
        h = self.harness(decisions=[decision("reply_now", "answer", reply_to="m1")] * 3, replies=["one", "two", "three"])
        kairo_id = await self.reply_once(h, 1)
        h.social.observe_reaction(GUILD, GAMES, kairo_id, BOB, "👎")
        self.assertEqual([outcome.result for outcome in h.memory.guild(GUILD).outcomes], ["negative"])
        self.assertIsNone(h.memory.quiet_for(GUILD, GAMES))  # one bad joke is not a reason to go silent
        self.assertGreater(h.social.min_confidence(GUILD, GAMES), sa.MIN_CONFIDENCE)
        h.clock.advance(sa.REPLY_CHANNEL_GAP)
        kairo_id = await self.reply_once(h, 10, "kairo, and now?")
        h.say(11, BOB, "тебя никто не спрашивал", reply_to=kairo_id)
        mute = h.memory.quiet_for(GUILD, GAMES)
        self.assertIsNotNone(mute)
        self.assertEqual(mute.reason, "auto")
        self.assertLessEqual(mute.until - h.clock.now, sa.AUTO_QUIET_SECONDS + 1)
        self.assertIsNone(h.say(12, ALICE, "kairo?"))
        # An automatic step back may be lifted by anyone who asks Kairo back.
        self.assertEqual(h.say(13, CARL, "kairo you can talk again"), "resume")
        self.assertIsNone(h.memory.quiet_for(GUILD, GAMES))

    async def test_bad_feedback_blocks_a_marginal_reply_but_not_a_clear_one(self):
        h = self.harness(decisions=[decision("reply_now", "answer", reply_to="m1", confidence=0.7), decision("reply_now", "answer", reply_to="m1", confidence=0.95)], replies=["clear"])
        for index in range(3):
            h.memory.record_outcome(GUILD, sm.Outcome(h.clock.now - 7200 - index, GAMES, "reply", "named", "negative"))
        self.assertGreater(h.social.min_confidence(GUILD, GAMES), 0.7)
        h.say(1, ALICE, "kairo?")
        await h.run(15)
        self.assertEqual(h.sent, [])
        h.clock.advance(sa.REASON_GAP["named"] + 1)
        h.say(2, ALICE, "kairo, really, answer")
        await h.run(15)
        self.assertEqual([text for _g, _c, text, _r in h.sent], ["clear"])

    async def test_the_analysis_may_decide_to_stay_out_for_a_while(self):
        h = self.harness(decisions=[{**decision("ignore"), "quiet_minutes": 30}])
        h.say(1, ALICE, "kairo again with the comments...")
        await h.run(15)
        mute = h.memory.quiet_for(GUILD, GAMES)
        self.assertEqual(mute.reason, "auto")
        self.assertAlmostEqual(mute.until - h.clock.now, 30 * 60, delta=20)


class LoreThroughAnalysisTests(MemoryHarnessCase):
    LORE = {"op": "remember", "kind": "meme", "text": "Bob's 'banana aim' is the server's running joke about his sniping"}

    async def test_lore_is_learned_only_when_seen_twice_and_then_used(self):
        h = self.harness(decisions=[{**decision("ignore"), "lore": [self.LORE, self.LORE, self.LORE]}, {**decision("ignore"), "lore": [self.LORE]}, decision("ignore")])
        h.say(1, ALICE, "kairo, Bob missed again, classic banana aim")
        await h.run(15)
        [entry] = h.memory.guild(GUILD).lore
        self.assertEqual(entry.status, "candidate")  # one analysis = one sighting, however often repeated
        self.assertEqual(json.loads(h.orchestrator.analyses()[0].messages[1].content)["server_lore"], [])
        h.clock.advance(sm.CONFIRM_GAP + 1)
        h.say(2, CARL, "kairo, banana aim strikes again")
        await h.run(15)
        self.assertEqual(h.memory.guild(GUILD).lore[0].status, "active")
        h.clock.advance(sa.REASON_GAP["named"] + 1)
        h.say(3, ALICE, "kairo, guess who missed")
        await h.run(15)
        lore = json.loads(h.orchestrator.analyses()[-1].messages[1].content)["server_lore"]
        self.assertEqual(lore, [{"ref": "L1", "kind": "meme", "text": self.LORE["text"]}])

    async def test_forgetting_and_switching_lore_off(self):
        h = self.harness(decisions=[{**decision("ignore"), "lore": [{"op": "forget", "ref": "L1"}]}])
        h.memory.propose(GUILD, "remember", kind="nickname", text="Members call Alice 'the Captain' in raids")
        h.clock.advance(sm.CONFIRM_GAP + 1)
        h.memory.propose(GUILD, "remember", kind="nickname", text="Members call Alice 'the Captain' in raids")
        h.say(1, ALICE, "kairo, nobody calls me captain anymore")
        await h.run(15)
        self.assertEqual(h.memory.guild(GUILD).lore, [])
        h = self.harness(config={"social_awareness_lore_enabled": False}, decisions=[{**decision("ignore"), "lore": [self.LORE]}])
        h.say(1, ALICE, "kairo, banana aim")
        await h.run(15)
        self.assertNotIn("server_lore", json.loads(h.orchestrator.analyses()[0].messages[1].content))
        self.assertEqual(h.memory.guild(GUILD).lore, [])


class DarkAbyssSystemTests(MemoryHarnessCase):
    async def test_kairo_knows_the_other_bots_and_links_talk_about_them(self):
        events_dir = Path(self.temp.name) / "events"
        clock = Clock()
        gp = bot_events.EventPublisher("gp-main", "game_presence", events_dir, clock=clock)
        gp.heartbeat(display_name="Games A", discord_user_id=555, discord_name="GroupUpBot", guild_ids=[GUILD])
        gp.publish(
            "group_up.suggested",
            GUILD,
            channel_id=GAMES,
            message_id=700,
            data={"kind": "join", "game": "Overwatch 2", "invited_user_ids": [str(BOB)], "voice_player_ids": [str(ALICE)], "voice_channel_name": "Gaming"},
        )
        instances = lambda: [("gp-main", "game_presence", "Games A"), ("sd-main", "stream_director", "Stream X"), ("admin-main", "admin", "Kairo")]
        h = self.harness(events=bot_events.EventReader("admin-main", events_dir, clock=clock), instances=instances, clock=clock)
        await h.run(5)
        h.say(700, 555, "@Bob, @Alice is already playing Overwatch 2 in Gaming. Jump in!", bot=True, name="GroupUpBot")
        self.assertIn("[DarkAbyss Game Presence", h.social.timeline(GUILD).items[-1].author_name)
        h.clock.advance(20 * 60)  # long after the post: no "after_event" any more
        gp.heartbeat(display_name="Games A", discord_user_id=555, discord_name="GroupUpBot", guild_ids=[GUILD], force=True)  # once a minute in real life
        self.assertEqual(h.say(701, CARL, "Group Up опять охуел", channel=OTHER), "about_bot")
        await h.run(30)
        context = json.loads(h.orchestrator.analyses()[0].messages[1].content)
        bots = {entry["name"]: entry for entry in context["darkabyss_bots"]}
        self.assertEqual(set(bots), {"Games A", "Stream X"})  # Kairo itself is not listed
        self.assertEqual((bots["Games A"]["status"], bots["Games A"]["in_this_server"]), ("running", True))
        self.assertIn("Group Up invited Bob to join Alice", bots["Games A"]["recent_actions_here"][0])
        self.assertEqual(bots["Stream X"]["status"], "unknown")

    async def test_the_phrase_alone_is_not_about_a_bot(self):
        h = self.harness()
        self.assertIsNone(h.say(1, ALICE, "let's group up after dinner"))


class AdminSocialLifeWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_reactions_need_permissions_and_the_listener_keeps_normal_ai(self):
        from unittest import mock

        admin = import_admin()
        added = []

        class Partial:
            def __init__(self, message_id):
                self.message_id = message_id

            async def add_reaction(self, emoji):
                added.append((self.message_id, emoji))

        class Perms:
            view_channel = True
            add_reactions = True
            read_message_history = True

        class TextChannel:
            id = GAMES
            perms = Perms()

            def permissions_for(self, member):
                return self.perms

            def get_partial_message(self, message_id):
                return Partial(message_id)

        channel = TextChannel()
        guild = types.SimpleNamespace(id=GUILD, me=object(), get_channel_or_thread=lambda channel_id: channel if channel_id == GAMES else None)
        with mock.patch.object(admin.bot, "get_guild", lambda guild_id: guild if guild_id == GUILD else None):
            self.assertTrue(await admin.social_react(GUILD, GAMES, 77, "😂"))
            channel.perms.add_reactions = False
            self.assertFalse(await admin.social_react(GUILD, GAMES, 78, "😂"))
        self.assertEqual(added, [(77, "😂")])

        # Even with Social Awareness told to be quiet, the listener still hands messages to the normal AI flow.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        h = Harness(memory_path=Path(temp.name) / sm.FILE_NAME)
        h.say(1, ALICE, "кайро помолчи везде")
        original_social, transport = admin.social, admin.ai_transport
        saved = (transport.control_channel_id, transport.mention_enabled, transport.handle_control_message, transport.handle_mention_message)
        self.addCleanup(lambda: (setattr(admin, "social", original_social), setattr(transport, "control_channel_id", saved[0]), setattr(transport, "mention_enabled", saved[1]), setattr(transport, "handle_control_message", saved[2]), setattr(transport, "handle_mention_message", saved[3])))
        admin.social = h.social
        transport.control_channel_id, transport.mention_enabled = GAMES, True
        transport.handle_control_message = mock.AsyncMock()
        transport.handle_mention_message = mock.AsyncMock()
        await admin.ai_control_channel_listener(message(2, ALICE, "@kairo create a channel"))
        transport.handle_control_message.assert_awaited_once()
        transport.handle_mention_message.assert_awaited_once()

    def test_heartbeat_describes_this_kairo(self):
        admin = import_admin()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        original = (admin.events_publisher, admin.instance_display_name)
        self.addCleanup(lambda: (setattr(admin, "events_publisher", original[0]), setattr(admin, "instance_display_name", original[1])))
        admin.events_publisher = bot_events.EventPublisher("kairo-a", "admin", Path(temp.name))
        admin.instance_display_name = "Kairo A"
        admin.send_heartbeat(force=True)
        info = bot_events.read_bot(Path(temp.name) / "kairo-a.json")
        self.assertEqual((info.bot_type, info.display_name, info.stopped), ("admin", "Kairo A", False))


if __name__ == "__main__":
    unittest.main()
