"""Kairo's content filter (content_filter.py), its AI tools (admin_tools_filter.py) and /filter."""

import asyncio
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parents[1] / "DarkAbyss_Core"
sys.path.insert(0, str(CORE_ROOT))

import bot_i18n  # noqa: E402
import content_filter as cf  # noqa: E402

GUILD = 1
OTHER_GUILD = 2
CHANNEL = 10
OWNER, VASYA, PETYA, ADMIN = 100, 101, 102, 103


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def verdict(category="insult", joking=False, severity=2, confidence=0.85, target="person", reason="calls him an idiot"):
    return {"category": category, "target": target, "joking": joking, "severity": severity, "confidence": confidence, "reason": reason}


class FakeOrchestrator:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    async def orchestrate(self, request, **kwargs):
        self.requests.append(request)
        answer = self.answers.pop(0) if self.answers else verdict("none")
        return types.SimpleNamespace(status=types.SimpleNamespace(value="COMPLETED"), content=json.dumps(answer))


def member(user_id, name, *, admin=False, top=1, bot=False):
    return types.SimpleNamespace(
        id=user_id,
        display_name=name,
        name=name,
        mention=f"<@{user_id}>",
        bot=bot,
        guild_permissions=types.SimpleNamespace(administrator=admin, moderate_members=True),
        top_role=types.SimpleNamespace(position=top),
        timed_out_until=None,
    )


def guild(guild_id=GUILD, moderate=True):
    return types.SimpleNamespace(
        id=guild_id,
        owner_id=OWNER,
        me=types.SimpleNamespace(id=900, guild_permissions=types.SimpleNamespace(moderate_members=moderate), top_role=types.SimpleNamespace(position=10)),
    )


def message(message_id, author, text, server=None, channel=CHANNEL, reply_to=None):
    return types.SimpleNamespace(
        id=message_id,
        guild=server or guild(),
        channel=types.SimpleNamespace(id=channel),
        author=author,
        clean_content=text,
        content=text,
        reference=types.SimpleNamespace(resolved=reply_to) if reply_to is not None else None,
        webhook_id=None,
    )


class Harness:
    def __init__(self, test, *answers, config=None, orchestrator=True, message_content=True):
        temp = tempfile.TemporaryDirectory()
        test.addCleanup(temp.cleanup)
        self.clock = Clock()
        self.store = cf.FilterStore(Path(temp.name) / cf.FILE_NAME, clock=self.clock)
        self.orchestrator = FakeOrchestrator(*answers) if orchestrator else None
        self.timeouts, self.replies, self.audits = [], [], []

        async def timeout(target, minutes, reason):
            self.timeouts.append((target.id, minutes, reason))

        async def reply(original, text, target):
            self.replies.append((original.id, text, target.id))

        async def audit(server, text):
            self.audits.append(text)

        self.filter = cf.ContentFilter(
            store=self.store,
            get_orchestrator=lambda: self.orchestrator,
            timeout=timeout,
            reply=reply,
            audit=audit,
            clock=self.clock,
            message_content=message_content,
            choose=lambda pool: pool[0],
        )
        self.filter.apply_config({"language": "ru", **(config or {})})
        self.vasya = member(VASYA, "Вася")
        self.petya = member(PETYA, "Петя")

    def say(self, message_id, author, text, **kwargs):
        return asyncio.run(self.filter.observe(message(message_id, author, text, **kwargs)))


class PolicyTests(unittest.TestCase):
    settings = cf.Settings()

    def minutes(self, previous=0, **kwargs):
        return cf.mute_minutes(cf.parse_verdict(json.dumps(verdict(**kwargs))), self.settings, previous)

    def test_banter_swearing_and_doubt_are_not_punished(self):
        self.assertIsNone(self.minutes(joking=True))  # friendly banter
        self.assertIsNone(self.minutes(category="profanity", target="game"))  # "бля, опять слили"
        self.assertIsNone(self.minutes(category="none"))
        self.assertIsNone(self.minutes(confidence=0.6))  # not sure enough for a judged category
        self.assertIsNone(self.minutes(target="self"))  # "я дебил"

    def test_zero_tolerance_kinds_are_punished_even_as_a_joke(self):
        self.assertEqual(self.minutes(category="threat", joking=True, severity=1), cf.IMMEDIATE_MIN_MINUTES)
        self.assertEqual(self.minutes(category="hate", severity=5), 180)
        self.assertIsNone(self.minutes(category="hate", confidence=0.5))
        lenient = cf.Settings(immediate=frozenset({"hate"}))
        family_joke = cf.parse_verdict(json.dumps(verdict(category="family", joking=True)))
        self.assertIsNone(cf.mute_minutes(family_joke, lenient))  # not zero tolerance there: a joke is a joke

    def test_duration_is_30_to_180_minutes_by_severity_and_repeats(self):
        self.assertEqual([self.minutes(severity=level) for level in range(1, 6)], [30, 45, 60, 120, 180])
        self.assertEqual(self.minutes(severity=1, previous=1), 60)
        self.assertEqual(self.minutes(severity=4, previous=3), 180)  # never more than 3 hours

    def test_durations_and_messages_in_both_languages(self):
        self.assertEqual([cf.format_duration(minutes, "ru") for minutes in (30, 45, 60, 90, 120, 180)], ["30 минут", "45 минут", "1 час", "1 час 30 минут", "2 часа", "3 часа"])
        self.assertEqual([cf.format_duration(minutes, "en") for minutes in (30, 60, 90, 180)], ["30 minutes", "1 hour", "1 hour 30 minutes", "3 hours"])
        first = lambda pool: pool[0]  # noqa: E731
        self.assertEqual(cf.punishment_text("ru", "<@7>", 30, "insult", first), "Фу, как некультурно. <@7>, у тебя 30 минут мута, мыло дать?")
        self.assertEqual(cf.punishment_text("en", "<@7>", 60, "insult", first), "Ew, how rude. <@7>, that's 1 hour of mute. Need some soap?")
        self.assertEqual(cf.punishment_text("ru", "<@7>", 120, "threat", first), "<@7>, это уже перебор. Мут на 2 часа.")
        for pool in (*cf.TEMPLATES.values(), *cf.SEVERE_TEMPLATES.values()):
            for template in pool:
                self.assertIn("{user}", template)
                self.assertIn("{duration}", template)

    def test_local_hints(self):
        self.assertEqual(cf.local_signals("ты дебил"), ["insult"])
        self.assertEqual(cf.local_signals("иди повесься"), ["threat"])
        self.assertEqual(cf.local_signals("бля, опять слили катку"), [])
        self.assertEqual(cf.local_signals("хачапури и жидкий азот"), [])
        self.assertEqual(cf.local_signals("kys lol"), ["threat"])

    def test_verdict_parsing(self):
        parsed = cf.parse_verdict('```json\n{"category": "insult", "target": "person", "joking": false, "severity": 9, "confidence": 2}\n```')
        self.assertEqual((parsed.category, parsed.severity, parsed.confidence), ("insult", 5, 1.0))
        self.assertIsNone(cf.parse_verdict('{"category": "ban him"}'))
        self.assertIsNone(cf.parse_verdict("not json"))

    def test_config_validation(self):
        config = {}
        cf.validate_config_fields(config)
        self.assertEqual(config, {"content_filter_enabled": True, "content_filter_immediate": ["hate", "threat", "harassment", "family"]})
        for bad in ({"content_filter_enabled": "yes"}, {"content_filter_immediate": ["everything"]}, {"content_filter_immediate": "hate"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cf.validate_config_fields(dict(bad))


class StoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / cf.FILE_NAME
        self.store = cf.FilterStore(self.path)

    def test_list_is_per_server_and_round_trips(self):
        self.assertTrue(self.store.watch(GUILD, VASYA, name="Вася", added_by="100", note="грубит"))
        self.assertFalse(self.store.watch(GUILD, VASYA, name="Вася 2"))  # already: name refreshed
        self.assertTrue(self.store.is_watched(GUILD, VASYA))
        self.assertFalse(self.store.is_watched(OTHER_GUILD, VASYA))
        again = cf.FilterStore(self.path)  # the Manager side
        self.assertEqual((again.guild(GUILD).watched[VASYA].name, again.guild(GUILD).watched[VASYA].note), ("Вася 2", "грубит"))
        self.assertTrue(again.unwatch(GUILD, VASYA))
        self.assertFalse(self.store.is_watched(GUILD, VASYA))
        self.assertFalse(self.store.any_watched())
        with self.assertRaises(ValueError):
            self.store.watch(GUILD, "not-an-id")

    def test_unreadable_list_fails_closed_and_reset_keeps_a_copy(self):
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(cf.FilterStoreError):
            self.store.is_watched(GUILD, VASYA)
        with self.assertRaises(cf.FilterStoreError):
            self.store.watch(GUILD, VASYA)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")
        self.assertFalse(cf.needs_message_content({}, self.store))
        backup = self.store.reset()
        self.assertEqual(backup.read_text(encoding="utf-8"), "{broken")
        self.assertIsNone(self.store.check())


class RuntimeTests(unittest.TestCase):
    def test_a_named_member_is_muted_and_told_off_in_a_reply(self):
        h = Harness(self, verdict(severity=2))
        h.store.watch(GUILD, VASYA, name="Вася")
        self.assertEqual(h.say(1, h.vasya, "Петя, ты тупой дебил"), 45)
        self.assertEqual(h.timeouts, [(VASYA, 45, "Фильтр сообщений: Прямые оскорбления, обзывательства")])
        self.assertEqual(h.replies, [(1, "Фу, как некультурно. <@101>, у тебя 45 минут мута, мыло дать?", VASYA)])
        [action] = h.store.guild(GUILD).actions
        self.assertEqual((action.user_id, action.minutes, action.category, action.result), (VASYA, 45, "insult", "muted"))
        self.assertIn("Фильтр выдал Вася мут на 45 мин", h.audits[0])
        request = h.orchestrator.requests[0]
        self.assertEqual((request.task_class.value, request.reasoning_effort, request.allowed_tool_names), ("ROUTINE", None, ()))  # normal effort
        self.assertEqual(json.loads(request.messages[1].content)["word_list_hints"], ["insult"])

    def test_social_awareness_hears_about_the_mute(self):
        h = Harness(self, verdict())
        h.store.watch(GUILD, VASYA)
        told = []
        h.filter._on_mute = lambda *args: told.append(args)
        h.say(1, h.vasya, "ты дебил")
        self.assertEqual(told, [(GUILD, CHANNEL, VASYA)])

    def test_people_not_on_the_list_cost_nothing(self):
        h = Harness(self, verdict())
        h.store.watch(GUILD, VASYA)
        self.assertIsNone(h.say(1, h.petya, "ты дебил"))
        self.assertIsNone(h.say(2, h.vasya, "ты дебил", server=guild(OTHER_GUILD)))  # named on another server only
        self.assertIsNone(h.say(3, member(555, "SomeBot", bot=True), "ты дебил"))
        self.assertEqual(h.orchestrator.requests, [])

    def test_banter_is_fine_zero_tolerance_is_not(self):
        h = Harness(self, verdict(joking=True), verdict(category="threat", joking=True, severity=1))
        h.store.watch(GUILD, VASYA)
        friend = member(PETYA, "Петя")
        self.assertIsNone(h.say(1, h.vasya, "ахах ты дурак 😂", reply_to=types.SimpleNamespace(author=friend, clean_content="я опять в стену врезался")))
        payload = json.loads(h.orchestrator.requests[0].messages[1].content)
        self.assertEqual(payload["replying_to"], {"author": "Петя", "text": "я опять в стену врезался"})
        self.assertEqual(h.say(2, h.vasya, "убью тебя лол"), 60)

    def test_repeats_get_longer_and_a_burst_is_one_mute(self):
        h = Harness(self, verdict(severity=1), verdict(severity=1), verdict(severity=1))
        h.store.watch(GUILD, VASYA)
        self.assertEqual(h.say(1, h.vasya, "идиот"), 30)
        self.assertIsNone(h.say(2, h.vasya, "и ты тоже идиот"))  # same burst, before the mute took hold
        h.clock.advance(3600)
        self.assertEqual(h.say(3, h.vasya, "опять идиоты"), 60)  # second mute this week: +30
        h.vasya.timed_out_until = datetime.now(timezone.utc) + timedelta(minutes=10)
        self.assertIsNone(h.say(4, h.vasya, "идиоты"))  # still muted: nothing to judge

    def test_without_ai_only_slurs_and_threats_are_recognised(self):
        h = Harness(self, orchestrator=False)
        h.store.watch(GUILD, VASYA)
        self.assertIsNone(h.say(1, h.vasya, "ты дебил"))  # judged kinds need the AI's context
        self.assertEqual(h.say(2, h.vasya, "иди повесься"), 60)
        self.assertIn("only slurs and threats", h.filter.status()["problem"])

    def test_who_cannot_be_muted_is_reported_not_punished(self):
        h = Harness(self, verdict(severity=3), verdict(severity=3))
        boss = member(ADMIN, "Админ", admin=True)
        high = member(PETYA, "Модер", top=20)
        h.store.watch(GUILD, ADMIN)
        h.store.watch(GUILD, PETYA)
        self.assertIsNone(h.say(1, boss, "все вы идиоты"))
        self.assertIsNone(h.say(2, high, "все вы идиоты"))
        self.assertEqual((h.timeouts, h.replies), ([], []))
        results = [action.result for action in h.store.guild(GUILD).actions]
        self.assertEqual(results, ["failed: administrators cannot be muted (Discord rule)", "failed: the member's role is at or above the bot's highest role"])
        self.assertIn("Could not mute", h.filter.status()["problem"])

    def test_language_switched_off_and_no_message_content(self):
        h = Harness(self, verdict(), config={"language": "en"})
        h.store.watch(GUILD, VASYA)
        h.say(1, h.vasya, "you are an idiot")
        self.assertEqual(h.replies[0][1], "Ew, how rude. <@101>, that's 45 minutes of mute. Need some soap?")
        h = Harness(self, verdict(), config={"content_filter_enabled": False})
        h.store.watch(GUILD, VASYA)
        self.assertIsNone(h.say(1, h.vasya, "ты дебил"))
        h = Harness(self, verdict(), message_content=False)
        h.store.watch(GUILD, VASYA)
        self.assertIsNone(h.say(1, h.vasya, "ты дебил"))
        self.assertIn("Message Content Intent", h.filter.status()["problem"])
        self.assertTrue(cf.needs_message_content({}, h.store))
        self.assertFalse(cf.needs_message_content({"content_filter_enabled": False}, h.store))

    def test_heavy_messages_still_checked_after_the_rate_limit(self):
        h = Harness(self, *[verdict("none")] * cf.RATE_LIMIT, verdict(category="threat", severity=4))
        h.store.watch(GUILD, VASYA)
        for index in range(cf.RATE_LIMIT):
            h.say(index, h.vasya, f"обычное сообщение номер {index}")
        self.assertIsNone(h.say(100, h.vasya, "ещё одно обычное"))  # over the limit, nothing suspicious: no AI call
        self.assertEqual(len(h.orchestrator.requests), cf.RATE_LIMIT)
        self.assertEqual(h.say(101, h.vasya, "убью тебя"), 120)  # a local signal is always checked


# --------------------------------------------------------------------------
# managing the list from Discord: AI tools and /filter
# --------------------------------------------------------------------------


def import_admin():
    import importlib

    try:
        import msvcrt  # noqa: F401
    except ImportError:
        sys.modules["msvcrt"] = types.SimpleNamespace(LK_NBLCK=2, locking=lambda *args: None)
    return importlib.import_module("Admin")


class ToolGuild:
    def __init__(self, members):
        self.id = GUILD
        self.owner_id = OWNER
        self.me = types.SimpleNamespace(id=900, top_role=types.SimpleNamespace(position=10))
        self._members = {item.id: item for item in members}

    def get_member(self, user_id):
        return self._members.get(user_id)


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_tools_follow_the_hierarchy_and_use_the_bot_store(self):
        admin_tools = import_admin().admin_tools
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = cf.FilterStore(Path(temp.name) / cf.FILE_NAME)
        moderator = member(PETYA, "Модер", top=5)
        server = ToolGuild([moderator, member(VASYA, "Вася", top=2), member(OWNER, "Owner", top=9), member(ADMIN, "Старший", top=6)])
        context = admin_tools.AdminToolContext(guild=server, source="/ai", requesting_user_id=PETYA, enforce_hierarchy=True, content_filter=store)
        added = await admin_tools.execute_tool(context, "content_filter_watch", {"member_id": str(VASYA), "note": "грубит"})
        self.assertTrue(added.ok, added.message)
        self.assertTrue(store.is_watched(GUILD, VASYA))
        for target in (OWNER, ADMIN):  # the owner, someone above the requester
            refused = await admin_tools.execute_tool(context, "content_filter_watch", {"member_id": str(target)})
            self.assertFalse(refused.ok)
            self.assertFalse(store.is_watched(GUILD, target))
        listed = await admin_tools.execute_tool(context, "content_filter_list", {})
        self.assertEqual([item["member_id"] for item in listed.data["members"]], [str(VASYA)])
        removed = await admin_tools.execute_tool(context, "content_filter_unwatch", {"member_id": str(VASYA)})
        self.assertTrue(removed.ok)
        self.assertFalse(store.is_watched(GUILD, VASYA))
        self.assertEqual(admin_tools.TOOL_DEFINITIONS["content_filter_watch"].risk, "normal")  # the AI plan is approved first
        missing = await admin_tools.execute_tool(admin_tools.AdminToolContext(guild=server, requesting_user_id=PETYA), "content_filter_list", {})
        self.assertFalse(missing.ok)


class PackagingTests(unittest.TestCase):
    def test_every_tool_extension_is_in_the_packaged_app(self):
        # admin_tools imports its extensions by name: PyInstaller only bundles them via hiddenimports.
        admin_tools = import_admin().admin_tools
        spec = (CORE_ROOT.parent / "packaging" / "DarkAbyssApp.spec").read_text(encoding="utf-8")
        for name in admin_tools.EXTENSION_MODULES:
            self.assertIn(f'"{name}"', spec)


class SlashCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.admin = import_admin()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = cf.FilterStore(Path(temp.name) / cf.FILE_NAME)
        originals = (self.admin.filter_store, self.admin.content_filter_runtime, self.admin.load_config, self.admin.send_audit)
        self.addCleanup(lambda: (setattr(self.admin, "filter_store", originals[0]), setattr(self.admin, "content_filter_runtime", originals[1]), setattr(self.admin, "load_config", originals[2]), setattr(self.admin, "send_audit", originals[3]), bot_i18n.set_bot_language("en")))
        self.admin.filter_store = self.store
        self.admin.content_filter_runtime = types.SimpleNamespace(message_content=False)
        self.config = {"allow_server_administrators": False, "allowed_user_ids": [PETYA], "allowed_role_ids": [], "audit_channel_id": None, "language": "ru"}
        self.admin.load_config = lambda runtime=None: (bot_i18n.set_bot_language("ru"), self.config)[1]

        async def no_audit(*args):
            return None

        self.admin.send_audit = no_audit

    def interaction(self, user):
        sent = []

        async def send_message(text, **kwargs):
            sent.append(text)

        import discord

        # A discord.Member for the isinstance check in actor_has_access (it uses __slots__).
        fake = type("FakeMember", (discord.Member,), {"__init__": lambda self: None, "__str__": lambda self: self.display_name, **vars(user)})()
        server = ToolGuild([user, member(VASYA, "Вася", top=2), member(OWNER, "Owner", top=9)])
        return types.SimpleNamespace(guild=server, user=fake, response=types.SimpleNamespace(send_message=send_message)), sent

    async def test_filter_commands_need_access_and_explain_a_needed_restart(self):
        allowed = member(PETYA, "Модер", top=5)
        allowed.roles = []
        target = types.SimpleNamespace(**vars(member(VASYA, "Вася", top=2)))
        interaction, sent = self.interaction(allowed)
        await self.admin.filter_add.callback(interaction, target, "грубит")
        self.assertTrue(self.store.is_watched(GUILD, VASYA))
        self.assertIn("Теперь фильтрую <@101>", sent[-1])
        self.assertIn("Message Content Intent", sent[-1])  # the running bot started without it
        await self.admin.filter_list.callback(interaction)
        self.assertIn("- <@101> (грубит) — мутов: 0", sent[-1])
        await self.admin.filter_remove.callback(interaction, target)
        self.assertFalse(self.store.is_watched(GUILD, VASYA))
        stranger = member(555, "Чужой", top=1)
        stranger.roles = []
        interaction, sent = self.interaction(stranger)
        await self.admin.filter_add.callback(interaction, target, None)
        self.assertEqual(sent, ["Доступ запрещён."])
        self.assertFalse(self.store.is_watched(GUILD, VASYA))

    def test_slash_descriptions_are_translated(self):
        import bot_i18n_ru_admin

        texts = []
        for command in self.admin.bot.tree.get_commands():
            texts.append(command.description)
            for child in getattr(command, "commands", []):
                texts.append(child.description)
                texts.extend(parameter.description for parameter in child.parameters)
            texts.extend(parameter.description for parameter in getattr(command, "parameters", []))
        missing = [text for text in texts if text and text not in bot_i18n_ru_admin.RU and text != "…"]
        self.assertEqual(missing, [])



class AdminStartTests(unittest.TestCase):
    def test_named_members_make_the_bot_request_message_content(self):
        admin = import_admin()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        data_dir = Path(temp.name) / "data"
        cf.FilterStore(data_dir / cf.FILE_NAME).watch(GUILD, VASYA)
        runtime = types.SimpleNamespace(instance_id="admin-main", config_path=Path("x"), token_path=Path("y"), lock_path=Path(temp.name) / "admin_bot.lock", data_dir=data_dir)
        base = {"allow_server_administrators": True, "allowed_user_ids": [], "allowed_role_ids": [], "audit_channel_id": None}
        seen = {}

        def fake_run(token):
            seen["message_content"] = admin.bot._connection._intents.message_content
            seen["filter"] = admin.content_filter_runtime
            seen["store"] = admin.ai_transport.content_filter

        patches = {
            "resolve_runtime": lambda instance_id: runtime,
            "load_config": lambda runtime=None: admin.validate_config(dict(base)),
            "load_token": lambda runtime=None: "not-a-real-token",
            "acquire_single_instance_lock": lambda runtime=None: True,
            "resolve_ai_stores": lambda runtime=None: None,
        }
        saved = {name: getattr(admin, name) for name in patches}
        saved_globals = (admin.bot.run, admin.social, admin.content_filter_runtime, admin.filter_store, admin.ai_transport.content_filter, admin.feature_store)
        for name, value in patches.items():
            setattr(admin, name, value)
        admin.bot.run = fake_run
        try:
            self.assertEqual(admin.main(["--instance", "admin-main"]), 0)
        finally:
            for name, value in saved.items():
                setattr(admin, name, value)
            admin.bot.run, admin.social, admin.content_filter_runtime, admin.filter_store, admin.ai_transport.content_filter, admin.feature_store = saved_globals
            admin.configure_message_content_intent(admin.bot, False)
        self.assertTrue(seen["message_content"])
        self.assertTrue(seen["filter"].active)
        self.assertTrue(seen["store"].is_watched(GUILD, VASYA))
        self.assertIn("content filter", admin.PRIVILEGED_INTENTS_HELP)

if __name__ == "__main__":
    unittest.main()
