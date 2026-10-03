"""Stream Director domain + persistence: sessions, duplicates, recovery,
moments, challenges, polls, inbox, progression, recap, fail-closed state."""

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"
sys.path.insert(0, str(CORE_ROOT))
_ISOLATED = tempfile.TemporaryDirectory()
os.environ["DARKABYSS_DATA_DIR"] = str(Path(_ISOLATED.name) / "data")

for _name in ("stream_director", "stream_director_config", "stream_director_store"):
    sys.modules.pop(_name, None)
sd = importlib.import_module("stream_director")
sdc = importlib.import_module("stream_director_config")
sds = importlib.import_module("stream_director_store")

START = 1_791_000_000.0  # a Saturday afternoon (local time differs per machine; tests use relative times)


class Clock:
    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


VIEWER = sd.Actor("discord", "100", "Viewer")
VIEWER2 = sd.Actor("discord", "101", "Viewer Two")
VIEWER3 = sd.Actor("twitch", "555", "chatter")
TEAM = sd.Actor("discord", "1", "Streamer", team=True)


def config(**changes):
    data = sdc.normalize_config({"guild_id": "123456789012345678", "channel_id": "223456789012345678", **changes})
    return sdc.parse_config(data)


class DirectorCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name) / "data"
        self.clock = Clock()
        self.director = self.make()

    def make(self, **changes):
        return sd.Director(sds.StateStore(self.data_dir), config(**changes), self.clock)

    def event(self, kind, message_id, **data):
        return sd.StreamEvent(kind, message_id, self.clock(), data)

    def go_live(self, stream_id="s-1", director=None):
        director = director or self.director
        return director.handle_event(self.event("online", f"m-online-{stream_id}", stream_id=stream_id, started_at=self.clock()))

    def kinds(self, effects):
        return [effect.kind for effect in effects]


class ConfigTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        data = sdc.normalize_config({})
        self.assertEqual(data, sdc.DEFAULT_CONFIG)
        self.assertFalse(sdc.is_configured(data))
        with self.assertRaises(sdc.ConfigError):
            sdc.normalize_config({"guild_id": "abc"})
        with self.assertRaises(sdc.ConfigError):
            sdc.normalize_config({"twitch_client_id": "not a client id!"})
        with self.assertRaises(sdc.ConfigError):
            sdc.normalize_config({"moment_cooldown_seconds": 99999})
        with self.assertRaises(sdc.ConfigError):
            sdc.normalize_config({"features": {"moments": "yes"}})
        # A feature saved by a newer version is ignored (rollback keeps working).
        data = sdc.normalize_config({"features": {"moments": False, "future_feature": True}})
        self.assertEqual(set(data["features"]), set(sdc.FEATURES))
        self.assertFalse(data["features"]["moments"])
        parsed = sdc.parse_config(sdc.normalize_config({"guild_id": 123456789012345678, "team_role_ids": ["55555", "55555", "66666"]}))
        self.assertEqual(parsed.guild_id, 123456789012345678)
        self.assertEqual(parsed.team_role_ids, (55555, 66666))

    def test_secrets_are_not_config(self):
        self.assertNotIn("token", json.dumps(sdc.DEFAULT_CONFIG).lower())
        self.assertNotIn("secret", json.dumps(sdc.DEFAULT_CONFIG).lower())


class SessionLifecycleTests(DirectorCase):
    def test_online_starts_one_session_and_duplicates_are_ignored(self):
        effects = self.go_live()
        self.assertEqual(self.kinds(effects), ["session_started"])
        session = self.director.session
        self.assertEqual((session["status"], session["stream_ids"]), ("live", ["s-1"]))
        # The same notification again (Twitch retries) and the same stream via another message id.
        self.assertEqual(self.go_live(), [])
        self.assertEqual(self.director.handle_event(self.event("online", "m-other", stream_id="s-1")), [])
        self.assertEqual(self.director.session["id"], session["id"])

    def test_short_drop_stays_one_session_and_recap_comes_after_the_grace(self):
        self.go_live()
        session_id = self.director.session["id"]
        self.clock.advance(3600)
        effects = self.director.handle_event(self.event("offline", "m-off-1"))
        self.assertEqual(self.kinds(effects), ["card_update"])
        self.assertEqual(self.director.session["status"], "ending")
        self.clock.advance(120)  # encoder hiccup: back within 5 minutes with a NEW stream id
        effects = self.director.handle_event(self.event("online", "m-on-2", stream_id="s-2"))
        self.assertIn("thread_post", self.kinds(effects))
        self.assertEqual(self.director.session["id"], session_id)
        self.assertEqual(self.director.session["stream_ids"], ["s-1", "s-2"])
        self.clock.advance(1800)
        self.director.handle_event(self.event("offline", "m-off-2"))
        self.clock.advance(4 * 60)
        self.assertEqual(self.director.tick(), [])  # still within the grace
        self.clock.advance(2 * 60)
        effects = self.director.tick()
        self.assertIn("session_ended", self.kinds(effects))
        self.assertIsNone(self.director.session)
        ended = self.director.state["sessions"][-1]
        self.assertEqual(ended["status"], "ended")
        self.assertEqual(ended["recap"]["duration_seconds"], 3600 + 120 + 1800)

    def test_no_grace_finalizes_on_offline(self):
        director = self.make(end_grace_minutes=0)
        self.go_live(director=director)
        self.clock.advance(600)
        effects = director.handle_event(self.event("offline", "m-off"))
        self.assertIn("session_ended", self.kinds(effects))

    def test_manual_session_without_twitch(self):
        self.assertFalse(self.director.manual_start(VIEWER).ok)
        outcome = self.director.manual_start(TEAM, "Chill stream")
        self.assertTrue(outcome.ok)
        self.assertEqual(self.director.session["source"], "manual")
        self.assertFalse(self.director.manual_start(TEAM).ok)  # one at a time
        # Twitch reports the stream later: it is attached, not a second session.
        self.go_live("s-9")
        self.assertEqual(self.director.session["source"], "twitch")
        self.assertEqual(self.director.session["stream_ids"], ["s-9"])
        self.clock.advance(900)
        outcome = self.director.manual_end(TEAM)
        self.assertIn("session_ended", self.kinds(outcome.effects))
        self.assertFalse(self.director.manual_end(TEAM).ok)


class DuplicateEventTests(DirectorCase):
    def test_repeated_message_ids_never_count_twice(self):
        self.go_live()
        raid = self.event("raid", "m-raid", from_name="Friend", viewers=7)
        first = self.director.handle_event(raid)
        second = self.director.handle_event(raid)
        self.assertIn("thread_post", self.kinds(first))
        self.assertEqual(second, [])
        self.assertEqual(len(self.director.session["stats"]["raids"]), 1)
        self.assertEqual(self.director.state["community"]["points"], sd.POINTS["raid"])

    def test_old_replays_are_ignored_and_ids_survive_a_restart(self):
        self.go_live()
        stale = sd.StreamEvent("raid", "m-old", self.clock() - 3600, {"from_name": "x", "viewers": 1})
        self.assertEqual(self.director.handle_event(stale), [])
        self.director.handle_event(self.event("cheer", "m-cheer", bits=100))
        restarted = self.make()
        self.assertEqual(restarted.handle_event(self.event("cheer", "m-cheer", bits=100)), [])
        self.assertEqual(restarted.session["stats"]["bits"], 100)

    def test_processed_ids_are_bounded(self):
        self.go_live()
        for index in range(sd.MAX_PROCESSED_EVENTS + 50):
            self.director.handle_event(self.event("chat", f"chat-{index}", user_id="9", user_name="a", text="hello"))
        self.assertLessEqual(len(self.director.state["processed_events"]), sd.MAX_PROCESSED_EVENTS)


class RecoveryTests(DirectorCase):
    def test_restart_continues_the_running_session(self):
        self.go_live()
        self.director.mark_moment(VIEWER, "first")
        session_id = self.director.session["id"]
        restarted = self.make()
        self.assertEqual(restarted.session["id"], session_id)
        self.assertEqual(len(restarted.session["moments"]), 1)
        # Helix after the reconnect: same stream -> same session, viewers recorded.
        effects = restarted.reconcile(sd.LiveStream("s-1", "Title", "Elden Ring", START, viewers=12), followers_total=40)
        self.assertNotIn("session_started", self.kinds(effects))
        self.assertEqual(restarted.session["stats"]["peak_viewers"], 12)
        self.assertEqual(restarted.session["stats"]["followers_start"], 40)

    def test_offline_during_the_downtime_is_noticed_by_reconcile(self):
        self.go_live()
        self.clock.advance(1800)
        restarted = self.make()
        self.clock.advance(60)
        restarted.reconcile(None)
        self.assertEqual(restarted.session["status"], "ending")
        self.clock.advance(6 * 60)
        self.assertIn("session_ended", self.kinds(restarted.tick()))

    def test_missed_events_between_two_streams(self):
        self.go_live("s-1")
        self.clock.advance(3 * 3600)  # bot was offline: the stream ended and a new one began
        effects = self.director.reconcile(sd.LiveStream("s-2", "New", "", self.clock() - 60))
        self.assertEqual([kind for kind in self.kinds(effects) if kind in ("session_ended", "session_started")], ["session_ended", "session_started"])
        self.assertEqual(self.director.session["stream_ids"], ["s-2"])

    def test_live_without_a_session_starts_one_from_helix(self):
        effects = self.director.reconcile(sd.LiveStream("s-5", "Late start", "Minecraft", START - 600, viewers=3))
        self.assertIn("session_started", self.kinds(effects))
        self.assertEqual(self.director.session["started_at"], START - 600)
        self.assertEqual(self.director.session["categories"][0]["name"], "Minecraft")

    def test_stale_session_is_closed_after_half_a_day_without_twitch(self):
        self.go_live()
        self.clock.advance(sd.STALE_SESSION_SECONDS + 60)
        self.assertIn("session_ended", self.kinds(self.director.tick()))


class MomentTests(DirectorCase):
    def test_moments_need_a_live_stream_and_respect_cooldowns(self):
        self.assertFalse(self.director.mark_moment(VIEWER).ok)
        self.go_live()
        self.clock.advance(600)
        outcome = self.director.mark_moment(VIEWER, "  huge\tplay \x00 ")
        self.assertTrue(outcome.ok)
        moment = self.director.session["moments"][0]
        self.assertEqual(moment["offset"], 600 - 15)  # shifted back by the reaction time
        self.assertEqual(moment["comment"], "huge play")
        self.assertIn("0:09:45", outcome.text)
        self.assertFalse(self.director.mark_moment(VIEWER).ok)  # cooldown
        self.clock.advance(31)
        self.assertTrue(self.director.mark_moment(VIEWER).ok)

    def test_close_marks_become_one_notable_moment(self):
        self.go_live()
        self.clock.advance(1000)
        self.director.mark_moment(VIEWER, "clutch")
        self.clock.advance(10)
        outcome = self.director.mark_moment(VIEWER2, "Clutch")
        self.assertIn("2 people", outcome.text)
        self.clock.advance(20)
        self.director.mark_moment(VIEWER3)
        self.clock.advance(1000)
        self.director.mark_moment(VIEWER2, "lonely")
        clusters = sd.moment_clusters(self.director.session["moments"], 90, 2)
        self.assertEqual(len(clusters), 2)
        self.assertEqual((clusters[0]["users"], clusters[0]["notable"], clusters[0]["comments"]), (3, True, ["clutch"]))
        self.assertFalse(clusters[1]["notable"])

    def test_team_marks_are_notable_and_recap_links_to_the_vod(self):
        self.go_live()
        self.clock.advance(3725)
        self.director.mark_moment(TEAM, "boss down")
        self.clock.advance(600)
        effects = self.director.manual_end(TEAM).effects
        session_id = next(effect.ref for effect in effects if effect.kind == "session_ended")
        self.director.attach_vod(session_id, "999", "https://www.twitch.tv/videos/999")
        session = self.director.find_session(session_id)
        lines = sd.recap_lines(session["recap"], session["vod"])
        moment_line = lines["Moments (1 marks)"][0]
        self.assertIn("1:01:50", moment_line)
        self.assertIn("https://www.twitch.tv/videos/999?t=01h01m50s", moment_line)


class ChallengeTests(DirectorCase):
    def test_suggest_support_and_decide(self):
        self.go_live()
        outcome = self.director.suggest_challenge(VIEWER, "Win with only a pistol")
        self.assertTrue(outcome.ok)
        self.assertIn("challenge_post", self.kinds(outcome.effects))
        challenge_id = outcome.effects[0].ref
        # The same idea again becomes support; a second support is refused.
        duplicate = self.director.suggest_challenge(VIEWER2, "win with ONLY a pistol!")
        self.assertIn("already suggested", duplicate.text)
        self.assertFalse(self.director.support_challenge(VIEWER2, challenge_id).ok)
        self.assertEqual(len(self.director.state["challenges"][challenge_id]["supporters"]), 2)
        # Viewers cannot decide.
        self.assertFalse(self.director.decide_challenge(VIEWER, challenge_id, "accept").ok)
        self.assertTrue(self.director.decide_challenge(TEAM, challenge_id, "accept").ok)
        self.assertFalse(self.director.decide_challenge(TEAM, challenge_id, "accept").ok)
        before = self.director.state["community"]["points"]
        done = self.director.decide_challenge(TEAM, challenge_id, "complete")
        self.assertIn("thread_post", self.kinds(done.effects))
        self.assertEqual(self.director.state["community"]["points"] - before, sd.POINTS["challenge_completed"])
        self.assertEqual(self.director.state["community"]["counters"]["total"]["challenges_completed"], 1)

    def test_limits_and_carry_over_to_the_next_stream(self):
        director = self.make(max_open_challenges_per_user=2, suggestion_cooldown_seconds=0)
        self.assertTrue(director.suggest_challenge(VIEWER, "first idea").ok)  # between streams: inbox only
        self.assertTrue(director.suggest_challenge(VIEWER, "second idea").ok)
        self.assertFalse(director.suggest_challenge(VIEWER, "third idea").ok)
        accepted = next(item["id"] for item in director.state["challenges"].values() if item["text"] == "second idea")
        director.decide_challenge(TEAM, accepted, "accept")
        effects = self.go_live(director=director)
        digest = next(effect for effect in effects if effect.kind == "challenges_digest")
        self.assertEqual(digest.data["ids"][0], accepted)  # accepted first, then waiting ones
        self.assertEqual(len(digest.data["ids"]), 2)

    def test_cooldown_between_suggestions(self):
        self.assertTrue(self.director.suggest_challenge(VIEWER, "one").ok)
        self.assertFalse(self.director.suggest_challenge(VIEWER, "two").ok)
        self.clock.advance(61)
        self.assertTrue(self.director.suggest_challenge(VIEWER, "two").ok)


class PollTests(DirectorCase):
    def test_poll_votes_close_and_score(self):
        self.go_live()
        self.assertFalse(self.director.create_poll(VIEWER, "poll", "Next map?", ["A", "B"]).ok)
        self.assertFalse(self.director.create_poll(TEAM, "poll", "Next map?", ["A", "a"]).ok)  # duplicate options
        outcome = self.director.create_poll(TEAM, "poll", "Next map?", ["Dust", "Mirage", "Inferno"], minutes=2)
        poll_id = outcome.effects[0].ref
        self.assertTrue(self.director.vote(VIEWER, poll_id, 0).ok)
        changed = self.director.vote(VIEWER, poll_id, 1)
        self.assertIn("changed to", changed.text)
        self.director.vote(VIEWER2, poll_id, 1)
        self.assertFalse(self.director.vote(VIEWER2, poll_id, 9).ok)
        self.clock.advance(121)
        effects = self.director.tick()
        self.assertIn("poll_update", self.kinds(effects))
        post = next(effect for effect in effects if effect.kind == "thread_post")
        self.assertIn("Mirage (2 of 2)", sd.plain(post.text))
        self.assertFalse(self.director.vote(VIEWER3, poll_id, 0).ok)
        self.assertEqual(self.director.state["community"]["counters"]["total"]["polls"], 1)

    def test_prediction_locks_and_resolves_without_stakes(self):
        self.go_live()
        poll_id = self.director.create_poll(TEAM, "prediction", "Will he beat the boss?", ["Yes", "No"], minutes=1).effects[0].ref
        self.director.vote(VIEWER, poll_id, 0)
        self.director.vote(VIEWER2, poll_id, 1)
        self.clock.advance(61)
        self.director.tick()
        self.assertEqual(self.director.state["polls"][poll_id]["status"], "locked")
        self.assertFalse(self.director.vote(VIEWER3, poll_id, 0).ok)
        self.assertFalse(self.director.resolve_prediction(VIEWER, poll_id, 0).ok)
        outcome = self.director.resolve_prediction(TEAM, poll_id, 0)
        self.assertTrue(outcome.ok)
        result = self.director.poll_result(self.director.state["polls"][poll_id])
        self.assertEqual((result["correct"], result["summary"]), (1, "1 of 2 predicted it"))
        state_text = json.dumps(self.director.state).lower()
        for word in ("bet", "wager", "currency", "balance"):
            self.assertNotIn(f'"{word}', state_text)


class InboxTests(DirectorCase):
    def test_one_queue_for_everything(self):
        director = self.make(suggestion_cooldown_seconds=0)
        self.assertTrue(director.suggest(VIEWER, "question", "What is your sensitivity?").ok)
        again = director.suggest(VIEWER2, "question", "what is your SENSITIVITY")
        self.assertIn("vote was added", again.text)
        self.assertFalse(director.suggest(VIEWER, "clip", "funny").ok)  # clip without link
        self.assertFalse(director.suggest(VIEWER, "clip", "x", link="javascript:alert(1)").ok)
        self.assertTrue(director.suggest(VIEWER, "clip", "funny", link="https://clips.twitch.tv/abc").ok)
        director.suggest_challenge(VIEWER3, "No jumping for 10 minutes")
        items = director.inbox_items()
        self.assertEqual(items[0]["supporters"], ["discord:100", "discord:101"])
        self.assertEqual({item["kind"] for item in items}, {"question", "clip", "challenge"})
        question_id = items[0]["id"]
        self.assertFalse(director.set_inbox_status(VIEWER, question_id, "done").ok)
        self.assertTrue(director.set_inbox_status(TEAM, question_id, "done").ok)
        challenge_id = next(item["id"] for item in items if item["kind"] == "challenge")
        self.assertTrue(director.set_inbox_status(TEAM, challenge_id, "done").ok)  # = accept
        self.assertEqual(director.state["challenges"][challenge_id]["status"], "accepted")
        self.assertEqual(director.inbox_counts(), {"clip": 1})

    def test_twitch_chat_commands(self):
        self.go_live()
        self.clock.advance(300)
        chat = lambda mid, text, team=False: self.director.handle_event(
            self.event("chat", mid, user_id="777", user_name="TwitchFan", text=text, team=team)
        )
        chat("c1", "hello everyone")
        chat("c2", "!moment what a shot")
        chat("c3", "!q how long is the stream?")
        chat("c4", "!game Hollow Knight")
        effects = chat("c5", "!challenge play blindfolded")
        self.assertIn("challenge_post", self.kinds(effects))
        session = self.director.session
        self.assertEqual(session["moments"][0]["user"], "twitch:777")
        self.assertEqual(session["stats"]["chat_messages"], 5)
        self.assertEqual(session["stats"]["chatters"], ["777"])
        self.assertEqual(self.director.inbox_counts(), {"question": 1, "game": 1, "challenge": 1})
        disabled = self.make(features={"twitch_chat": False})
        disabled.handle_event(self.event("chat", "c6", user_id="778", user_name="x", text="!moment"))
        self.assertEqual(len(disabled.session["moments"]), 1)


class ProgressionTests(DirectorCase):
    def test_levels(self):
        self.assertEqual(sd.level_for(0), (1, 0, 100))
        self.assertEqual(sd.level_for(99), (1, 0, 100))
        self.assertEqual(sd.level_for(100), (2, 100, 300))
        self.assertEqual(sd.level_for(299), (2, 100, 300))
        self.assertEqual(sd.level_for(300), (3, 300, 600))
        self.assertEqual(sd.level_for(10_000)[0], 14)

    def test_default_goals_and_weekly_reset(self):
        goals = {goal["metric"]: goal for goal in self.director.state["goals"].values()}
        self.assertEqual(set(goals), {"streams", "challenges_completed", "moments"})
        goal = goals["streams"]
        rewards = []
        for index in range(3):
            self.go_live(f"w1-{index}")
            self.clock.advance(1800)
            effects = self.director.manual_end(TEAM).effects
            rewards += [effect for effect in effects if effect.kind == "goal_completed"]
            self.clock.advance(3600)
        self.assertEqual(len(rewards), 1)
        self.assertIn("Stream 3 times", rewards[0].text)
        view = next(item for item in self.director.goals_view() if item["id"] == goal["id"])
        self.assertTrue(view["done"])
        self.clock.advance(8 * 86400)  # next week: the weekly goal starts again
        view = next(item for item in self.director.goals_view() if item["id"] == goal["id"])
        self.assertEqual((view["progress"], view["done"]), (0, False))

    def test_goal_management_is_team_only(self):
        self.assertFalse(self.director.add_goal(VIEWER, "x", "moments", 5, "week").ok)
        self.assertFalse(self.director.add_goal(TEAM, "x", "nonsense", 5, "week").ok)
        outcome = self.director.add_goal(TEAM, "", "raids", 2, "season")
        self.assertTrue(outcome.ok)
        goal_id = [goal["id"] for goal in self.director.goals_view()][-1]
        self.assertFalse(self.director.remove_goal(VIEWER, goal_id).ok)
        self.assertTrue(self.director.remove_goal(TEAM, goal_id).ok)

    def test_stream_points_and_progression_off(self):
        self.go_live()
        self.clock.advance(2 * 3600)
        self.director.manual_end(TEAM)
        recap = self.director.state["sessions"][-1]["recap"]
        self.assertEqual(recap["points"]["earned"], sd.POINTS["stream"] + 8)
        off = sd.Director(sds.StateStore(Path(self.temp.name) / "off"), config(features={"progression": False}), self.clock)
        off.manual_start(TEAM)
        self.clock.advance(3600)
        off.manual_end(TEAM)
        self.assertEqual(off.state["community"]["points"], 0)


class RecapAndNextStreamTests(DirectorCase):
    def test_recap_contents(self):
        director = self.make(suggestion_cooldown_seconds=0)
        director.reconcile(sd.LiveStream("s-1", "Road to Diamond", "Valorant", START, viewers=5), followers_total=100)
        self.clock.advance(1200)
        director.reconcile(sd.LiveStream("s-1", "Road to Diamond", "Valorant", START, viewers=9), followers_total=104)
        director.handle_event(self.event("update", "u1", title="Road to Diamond", category="Just Chatting"))
        director.handle_event(self.event("sub", "sub1", user_name="a"))
        director.handle_event(self.event("gift", "gift1", total=3))
        director.handle_event(self.event("raid", "raid1", from_name="Buddy", viewers=4))
        director.suggest(VIEWER, "game", "Hollow Knight")
        director.suggest(VIEWER2, "game", "Celeste")
        director.suggest(VIEWER3, "game", "hollow knight")
        self.clock.advance(1200)
        effects = director.manual_end(TEAM).effects
        self.assertIn("poll_post", self.kinds(effects))
        session = director.state["sessions"][-1]
        recap = session["recap"]
        self.assertEqual(recap["followers_gained"], 4)
        self.assertEqual((recap["subs"], recap["gift_subs"]), (1, 3))
        self.assertEqual([item["name"] for item in recap["categories"]], ["Valorant", "Just Chatting"])
        self.assertEqual(recap["viewers"], {"peak": 9, "average": 7})
        lines = sd.recap_lines(recap)
        self.assertIn("raid from Buddy (4)", lines["Stream"][-1])
        self.assertIn("Community", lines)
        poll_id = next(effect.ref for effect in effects if effect.kind == "poll_post")
        poll = director.state["polls"][poll_id]
        self.assertEqual(poll["options"][:2], ["Hollow Knight", "Celeste"])
        self.assertEqual(poll["target"], "channel")
        director.vote(VIEWER, poll_id, 1)
        self.clock.advance(sd.NEXT_STREAM_POLL_MINUTES * 60 + 1)
        director.tick()
        self.assertEqual(director.state["meta"]["next_stream_choice"]["leaders"], ["Celeste"])

    def test_status_summary_has_no_secrets_and_shows_the_inbox(self):
        self.director.suggest(VIEWER, "topic", "Talk about setups")
        summary = self.director.status_summary()
        self.assertEqual(summary["inbox"][0]["text"], "Talk about setups")
        self.assertNotIn("token", json.dumps(summary).lower())


class PersistenceTests(DirectorCase):
    def test_every_change_is_saved_atomically(self):
        self.go_live()
        self.director.mark_moment(VIEWER)
        on_disk = json.loads((self.data_dir / sds.STATE_FILE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(len(on_disk["active_session"]["moments"]), 1)
        self.assertEqual(list(self.data_dir.glob("*.tmp")), [])

    def test_corrupted_state_fails_closed_and_is_kept(self):
        self.go_live()
        path = self.data_dir / sds.STATE_FILE_NAME
        path.write_text("{ broken", encoding="utf-8")
        director = self.make()
        self.assertFalse(director.available)
        self.assertIn("restore stream_director_state.backup.json", director.problem)
        self.assertFalse(director.mark_moment(VIEWER).ok)
        self.assertFalse(director.suggest(VIEWER, "topic", "hello").ok)
        self.assertEqual(director.handle_event(self.event("online", "m-x", stream_id="s-x")), [])
        self.assertEqual(director.tick(), [])
        self.assertEqual(path.read_text(encoding="utf-8"), "{ broken")
        self.assertIn("problem", director.status_summary())
        self.assertIsNotNone(sds.StateStore(self.data_dir).verify())

    def test_wrong_shape_and_backup(self):
        path = self.data_dir / sds.STATE_FILE_NAME
        self.go_live()
        sds.StateStore(self.data_dir).load()  # a good load refreshes the backup
        backup = self.data_dir / sds.BACKUP_FILE_NAME
        self.assertTrue(backup.is_file())
        path.write_text(json.dumps({"version": 1, "challenges": []}), encoding="utf-8")
        self.assertFalse(self.make().available)
        backup_state = json.loads(backup.read_text(encoding="utf-8"))
        self.assertIsNotNone(backup_state["active_session"])
        snapshot, problem = sds.read_only_snapshot(self.data_dir)
        self.assertIsNone(snapshot)
        self.assertIn("invalid shape", problem)


class TextSafetyTests(DirectorCase):
    def test_markdown_and_mentions_are_shown_literally(self):
        for raw in ("[free nitro](https://evil.example)", "@everyone look", "<@123456789012345678>", "**bold** # title", "a\\b"):
            escaped = sd.md(raw)
            self.assertEqual(sd.plain(escaped), raw)
            self.assertNotIn("](", escaped.replace("\\](", ""))
            self.assertNotIn("@everyone", escaped)
            self.assertNotIn("<@1", escaped)

    def test_user_text_in_posts_is_escaped(self):
        self.go_live()
        challenge_id = self.director.suggest_challenge(VIEWER, "@everyone [click](https://x.example)").effects[0].ref
        self.director.decide_challenge(TEAM, challenge_id, "accept")
        post = next(effect for effect in self.director.decide_challenge(TEAM, challenge_id, "complete").effects if effect.kind == "thread_post")
        self.assertNotIn("@everyone", post.text)
        self.assertIn("\\[click\\]\\(https://x.example\\)", post.text)
        self.assertEqual(sd.clean_text("a‮b\nc", 10), "a b c")


if __name__ == "__main__":
    unittest.main()
