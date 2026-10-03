"""Kairo's long-term social memory (social_memory), social signals (social_signals) and bot heartbeats."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parents[1] / "DarkAbyss_Core"
sys.path.insert(0, str(CORE_ROOT))

import bot_events  # noqa: E402
import social_memory as sm  # noqa: E402
import social_signals as signals  # noqa: E402

G1, G2 = 1, 2


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class MemoryCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / sm.FILE_NAME
        self.clock = Clock()
        self.memory = sm.SocialMemory(self.path, clock=self.clock)


class LoreTests(MemoryCase):
    MEME = "Bob's 'banana aim' is the server's running joke about his sniping"

    def test_a_single_sighting_is_only_a_candidate(self):
        self.assertEqual(self.memory.propose(G1, "remember", kind="meme", text=self.MEME), "candidate")
        self.assertEqual(self.memory.guild(G1).active_lore(), [])
        # The same burst again is not a second sighting.
        self.clock.advance(60)
        self.assertIsNone(self.memory.propose(G1, "remember", kind="meme", text="Bob's banana aim is the running joke about his sniping"))
        self.clock.advance(sm.CONFIRM_GAP)
        self.assertEqual(self.memory.propose(G1, "remember", kind="meme", text=self.MEME), "activated")
        [entry] = self.memory.guild(G1).active_lore()
        self.assertEqual((entry.kind, entry.confirmations), ("meme", 2))
        self.clock.advance(sm.CONFIRM_GAP)
        self.assertEqual(self.memory.propose(G1, "remember", kind="meme", text=self.MEME), "confirmed")
        self.assertEqual(self.memory.guild(G1).active_lore()[0].confirmations, 3)
        self.assertEqual(self.memory.guild(G2).lore, [])  # per server

    def test_junk_and_private_data_are_refused(self):
        for text in (
            "lol",
            "ping <@123456789012345678> every day",
            "see https://example.com for the meme",
            "Bob's ID is 123456789012345678",
            "write to bob@example.com about it",
            "call Bob at +7 999 123 45 67",
            "@everyone is the server joke",
            "x" * 200,
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.memory.propose(G1, "remember", kind="fact", text=text))
        self.assertIsNone(self.memory.propose(G1, "remember", kind="secret", text="A valid sentence that is long enough"))
        self.assertFalse(self.path.exists())  # nothing written for refused proposals

    def test_update_forget_clear_and_caps(self):
        self.memory.propose(G1, "remember", kind="nickname", text="Members call Alice 'the Captain' in raids")
        self.clock.advance(sm.CONFIRM_GAP)
        self.memory.propose(G1, "remember", kind="nickname", text="Members call Alice 'the Captain' in raids")
        [entry] = self.memory.guild(G1).active_lore()
        self.assertEqual(self.memory.propose(G1, "update", text="Members call Alice 'Captain' in every raid", entry_id=entry.id), "updated")
        self.assertEqual(self.memory.guild(G1).active_lore()[0].text, "Members call Alice 'Captain' in every raid")
        self.assertEqual(self.memory.propose(G1, "forget", entry_id=entry.id), "forgot")
        self.assertEqual(self.memory.guild(G1).lore, [])
        for index in range(sm.MAX_CANDIDATES + 5):
            self.clock.advance(1)
            self.memory.propose(G1, "remember", kind="fact", text=f"Tradition alpha{index} beta{index} gamma{index} delta{index}")
        self.assertEqual(len(self.memory.guild(G1).lore), sm.MAX_CANDIDATES)
        self.assertEqual(self.memory.clear_lore(G1), sm.MAX_CANDIDATES)
        self.assertEqual(self.memory.guild(G1).lore, [])

    def test_candidates_expire_and_weak_lore_fades(self):
        self.memory.propose(G1, "remember", kind="event", text="The server had a big tournament night in May")
        self.clock.advance(sm.CANDIDATE_TTL + 1)
        self.memory.propose(G2, "remember", kind="event", text="Another server is not touched by this cleanup")
        self.memory.propose(G1, "remember", kind="joke", text="Someone always says the patch broke everything")
        texts = [entry.text for entry in self.memory.guild(G1).lore]
        self.assertEqual(texts, ["Someone always says the patch broke everything"])


class QuietTests(MemoryCase):
    def test_scopes_replacement_expiry_and_lifting(self):
        channel = self.memory.add_mute(G1, "channel", 3600, channel_id=10, by_user_id=101)
        self.memory.add_mute(G1, "user", 7200, user_id=102, by_user_id=102)
        self.assertIsNotNone(self.memory.quiet_for(G1, 10))
        self.assertIsNotNone(self.memory.quiet_for(G1, 55, parent_id=10))  # a thread of that channel
        self.assertIsNone(self.memory.quiet_for(G1, 11))
        self.assertIsNotNone(self.memory.quiet_for(G1, 11, user_ids=(102,)))
        self.assertIsNone(self.memory.quiet_for(G2, 10))
        again = self.memory.add_mute(G1, "channel", 600, channel_id=10)
        self.assertEqual([mute.id for mute in self.memory.active_mutes(G1) if mute.scope == "channel"], [again.id])
        self.assertNotEqual(again.id, channel.id)
        self.clock.advance(601)
        self.assertIsNone(self.memory.quiet_for(G1, 10))
        self.assertEqual(self.memory.lift(G1, lambda mute: mute.user_id == 102), 1)
        self.assertEqual(self.memory.active_mutes(G1), [])
        long = self.memory.add_mute(G1, "guild", 10**9)
        self.assertLessEqual(long.until - self.clock.now, sm.MAX_MUTE_SECONDS)


class FileSafetyTests(MemoryCase):
    def test_unreadable_file_fails_closed_and_reset_keeps_a_copy(self):
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(sm.SocialMemoryError):
            self.memory.guild(G1)
        with self.assertRaises(sm.SocialMemoryError):
            self.memory.add_mute(G1, "guild", 60)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")  # never written over
        self.assertIn("unreadable", self.memory.check())
        backup = self.memory.reset()
        self.assertEqual(backup.read_text(encoding="utf-8"), "{broken")
        self.assertIsNone(self.memory.check())
        self.memory.add_mute(G1, "guild", 60)
        self.assertEqual(len(self.memory.active_mutes(G1)), 1)

    def test_two_stores_on_one_file_see_each_other(self):
        manager_side = sm.SocialMemory(self.path, clock=self.clock)
        self.memory.add_mute(G1, "channel", 3600, channel_id=10)
        self.assertIsNotNone(manager_side.quiet_for(G1, 10))
        manager_side.lift(G1, lambda mute: True)
        self.assertIsNone(self.memory.quiet_for(G1, 10))
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], sm.VERSION)

    def test_outcomes_are_capped_and_scored(self):
        for index in range(sm.MAX_OUTCOMES + 5):
            self.memory.record_outcome(G1, sm.Outcome(self.clock.now + index, 10, "reply", "named", "engaged" if index % 2 else "ignored"))
        outcomes = self.memory.guild(G1).outcomes
        self.assertEqual(len(outcomes), sm.MAX_OUTCOMES)
        counts = sm.feedback_counts(outcomes, 10, last=10)
        self.assertEqual((counts["engaged"], counts["ignored"]), (5, 5))
        self.assertAlmostEqual(sm.feedback_score(counts), 5 - 3.5)


class SignalTests(unittest.TestCase):
    def test_quiet_requests(self):
        cases = {
            "Кайро, помолчи": ("quiet", "channel", signals.HOUR),
            "кайро заткнись на час": ("quiet", "channel", signals.HOUR),
            "Кайро, заткнись на 2 часа": ("quiet", "channel", 2 * signals.HOUR),
            "кайро не лезь сюда": ("quiet", "channel", signals.LONG_QUIET_SECONDS),
            "кайро, не отвечай в этом канале": ("quiet", "channel", signals.LONG_QUIET_SECONDS),
            "кайро, мне не отвечай": ("quiet", "user", signals.LONG_QUIET_SECONDS),
            "кайро помолчи везде до завтра": ("quiet", "guild", 12 * signals.HOUR),
            "kairo shut up for 10 minutes": ("quiet", "channel", 10 * signals.MINUTE),
            "kairo please don't reply to me": ("quiet", "user", signals.LONG_QUIET_SECONDS),
            "Кайро, можешь снова говорить": ("resume", "channel", None),
            "kairo you can talk again": ("resume", "channel", None),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                request = signals.parse_quiet_request(text)
                self.assertEqual((request.action, request.scope, request.seconds), expected)
        for text in ("кайро, не молчи", "кайро, как дела?", "kairo, here is the plan", "кайро, тихо сегодня на сервере", ""):
            with self.subTest(text=text):
                self.assertIsNone(signals.parse_quiet_request(text))

    def test_reactions_and_replies(self):
        self.assertEqual(signals.kairo_reaction("❤"), "❤️")
        self.assertIsNone(signals.kairo_reaction("🍆"))
        self.assertIsNone(signals.kairo_reaction("<:custom:123>"))
        self.assertEqual((signals.classify_reaction("😂"), signals.classify_reaction("👎"), signals.classify_reaction("🧀")), ("positive", "negative", None))
        self.assertEqual(signals.classify_text("тебя никто не спрашивал"), "negative")
        self.assertEqual(signals.classify_text("ахах, спасибо"), "positive")
        self.assertEqual(signals.classify_text("ок, понял"), "neutral")


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()

    def test_running_stale_stopped_and_events_are_kept(self):
        gp = bot_events.EventPublisher("gp-main", "game_presence", self.root, clock=self.clock)
        gp.publish("group_up.suggested", 1)
        self.assertTrue(gp.heartbeat(display_name="Games A", discord_user_id=555, discord_name="GroupUp", guild_ids=[1, "2", "x"]))
        self.assertFalse(gp.heartbeat(display_name="Games A"))  # at most once a minute
        reader = bot_events.EventReader("admin-main", self.root, clock=self.clock)
        [info] = reader.bots()
        self.assertEqual((info.instance_id, info.bot_type, info.display_name, info.discord_user_id, info.guild_ids), ("gp-main", "game_presence", "Games A", 555, frozenset({1, 2})))
        self.assertTrue(info.running(self.clock.now))
        self.assertEqual(len(bot_events.read_file(self.root / "gp-main.json")), 1)  # events survive heartbeats
        self.assertFalse(info.running(self.clock.now + bot_events.HEARTBEAT_STALE_SECONDS + 1))  # crashed: stale
        gp.stopped()
        [info] = reader.bots()
        self.assertFalse(info.running(self.clock.now))
        own = bot_events.EventPublisher("admin-main", "admin", self.root, clock=self.clock)
        own.heartbeat(display_name="Kairo")
        self.assertEqual([item.instance_id for item in reader.bots()], ["gp-main"])  # never itself

    def test_a_file_cannot_describe_another_bot(self):
        gp = bot_events.EventPublisher("gp-main", "game_presence", self.root, clock=self.clock)
        gp.heartbeat(display_name="Games A")
        raw = json.loads((self.root / "gp-main.json").read_text(encoding="utf-8"))
        (self.root / "fake.json").write_text(json.dumps(raw), encoding="utf-8")  # says source_instance gp-main
        self.assertIsNone(bot_events.read_bot(self.root / "fake.json"))


if __name__ == "__main__":
    unittest.main()
