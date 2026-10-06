"""Per-bot language: config, catalogs, deterministic Discord output (Admin,
Game Presence, Stream Director), the AI language rule, old configs."""

import asyncio
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"
sys.path.insert(0, str(CORE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
if not os.environ.get("DARKABYSS_DATA_DIR"):
    os.environ["DARKABYSS_DATA_DIR"] = tempfile.mkdtemp(prefix="darkabyss-test-")

import bot_i18n  # noqa: E402
import bot_i18n_keys  # noqa: E402

ADMIN_MODULES = (
    "Admin.py", "admin_ai.py", "admin_tools.py", "admin_tools_server.py", "admin_tools_content.py", "admin_features.py", "admin_blueprint.py",
    "social_awareness.py", "content_filter.py", "admin_tools_filter.py",
)


def reset_language(test):
    test.addCleanup(bot_i18n.set_bot_language, "en")


class CatalogTests(unittest.TestCase):
    """Every text that goes through translation has a Russian text with the same placeholders."""

    def check(self, files, catalog, extra_keys=()):
        keys = set(extra_keys)
        for name in files:
            keys |= bot_i18n_keys.literal_keys(CORE_ROOT / name)
        missing = sorted(key for key in keys if key not in catalog)
        self.assertEqual(missing, [], f"{len(missing)} texts without a Russian translation")
        for key in keys:
            self.assertEqual(bot_i18n.placeholders(key), bot_i18n.placeholders(catalog[key]), key)

    def test_admin_catalog(self):
        import admin_ai
        import admin_tools
        import bot_i18n_ru_admin

        constants = {
            getattr(admin_ai, name)
            for name in (
                "ACCESS_DENIED_MESSAGE", "UNAVAILABLE_MESSAGE", "CONFIG_UNAVAILABLE_MESSAGE", "NOT_OWNER_MESSAGE", "INACTIVE_MESSAGE",
                "EXPIRED_MESSAGE", "UNEXPECTED_FAILURE_MESSAGE", "EARLIER_ACTIONS_NOTE", "AUTH_REVOKED_TOOL_MESSAGE",
                "MENTION_DENIED_MESSAGE", "MENTION_EMPTY_MESSAGE", "MEMORY_CLEARED_MESSAGE",
            )
        } | {admin_tools.MESSAGE_CONTENT_HELP, admin_tools.PERMISSION_NAMES_HINT}
        constants |= {"Deleted role menu {menu_id}.", "Deleted role menu {menu_id} and its message.", "Deleted role menu {menu_id} (its message was already gone)."}
        import content_filter

        constants |= set(content_filter.CATEGORY_LABELS.values())
        self.check(ADMIN_MODULES, bot_i18n_ru_admin.RU, constants)

    def test_game_presence_catalog(self):
        import bot_i18n_ru_game_presence
        import game_presence_discord as gpd

        texts = {
            gpd.PREF_MUTED, gpd.PREF_ALREADY_MUTED, gpd.PREF_ALLOWED, gpd.PREF_ALREADY_ALLOWED, gpd.PREF_UNAVAILABLE,
            gpd.BUTTON_MUTE, gpd.BUTTON_ALLOW, *gpd.TEMPLATES,
        }
        self.check(("game_presence_discord.py",), bot_i18n_ru_game_presence.RU, texts)
        # Template variants are distinct keys (one Russian wording each).
        self.assertEqual(len(set(gpd.TEMPLATES)), len(gpd.TEMPLATES))

    def test_stream_director_catalog(self):
        import bot_i18n_ru_stream_director
        import stream_director as sd
        import stream_director_discord as sdd

        labels = set(sd.INBOX_LABELS.values()) | set(sd.METRICS.values()) | set(sd.PERIOD_LABELS.values()) | set(sd.CHALLENGE_STATUS_LABELS.values())
        labels |= {head for head, _color in sdd.STATUS_HEADS.values()} | set(sdd.CHALLENGE_HEADS.values())
        labels |= {label for label, _value in sdd.KINDS} | {label for label, _value in sdd.PERIODS} | {goal[0] for goal in sd.DEFAULT_GOALS}
        labels |= {sdd.UNAVAILABLE, sdd.NOT_YOURS, "Accept #{id}", "Done #{id}", "🔮 Prediction", "🔒 Prediction locked", "🔮 Prediction resolved", "📊 Poll", "📊 Poll closed"}
        self.check(("stream_director.py", "stream_director_discord.py"), bot_i18n_ru_stream_director.RU, labels)

    def test_shared_texts_have_one_translation(self):
        import bot_i18n_ru_admin
        import bot_i18n_ru_game_presence
        import bot_i18n_ru_stream_director

        catalogs = [bot_i18n_ru_admin.RU, bot_i18n_ru_game_presence.RU, bot_i18n_ru_stream_director.RU]
        for index, first in enumerate(catalogs):
            for second in catalogs[index + 1 :]:
                for key in set(first) & set(second):
                    self.assertEqual(first[key], second[key], key)


class I18nCoreTests(unittest.TestCase):
    def test_normalize_translate_plural(self):
        self.assertEqual(bot_i18n.normalize_language(None), "en")
        self.assertEqual(bot_i18n.normalize_language("", "ru"), "ru")
        self.assertEqual(bot_i18n.normalize_language(" RU "), "ru")
        with self.assertRaises(bot_i18n.LanguageError):
            bot_i18n.normalize_language("de")
        self.assertEqual(bot_i18n.tr("ru", "Goal #{id} removed.", id=3), "Цель #3 удалена.")
        self.assertEqual(bot_i18n.tr("en", "Goal #{id} removed.", id=3), "Goal #3 removed.")
        self.assertEqual(bot_i18n.tr("ru", "a text nobody translated {x}", x=1), "a text nobody translated 1")  # falls back to English
        self.assertEqual([bot_i18n.plural("ru", n, "a", "b", "один", "два", "много") for n in (1, 3, 5, 11, 21)], ["один", "два", "много", "много", "один"])

    def test_process_language_is_per_bot_process(self):
        reset_language(self)
        self.assertEqual(bot_i18n.set_bot_language("ru"), "ru")
        self.assertEqual(bot_i18n.t("Access denied."), "Доступ запрещён.")
        self.assertEqual(bot_i18n.set_bot_language("klingon"), "en")  # unknown -> English, never a crash
        self.assertEqual(bot_i18n.t("Access denied."), "Access denied.")


# --------------------------------------------------------------------------
# config: per instance, round trip, old configs
# --------------------------------------------------------------------------


class InstanceLanguageConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        os.environ["DARKABYSS_DATA_DIR"] = self.temp.name
        for name in ("app_paths", "instance_store", "config_store", "bot_registry", "runtime_layout"):
            sys.modules.pop(name, None)
        self.instance_store = importlib.import_module("instance_store")
        self.config_store = importlib.import_module("config_store")

    def test_two_bots_of_the_same_type_keep_their_own_language(self):
        import game_presence as gp
        import stream_director_config as sdc

        self.instance_store.create_instance("game_presence", "gp-ru")
        self.instance_store.create_instance("game_presence", "gp-en")
        self.config_store.save_config_overrides("gp-en", {"language": "en"})
        self.instance_store.create_instance("stream_director", "sd-ru")
        self.instance_store.create_instance("stream_director", "sd-en")
        self.config_store.save_config_overrides("sd-ru", {"language": "ru"})
        self.assertEqual(gp.parse_bot_config(self.config_store.load_effective_config("gp-ru")).language, "ru")
        self.assertEqual(gp.parse_bot_config(self.config_store.load_effective_config("gp-en")).language, "en")
        self.assertEqual(sdc.load_config(self.config_store.load_effective_config("sd-ru")).language, "ru")
        self.assertEqual(sdc.load_config(self.config_store.load_effective_config("sd-en")).language, "en")
        on_disk = json.loads(self.instance_store.get_instance_paths("gp-en").config.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["language"], "en")

    def test_old_configs_without_language_keep_their_old_language(self):
        import game_presence as gp
        import stream_director_config as sdc

        self.instance_store.create_instance("admin", "admin-old")
        self.instance_store.create_instance("game_presence", "gp-old")
        self.instance_store.create_instance("stream_director", "sd-old")
        # Configs saved before languages existed: no "language" key at all.
        self.config_store.save_config_overrides("admin-old", {"allowed_user_ids": ["5"]})
        self.config_store.save_config_overrides("gp-old", {**{k: v for k, v in gp.DEFAULT_CONFIG.items() if k != "language"}, "enabled": True})
        self.assertEqual(self.config_store.load_effective_config("admin-old")["language"], "en")  # Admin spoke English
        self.assertEqual(gp.parse_bot_config(self.config_store.load_effective_config("gp-old")).language, "ru")  # Game Presence spoke Russian
        self.assertEqual(sdc.load_config(self.config_store.load_effective_config("sd-old")).language, "en")
        with self.assertRaises(Exception):
            self.config_store.save_config_overrides("gp-old", {"language": "de"})

    def test_admin_validation_sets_the_process_language(self):
        reset_language(self)
        admin = importlib.import_module("Admin")
        base = json.loads((CORE_ROOT / "defaults" / "admin_config.json").read_text(encoding="utf-8"))
        old = {key: value for key, value in base.items() if key != "language"}
        self.assertEqual(admin.validate_config(dict(old))["language"], "en")
        self.assertEqual(admin.validate_config({**old, "language": "ru"})["language"], "ru")
        with self.assertRaises(ValueError):
            admin.validate_config({**old, "language": "xx"})


# --------------------------------------------------------------------------
# deterministic bot output
# --------------------------------------------------------------------------


class StreamDirectorLanguageTests(unittest.TestCase):
    def director(self, language):
        import stream_director as sd
        import stream_director_config as sdc
        import stream_director_store as sds

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = sdc.parse_config(sdc.normalize_config({"guild_id": "123456789012345678", "channel_id": "223456789012345678", "language": language}))
        clock = types.SimpleNamespace(now=1_791_000_000.0)
        return sd.Director(sds.StateStore(Path(temp.name)), config, lambda: clock.now), clock, sd

    def test_two_directors_answer_in_their_own_language_at_the_same_time(self):
        ru, clock_ru, sd = self.director("ru")
        en, clock_en, _ = self.director("en")
        viewer = sd.Actor("discord", "1", "Viewer")
        for director, clock in ((ru, clock_ru), (en, clock_en)):
            director.manual_start(sd.Actor("discord", "9", "Streamer", team=True), "Show")
            clock.now += 120
        self.assertEqual(ru.mark_moment(viewer, "wow").text, "📍 Момент сохранён на 0:01:45.")
        self.assertEqual(en.mark_moment(viewer, "wow").text, "📍 Moment saved at 0:01:45.")
        self.assertEqual(ru.suggest_challenge(viewer, "x").text, "Опиши челлендж в нескольких словах.")
        self.assertEqual(en.suggest_challenge(viewer, "x").text, "Describe the challenge in a few words.")
        self.assertEqual(ru.vote(viewer, "999", 0).text, "Этого опроса больше нет.")
        # Built-in goals are shown in the bot's language; recap sections too.
        self.assertIn("Провести 3 стрима за неделю", [goal["title"] for goal in ru.goals_view()])
        self.assertIn("Stream 3 times this week", [goal["title"] for goal in en.goals_view()])
        clock_ru.now += 3600
        ru.manual_end(sd.Actor("discord", "9", "Streamer", team=True))
        titles = [title for _key, title, _lines in sd.recap_sections(ru.state["sessions"][-1]["recap"], None, "ru")]
        self.assertEqual(titles[0], "Стрим")
        self.assertIn("Сообщество", titles)

    def test_discord_components_follow_the_language(self):
        import stream_director_discord as sdd

        ru_labels = [item.label for item in sdd.card_components("live", "ru").children]
        en_labels = [item.label for item in sdd.card_components("live", "en").children]
        self.assertEqual(ru_labels, ["Момент", "Челлендж", "Предложить"])
        self.assertEqual(en_labels, ["Moment", "Challenge", "Suggest"])
        poll = {"id": "1", "kind": "prediction", "question": "Q?", "options": ["Да", "Нет"], "status": "open", "deadline": 0, "outcome": None}
        embed = sdd.poll_embed(poll, {"counts": [0, 0], "total": 0, "summary": ""}, "ru")
        self.assertTrue(embed.title.startswith("🔮 Предсказание"))
        self.assertIn("Без ставок", [field.name for field in embed.fields])
        self.assertEqual(sdd.moment_modal("ru").title, "Отметить момент")


class GamePresenceLanguageTests(unittest.IsolatedAsyncioTestCase):
    def suggestion(self, kind="gather", targets=(1, 2), outsiders=(), insiders=(), crew=()):
        import game_presence as gp

        return gp.Suggestion(1, "ow", "Overwatch 2", kind, targets, outsiders, insiders, 500 if kind == "join" else None, crew)

    def test_templates_and_buttons(self):
        import game_presence_discord as gpd

        self.assertEqual(
            gpd.render_message(self.suggestion(), None, language="en"),
            "<@1> <@2>, you have both been in **Overwatch 2** for a few minutes. Want to get together in voice?",
        )
        self.assertEqual(
            gpd.render_message(self.suggestion(), None, language="ru"),
            "<@1> <@2>, вы оба уже несколько минут в **Overwatch 2**. Может, соберётесь в войсе?",
        )
        join = self.suggestion("join", (3, 1, 2), (3,), (1, 2))
        self.assertEqual(gpd.render_message(join, "Gaming", language="en"), "<@3>, <@1> and <@2> are already playing **Overwatch 2** in **Gaming**. Jump in!")
        self.assertIn("<@1> и <@2>", gpd.render_message(join, "Gaming", language="ru"))
        # Whole voice: the rest of the channel is told in the same language.
        solo = self.suggestion("join", (3, 1), (3,), (1,), crew=(7, 8))
        self.assertEqual(
            gpd.render_message(solo, "Gaming", language="en"),
            "<@3>, <@1> is already playing **Overwatch 2** in **Gaming**. Jump in! <@7> and <@8>, heads up: someone may join you.",
        )
        self.assertEqual(
            gpd.render_message(solo, "Gaming", language="ru"),
            "<@3>, <@1> уже играет в **Overwatch 2** и сидит в **Gaming**. Залетай! <@7> и <@8>, к вам, возможно, присоединятся.",
        )
        group = self.suggestion(targets=(1, 2, 3))
        self.assertIn("3 of you", gpd.render_message(group, None, language="en"))
        self.assertIn("вас уже трое", gpd.render_message(group, None, language="ru"))
        self.assertEqual([item.label for item in gpd.preference_view("en").children], ["Mute pings", "Allow pings"])
        self.assertEqual([item.label for item in gpd.preference_view("ru").children], ["Отключить пинги", "Включить пинги"])

    async def test_preference_replies_follow_the_language(self):
        import game_presence_discord as gpd

        class Book:
            def set_muted(self, guild_id, user_id, muted):
                return True

        sent = []
        interaction = types.SimpleNamespace(
            data={"custom_id": gpd.CUSTOM_ID_MUTE},
            guild_id=1,
            user=types.SimpleNamespace(id=2),
            response=types.SimpleNamespace(send_message=lambda text, **kwargs: _record(sent, text)),
        )
        await gpd.handle_preference_interaction(interaction, Book(), "en")
        await gpd.handle_preference_interaction(interaction, Book(), "ru")
        self.assertEqual(sent[0], gpd.PREF_MUTED)
        self.assertTrue(sent[1].startswith("Готово: я больше не буду упоминать тебя"))

    async def test_ai_rewrite_request_carries_the_bot_language(self):
        import game_presence_discord as gpd

        requests = []

        class Orchestrator:
            async def orchestrate(self, request):
                requests.append(request)
                return types.SimpleNamespace(status=types.SimpleNamespace(value="COMPLETED"), content="{targets}, go {game}?")

        rewrite = gpd.make_ai_rewriter(lambda: Orchestrator())
        template = gpd.template_for(self.suggestion(), language="en")
        result = await rewrite(template, self.suggestion(), gpd.rewrite_context(self.suggestion(), language="en", channel_name=None, template=template))
        self.assertEqual(result, "{targets}, go {game}?")
        self.assertEqual(requests[0].response_language, "en")
        self.assertIn("in English", requests[0].messages[0].content)

    async def test_ai_rewrite_in_the_wrong_language_falls_back(self):
        import game_presence_discord as gpd

        class Orchestrator:
            def __init__(self, content):
                self.content = content

            async def orchestrate(self, request):
                return types.SimpleNamespace(status=types.SimpleNamespace(value="COMPLETED"), content=self.content)

        template_ru = gpd.template_for(self.suggestion(), language="ru")
        context_ru = gpd.rewrite_context(self.suggestion(), language="ru", channel_name=None, template=template_ru)
        self.assertIsNone(await gpd.make_ai_rewriter(lambda: Orchestrator("{targets}, go play {game}?"))(template_ru, self.suggestion(), context_ru))
        self.assertEqual(
            await gpd.make_ai_rewriter(lambda: Orchestrator("{targets}, погнали в {game}?"))(template_ru, self.suggestion(), context_ru),
            "{targets}, погнали в {game}?",
        )
        template_en = gpd.template_for(self.suggestion(), language="en")
        context_en = gpd.rewrite_context(self.suggestion(), language="en", channel_name=None, template=template_en)
        self.assertIsNone(await gpd.make_ai_rewriter(lambda: Orchestrator("{targets}, погнали в {game}?"))(template_en, self.suggestion(), context_en))


async def _record(sent, text):
    sent.append(text)


# --------------------------------------------------------------------------
# the AI language rule is part of every provider call
# --------------------------------------------------------------------------


class AILanguageRuleTests(unittest.IsolatedAsyncioTestCase):
    async def test_orchestrator_adds_the_rule_to_every_provider_call(self):
        for name in ("ai_orchestrator", "ai_platform", "admin_tools"):
            sys.modules.pop(name, None)
        import ai_orchestrator
        import ai_platform
        import admin_tools
        from test_ai_orchestrator import FakeExecutor, FakeProvider

        provider = FakeProvider(
            ai_platform,
            responses=[
                ai_platform.AIResponse(tool_calls=(ai_platform.AIToolCall(call_id="c1", tool_name="list_channels", arguments={}),)),
                ai_platform.AIResponse(content="Готово"),
            ],
        )
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = ai_platform.AISettingsStore(Path(temp.name) / "ai.json")
        store.save(
            ai_platform.AISettings(
                profiles=(ai_platform.AIProfile("routine", "fake", "fake-model"),),
                routing=ai_platform.RoutingConfig(routine_profile_id="routine", planner_profile_id="routine", creative_profile_id="routine"),
            )
        )
        orchestrator = ai_orchestrator.AIOrchestrator(
            settings_store=store,
            provider_registry=ai_platform.ProviderRegistry({"fake": provider}),
            credential_store=ai_platform.CredentialStore(Path(temp.name) / "secrets"),
        )
        request = ai_orchestrator.OrchestratorRequest(
            messages=(ai_platform.AIMessage(role=ai_platform.MessageRole.USER, content="list the channels"),),
            task_class="ROUTINE",
            response_language="ru",
        )
        result = await orchestrator.orchestrate(request, executor=FakeExecutor(admin_tools), confirmation_policy=ai_orchestrator.ConfirmationPolicy(confirm_normal=False))
        self.assertEqual(result.status.value, "COMPLETED")
        self.assertEqual(len(provider.requests), 2)  # the tool round and the final answer
        rule = bot_i18n.ai_language_rule("ru")
        for sent, _credential in provider.requests:
            systems = [message.content for message in sent.messages if message.role == ai_platform.MessageRole.SYSTEM]
            self.assertIn(rule, systems)
        # Without a language nothing is added (other callers are unchanged).
        provider.responses = [ai_platform.AIResponse(content="ok")]
        await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(ai_platform.AIMessage(role=ai_platform.MessageRole.USER, content="hi"),), task_class="ROUTINE")
        )
        self.assertNotIn(rule, [message.content for message in provider.requests[-1][0].messages])
        with self.assertRaises(ValueError):
            ai_orchestrator.OrchestratorRequest(messages=(ai_platform.AIMessage(role=ai_platform.MessageRole.USER, content="hi"),), task_class="ROUTINE", response_language="de")


class AdminLanguageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        reset_language(self)
        from test_admin_ai import AdminAITestBase, RecordingOrchestrator

        class Base(AdminAITestBase):
            def runTest(self):  # pragma: no cover - helper only
                pass

        self.base = Base()
        self.base.setUp()
        self.recording = RecordingOrchestrator

    async def test_admin_ai_replies_and_requests_in_the_bot_language(self):
        base = self.base
        base.config["language"] = "ru"
        result = base.ai_orchestrator.OrchestratorResult(status=base.ai_orchestrator.OrchestratorStatus.COMPLETED, content="Готово")
        orchestrator = self.recording(result)
        transport = base.make_transport(orchestrator)
        outsider = base.guild.add_member(type(base.owner)(999, "outsider"))
        denied = await base.start(transport, user=outsider)
        self.assertEqual(denied.all_texts()[0], bot_i18n.tr("ru", base.admin_ai.ACCESS_DENIED_MESSAGE))
        self.assertTrue(denied.all_texts()[0].startswith("Доступ запрещён"))
        await base.start(transport, prompt="покажи каналы")
        request = orchestrator.calls[-1][0]
        self.assertEqual(request.response_language, "ru")
        self.assertIn("LANGUAGE RULE", request.messages[0].content)  # the executor follows the rule, not "the user's language"
        self.assertNotIn("the user's language", request.messages[0].content)

        base.config["language"] = "en"
        denied = await base.start(transport, user=outsider)
        self.assertEqual(denied.all_texts()[0], base.admin_ai.ACCESS_DENIED_MESSAGE)
        await base.start(transport, prompt="list channels")
        self.assertEqual(orchestrator.calls[-1][0].response_language, "en")

    async def test_rendered_admin_output_is_translated(self):
        admin_ai = self.base.admin_ai
        admin_tools = self.base.admin_tools
        bot_i18n.set_bot_language("ru")
        cancelled = types.SimpleNamespace(status=types.SimpleNamespace(value="CANCELLED"), message=admin_ai.CORE_REJECTED_MESSAGE, executed_tools=())
        self.assertEqual(admin_ai.render_result_messages(cancelled), ["Отменено. Ничего из этого плана не выполнено."])
        failed = await admin_tools.execute_tool(admin_tools.AdminToolContext(guild=None, fetch_user=None, source="/execute"), "no_such_tool", {})
        self.assertEqual(failed.message, "Неизвестный инструмент администратора: no_such_tool")
        bot_i18n.set_bot_language("en")
        failed = await admin_tools.execute_tool(admin_tools.AdminToolContext(guild=None, fetch_user=None, source="/execute"), "no_such_tool", {})
        self.assertEqual(failed.message, "Unknown admin tool: no_such_tool")

    async def test_slash_command_descriptions_follow_the_bot_language(self):
        admin = importlib.import_module("Admin")
        from discord import Locale, app_commands

        translator = admin.BotLanguageTranslator()
        description = app_commands.TranslationContext(app_commands.TranslationContextLocation.command_description, None)
        name = app_commands.TranslationContext(app_commands.TranslationContextLocation.command_name, None)
        text = app_commands.locale_str("Execute a whitelisted Discord admin action")
        bot_i18n.set_bot_language("ru")
        self.assertEqual(await translator.translate(text, Locale.american_english, description), "Выполнить разрешённое действие администратора Discord")
        self.assertIsNone(await translator.translate(app_commands.locale_str("execute"), Locale.russian, name))  # names never change
        bot_i18n.set_bot_language("en")
        self.assertIsNone(await translator.translate(text, Locale.russian, description))


if __name__ == "__main__":
    unittest.main()
