"""Kairo's generic custom-message-rule engine (message_policy.py)."""

import asyncio
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parents[1] / "DarkAbyss_Core"
sys.path.insert(0, str(CORE_ROOT))

import message_policy as mp  # noqa: E402

GUILD = 1
CHANNEL = 10
OTHER_CHANNEL = 11
ALICE, BOB = 101, 102


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def rule(**kwargs):
    base = {
        "id": "no-politics",
        "enabled": True,
        "applies_to": "everyone",
        "channel_ids": [],
        "description": "Heated political arguments, off-topic on this game server.",
        "reply_templates": ["{user}, let's keep politics out of here."],
        "min_confidence": 0.75,
    }
    base.update(kwargs)
    return base


def config(policies=None, enabled=True, **extra):
    return {mp.CONFIG_ENABLED: enabled, mp.CONFIG_POLICIES: policies if policies is not None else [rule()], "language": "ru", **extra}


def decision(violates=True, confidence=0.9, distress=False, reason="states opinion as fact"):
    return {"violates": violates, "confidence": confidence, "distress": distress, "reason": reason}


class FakeOrchestrator:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    async def orchestrate(self, request, **kwargs):
        self.requests.append(request)
        answer = self.answers.pop(0) if self.answers else decision(violates=False)
        return types.SimpleNamespace(status=types.SimpleNamespace(value="COMPLETED"), content=json.dumps(answer))


def member(user_id, name="Member", bot=False):
    return types.SimpleNamespace(id=user_id, display_name=name, name=name, mention=f"<@{user_id}>", bot=bot)


def message(message_id, author, text, channel=CHANNEL, reply_to=None):
    return types.SimpleNamespace(
        id=message_id,
        guild=types.SimpleNamespace(id=GUILD),
        channel=types.SimpleNamespace(id=channel, parent_id=None),
        author=author,
        clean_content=text,
        content=text,
        reference=types.SimpleNamespace(resolved=reply_to) if reply_to is not None else None,
        webhook_id=None,
    )


class Harness:
    def __init__(self, test, *answers, cfg=None, orchestrator=True, message_content=True):
        self.clock = Clock()
        self.orchestrator = FakeOrchestrator(*answers) if orchestrator else None
        self.events = []  # ("reply"|"delete", message_id)
        self.audits = []
        self.delete_ok = True

        async def reply(original, text, author):
            self.events.append(("reply", original.id, text, author.id))

        async def delete(original):
            self.events.append(("delete", original.id))
            return self.delete_ok

        async def audit(guild, text):
            self.audits.append(text)

        self.engine = mp.MessagePolicyEngine(
            get_orchestrator=lambda: self.orchestrator,
            reply=reply,
            delete=delete,
            audit=audit,
            clock=self.clock,
            message_content=message_content,
            choose=lambda pool: pool[0],
        )
        self.engine.apply_config(config() if cfg is None else cfg)
        self.alice = member(ALICE, "Alice")

    def say(self, message_id, author, text, **kwargs):
        return asyncio.run(self.engine.observe(message(message_id, author, text, **kwargs)))


class SettingsTests(unittest.TestCase):
    def test_parsing_and_scope(self):
        settings = mp.Settings.from_config(config(policies=[rule(applies_to=[str(ALICE)], channel_ids=[str(CHANNEL)], min_confidence=0.99)]))
        [policy] = settings.active_policies()
        self.assertFalse(policy.applies_to_everyone)
        self.assertEqual(policy.user_ids, frozenset({ALICE}))
        self.assertAlmostEqual(policy.min_confidence, 0.99)
        self.assertTrue(policy.scope_matches(CHANNEL, None, ALICE))
        self.assertFalse(policy.scope_matches(OTHER_CHANNEL, None, ALICE))  # wrong channel
        self.assertFalse(policy.scope_matches(CHANNEL, None, BOB))  # not in applies_to
        self.assertTrue(policy.scope_matches(55, CHANNEL, ALICE))  # a thread of the watched channel

    def test_active_policies_needs_enabled_description_and_templates(self):
        self.assertEqual(mp.Settings.from_config(config(policies=[rule(enabled=False)])).active_policies(), ())
        self.assertFalse(mp.Settings.from_config(config(enabled=False)).enabled)
        self.assertEqual(len(mp.Settings.from_config(config()).active_policies()), 1)
        self.assertEqual(mp.Settings.from_config({}).policies, ())

    def test_validation(self):
        cfg = {}
        mp.validate_config_fields(cfg)
        self.assertEqual(cfg, {mp.CONFIG_ENABLED: False, mp.CONFIG_POLICIES: []})
        ok = {mp.CONFIG_POLICIES: [rule()]}
        mp.validate_config_fields(ok)
        self.assertEqual(ok[mp.CONFIG_POLICIES][0]["applies_to"], "everyone")
        for bad in (
            {mp.CONFIG_ENABLED: "yes"},
            {mp.CONFIG_POLICIES: "x"},
            {mp.CONFIG_POLICIES: [rule(description="")]},
            {mp.CONFIG_POLICIES: [rule(reply_templates=[])]},
            {mp.CONFIG_POLICIES: [rule(), rule()]},  # duplicate id
            {mp.CONFIG_POLICIES: [rule(applies_to="nobody")]},
            {mp.CONFIG_POLICIES: [rule(id=str(i)) for i in range(mp.MAX_POLICIES + 1)]},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                mp.validate_config_fields(dict(bad))

    def test_parse_decision(self):
        parsed = mp.parse_decision('```json\n{"violates": true, "confidence": 2, "distress": false, "reason": "x"}\n```')
        self.assertEqual((parsed.violates, parsed.confidence, parsed.distress), (True, 1.0, False))
        self.assertIsNone(mp.parse_decision("no json here"))


class EngineTests(unittest.TestCase):
    def test_off_by_default_does_nothing(self):
        h = Harness(self, decision(), cfg=config(enabled=False))
        self.assertIsNone(h.say(1, h.alice, "all politicians are thieves and ruined everything"))
        self.assertEqual(h.orchestrator.requests, [])
        self.assertFalse(h.engine.active)

    def test_violation_replies_then_deletes_pinging_only_the_author(self):
        h = Harness(self, decision(confidence=0.9))
        self.assertEqual(h.say(1, h.alice, "all politicians are thieves and ruined everything"), "no-politics")
        self.assertEqual(h.events, [("reply", 1, "<@101>, let's keep politics out of here.", ALICE), ("delete", 1)])  # reply BEFORE delete
        self.assertEqual(h.engine.counters["removed"], 1)
        self.assertEqual(h.engine.last_action["rule"], "no-politics")
        self.assertIn("removed the message", h.audits[0])
        request = h.orchestrator.requests[0]
        self.assertEqual((request.task_class.value, request.reasoning_effort), ("ROUTINE", None))  # normal effort, not high
        self.assertEqual(json.loads(request.messages[1].content)["rule"], "Heated political arguments, off-topic on this game server.")

    def test_distress_is_never_touched(self):
        h = Harness(self, decision(violates=True, distress=True))
        self.assertIsNone(h.say(1, h.alice, "мне угрожают расправой, мне страшно"))
        self.assertEqual(h.events, [])
        self.assertEqual(h.engine.counters["distress_skipped"], 1)

    def test_low_confidence_and_non_violation_do_nothing(self):
        h = Harness(self, decision(confidence=0.6), decision(violates=False))
        self.assertIsNone(h.say(1, h.alice, "кажется, тут была политика"))
        h.clock.advance(mp.ACT_COOLDOWN + 1)
        self.assertIsNone(h.say(2, h.alice, "сегодня хорошая погода"))
        self.assertEqual(h.events, [])

    def test_scope_bots_and_short_messages(self):
        h = Harness(self, *[decision()] * 5, cfg=config(policies=[rule(applies_to=[str(ALICE)], channel_ids=[str(CHANNEL)])]))
        self.assertIsNone(h.say(1, member(BOB, "Bob"), "all politicians are thieves"))  # not in applies_to
        self.assertIsNone(h.say(2, h.alice, "all politicians are thieves", channel=OTHER_CHANNEL))  # wrong channel
        self.assertIsNone(h.say(3, member(ALICE, "AliceBot", bot=True), "all politicians are thieves"))  # a bot
        self.assertIsNone(h.say(4, h.alice, "👍"))  # too short / no words
        self.assertEqual(h.orchestrator.requests, [])
        self.assertEqual(h.say(5, h.alice, "all politicians are thieves"), "no-politics")

    def test_delete_failure_keeps_the_reply_and_reports(self):
        h = Harness(self, decision())
        h.delete_ok = False

        async def failing(original):
            raise RuntimeError("Missing Permissions")

        h.engine._delete = failing
        self.assertEqual(h.say(1, h.alice, "the government ruined everything on purpose"), "no-politics")
        self.assertEqual([event[0] for event in h.events], ["reply"])
        self.assertEqual(h.engine.counters["reply_only"], 1)
        self.assertIn("could not delete", h.engine.problem)

    def test_rate_limit_and_cooldown(self):
        h = Harness(self, *[decision(violates=False)] * (mp.RATE_LIMIT + 2))
        for index in range(mp.RATE_LIMIT):
            h.clock.advance(mp.ACT_COOLDOWN + 1)
            h.say(index, h.alice, f"обычное сообщение {index}")
        before = len(h.orchestrator.requests)
        h.clock.advance(mp.ACT_COOLDOWN + 1)
        h.say(100, h.alice, "ещё сообщение")
        self.assertEqual(len(h.orchestrator.requests), before)  # over the per-author limit
        self.assertEqual(h.engine.counters["skipped_rate"], 1)

    def test_cooldown_after_acting(self):
        h = Harness(self, decision(), decision())
        self.assertEqual(h.say(1, h.alice, "all politicians are thieves"), "no-politics")
        self.assertIsNone(h.say(2, h.alice, "and liars too"))  # within ACT_COOLDOWN: left alone
        self.assertEqual(len(h.orchestrator.requests), 1)

    def test_no_message_content_is_inactive_and_reports(self):
        h = Harness(self, decision(), message_content=False)
        self.assertIsNone(h.say(1, h.alice, "all politicians are thieves"))
        self.assertFalse(h.engine.active)
        self.assertIn("Message Content Intent", h.engine.status()["problem"])
        self.assertTrue(mp.needs_message_content(config()))
        self.assertFalse(mp.needs_message_content(config(enabled=False)))
        self.assertFalse(mp.needs_message_content(config(policies=[])))

    def test_without_ai_nothing_happens(self):
        h = Harness(self, orchestrator=False)
        self.assertIsNone(h.say(1, h.alice, "all politicians are thieves"))
        self.assertIn("AI is unavailable", h.engine.status()["problem"])


class AdminWiringTests(unittest.TestCase):
    def base(self, **extra):
        return {"allow_server_administrators": True, "allowed_user_ids": [], "allowed_role_ids": [], "audit_channel_id": None, **extra}

    def import_admin(self):
        import importlib

        try:
            import msvcrt  # noqa: F401
        except ImportError:
            sys.modules["msvcrt"] = types.SimpleNamespace(LK_NBLCK=2, locking=lambda *args: None)
        return importlib.import_module("Admin")

    def test_validate_defaults_and_engine_built_on_start(self):
        admin = self.import_admin()
        validated = admin.validate_config(self.base())
        self.assertEqual((validated[mp.CONFIG_ENABLED], validated[mp.CONFIG_POLICIES]), (False, []))
        with self.assertRaises(ValueError):
            admin.validate_config(self.base(message_policies=[{"id": "x"}]))  # no description/templates

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = types.SimpleNamespace(instance_id="admin-main", config_path=Path("x"), token_path=Path("y"), lock_path=Path(temp.name) / "admin_bot.lock", data_dir=Path(temp.name) / "data")
        cfg = self.base(message_policies_enabled=True, message_policies=[rule()])
        seen = {}

        def fake_run(token):
            seen["message_content"] = admin.bot._connection._intents.message_content
            seen["engine"] = admin.message_policy_runtime

        patches = {
            "resolve_runtime": lambda instance_id: runtime,
            "load_config": lambda runtime=None: admin.validate_config(dict(cfg)),
            "load_token": lambda runtime=None: "not-a-real-token",
            "acquire_single_instance_lock": lambda runtime=None: True,
            "resolve_ai_stores": lambda runtime=None: None,
        }
        saved = {name: getattr(admin, name) for name in patches}
        saved_globals = (admin.bot.run, admin.social, admin.content_filter_runtime, admin.message_policy_runtime, admin.filter_store, admin.feature_store)
        for name, value in patches.items():
            setattr(admin, name, value)
        admin.bot.run = fake_run
        try:
            self.assertEqual(admin.main(["--instance", "admin-main"]), 0)
        finally:
            for name, value in saved.items():
                setattr(admin, name, value)
            admin.bot.run, admin.social, admin.content_filter_runtime, admin.message_policy_runtime, admin.filter_store, admin.feature_store = saved_globals
            admin.configure_message_content_intent(admin.bot, False)
        self.assertTrue(seen["message_content"])  # a usable rule asks for the intent
        self.assertTrue(seen["engine"].active)

    def test_policy_delete_calls_discord_delete(self):
        admin = self.import_admin()
        deleted = []

        class Msg:
            async def delete(self_inner):
                deleted.append(True)

        self.assertTrue(asyncio.run(admin.policy_delete(Msg())))
        self.assertEqual(deleted, [True])


if __name__ == "__main__":
    unittest.main()
