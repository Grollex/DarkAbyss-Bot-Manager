"""Game Presence: engine, policy, persistence, Discord adapter, the dedicated
Game Presence bot type, Admin separation, Manager page."""

import asyncio
import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
import unittest.mock
from dataclasses import dataclass, replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"

# Never touch the real user data folder: modules imported by these tests
# resolve app_paths from DARKABYSS_DATA_DIR (AI storage, instances, migration).
import os as _os  # noqa: E402
import tempfile as _tempfile  # noqa: E402

if not _os.environ.get("DARKABYSS_DATA_DIR"):
    _os.environ["DARKABYSS_DATA_DIR"] = _tempfile.mkdtemp(prefix="darkabyss-test-")
sys.path.insert(0, str(CORE_ROOT))

import discord  # noqa: E402

gp = importlib.import_module("game_presence")
gpd = importlib.import_module("game_presence_discord")


def presence_state_store(path):
    """The Game Presence bot's own state store (data/game_presence_state.json)."""
    return importlib.import_module("GamePresence").PresenceStateStore(path)

GUILD = 1
CHANNEL = 2
A, B, C, D = 101, 102, 103, 104
OW = "Overwatch 2"


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def config(**changes):
    raw = {"enabled": True, "guild_id": str(GUILD), "channel_id": str(CHANNEL)}
    raw.update(changes)
    return gp.parse_config(raw)


def make_engine(clock=None, store=None, **changes):
    clock = clock or Clock()
    engine = gp.GamePresenceEngine(store or gp.MemoryPresenceStore(), clock)
    engine.configure(config(**changes))
    return engine, clock


def play(engine, user, game=OW, voice=None, hint=None):
    engine.observe(GUILD, user, game, voice, hint)


def run_until(engine, clock, seconds, step=15):
    """Tick like the bot loop; returns every suggestion produced."""
    produced = []
    end = clock.now + seconds
    while clock.now < end:
        clock.advance(min(step, end - clock.now))
        for suggestion in engine.tick():
            produced.append(suggestion)
            engine.mark_sent(suggestion)
    return produced


class EngineScenarioTests(unittest.TestCase):
    def test_two_players_get_one_suggestion_only_after_the_delay(self):
        engine, clock = make_engine()
        play(engine, A)
        play(engine, B)
        self.assertEqual(engine.tick(), [])  # never right after launch
        self.assertEqual(run_until(engine, clock, 165), [])  # before the delay
        produced = run_until(engine, clock, 30)
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].target_user_ids, (A, B))
        self.assertEqual(produced[0].kind, "gather")
        self.assertEqual(run_until(engine, clock, 3600), [])  # no repeats

    def test_player_leaving_or_switching_game_before_recheck_cancels(self):
        for change in ("leave", "switch"):
            with self.subTest(change=change):
                engine, clock = make_engine()
                play(engine, A)
                play(engine, B)
                engine.tick()
                run_until(engine, clock, 100)
                play(engine, B, None if change == "leave" else "Minecraft")
                self.assertEqual(run_until(engine, clock, 600), [])

    def test_members_together_in_one_voice_get_nothing(self):
        engine, clock = make_engine()
        play(engine, A, voice=500)
        play(engine, B, voice=500)
        engine.tick()
        self.assertEqual(run_until(engine, clock, 600), [])
        engine.set_voice(GUILD, B, None)  # B leaves the voice channel
        engine.set_voice(GUILD, A, 501)
        produced = run_until(engine, clock, 400)
        self.assertEqual(len(produced), 1)

    def test_third_player_during_waiting_is_aggregated_into_one_message(self):
        engine, clock = make_engine()
        play(engine, A)
        play(engine, B)
        engine.tick()
        run_until(engine, clock, 60)
        play(engine, C)
        produced = run_until(engine, clock, 600)
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].target_user_ids, (A, B, C))
        text = gpd.render_message(produced[0], None)
        self.assertIn("вас уже трое", text)

    def test_membership_changes_do_not_spam(self):
        engine, clock = make_engine()
        for user in (A, B, C):
            play(engine, user)
        engine.tick()
        self.assertEqual(len(run_until(engine, clock, 200)), 1)
        play(engine, C, None)
        play(engine, D)
        # D joins, C leaves: same game, A and B already suggested together.
        self.assertEqual(run_until(engine, clock, 3 * 3600 - 300), [])

    def test_voice_aware_join_and_disabled_voice_awareness(self):
        engine, clock = make_engine()
        play(engine, A, voice=500)
        play(engine, B, voice=500)
        play(engine, C)
        engine.tick()
        produced = run_until(engine, clock, 200)
        self.assertEqual(produced[0].kind, "join")
        self.assertEqual(produced[0].outsider_user_ids, (C,))
        self.assertEqual(produced[0].voice_member_ids, (A, B))
        text = gpd.render_message(produced[0], "Gaming")
        self.assertTrue(text.startswith(f"<@{C}>, <@{A}> и <@{B}> уже играют в **Overwatch 2** и сидят в **Gaming**"))

        engine, clock = make_engine(voice_aware=False)
        play(engine, A, voice=500)
        play(engine, B, voice=500)
        play(engine, C)
        engine.tick()
        self.assertEqual(run_until(engine, clock, 200)[0].kind, "gather")

    def test_muted_users_are_never_targets_and_too_few_targets_means_silence(self):
        engine, clock = make_engine()
        engine.preferences.set_muted(GUILD, A, True)
        for user in (A, B, C):
            play(engine, user)
        engine.tick()
        produced = run_until(engine, clock, 200)
        self.assertEqual(produced[0].target_user_ids, (B, C))

        engine, clock = make_engine()
        engine.preferences.set_muted(GUILD, A, True)
        play(engine, A)
        play(engine, B)
        engine.tick()
        self.assertEqual(run_until(engine, clock, 600), [])

    def test_cooldowns_group_user_and_guild(self):
        engine, clock = make_engine()
        play(engine, A)
        play(engine, B)
        engine.tick()
        self.assertEqual(len(run_until(engine, clock, 200)), 1)
        # Guild cooldown: another game group within 15 minutes waits.
        play(engine, C, "Dota 2")
        play(engine, D, "Dota 2")
        self.assertEqual(run_until(engine, clock, 10 * 60), [])
        later = run_until(engine, clock, 10 * 60)
        self.assertEqual([s.game_key for s in later], ["dota 2"])
        # Per-user cooldown: A with a new partner within 1 hour is not pinged.
        play(engine, C, OW)
        self.assertEqual(run_until(engine, clock, 20 * 60), [])
        # Group cooldown: A+B again within 3 hours stay silent even after user cooldowns.
        play(engine, C, None)
        play(engine, D, None)
        self.assertEqual(run_until(engine, clock, 2 * 3600), [])
        self.assertEqual(len(run_until(engine, clock, 3600)), 1)

    def test_restart_keeps_cooldowns_and_never_posts_immediately(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "game_presence_state.json"
        clock = Clock()
        engine, _ = make_engine(clock, gp.FeatureStorePresenceStore(presence_state_store(path)))
        play(engine, A)
        play(engine, B)
        engine.tick()
        self.assertEqual(len(run_until(engine, clock, 200)), 1)

        # New process: fresh store object reading the same file, fresh trackers.
        restarted, _ = make_engine(clock, gp.FeatureStorePresenceStore(presence_state_store(path)))
        old_start = clock.now - 3600
        play(restarted, A, hint=old_start)
        play(restarted, B, hint=old_start)
        self.assertEqual(restarted.tick(), [])  # long-running sessions, but fresh pending delay
        self.assertEqual(run_until(restarted, clock, 3600), [])  # recent suggestion remembered

    def test_quick_restart_keeps_the_session_start(self):
        sessions = gp.SessionTracker(restart_grace=120)
        self.assertEqual(sessions.start(GUILD, A, "ow", 100.0), 100.0)
        sessions.end(GUILD, A, "ow", 160.0)
        self.assertEqual(sessions.start(GUILD, A, "ow", 200.0), 100.0)
        sessions.end(GUILD, A, "ow", 210.0)
        self.assertEqual(sessions.start(GUILD, A, "ow", 500.0), 500.0)

    def test_member_leaving_the_server_is_cleaned_up(self):
        engine, clock = make_engine()
        play(engine, A)
        play(engine, B)
        engine.tick()
        engine.remove_member(GUILD, B)
        self.assertEqual(run_until(engine, clock, 600), [])
        self.assertEqual(engine.status()["tracked_players"], 1)

    def test_other_guilds_and_disabled_module_are_ignored(self):
        engine, clock = make_engine()
        engine.observe(999, A, OW, None)
        engine.observe(999, B, OW, None)
        self.assertEqual(run_until(engine, clock, 600), [])
        engine, clock = make_engine(enabled=False, guild_id=None, channel_id=None)
        play(engine, A)
        play(engine, B)
        self.assertEqual(run_until(engine, clock, 600), [])


class NormalizationAndConfigTests(unittest.TestCase):
    def test_game_normalization_and_aliases(self):
        normalizer = gp.GameNormalizer()
        first = normalizer.normalize("  Overwatch®  2 ")
        second = normalizer.normalize("overwatch 2")
        self.assertEqual(first.key, second.key)
        self.assertEqual(first.display_name, "Overwatch® 2")
        self.assertIsNone(normalizer.normalize("   "))
        aliased = gp.GameNormalizer({"Overwatch": "Overwatch 2"})
        self.assertEqual(aliased.normalize("OVERWATCH").key, "overwatch 2")

    def test_allowlist_and_ignore_list(self):
        engine, clock = make_engine(allowlist=["overwatch 2"])
        for user, game in ((A, OW), (B, OW), (C, "Dota 2"), (D, "Dota 2")):
            play(engine, user, game)
        engine.tick()
        produced = run_until(engine, clock, 3600)
        self.assertEqual({s.game_key for s in produced}, {"overwatch 2"})

        engine, clock = make_engine(ignore_list=["OVERWATCH 2"])
        play(engine, A)
        play(engine, B)
        engine.tick()
        self.assertEqual(run_until(engine, clock, 600), [])

    def test_config_defaults_and_fail_closed_validation(self):
        defaults = gp.normalize_config_dict(None)
        self.assertFalse(defaults["enabled"])
        self.assertEqual(defaults["delay_minutes"], 3)
        self.assertEqual(defaults["user_cooldown_minutes"], 60)
        self.assertEqual(defaults["group_cooldown_minutes"], 180)
        self.assertEqual(defaults["guild_cooldown_minutes"], 15)
        for bad in (
            {"enabled": "yes"},
            {"delay_minutes": 0},
            {"delay_minutes": True},
            {"guild_id": "abc"},
            {"allowlist": "Overwatch"},
            {"unknown": 1},
            {"enabled": True},  # no server/channel
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                gp.normalize_config_dict(bad)
        for bad in ({"enabled": "yes"}, {"delay_minutes": 0}, {"unknown": 1}, {"guild_id": "abc"}):
            with self.subTest(bot_config=bad), self.assertRaises(ValueError):
                gp.normalize_bot_config(bad)
        not_configured = gp.normalize_bot_config({"enabled": True})
        self.assertTrue(not_configured["enabled"])
        self.assertFalse(gp.is_configured(not_configured))
        self.assertFalse(gp.parse_bot_config({"enabled": True}).active)
        self.assertTrue(gp.parse_bot_config({"enabled": True, "guild_id": "1", "channel_id": "2"}).active)


@dataclass
class FakeResponse:
    sent: list

    async def send_message(self, content, **kwargs):
        self.sent.append((content, kwargs))


def click(custom_id, user_id, guild_id=GUILD):
    response = FakeResponse([])
    interaction = types.SimpleNamespace(
        data={"custom_id": custom_id},
        guild_id=guild_id,
        user=types.SimpleNamespace(id=user_id),
        response=response,
    )
    return interaction, response


class PreferenceButtonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "game_presence_state.json"

    def book(self):
        return gp.PreferenceBook(gp.FeatureStorePresenceStore(presence_state_store(self.path)))

    async def test_mute_and_allow_are_persistent_idempotent_ephemeral_and_personal(self):
        interaction, response = click(gpd.CUSTOM_ID_MUTE, A)
        self.assertTrue(await gpd.handle_preference_interaction(interaction, self.book()))
        self.assertEqual(response.sent[0][0], gpd.PREF_MUTED)
        self.assertIs(response.sent[0][1]["ephemeral"], True)
        self.assertTrue(self.book().is_muted(GUILD, A))  # persisted (new store object)
        self.assertFalse(self.book().is_muted(GUILD, B))  # nobody else changed

        interaction, response = click(gpd.CUSTOM_ID_MUTE, A)
        await gpd.handle_preference_interaction(interaction, self.book())
        self.assertEqual(response.sent[0][0], gpd.PREF_ALREADY_MUTED)

        interaction, response = click(gpd.CUSTOM_ID_ALLOW, A)
        await gpd.handle_preference_interaction(interaction, self.book())
        self.assertEqual(response.sent[0][0], gpd.PREF_ALLOWED)
        self.assertFalse(self.book().is_muted(GUILD, A))

        interaction, response = click(gpd.CUSTOM_ID_ALLOW, A)
        await gpd.handle_preference_interaction(interaction, self.book())
        self.assertEqual(response.sent[0][0], gpd.PREF_ALREADY_ALLOWED)

    async def test_actor_is_only_interaction_user_and_state_is_per_guild(self):
        interaction, _ = click(gpd.CUSTOM_ID_MUTE, B)
        interaction.message = types.SimpleNamespace(content=f"<@{A}> mute me", mentions=[A])
        await gpd.handle_preference_interaction(interaction, self.book())
        self.assertTrue(self.book().is_muted(GUILD, B))
        self.assertFalse(self.book().is_muted(GUILD, A))
        self.assertFalse(self.book().is_muted(GUILD + 1, B))

    async def test_foreign_buttons_are_ignored(self):
        interaction, response = click("dab:rm:abc:1", A)
        self.assertFalse(await gpd.handle_preference_interaction(interaction, self.book()))
        self.assertEqual(response.sent, [])


class FakeTextChannel:
    def __init__(self, channel_id, name):
        self.id = channel_id
        self.name = name
        self.sent = []

    async def send(self, content, **kwargs):
        self.sent.append((content, kwargs))
        return types.SimpleNamespace(id=777)


class FakeGuild:
    def __init__(self):
        self.id = GUILD
        self.name = "Null"
        self.text = FakeTextChannel(CHANNEL, "games")
        self.voice = types.SimpleNamespace(id=500, name="Gaming")
        self.members = []

    def get_channel(self, channel_id):
        return {CHANNEL: self.text, 500: self.voice}.get(channel_id)


def member(guild, user_id, game=None, voice=False):
    activities = [discord.Game(name=game)] if game else []
    return types.SimpleNamespace(
        id=user_id,
        guild=guild,
        bot=False,
        activities=activities,
        voice=types.SimpleNamespace(channel=guild.voice) if voice else None,
    )


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def make_runtime(self, rewriter=None, **changes):
        guild = FakeGuild()
        client = types.SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == GUILD else None)
        clock = Clock()
        engine = gp.GamePresenceEngine(gp.MemoryPresenceStore(), clock)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = gpd.GamePresenceRuntime(client, engine, rewriter=rewriter, runtime_dir=Path(temp.name), presence_intent=True)
        runtime.apply_config(config(**changes))
        return runtime, guild, clock

    async def post_after_delay(self, runtime, guild, clock):
        guild.members = [member(guild, A, OW), member(guild, B, OW), member(guild, 999, None)]
        runtime.seed()
        await runtime.tick()
        clock.advance(200)
        return await runtime.tick()

    async def test_public_suggestion_with_buttons_and_only_approved_mentions(self):
        runtime, guild, clock = self.make_runtime()
        self.assertEqual(await self.post_after_delay(runtime, guild, clock), 1)
        content, kwargs = guild.text.sent[0]
        self.assertEqual(content, f"<@{A}> <@{B}>, вы оба уже несколько минут в **Overwatch 2** и не в одном войсе. Может, соберётесь?")
        self.assertNotIn("ephemeral", kwargs)
        mentions = kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.roles)
        self.assertEqual(sorted(user.id for user in mentions.users), [A, B])
        labels = [item.label for item in kwargs["view"].children]
        self.assertEqual(labels, ["Mute pings", "Allow pings"])
        self.assertEqual([item.custom_id for item in kwargs["view"].children], [gpd.CUSTOM_ID_MUTE, gpd.CUSTOM_ID_ALLOW])
        status = json.loads((runtime.runtime_dir / gpd.STATUS_FILE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(status["channel_name"], "games")
        self.assertEqual(status["last_suggestion"]["users"], [str(A), str(B)])

    async def test_mass_mentions_in_names_are_neutralized(self):
        runtime, guild, clock = self.make_runtime()
        guild.members = [member(guild, A, "@everyone <@&5> raid"), member(guild, B, "@everyone <@&5> raid")]
        runtime.seed()
        await runtime.tick()
        clock.advance(200)
        await runtime.tick()
        content, kwargs = guild.text.sent[0]
        self.assertNotIn("@everyone", content)
        self.assertNotIn("<@&", content)
        self.assertFalse(kwargs["allowed_mentions"].everyone)

    async def test_ai_rewrite_cannot_change_targets_and_failures_fall_back(self):
        calls = []

        async def sneaky(template, suggestion):
            calls.append(template)
            return "{targets} и <@999>, @here го в {game}!"

        runtime, guild, clock = self.make_runtime(rewriter=sneaky, ai_rewrite=True)
        await self.post_after_delay(runtime, guild, clock)
        content, kwargs = guild.text.sent[0]
        self.assertEqual(len(calls), 1)
        self.assertNotIn("<@999>", content)  # rejected -> deterministic template
        self.assertIn("вы оба уже несколько минут", content)
        self.assertEqual(sorted(user.id for user in kwargs["allowed_mentions"].users), [A, B])

        async def nice(template, suggestion):
            return gpd.validate_rewrite("{targets}, ну что, в {game} вместе?", template)

        runtime, guild, clock = self.make_runtime(rewriter=nice, ai_rewrite=True)
        await self.post_after_delay(runtime, guild, clock)
        self.assertEqual(guild.text.sent[0][0], f"<@{A}> <@{B}>, ну что, в **Overwatch 2** вместе?")

        async def broken(template, suggestion):
            raise RuntimeError("provider down")

        runtime, guild, clock = self.make_runtime(rewriter=broken, ai_rewrite=True)
        self.assertEqual(await self.post_after_delay(runtime, guild, clock), 1)
        self.assertIn("вы оба уже несколько минут", guild.text.sent[0][0])

    def test_validate_rewrite_rules(self):
        template = gpd.TEMPLATE_PAIR
        self.assertIsNone(gpd.validate_rewrite("{targets} {targets} {game}", template))
        self.assertIsNone(gpd.validate_rewrite("{targets} {game} {admins}", template))
        self.assertIsNone(gpd.validate_rewrite("{game} {targets}", template))
        self.assertIsNone(gpd.validate_rewrite("{targets} @everyone {game}", template))
        self.assertIsNone(gpd.validate_rewrite("{targets} https://x.y {game}", template))
        self.assertEqual(gpd.validate_rewrite(" {targets}, в {game}? ", template), "{targets}, в {game}?")

    def test_status_survives_unreadable_engine_state(self):
        class BrokenStore:
            def load(self, guild_id):
                raise RuntimeError("cannot read")

            def save(self, guild_id, state):
                raise RuntimeError("cannot write")

        guild = FakeGuild()
        client = types.SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == GUILD else None)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = gpd.GamePresenceRuntime(client, gp.GamePresenceEngine(BrokenStore(), Clock()), runtime_dir=Path(temp.name), presence_intent=True)
        runtime.apply_config(config())
        status = runtime.status()
        self.assertIn("state is unavailable (RuntimeError)", status["problem"])
        runtime.problem = "explicit reason"
        self.assertEqual(runtime.status()["problem"], "explicit reason")
        runtime.write_status()
        self.assertTrue((Path(temp.name) / gpd.STATUS_FILE_NAME).is_file())

    async def test_missing_channel_or_intent_is_reported_not_crashed(self):
        runtime, guild, clock = self.make_runtime(channel_id="424242")
        self.assertEqual(await self.post_after_delay(runtime, guild, clock), 0)
        self.assertIn("channel was not found", runtime.problem)
        runtime.presence_intent = False
        await runtime.tick()
        self.assertIn("Presence Intent", runtime.problem)


def import_admin_module():
    try:
        import msvcrt  # noqa: F401
    except ImportError:
        sys.modules["msvcrt"] = types.SimpleNamespace(LK_NBLCK=2, locking=lambda *args: None)
    sys.modules.pop("Admin", None)
    return importlib.import_module("Admin")


PATH_MODULES = (
    "GamePresence",
    "Admin",
    "admin_instance",
    "ai_storage",
    "ai_connections",
    "config_store",
    "instance_store",
    "bot_registry",
    "app_paths",
)


def load_with_data_root(data_root, *names):
    """Fresh path-dependent modules bound to a temporary DATA_ROOT."""
    os.environ["DARKABYSS_DATA_DIR"] = str(data_root)
    for name in PATH_MODULES:
        sys.modules.pop(name, None)
    return [importlib.import_module(name) for name in names]


class AdminSeparationTests(unittest.TestCase):
    """The Admin bot no longer knows anything about Game Presence."""

    def base(self, **extra):
        return {
            "allow_server_administrators": True,
            "allowed_user_ids": [],
            "allowed_role_ids": [],
            "audit_channel_id": None,
            **extra,
        }

    def run_main(self, admin, config_dict, side_effect=None):
        seen = {}

        def fake_run(token):
            seen["presences"] = admin.bot.intents.presences
            seen["members"] = admin.bot.intents.members
            seen["message_content"] = admin.bot.intents.message_content
            if side_effect is not None:
                raise side_effect

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = types.SimpleNamespace(
            instance_id="admin-main", config_path=Path("x"), token_path=Path("y"), lock_path=Path(temp.name) / "admin_bot.lock"
        )
        patches = {
            "resolve_runtime": lambda instance_id: runtime,
            "load_config": lambda runtime=None: admin.validate_config(json.loads(json.dumps(config_dict))),
            "load_token": lambda runtime=None: "not-a-real-token",
            "acquire_single_instance_lock": lambda runtime=None: True,
            "resolve_ai_stores": lambda runtime=None: None,
        }
        originals = {name: getattr(admin, name) for name in patches}
        original_run = admin.bot.run
        for name, value in patches.items():
            setattr(admin, name, value)
        admin.bot.run = fake_run
        try:
            code = admin.main(["--instance", "admin-main"])
        finally:
            for name, value in originals.items():
                setattr(admin, name, value)
            admin.bot.run = original_run
        return code, seen

    def test_admin_source_has_no_game_presence_wiring(self):
        source = (CORE_ROOT / "Admin.py").read_text(encoding="utf-8")
        for needle in ("game_presence", "GamePresence", "presences", "on_presence_update", "Presence Intent"):
            self.assertNotIn(needle, source)
        admin = import_admin_module()
        self.assertFalse(hasattr(admin, "game_presence_runtime"))
        self.assertFalse(hasattr(admin, "game_presence_loop"))
        self.assertNotIn("on_presence_update", getattr(admin.bot, "extra_events", {}))
        self.assertFalse(admin.bot.intents.presences)

    def test_admin_defaults_and_schema_have_no_game_presence(self):
        defaults = json.loads((CORE_ROOT / "defaults" / "admin_config.json").read_text(encoding="utf-8"))
        schema = json.loads((PROJECT_ROOT / "bots" / "admin" / "config.schema.json").read_text(encoding="utf-8"))
        self.assertNotIn("game_presence", defaults)
        self.assertNotIn("game_presence", schema["properties"])

    def test_legacy_enabled_section_starts_admin_without_presence(self):
        admin = import_admin_module()
        legacy = {"enabled": True, "guild_id": "1", "channel_id": "2", "delay_minutes": 5}
        validated = admin.validate_config(self.base(game_presence=dict(legacy)))
        self.assertEqual(validated["game_presence"], legacy)  # kept as-is (not deleted), just ignored
        code, seen = self.run_main(admin, self.base(game_presence=legacy))
        self.assertEqual(code, 0)
        self.assertFalse(seen["presences"])
        self.assertTrue(seen["members"])
        self.assertFalse(seen["message_content"])

    def test_even_broken_legacy_section_does_not_stop_admin(self):
        admin = import_admin_module()
        code, seen = self.run_main(admin, self.base(game_presence={"enabled": True, "delay_minutes": "soon"}))
        self.assertEqual(code, 0)
        self.assertFalse(seen["presences"])

    def test_legacy_section_passes_admin_config_store_validation(self):
        with tempfile.TemporaryDirectory() as data_dir:
            instance_store, config_store = load_with_data_root(data_dir, "instance_store", "config_store")
            instance = instance_store.create_instance("admin", "admin-old")
            instance.paths.config.write_text(json.dumps({"game_presence": {"enabled": True, "guild_id": "1", "channel_id": "2"}}), encoding="utf-8")
            effective = config_store.load_effective_config("admin-old")
            self.assertTrue(effective["game_presence"]["enabled"])  # preserved for the Manager import
            self.assertEqual(instance_store.list_instances()[0].bot_type, "admin")  # no GP instance auto-created


class GamePresenceBotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data_root = Path(temp.name)
        (
            self.G,
            self.instance_store,
            self.config_store,
            self.bot_registry,
        ) = load_with_data_root(self.data_root, "GamePresence", "instance_store", "config_store", "bot_registry")
        self.instance = self.instance_store.create_instance("game_presence", "gp-main", "Games")

    def configure(self, **overrides):
        self.config_store.save_config_overrides("gp-main", overrides)

    def bot(self):
        return self.G.GamePresenceBot(self.G.resolve_runtime("gp-main"))

    def test_registry_discovers_both_bot_types(self):
        types_ = self.bot_registry.discover_bot_types()
        self.assertIn("admin", types_)
        self.assertIn("game_presence", types_)
        gp_type = types_["game_presence"]
        self.assertEqual(gp_type.entrypoint.name, "GamePresence.py")
        self.assertEqual(gp_type.display_name, "Game Presence Bot")
        self.assertNotEqual(gp_type.entrypoint, types_["admin"].entrypoint)

    def test_own_token_config_runtime_logs_data(self):
        admin = self.instance_store.create_instance("admin", "admin-main")
        self.assertNotEqual(admin.paths.token, self.instance.paths.token)
        self.assertNotEqual(admin.paths.data_dir, self.instance.paths.data_dir)
        self.assertEqual(self.instance.bot_type, "game_presence")
        runtime = self.G.resolve_runtime("gp-main")
        self.assertEqual(runtime.token_path, self.instance.paths.token)
        self.assertTrue(str(runtime.lock_path).startswith(str(self.instance.paths.runtime_dir)))
        with self.assertRaises(RuntimeError):
            self.G.resolve_runtime("admin-main")  # an Admin instance can never run as Game Presence

    def test_intents_are_only_what_game_presence_needs(self):
        intents = self.bot().intents
        self.assertTrue(intents.guilds)
        self.assertTrue(intents.members)
        self.assertTrue(intents.presences)
        self.assertTrue(intents.voice_states)
        self.assertFalse(intents.message_content)
        self.assertFalse(intents.messages)

    def test_fresh_instance_is_not_configured_and_posts_nothing(self):
        config, problem = self.G.load_settings("gp-main")
        self.assertFalse(config.active)
        self.assertEqual(problem, gp.NOT_CONFIGURED_TEXT)
        self.configure(guild_id="1", channel_id="2")
        config, problem = self.G.load_settings("gp-main")
        self.assertTrue(config.active)
        self.assertIsNone(problem)
        self.configure(guild_id="1", channel_id="2", enabled=False)  # pause
        config, problem = self.G.load_settings("gp-main")
        self.assertFalse(config.active)

    def test_corrupted_or_invalid_config_fails_closed(self):
        for raw in (b"{not json", b'{"delay_minutes": 0}', b'{"unknown": 1}', b'{"guild_id": "abc", "channel_id": "2"}'):
            with self.subTest(raw=raw):
                self.instance.paths.config.write_bytes(raw)
                config, problem = self.G.load_settings("gp-main")
                self.assertFalse(config.active)
                self.assertFalse(config.enabled)
                self.assertTrue(problem)

    async def test_live_config_reload_and_problem_in_status(self):
        bot = self.bot()
        self.assertEqual(bot.presence_runtime.config_problem, gp.NOT_CONFIGURED_TEXT)
        self.configure(guild_id="1", channel_id="2", delay_minutes=7)
        await bot.run_tick()
        self.assertEqual(bot.presence_runtime.engine.config.delay_seconds, 420.0)
        self.assertIsNone(bot.presence_runtime.config_problem)
        self.instance.paths.config.write_bytes(b"{broken")
        await bot.run_tick()
        self.assertFalse(bot.presence_runtime.engine.config.active)
        status = json.loads((self.instance.paths.runtime_dir / gpd.STATUS_FILE_NAME).read_text(encoding="utf-8"))
        self.assertIn("Invalid Game Presence config", status["problem"])

    async def test_buttons_use_this_instance_state_only(self):
        admin = self.instance_store.create_instance("admin", "admin-main")
        bot = self.bot()
        interaction, response = click(gpd.CUSTOM_ID_MUTE, A)
        interaction.type = discord.InteractionType.component
        await bot.on_interaction(interaction)
        self.assertEqual(response.sent[0][0], gpd.PREF_MUTED)
        self.assertIs(response.sent[0][1]["ephemeral"], True)
        state_file = self.instance.paths.data_dir / self.G.STATE_FILE_NAME
        self.assertTrue(state_file.is_file())
        self.assertTrue(self.bot().preferences.is_muted(GUILD, A))  # persistent across restarts
        self.assertFalse(self.bot().preferences.is_muted(GUILD, B))
        self.assertFalse((admin.paths.data_dir / "admin_features.json").exists())

    async def test_corrupted_state_stops_posting_and_buttons(self):
        state_file = self.instance.paths.data_dir / self.G.STATE_FILE_NAME
        state_file.write_text("{oops", encoding="utf-8")
        self.configure(guild_id=str(GUILD), channel_id=str(CHANNEL))
        bot = self.bot()
        guild = FakeGuild()
        bot.presence_runtime.client = types.SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == GUILD else None)
        bot.presence_runtime.engine.clock = Clock()
        guild.members = [member(guild, A, OW), member(guild, B, OW)]
        bot.presence_runtime.seed()
        await bot.run_tick()
        bot.presence_runtime.engine.clock.advance(600)
        await bot.run_tick()
        self.assertEqual(guild.text.sent, [])
        self.assertIn("state file", bot.presence_runtime.problem)
        self.assertEqual(state_file.read_text(encoding="utf-8"), "{oops")  # never reset to "everyone allowed"
        interaction, response = click(gpd.CUSTOM_ID_MUTE, A)
        interaction.type = discord.InteractionType.component
        await bot.on_interaction(interaction)
        self.assertEqual(response.sent[0][0], gpd.PREF_UNAVAILABLE)
        # The Manager reads the reason from the status file (written although
        # the engine status itself cannot be read).
        status = json.loads((self.instance.paths.runtime_dir / gpd.STATUS_FILE_NAME).read_text(encoding="utf-8"))
        self.assertIn("game_presence_state.json is unreadable", status["problem"])
        self.assertIn("Posting is paused", status["problem"])
        self.assertEqual(status["tracked_players"], 0)

    async def test_corrupted_state_is_reported_on_ready_and_at_startup(self):
        state_file = self.instance.paths.data_dir / self.G.STATE_FILE_NAME
        state_file.write_text('["not", "an", "object"]', encoding="utf-8")
        bot = self.bot()
        self.assertIn("invalid shape", bot.state_problem())
        self.assertTrue(bot.publish_state_problem())
        status = json.loads((self.instance.paths.runtime_dir / gpd.STATUS_FILE_NAME).read_text(encoding="utf-8"))
        self.assertIn("invalid shape", status["problem"])

        self.instance.paths.token.write_text("not-a-real-token\n", encoding="utf-8")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()), unittest.mock.patch.object(
            self.G, "acquire_single_instance_lock", return_value=True
        ), unittest.mock.patch.object(self.G.GamePresenceBot, "run", return_value=None):
            self.assertEqual(self.G.main(["--instance", "gp-main"]), 0)
        self.assertIn("game_presence_state.json has an invalid shape", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    async def test_healthy_state_has_no_problem(self):
        bot = self.bot()
        self.assertIsNone(bot.state_problem())
        self.assertFalse(bot.publish_state_problem())

    async def test_third_player_aggregation_in_the_dedicated_bot(self):
        self.configure(guild_id=str(GUILD), channel_id=str(CHANNEL))
        bot = self.bot()
        guild = FakeGuild()
        clock = Clock()
        bot.presence_runtime.client = types.SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == GUILD else None)
        bot.presence_runtime.engine.clock = clock
        guild.members = [member(guild, A, OW), member(guild, B, OW)]
        bot.presence_runtime.seed()
        await bot.run_tick()
        clock.advance(60)
        bot.presence_runtime.on_presence_update(None, member(guild, C, OW))
        await bot.run_tick()
        clock.advance(200)
        await bot.run_tick()
        self.assertEqual(len(guild.text.sent), 1)
        content, kwargs = guild.text.sent[0]
        self.assertEqual(sorted(user.id for user in kwargs["allowed_mentions"].users), [A, B, C])
        self.assertFalse(kwargs["allowed_mentions"].everyone)
        self.assertFalse(kwargs["allowed_mentions"].roles)

    async def test_ai_wording_uses_this_instance_and_never_tools(self):
        captured = {}

        class FakeOrchestrator:
            async def orchestrate(self, request, **kwargs):
                captured["request"] = request
                captured["kwargs"] = kwargs
                return types.SimpleNamespace(status=types.SimpleNamespace(value="COMPLETED"), content="{targets}, го в {game}?")

        import ai_orchestrator

        request = ai_orchestrator.OrchestratorRequest(
            messages=(importlib.import_module("ai_platform").AIMessage(role="user", content="x"),),
            task_class="CREATIVE",
            allowed_tool_names=("send_message",),
        )
        ai = self.G.TextOnlyAI(FakeOrchestrator())
        await ai.orchestrate(request)
        self.assertEqual(captured["request"].allowed_tool_names, ())
        self.assertEqual(captured["kwargs"], {})  # no executor -> no tool can ever run

        instance_ai = self.G.InstanceAI("gp-main")
        built = instance_ai.get()
        settings_view, credentials = built._orchestrator._settings_store, built._orchestrator._credential_store
        # This bot's own choice (base set by default) over the shared connections.
        self.assertEqual(settings_view.selection.path, (self.instance.paths.data_dir / "ai_selection.json").resolve())
        self.assertEqual(credentials.root, (self.data_root / "secrets" / "ai_connections").resolve())
        self.assertEqual(built._orchestrator._usage_store.path, (self.instance.paths.data_dir / "ai_usage.json").resolve())

    def test_missing_token_is_actionable(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = self.G.main(["--instance", "gp-main"])
        self.assertEqual(code, 1)
        self.assertIn("Bot Setup", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_rejected_intents_give_actionable_diagnostic(self):
        self.instance.paths.token.write_text("not-a-real-token\n", encoding="utf-8")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()), unittest.mock.patch.object(
            self.G, "acquire_single_instance_lock", return_value=True
        ), unittest.mock.patch.object(self.G.GamePresenceBot, "run", side_effect=discord.PrivilegedIntentsRequired(None)):
            code = self.G.main(["--instance", "gp-main"])
        self.assertEqual(code, 1)
        text = errors.getvalue()
        self.assertIn("Presence Intent", text)
        self.assertIn("Server Members Intent", text)
        self.assertIn("Message Content Intent' is NOT needed", text)
        self.assertNotIn("Traceback", text)
        status = json.loads((self.instance.paths.runtime_dir / gpd.STATUS_FILE_NAME).read_text(encoding="utf-8"))
        self.assertIn("Presence Intent", status["problem"])

    def test_rejected_token_is_actionable(self):
        self.instance.paths.token.write_text("not-a-real-token\n", encoding="utf-8")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()), unittest.mock.patch.object(
            self.G, "acquire_single_instance_lock", return_value=True
        ), unittest.mock.patch.object(self.G.GamePresenceBot, "run", side_effect=discord.LoginFailure("bad")):
            code = self.G.main(["--instance", "gp-main"])
        self.assertEqual(code, 1)
        self.assertIn("Game Presence bot token", errors.getvalue())

    def test_app_entry_runs_game_presence_bot_type(self):
        sys.modules.pop("app_entry", None)
        app_entry = importlib.import_module("app_entry")
        calls = []
        args = app_entry.parse_args(["--bot-runner", "game_presence", "--instance", "gp-main"])
        self.assertEqual(app_entry.dispatch(args, game_presence_main=lambda argv: calls.append(argv) or 0), 0)
        self.assertEqual(calls, [["--instance", "gp-main"]])
        with self.assertRaises(app_entry.AppEntryError):
            app_entry.dispatch(app_entry.parse_args(["--bot-runner", "game_presence"]), game_presence_main=lambda argv: 0)
        spec = (PROJECT_ROOT / "packaging" / "DarkAbyssApp.spec").read_text(encoding="utf-8")
        self.assertIn("GamePresence.py", spec)
        self.assertIn('"GamePresence"', spec)


@unittest.skipIf(importlib.util.find_spec("PySide6") is None, "PySide6 not installed")
class ManagerPageTests(unittest.TestCase):
    def setUp(self):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([])
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data_root = Path(temp.name)
        os.environ["DARKABYSS_DATA_DIR"] = str(self.data_root)
        for name in ("manager_game_presence", "admin_terminal", *PATH_MODULES):
            sys.modules.pop(name, None)
        self.page_module = importlib.import_module("manager_game_presence")
        self.terminal = importlib.import_module("admin_terminal")
        self.logs = self.data_root / "instances" / "gp-main" / "logs"
        self.runtime = self.terminal.runtime_dir_for_logs(self.logs)
        self.terminal.write_bot_status(
            self.runtime,
            {"guilds": [{"id": "1", "name": "Null", "channels": [{"id": "2", "name": "games", "type": "text"}, {"id": "3", "name": "vc", "type": "voice"}]}]},
        )

    def make_page(self, effective=None, state="RUNNING", admin_overrides=None, extra_bots=()):
        info = types.SimpleNamespace(logs_dir=self.logs, state=state, bot_type="game_presence")
        admin_info = types.SimpleNamespace(logs_dir=self.data_root / "instances" / "admin-main" / "logs", state="STOPPED", bot_type="admin")
        saved = []
        restarted = []
        defaults = dict(gp.DEFAULT_CONFIG, enabled=True)

        class ConfigApi:
            def get_config_snapshot(self, instance_id):
                if instance_id == "admin-main":
                    overrides = dict(admin_overrides or {"allowed_user_ids": ["5"]})
                    return types.SimpleNamespace(effective=overrides, overrides=overrides)
                overrides = saved[-1][1] if saved else {}
                current = {**defaults, **(effective or {}), **overrides}
                return types.SimpleNamespace(effective=current, overrides=overrides)

            def save_config_overrides(self, instance_id, overrides):
                saved.append((instance_id, json.loads(json.dumps(overrides))))
                return overrides

        bots = [("admin-main", "Main · Admin", admin_info), ("gp-main", "Main · Games", info), *extra_bots]
        page = self.page_module.GamePresencePanel(lambda: list(bots), ConfigApi(), restarted.append)
        self.addCleanup(page.close)
        return page, saved, restarted

    def test_only_game_presence_bots_are_listed(self):
        page, _saved, _restarted = self.make_page()
        self.assertEqual([page.bot_combo.itemData(i) for i in range(page.bot_combo.count())], ["gp-main"])
        self.assertTrue(page.legacy_panel.isHidden())

    def test_no_game_presence_bot_yet(self):
        info = types.SimpleNamespace(logs_dir=self.logs, state="STOPPED", bot_type="admin")
        page = self.page_module.GamePresencePanel(lambda: [("admin-main", "Main", info)], types.SimpleNamespace(), lambda _id: None)
        self.addCleanup(page.close)
        self.assertEqual(page.status_title.text(), "No Game Presence bot")
        self.assertFalse(page.save_button.isEnabled())
        self.assertFalse(page.save())

    def test_load_defaults_then_save_settings_and_restart(self):
        page, saved, restarted = self.make_page()
        self.assertTrue(page.enabled_checkbox.isChecked())
        self.assertEqual(page.spins["delay_minutes"].value(), 3)
        # Regression (silent running bot): nothing is pre-selected while nothing is saved.
        self.assertEqual(page.guild_combo.currentText(), self.page_module.CHOOSE_SERVER)
        self.assertEqual(page.channel_combo.currentText(), self.page_module.CHOOSE_CHANNEL)
        self.assertEqual(page.status_title.text(), "Not configured — nothing is posted")
        self.assertIn("has no server and channel saved", page.status_details.text())
        self.assertIn("Presence Intent", self.page_module.REQUIRED_INTENTS_TEXT)
        self.assertIn("Message Content Intent is not needed", self.page_module.REQUIRED_INTENTS_TEXT)

        page.guild_combo.setCurrentIndex(page.guild_combo.findText("Null"))
        self.assertEqual(
            [page.channel_combo.itemText(i) for i in range(page.channel_combo.count())], [self.page_module.CHOOSE_CHANNEL, "#games"]
        )
        page.channel_combo.setCurrentIndex(page.channel_combo.findText("#games"))
        page.spins["delay_minutes"].setValue(5)
        page.voice_checkbox.setChecked(False)
        page.allowlist_edit.setPlainText("Overwatch 2\n\nDota 2")
        page.ignore_edit.setPlainText("Wallpaper Engine")
        self.assertTrue(page.save(restart=True))
        instance_id, overrides = saved[-1]
        self.assertEqual(instance_id, "gp-main")
        self.assertNotIn("game_presence", overrides)  # top-level config of the Game Presence bot
        self.assertEqual((overrides["enabled"], overrides["guild_id"], overrides["channel_id"]), (True, "1", "2"))
        self.assertEqual(overrides["delay_minutes"], 5)
        self.assertFalse(overrides["voice_aware"])
        self.assertEqual(overrides["allowlist"], ["Overwatch 2", "Dota 2"])
        self.assertEqual(overrides["ignore_list"], ["Wallpaper Engine"])
        self.assertEqual(restarted, ["gp-main"])

    def test_saved_values_reload_and_status_is_shown(self):
        section = {"guild_id": "1", "channel_id": "2", "guild_cooldown_minutes": 30}
        self.terminal.write_runtime_json(
            self.runtime,
            self.page_module.STATUS_FILE_NAME,
            {"enabled": True, "presence_intent": True, "guild_name": "Null", "channel_name": "games", "problem": None,
             "tracked_players": 4, "pending_groups": 1, "top_games": [{"game": "Overwatch 2", "players": 3}], "last_suggestion": None},
        )
        page, _saved, _restarted = self.make_page(section)
        self.assertTrue(page.enabled_checkbox.isChecked())
        self.assertEqual(page.spins["guild_cooldown_minutes"].value(), 30)
        self.assertEqual(page.channel_combo.currentData(), "2")
        self.assertEqual(page.status_title.text(), "Active")
        self.assertIn("Overwatch 2 (3)", page.status_details.text())

        page, _saved, _restarted = self.make_page({**section, "enabled": False})
        self.assertEqual(page.status_title.text(), "Paused")

    def test_invalid_settings_are_not_saved(self):
        page, saved, _ = self.make_page()
        page.allowlist_edit.setPlainText("x" * 101)
        self.assertFalse(page.save())
        self.assertEqual(saved, [])
        self.assertIn("Not saved", page.result_label.text())

    def test_corrupted_state_file_is_shown_even_when_the_bot_is_stopped(self):
        state = self.data_root / "instances" / "gp-main" / "data" / "game_presence_state.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text("{oops", encoding="utf-8")
        page, saved, _ = self.make_page({"guild_id": "1", "channel_id": "2"}, state="STOPPED")
        self.assertEqual(page.status_title.text(), "State file problem")
        self.assertIn("game_presence_state.json is unreadable", page.status_details.text())
        self.assertIn("fails closed", page.status_details.text())
        self.assertEqual(state.read_text(encoding="utf-8"), "{oops")  # the Manager never repairs or resets it

        state.write_text(json.dumps({"version": 1, "guilds": {}}), encoding="utf-8")
        page, _saved, _ = self.make_page({"guild_id": "1", "channel_id": "2"}, state="STOPPED")
        self.assertEqual(page.status_title.text(), "Stopped")

    def test_bot_reported_state_problem_is_shown_while_running(self):
        self.terminal.write_runtime_json(
            self.runtime,
            self.page_module.STATUS_FILE_NAME,
            {"enabled": True, "presence_intent": True, "problem": "Game Presence state is unavailable (PresenceStateError); posting is paused.",
             "tracked_players": 0, "pending_groups": 0, "top_games": [], "last_suggestion": None},
        )
        page, _saved, _ = self.make_page({"guild_id": "1", "channel_id": "2"})
        self.assertEqual(page.status_title.text(), "Problem")
        self.assertIn("posting is paused", page.status_details.text())

    def test_corrupted_config_is_shown_not_silently_used(self):
        page, saved, _ = self.make_page({"delay_minutes": 0})
        self.assertEqual(page.status_title.text(), "Config problem")
        self.assertIn("Config problem", page.result_label.text())
        self.assertEqual(saved, [])

    def test_legacy_admin_settings_are_shown_and_imported_on_request(self):
        legacy = {"enabled": True, "guild_id": "1", "channel_id": "2", "delay_minutes": 9}
        admin_root = self.data_root / "instances" / "admin-main" / "data"
        admin_root.mkdir(parents=True)
        admin_state = {"version": 1, "guilds": {"1": {"game_presence": {"muted": {str(A): 1.0}}, "role_menus": {"x": 1}}}}
        (admin_root / "admin_features.json").write_text(json.dumps(admin_state), encoding="utf-8")
        page, saved, _ = self.make_page(state="STOPPED", admin_overrides={"game_presence": legacy})
        self.assertFalse(page.legacy_panel.isHidden())
        self.assertIn("Main · Admin", page.legacy_label.text())
        self.assertEqual(saved, [])  # nothing happens without the user's click

        self.assertTrue(page.import_legacy())
        self.assertEqual(saved[-1][0], "gp-main")
        self.assertEqual(saved[-1][1]["delay_minutes"], 9)
        target = json.loads((self.data_root / "instances" / "gp-main" / "data" / "game_presence_state.json").read_text(encoding="utf-8"))
        self.assertEqual(target["guilds"]["1"]["game_presence"], {"muted": {str(A): 1.0}})
        self.assertNotIn("role_menus", target["guilds"]["1"])  # only Game Presence state moves
        self.assertEqual(json.loads((admin_root / "admin_features.json").read_text(encoding="utf-8")), admin_state)  # untouched

    def test_import_never_overwrites_state_and_skips_state_while_running(self):
        legacy = {"enabled": True, "guild_id": "1", "channel_id": "2"}
        admin_root = self.data_root / "instances" / "admin-main" / "data"
        admin_root.mkdir(parents=True)
        (admin_root / "admin_features.json").write_text(json.dumps({"guilds": {"1": {"game_presence": {"muted": {"1": 1.0}}}}}), encoding="utf-8")
        gp_state = self.data_root / "instances" / "gp-main" / "data" / "game_presence_state.json"
        gp_state.parent.mkdir(parents=True)
        own = {"version": 1, "guilds": {"1": {"game_presence": {"muted": {"2": 5.0}}}}}
        gp_state.write_text(json.dumps(own), encoding="utf-8")

        page, _saved, _ = self.make_page(state="RUNNING", admin_overrides={"game_presence": legacy})
        self.assertTrue(page.import_legacy())
        self.assertIn("not copied because the Game Presence bot is running", page.result_label.text())
        self.assertEqual(json.loads(gp_state.read_text(encoding="utf-8")), own)

        page, _saved, _ = self.make_page(state="STOPPED", admin_overrides={"game_presence": legacy})
        self.assertTrue(page.import_legacy())
        self.assertEqual(json.loads(gp_state.read_text(encoding="utf-8")), own)  # existing state wins


class DiagnosisTests(unittest.TestCase):
    """Why there is no suggestion: one compact reason per tick (decisions unchanged)."""

    def tick(self, engine):
        suggestions = engine.tick()
        return suggestions, engine.diagnosis

    def test_nobody_playing_one_player_and_filtered_games(self):
        engine, clock = make_engine()
        self.assertEqual(self.tick(engine)[1].code, "no_activity")
        play(engine, A)
        _, diagnosis = self.tick(engine)
        self.assertEqual(diagnosis.code, "single_player")
        self.assertIn("Overwatch 2: 1 player", diagnosis.text)

        engine, clock = make_engine(ignore_list=["Overwatch 2"])
        play(engine, A)
        play(engine, B)
        _, diagnosis = self.tick(engine)
        self.assertEqual(diagnosis.code, "game_filtered")
        self.assertIn("Overwatch 2", diagnosis.text)

    def test_waiting_then_suggestion(self):
        engine, clock = make_engine()
        play(engine, A)
        play(engine, B)
        _, diagnosis = self.tick(engine)
        self.assertEqual(diagnosis.code, "waiting_delay")
        self.assertIn("2 players in Overwatch 2, check in 3:00", diagnosis.text)
        clock.advance(60)
        self.assertIn("check in 2:00", self.tick(engine)[1].text)
        clock.advance(121)
        suggestions, diagnosis = self.tick(engine)
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(diagnosis.code, "suggesting")

    def test_policy_reasons(self):
        # muted
        engine, clock = make_engine()
        engine.preferences.set_muted(GUILD, B, True)
        play(engine, A)
        play(engine, B)
        engine.tick()
        clock.advance(200)
        suggestions, diagnosis = self.tick(engine)
        self.assertEqual((suggestions, diagnosis.code), ([], "user_muted"))
        self.assertIn("1 muted", diagnosis.text)

        # same voice channel
        engine, clock = make_engine()
        play(engine, A, voice=500)
        play(engine, B, voice=500)
        engine.tick()
        clock.advance(200)
        self.assertEqual(self.tick(engine)[1].code, "same_voice")

        # guild cooldown: another group right after a suggestion
        engine, clock = make_engine()
        play(engine, A)
        play(engine, B)
        self.assertEqual(len(run_until(engine, clock, 200)), 1)
        play(engine, C, "Dota 2")
        play(engine, D, "Dota 2")
        engine.tick()
        clock.advance(200)
        _, diagnosis = self.tick(engine)
        self.assertEqual(diagnosis.code, "guild_cooldown")
        self.assertIn("next suggestion possible in", diagnosis.text)

        # per-user cooldown: the same players again after the server cooldown is over
        engine, clock = make_engine()
        play(engine, A)
        play(engine, B)
        self.assertEqual(len(run_until(engine, clock, 200)), 1)
        clock.advance(20 * 60)
        engine.tick()
        clock.advance(200)
        _, diagnosis = self.tick(engine)
        self.assertEqual(diagnosis.code, "user_cooldown")
        self.assertIn("2 pinged recently", diagnosis.text)

        # group cooldown (no per-user cooldown configured)
        engine, clock = make_engine(user_cooldown_minutes=0)
        play(engine, A)
        play(engine, B)
        self.assertEqual(len(run_until(engine, clock, 200)), 1)
        clock.advance(20 * 60)
        engine.tick()
        clock.advance(200)
        self.assertEqual(self.tick(engine)[1].code, "group_cooldown")

    def test_mechanics_unchanged_with_diagnosis(self):
        # Third player during the delay is still aggregated into ONE message.
        engine, clock = make_engine()
        play(engine, A)
        play(engine, B)
        engine.tick()
        run_until(engine, clock, 60)
        play(engine, C)
        produced = run_until(engine, clock, 600)
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].target_user_ids, (A, B, C))
        self.assertEqual(engine.status()["diagnosis"]["code"], engine.diagnosis.code)


class FakePermissions:
    def __init__(self, view=True, send=True):
        self.view_channel = view
        self.send_messages = send


class PermissionChannel(FakeTextChannel):
    def __init__(self, channel_id, name, permissions):
        super().__init__(channel_id, name)
        self.permissions = permissions

    def permissions_for(self, member):
        return self.permissions


class RuntimeDiagnosisTests(unittest.IsolatedAsyncioTestCase):
    def make_runtime(self, **changes):
        guild = FakeGuild()
        guild.me = types.SimpleNamespace(id=9999)
        client = types.SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == GUILD else None, guilds=[guild])
        clock = Clock()
        logs = []
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = gpd.GamePresenceRuntime(
            client, gp.GamePresenceEngine(gp.MemoryPresenceStore(), clock), runtime_dir=Path(temp.name), presence_intent=True, log=logs.append
        )
        runtime.apply_config(config(**changes))
        return runtime, guild, clock, logs

    def status(self, runtime):
        return json.loads((runtime.runtime_dir / gpd.STATUS_FILE_NAME).read_text(encoding="utf-8"))

    async def test_not_configured_and_paused_are_explained_and_logged_once(self):
        runtime, guild, clock, logs = self.make_runtime()
        runtime.apply_config(gp.GamePresenceConfig(enabled=False))
        runtime.config_problem = gp.NOT_CONFIGURED_TEXT
        await runtime.tick()
        await runtime.tick()
        self.assertEqual(self.status(runtime)["diagnosis"]["code"], "not_configured")
        self.assertEqual(len(logs), 1)
        self.assertIn("Not configured", logs[0])
        runtime.config_problem = None
        await runtime.tick()
        self.assertEqual(self.status(runtime)["diagnosis"]["code"], "paused")

    async def test_channel_guild_and_permission_problems(self):
        runtime, guild, clock, logs = self.make_runtime(channel_id="424242")
        await runtime.tick()
        self.assertEqual(self.status(runtime)["diagnosis"]["code"], "channel_unavailable")
        runtime.apply_config(config(guild_id="77"))
        await runtime.tick()
        self.assertEqual(self.status(runtime)["diagnosis"]["code"], "guild_unavailable")

        runtime, guild, clock, logs = self.make_runtime()
        guild.text = PermissionChannel(CHANNEL, "games", FakePermissions(send=False))
        guild.members = [member(guild, A, OW), member(guild, B, OW)]
        runtime.seed()
        await runtime.tick()
        clock.advance(600)
        self.assertEqual(await runtime.tick(), 0)
        status = self.status(runtime)
        self.assertEqual(status["diagnosis"]["code"], "permission_denied")
        self.assertIn("Send Messages", status["diagnosis"]["text"])
        self.assertEqual(guild.text.sent, [])
        self.assertEqual(runtime.engine.status()["pending_groups"], 0)  # suggestion not consumed while it cannot be posted
        self.assertEqual(sum("Send Messages" in line for line in logs), 1)
        # Permission granted: the pending group forms and the suggestion goes out.
        guild.text.permissions = FakePermissions()
        await runtime.tick()
        clock.advance(200)
        self.assertEqual(await runtime.tick(), 1)
        self.assertEqual(self.status(runtime)["diagnosis"]["code"], "posted")
        self.assertTrue(any("Suggestion posted" in line for line in logs))

    async def test_forbidden_on_send_is_reported(self):
        runtime, guild, clock, logs = self.make_runtime()

        class Refusing(FakeTextChannel):
            async def send(self, content, **kwargs):
                raise discord.Forbidden(types.SimpleNamespace(status=403, reason="Forbidden"), "Missing Access")

        guild.text = Refusing(CHANNEL, "games")
        guild.members = [member(guild, A, OW), member(guild, B, OW)]
        runtime.seed()
        await runtime.tick()
        clock.advance(200)
        self.assertEqual(await runtime.tick(), 0)
        self.assertEqual(self.status(runtime)["diagnosis"]["code"], "permission_denied")

    async def test_engine_reason_and_presence_counts_reach_the_status(self):
        runtime, guild, clock, logs = self.make_runtime()
        bot = types.SimpleNamespace(id=5, guild=guild, bot=True, activities=[discord.Game(name=OW)], voice=None)
        guild.members = [member(guild, A, OW), member(guild, B, None), member(guild, C, None), bot]
        runtime.seed()
        await runtime.tick()
        status = self.status(runtime)
        self.assertEqual(status["diagnosis"]["code"], "single_player")
        self.assertEqual((status["visible_members"], status["playing_members"]), (3, 1))  # bots never count
        self.assertEqual(logs, [])  # waiting/idle reasons are not logged


class OutputBufferingTests(unittest.TestCase):
    def test_bot_output_is_flushed_line_by_line(self):
        runtime_layout = importlib.import_module("runtime_layout")
        calls = []
        line_stream = types.SimpleNamespace(reconfigure=lambda **kwargs: calls.append(kwargs))
        with unittest.mock.patch.object(sys, "stdout", line_stream), unittest.mock.patch.object(sys, "stderr", io.StringIO()):
            runtime_layout.line_buffered_output()
        self.assertEqual(calls, [{"line_buffering": True}])


@unittest.skipIf(importlib.util.find_spec("PySide6") is None, "PySide6 not installed")
class SilentBotRegressionTests(unittest.IsolatedAsyncioTestCase):
    """The real case: a Game Presence bot that was online but never posted.

    Its config.json was "{}": no server/channel was ever saved, while the
    Game Presence page pre-selected the first server and channel, so the bot
    looked configured. It also printed its reason only into a buffered log.
    """

    def setUp(self):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([])
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data_root = Path(temp.name)
        self.G, self.instance_store, self.config_store = load_with_data_root(self.data_root, "GamePresence", "instance_store", "config_store")
        for name in ("manager_game_presence", "admin_terminal"):
            sys.modules.pop(name, None)
        self.page_module = importlib.import_module("manager_game_presence")
        self.terminal = importlib.import_module("admin_terminal")
        self.instance = self.instance_store.create_instance("game_presence", "group-main", "Game Presence Bot")
        self.terminal.write_bot_status(
            self.instance.paths.runtime_dir,
            {"guilds": [{"id": str(GUILD), "name": "Null", "channels": [{"id": str(CHANNEL), "name": "games", "type": "text"}]}]},
        )

    def page(self):
        info = types.SimpleNamespace(logs_dir=self.instance.paths.logs_dir, state="RUNNING", bot_type="game_presence")
        page = self.page_module.GamePresencePanel(lambda: [("group-main", "Main · Game Presence Bot", info)], self.config_store, lambda _id: None)
        self.addCleanup(page.close)
        return page

    async def test_unconfigured_bot_is_obvious_and_works_once_saved(self):
        logs = []
        bot = self.G.GamePresenceBot(self.G.resolve_runtime("group-main"))
        bot.presence_runtime.log = logs.append
        guild = FakeGuild()
        guild.me = types.SimpleNamespace(id=9999)
        clock = Clock()
        bot.presence_runtime.client = types.SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == GUILD else None, guilds=[guild])
        bot.presence_runtime.engine.clock = clock
        guild.members = [member(guild, A, OW), member(guild, B, OW)]

        # 1. Online, "{}" config: nothing posted, and now it says why.
        await bot.run_tick()
        status = json.loads((self.instance.paths.runtime_dir / gpd.STATUS_FILE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(status["diagnosis"]["code"], "not_configured")
        self.assertEqual((status["visible_members"], status["playing_members"]), (2, 2))  # presence data does arrive
        self.assertTrue(any("Not configured" in line for line in logs))

        # 2. The page no longer looks configured, and Save without a choice keeps it unconfigured.
        page = self.page()
        self.assertEqual(page.guild_combo.currentText(), self.page_module.CHOOSE_SERVER)
        self.assertEqual(page.status_title.text(), "Not configured — nothing is posted")
        self.assertTrue(page.save())
        self.assertIsNone(self.config_store.load_effective_config("group-main")["guild_id"])

        # 3. Choose server + channel, Save: the running bot picks it up on its next tick.
        page.guild_combo.setCurrentIndex(page.guild_combo.findText("Null"))
        page.channel_combo.setCurrentIndex(page.channel_combo.findText("#games"))
        self.assertTrue(page.save())
        await bot.run_tick()
        self.assertTrue(bot.presence_runtime.engine.config.active)
        self.assertEqual(bot.presence_runtime.diagnosis.code, "waiting_delay")

        # 4. Two non-bot players, same game, not together in voice -> one public message after the delay.
        clock.advance(200)
        await bot.run_tick()
        self.assertEqual(len(guild.text.sent), 1)
        content, kwargs = guild.text.sent[0]
        self.assertIn(f"<@{A}>", content)
        self.assertIn(f"<@{B}>", content)
        self.assertEqual(sorted(user.id for user in kwargs["allowed_mentions"].users), [A, B])
        self.assertFalse(kwargs["allowed_mentions"].everyone)
        self.assertEqual([item.custom_id for item in kwargs["view"].children], [gpd.CUSTOM_ID_MUTE, gpd.CUSTOM_ID_ALLOW])
        self.assertEqual(bot.presence_runtime.diagnosis.code, "posted")
        page.refresh()
        self.assertIn("Now: Suggestion posted", page.status_details.text())
        self.assertIn("Discord presence data: 2 members · 2 online · 2 playing a game", page.status_details.text())
        self.assertNotIn("Discord shares no game", page.status_details.text())


class PresenceDataTests(unittest.IsolatedAsyncioTestCase):
    """Live case (03.10): 14 members, 3 online, one custom status, nobody "Playing"."""

    def live_guild(self):
        guild = FakeGuild()
        guild.me = types.SimpleNamespace(id=9999)

        def person(user_id, status, activities=()):
            return types.SimpleNamespace(id=user_id, guild=guild, bot=False, status=status, activities=tuple(activities), voice=None)

        custom = discord.CustomActivity(name="afk")
        guild.members = (
            [person(200 + n, discord.Status.offline) for n in range(11)]
            + [person(300, discord.Status.online, [custom]), person(301, discord.Status.online), person(302, discord.Status.idle)]
            + [types.SimpleNamespace(id=1, guild=guild, bot=True, status=discord.Status.online, activities=(), voice=None)]
        )
        return guild

    async def test_nobody_sharing_a_game_is_reported_with_what_discord_delivers(self):
        guild = self.live_guild()
        client = types.SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == GUILD else None, guilds=[guild])
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        runtime = gpd.GamePresenceRuntime(
            client, gp.GamePresenceEngine(gp.MemoryPresenceStore(), Clock()), runtime_dir=Path(temp.name), presence_intent=True, log=lambda _t: None
        )
        runtime.apply_config(config())
        await runtime.tick()
        status = json.loads((Path(temp.name) / gpd.STATUS_FILE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(
            (status["visible_members"], status["online_members"], status["playing_members"], status["other_activities"]),
            (14, 3, 0, {"custom": 1}),
        )
        self.assertEqual(status["diagnosis"]["code"], "no_activity")
        self.assertEqual(status["diagnosis"]["text"], "No game activity shared: 3 of 14 members online, none shows 'Playing' in Discord.")
        self.assertEqual(guild.text.sent, [])

        # As soon as Discord shares a game for two of them, the normal flow continues.
        guild.members[11].activities = (discord.Game(name=OW),)
        guild.members[12].activities = (discord.Game(name=OW),)
        runtime.on_presence_update(None, guild.members[11])
        runtime.on_presence_update(None, guild.members[12])
        await runtime.tick()
        self.assertEqual(runtime.diagnosis.code, "waiting_delay")

    @unittest.skipIf(importlib.util.find_spec("PySide6") is None, "PySide6 not installed")
    def test_manager_lines_tell_bot_problem_from_discord_sharing(self):
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        sys.modules.pop("manager_game_presence", None)
        page_module = importlib.import_module("manager_game_presence")
        lines = page_module.presence_data_lines(
            {"visible_members": 14, "online_members": 3, "playing_members": 0, "other_activities": {"custom": 1}}
        )
        self.assertEqual(lines[0], "Discord presence data: 14 members · 3 online · 0 playing a game · other: 1 custom status")
        self.assertIn("Activity Privacy", lines[1])
        self.assertIn("Registered Games", lines[1])
        offline = page_module.presence_data_lines({"visible_members": 14, "online_members": 0, "playing_members": 0})
        self.assertIn("Presence Intent", offline[1])
        no_members = page_module.presence_data_lines({"visible_members": 0, "online_members": 0, "playing_members": 0})
        self.assertIn("Server Members Intent", no_members[1])
        playing = page_module.presence_data_lines({"visible_members": 14, "online_members": 3, "playing_members": 2})
        self.assertEqual(len(playing), 1)


if __name__ == "__main__":
    unittest.main()
