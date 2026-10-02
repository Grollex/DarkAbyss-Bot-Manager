import asyncio
import contextlib
import importlib
import io
import json
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"

AI_MODULES = ("ai_orchestrator", "ai_platform", "ai_groq", "ai_gemini")


def load_modules():
    sys.path.insert(0, str(CORE_ROOT))
    for name in ("admin_ai", "admin_tools", *AI_MODULES):
        sys.modules.pop(name, None)
    admin_ai = importlib.import_module("admin_ai")
    admin_tools = importlib.import_module("admin_tools")
    ai_platform = importlib.import_module("ai_platform")
    ai_orchestrator = importlib.import_module("ai_orchestrator")
    return admin_ai, admin_tools, ai_platform, ai_orchestrator


def import_admin_module():
    """Import Admin.py; on non-Windows provide a temporary no-op msvcrt shim
    (Admin.py's single-instance lock is Windows-only) and remove it afterwards."""
    sys.path.insert(0, str(CORE_ROOT))
    inserted = False
    try:
        import msvcrt  # noqa: F401  (Windows)
    except ImportError:
        shim = types.ModuleType("msvcrt")
        shim.LK_NBLCK = 2
        shim.locking = lambda *args: None
        sys.modules["msvcrt"] = shim
        inserted = True
    try:
        sys.modules.pop("Admin", None)
        return importlib.import_module("Admin")
    finally:
        if inserted:
            sys.modules.pop("msvcrt", None)


def strip_engine_footer(text):
    """Remove the trailing "-# provider · profile · model" engine line, if any."""
    lines = text.split("\n")
    if lines and lines[-1].startswith("-# "):
        lines = lines[:-1]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fake Discord objects (no real Discord connection)
# ---------------------------------------------------------------------------


class FakePermissions:
    def __init__(self, administrator=False):
        self.administrator = administrator
        self.value = 8 if administrator else 0


class FakeRole:
    def __init__(self, role_id, name="role"):
        self.id = role_id
        self.name = name
        self.position = 1
        self.managed = False
        self.color = "#000000"
        self.permissions = FakePermissions()


class FakeMember:
    def __init__(self, member_id, name="user", *, roles=(), administrator=False, guild=None):
        self.id = member_id
        self.name = name
        self.display_name = name
        self.roles = list(roles)
        self.guild = guild
        self.guild_permissions = FakePermissions(administrator)
        self.calls = []

    async def ban(self, *, reason=None, delete_message_seconds=0):
        self.calls.append(("ban", reason))

    def __str__(self):
        return self.name


class FakeChannel:
    def __init__(self, channel_id, name="general", channel_type="text"):
        self.id = channel_id
        self.name = name
        self.type = channel_type
        self.position = 0
        self.parent = None
        self.category = None
        self.sent = []

    async def send(self, content, **kwargs):
        self.sent.append((content, kwargs))


class FakeGuild:
    def __init__(self, guild_id=10):
        self.id = guild_id
        self.name = "Guild"
        self.member_count = 3
        self.text_channel = FakeChannel(100)
        self.channels = [self.text_channel]
        self.roles = []
        self.members = {}

    def add_member(self, member):
        member.guild = self
        self.members[member.id] = member
        return member

    def get_member(self, member_id):
        return self.members.get(member_id)

    def get_channel(self, channel_id):
        return next((channel for channel in self.channels if channel.id == channel_id), None)

    def get_role(self, role_id):
        return next((role for role in self.roles if role.id == role_id), None)


class FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeResponse:
    def __init__(self):
        self._done = False
        self.sent = []
        self.deferred = []
        self.edited = []

    def is_done(self):
        return self._done

    async def send_message(self, content, **kwargs):
        self._done = True
        self.sent.append((content, kwargs))

    async def defer(self, **kwargs):
        self._done = True
        self.deferred.append(kwargs)

    async def edit_message(self, **kwargs):
        self._done = True
        self.edited.append(kwargs)


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content, **kwargs):
        self.sent.append((content, kwargs))
        return FakeMessage()


class FakeInteraction:
    def __init__(self, user, guild, channel_id=100):
        self.user = user
        self.guild = guild
        self.guild_id = getattr(guild, "id", None)
        self.channel_id = channel_id
        self.response = FakeResponse()
        self.followup = FakeFollowup()

    def all_messages(self):
        return [*self.response.sent, *self.followup.sent]

    def all_texts(self):
        texts = [content for content, _ in self.all_messages()]
        texts += [edit.get("content", "") for edit in self.response.edited]
        return texts


class RecordingView:
    def __init__(self, transport, state):
        self.transport = transport
        self.state = state
        self.stopped = False
        self.message = None

    def stop(self):
        self.stopped = True


class FakeProvider:
    def __init__(self, ai_platform, responses=None, error=None, on_generate=None):
        self._metadata = ai_platform.ProviderMetadata(
            provider_id="fake",
            display_name="fake",
            models=(ai_platform.ProviderModel("fake-model", "Fake", supports_tool_calls=True),),
        )
        self.responses = list(responses or [])
        self.error = error
        self.on_generate = on_generate
        self.requests = []

    @property
    def metadata(self):
        return self._metadata

    def get_local_availability(self, *, credential_ref=None, credential_available=False):
        return sys.modules["ai_platform"].Availability(sys.modules["ai_platform"].AvailabilityState.AVAILABLE, "ok")

    async def test_connection(self, credential_ref=None):
        raise AssertionError("transport must never call test_connection")

    async def generate(self, request, credential_ref=None):
        self.requests.append(request)
        if self.on_generate is not None:
            self.on_generate(len(self.requests))
        if self.error is not None:
            raise self.error
        if not self.responses:
            return sys.modules["ai_platform"].AIResponse(content="done")
        return self.responses.pop(0)


AI_ROLE_ID = 4242
AI_USER_ID = 501
OWNER_ID = 300


class AdminAITestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.admin_ai, self.admin_tools, self.ai_platform, self.ai_orchestrator = load_modules()
        self.admin_ai.MEMBER_TYPES = (FakeMember,)
        self.guild = FakeGuild()
        self.ai_role = FakeRole(AI_ROLE_ID, "AI")
        self.owner = self.guild.add_member(FakeMember(OWNER_ID, "owner", roles=[self.ai_role]))
        self.target = self.guild.add_member(FakeMember(777, "target"))
        self.config = {
            "allow_server_administrators": True,
            "allowed_user_ids": [],
            "allowed_role_ids": [],
            "audit_channel_id": None,
            "ai_allowed_user_ids": [],
            "ai_allowed_role_ids": [AI_ROLE_ID],
        }
        self.audits = []
        self.factory_calls = 0

    def call(self, call_id, tool_name, **arguments):
        return self.ai_platform.AIResponse(
            tool_calls=(self.ai_platform.AIToolCall(call_id=call_id, tool_name=tool_name, arguments=arguments),)
        )

    def make_orchestrator(self, provider):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        store = self.ai_platform.AISettingsStore(Path(temp_dir.name) / "config" / "ai.json")
        store.save(
            self.ai_platform.AISettings(
                profiles=(self.ai_platform.AIProfile("routine", "fake", "fake-model"),),
                routing=self.ai_platform.RoutingConfig(
                    routine_profile_id="routine",
                    planner_profile_id="routine",
                    creative_profile_id="routine",
                ),
            )
        )
        return self.ai_orchestrator.AIOrchestrator(
            settings_store=store,
            provider_registry=self.ai_platform.ProviderRegistry({"fake": provider}),
            credential_store=self.ai_platform.CredentialStore(Path(temp_dir.name) / "secrets"),
        )

    def make_transport(self, orchestrator=None, *, factory=None, view_factory=None):
        async def audit(interaction, config, action, result):
            self.audits.append((action, result))
            return None

        def default_factory():
            self.factory_calls += 1
            return orchestrator

        return self.admin_ai.AITransport(
            load_config=lambda: json.loads(json.dumps(self.config)),
            fetch_user=None,
            audit=audit,
            orchestrator_factory=factory or default_factory,
            view_factory=view_factory or RecordingView,
        )

    def interaction(self, user=None, guild=None, channel_id=100):
        return FakeInteraction(user or self.owner, guild if guild is not None else self.guild, channel_id)

    def assert_no_mentions(self, interaction):
        for _content, kwargs in interaction.all_messages():
            self.assert_mentions_none(kwargs.get("allowed_mentions"))
        for edit in interaction.response.edited:
            self.assert_mentions_none(edit.get("allowed_mentions"))

    def assert_mentions_none(self, mentions):
        self.assertIsNotNone(mentions)
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.users)
        self.assertFalse(mentions.roles)
        self.assertFalse(mentions.replied_user)

    def views(self, interaction):
        return [kwargs["view"] for _content, kwargs in interaction.followup.sent if "view" in kwargs]

    async def start(self, transport, prompt="do it", mode=None, user=None):
        interaction = self.interaction(user=user)
        await transport.handle_ai_command(interaction, prompt, mode)
        return interaction

    async def decide(self, transport, state, approved=True, user=None, guild=None, channel_id=100, view=None):
        interaction = self.interaction(user=user, guild=guild, channel_id=channel_id)
        await transport.handle_decision(interaction, state, approved=approved, view=view)
        return interaction


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class AIAuthorizationTests(AdminAITestBase):
    def test_explicit_ai_user_and_role_allowed(self):
        config = dict(self.config, ai_allowed_user_ids=[AI_USER_ID], ai_allowed_role_ids=[])
        user = self.guild.add_member(FakeMember(AI_USER_ID))
        self.assertTrue(self.admin_ai.actor_has_ai_access(user, self.guild, config))
        role_config = dict(self.config, ai_allowed_user_ids=[], ai_allowed_role_ids=[AI_ROLE_ID])
        self.assertTrue(self.admin_ai.actor_has_ai_access(self.owner, self.guild, role_config))

    def test_administrator_only_is_denied(self):
        admin = self.guild.add_member(FakeMember(9, "admin", administrator=True))
        config = dict(self.config, allow_server_administrators=True)
        self.assertFalse(self.admin_ai.actor_has_ai_access(admin, self.guild, config))
        # Sanity: the same member DOES pass the /execute policy.
        self.assertTrue(self.admin_tools.actor_has_access(admin, config))

    def test_execute_whitelist_does_not_grant_ai(self):
        user = self.guild.add_member(FakeMember(55, "exec-user", roles=[FakeRole(66)]))
        config = dict(self.config, allowed_user_ids=[55], allowed_role_ids=[66], ai_allowed_role_ids=[AI_ROLE_ID])
        self.assertTrue(self.admin_tools.actor_has_access(user, config))
        self.assertFalse(self.admin_ai.actor_has_ai_access(user, self.guild, config))

    def test_empty_whitelist_dm_and_non_member_denied(self):
        empty = dict(self.config, ai_allowed_user_ids=[], ai_allowed_role_ids=[])
        self.assertFalse(self.admin_ai.actor_has_ai_access(self.owner, self.guild, empty))
        self.assertFalse(self.admin_ai.actor_has_ai_access(self.owner, None, self.config))
        not_member = types.SimpleNamespace(id=OWNER_ID, roles=[self.ai_role])
        self.assertFalse(self.admin_ai.actor_has_ai_access(not_member, self.guild, self.config))
        other_guild = FakeGuild(guild_id=99)
        self.assertFalse(self.admin_ai.actor_has_ai_access(self.owner, other_guild, self.config))

    async def test_unauthorized_ai_does_not_construct_orchestrator(self):
        admin = self.guild.add_member(FakeMember(9, "admin", administrator=True))
        transport = self.make_transport(object())
        interaction = await self.start(transport, user=admin)
        self.assertEqual(self.factory_calls, 0)
        self.assertEqual(interaction.response.sent[0][0], self.admin_ai.ACCESS_DENIED_MESSAGE)
        self.assertTrue(interaction.response.sent[0][1]["ephemeral"])
        self.assert_no_mentions(interaction)

        dm = FakeInteraction(self.owner, None, None)
        await transport.handle_ai_command(dm, "hello", None)
        self.assertEqual(self.factory_calls, 0)
        self.assertEqual(dm.response.sent[0][0], self.admin_ai.ACCESS_DENIED_MESSAGE)


# ---------------------------------------------------------------------------
# Lazy / optional AI
# ---------------------------------------------------------------------------


class LazyAITests(AdminAITestBase):
    def test_admin_ai_import_does_not_import_ai_modules(self):
        sys.path.insert(0, str(CORE_ROOT))
        for name in ("admin_ai", *AI_MODULES):
            sys.modules.pop(name, None)
        importlib.import_module("admin_ai")
        for name in AI_MODULES:
            self.assertNotIn(name, sys.modules)

    def test_admin_import_registers_ai_and_execute_without_ai_or_message_content(self):
        for name in ("admin_ai", *AI_MODULES):
            sys.modules.pop(name, None)
        admin = import_admin_module()
        self.assertIsNotNone(admin.bot.tree.get_command("execute"))
        ai_command = admin.bot.tree.get_command("ai")
        self.assertIsNotNone(ai_command)
        self.assertTrue(ai_command.guild_only)
        prompt = next(parameter for parameter in ai_command.parameters if parameter.name == "prompt")
        self.assertTrue(prompt.required)
        self.assertEqual((prompt.min_value, prompt.max_value), (1, 2000))
        mode = next(parameter for parameter in ai_command.parameters if parameter.name == "mode")
        self.assertFalse(mode.required)
        self.assertEqual([choice.value for choice in mode.choices], ["routine", "planner", "creative"])
        self.assertIsNotNone(admin.ai_transport)
        self.assertFalse(admin.bot.intents.message_content)
        for name in ("ai_orchestrator", "ai_groq", "ai_gemini"):
            self.assertNotIn(name, sys.modules)

    async def test_factory_failure_is_contained_and_retried(self):
        attempts = []
        provider = FakeProvider(self.ai_platform, responses=[self.ai_platform.AIResponse(content="hello")])
        orchestrator = self.make_orchestrator(provider)

        def flaky_factory():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("secret-construction-detail")
            return orchestrator

        transport = self.make_transport(factory=flaky_factory)
        first = await self.start(transport)
        self.assertEqual(first.followup.sent[-1][0], self.admin_ai.UNAVAILABLE_MESSAGE)
        self.assertNotIn("secret-construction-detail", " ".join(first.all_texts()))
        second = await self.start(transport)
        self.assertEqual(strip_engine_footer(second.followup.sent[-1][0]), "hello")
        self.assertEqual(len(attempts), 2)

    async def test_broken_ai_module_import_is_contained(self):
        original = self.admin_ai._import_ai_modules

        def broken():
            raise ImportError("provider package broken")

        self.admin_ai._import_ai_modules = broken
        self.addCleanup(setattr, self.admin_ai, "_import_ai_modules", original)
        transport = self.make_transport(object())
        interaction = await self.start(transport)
        self.assertEqual(interaction.followup.sent[-1][0], self.admin_ai.UNAVAILABLE_MESSAGE)
        self.assert_no_mentions(interaction)

    async def test_provider_failure_renders_safe_unavailable(self):
        provider = FakeProvider(self.ai_platform, error=RuntimeError("Authorization: Bearer sk-secret"))
        transport = self.make_transport(self.make_orchestrator(provider))
        interaction = await self.start(transport)
        texts = " ".join(interaction.all_texts())
        self.assertIn("unavailable", texts.lower())
        self.assertNotIn("sk-secret", texts)
        self.assertNotIn("Authorization", texts)
        self.assert_no_mentions(interaction)


# ---------------------------------------------------------------------------
# Orchestration / rendering
# ---------------------------------------------------------------------------


class RecordingOrchestrator:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def orchestrate(self, request, *, executor=None, confirmation_policy=None):
        self.calls.append((request, confirmation_policy, executor))
        return self.result


class OrchestrationTests(AdminAITestBase):
    async def test_request_message_modes_and_strict_policy(self):
        result = self.ai_orchestrator.OrchestratorResult(status=self.ai_orchestrator.OrchestratorStatus.COMPLETED, content="ok")
        orchestrator = RecordingOrchestrator(result)
        transport = self.make_transport(orchestrator)
        for mode, expected in ((None, "ROUTINE"), ("planner", "PLANNER"), ("creative", "CREATIVE"), ("routine", "ROUTINE")):
            with self.subTest(mode=mode):
                await self.start(transport, prompt="hello there", mode=mode)
                request, policy, executor = orchestrator.calls[-1]
                self.assertEqual(request.task_class.value, expected)
                self.assertEqual(len(request.messages), 1)
                self.assertIs(request.messages[0].role, self.ai_platform.MessageRole.USER)
                self.assertEqual(request.messages[0].content, "hello there")
                self.assertIs(type(policy), self.ai_orchestrator.ConfirmationPolicy)
                self.assertTrue(policy.confirm_normal)
                self.assertTrue(callable(executor))

    async def test_invalid_prompts_and_modes_are_rejected_before_ai(self):
        orchestrator = RecordingOrchestrator(None)
        transport = self.make_transport(orchestrator)
        for prompt, mode in (("   ", None), ("x" * (self.admin_ai.AI_PROMPT_MAX_CHARS + 1), None), ("ok", "direct")):
            interaction = await self.start(transport, prompt=prompt, mode=mode)
            self.assertEqual(interaction.response.deferred, [])
            self.assertTrue(interaction.response.sent[0][1]["ephemeral"])
        self.assertEqual(orchestrator.calls, [])
        self.assertEqual(self.factory_calls, 0)

    async def test_completed_long_output_is_chunked_and_mention_safe(self):
        long_text = "@everyone " + ("y" * 9000)
        result = self.ai_orchestrator.OrchestratorResult(status=self.ai_orchestrator.OrchestratorStatus.COMPLETED, content=long_text)
        transport = self.make_transport(RecordingOrchestrator(result))
        interaction = await self.start(transport)
        sent = interaction.followup.sent
        self.assertEqual(len(sent), self.admin_ai.AI_MAX_RESPONSE_CHUNKS)
        for content, kwargs in sent:
            self.assertLessEqual(len(content), self.admin_ai.DISCORD_MESSAGE_LIMIT)
            self.assertTrue(kwargs["ephemeral"])
        self.assertIn("truncated", sent[-1][0])
        self.assertTrue(interaction.response.deferred[0]["ephemeral"])
        self.assert_no_mentions(interaction)

    def test_result_rendering_never_claims_success_for_failures(self):
        Status = self.ai_orchestrator.OrchestratorStatus
        summary = self.ai_orchestrator.ExecutedToolSummary("c1", "send_message", False, "boom")
        for status in (Status.UNAVAILABLE, Status.INVALID_TOOL_PLAN, Status.TOOL_EXECUTION_FAILED, Status.LIMIT_REACHED, Status.CANCELLED):
            with self.subTest(status=status):
                result = self.ai_orchestrator.OrchestratorResult(status=status, message="m" * 5000, executed_tools=(summary,))
                texts = self.admin_ai.render_result_messages(result)
                joined = " ".join(texts).lower()
                self.assertNotIn("success", joined)
                for text in texts:
                    self.assertLessEqual(len(text), self.admin_ai.AI_MESSAGE_CHUNK_CHARS)
        rejected = self.ai_orchestrator.OrchestratorResult(status=Status.CANCELLED, message="Confirmation rejected.")
        self.assertIn("Cancelled", self.admin_ai.render_result_messages(rejected)[0])
        expired = self.ai_orchestrator.OrchestratorResult(status=Status.CANCELLED, message="Unknown or expired confirmation.")
        self.assertEqual(self.admin_ai.render_result_messages(expired)[0], self.admin_ai.EXPIRED_MESSAGE)


# ---------------------------------------------------------------------------
# Confirmation flow (real AI-3A orchestrator + fake provider)
# ---------------------------------------------------------------------------


class ConfirmationTests(AdminAITestBase):
    def send_call(self, call_id="c1", content="@everyone hello"):
        return self.call(call_id, "send_message", channel_id="100", content=content)

    async def test_normal_tool_requires_confirmation_and_shows_validated_plan(self):
        provider = FakeProvider(self.ai_platform, responses=[self.send_call()])
        orchestrator = self.make_orchestrator(provider)
        transport = self.make_transport(orchestrator)
        interaction = await self.start(transport)

        views = self.views(interaction)
        self.assertEqual(len(views), 1)
        self.assertEqual(self.guild.text_channel.sent, [])
        control = interaction.followup.sent[-1][0]
        self.assertIn("NORMAL", control)
        self.assertIn("view", interaction.followup.sent[-1][1])
        preview = interaction.followup.sent[0][0]
        self.assertIn("review page 1 of 1", preview)
        self.assertIn('tool `"send_message"`', preview)
        self.assertIn('risk `"NORMAL"`', preview)
        self.assertIn('"100"', preview)
        self.assertIn('"@everyone hello"', preview)
        for content, _kwargs in interaction.followup.sent:
            self.assertLessEqual(len(content), self.admin_ai.DISCORD_MESSAGE_LIMIT)
        confirmation_id = views[0].state.confirmation_id
        self.assertNotIn(confirmation_id, " ".join(interaction.all_texts()))
        self.assertNotIn(confirmation_id, repr(views[0].state))
        self.assert_no_mentions(interaction)

    async def test_approve_executes_exact_plan_once_with_suppressed_mentions(self):
        provider = FakeProvider(self.ai_platform, responses=[self.send_call(), self.ai_platform.AIResponse(content="sent it")])
        transport = self.make_transport(self.make_orchestrator(provider))
        first = await self.start(transport)
        view = self.views(first)[0]

        approval = await self.decide(transport, view.state, approved=True, view=view)
        self.assertEqual(len(self.guild.text_channel.sent), 1)
        content, kwargs = self.guild.text_channel.sent[0]
        self.assertEqual(content, "@everyone hello")
        self.assert_mentions_none(kwargs["allowed_mentions"])
        self.assertTrue(view.stopped)
        self.assertIsNone(approval.response.edited[0]["view"])
        followup_texts = [content for content, _ in approval.followup.sent]
        self.assertEqual(strip_engine_footer(followup_texts[0]), "sent it")
        self.assertIn("send_message", followup_texts[-1])
        self.assertIn("ok", followup_texts[-1])
        self.assertEqual(self.audits[0][0], "/ai send_message")
        self.assert_no_mentions(approval)

        again = await self.decide(transport, view.state, approved=True, view=view)
        self.assertEqual(again.response.sent[0][0], self.admin_ai.INACTIVE_MESSAGE)
        self.assertEqual(len(self.guild.text_channel.sent), 1)

    async def test_cancel_executes_nothing(self):
        provider = FakeProvider(self.ai_platform, responses=[self.send_call()])
        transport = self.make_transport(self.make_orchestrator(provider))
        view = self.views(await self.start(transport))[0]
        cancel = await self.decide(transport, view.state, approved=False, view=view)
        self.assertEqual(self.guild.text_channel.sent, [])
        self.assertIn("Cancelled", cancel.followup.sent[-1][0])
        again = await self.decide(transport, view.state, approved=True, view=view)
        self.assertEqual(again.response.sent[0][0], self.admin_ai.INACTIVE_MESSAGE)
        self.assertEqual(self.guild.text_channel.sent, [])

    async def test_read_tool_runs_without_confirmation(self):
        provider = FakeProvider(
            self.ai_platform, responses=[self.call("c1", "list_channels"), self.ai_platform.AIResponse(content="1 channel")]
        )
        transport = self.make_transport(self.make_orchestrator(provider))
        interaction = await self.start(transport)
        self.assertEqual(self.views(interaction), [])
        self.assertEqual(strip_engine_footer(interaction.followup.sent[0][0]), "1 channel")
        self.assertEqual(self.audits[0][0], "/ai list_channels")

    async def test_destructive_requires_confirmation_and_multi_round_confirmations(self):
        provider = FakeProvider(
            self.ai_platform,
            responses=[
                self.send_call(),
                self.call("c2", "ban_member", member_id="777"),
                self.ai_platform.AIResponse(content="all done"),
            ],
        )
        transport = self.make_transport(self.make_orchestrator(provider))
        first_view = self.views(await self.start(transport))[0]

        first_approval = await self.decide(transport, first_view.state, approved=True, view=first_view)
        second_views = self.views(first_approval)
        self.assertEqual(len(second_views), 1)
        second_view = second_views[0]
        self.assertIsNot(second_view.state, first_view.state)
        self.assertEqual(second_view.state.binding, first_view.state.binding)
        second_round_texts = " ".join(content for content, _ in first_approval.followup.sent)
        self.assertIn('tool `"ban_member"`', second_round_texts)
        self.assertIn('"777"', second_round_texts)
        self.assertIn("DESTRUCTIVE", first_approval.followup.sent[-1][0])
        self.assertEqual(self.target.calls, [])

        second_approval = await self.decide(transport, second_view.state, approved=True, view=second_view)
        self.assertEqual(len(self.target.calls), 1)
        self.assertEqual(strip_engine_footer(second_approval.followup.sent[0][0]), "all done")
        self.assertEqual(len(self.guild.text_channel.sent), 1)

    async def test_real_confirmation_view_has_buttons_and_bounded_timeout(self):
        state = self.admin_ai.PendingConfirmation(
            confirmation_id="confirm_secret_value",
            binding=self.admin_ai.RequestBinding(OWNER_ID, self.guild.id, 100),
            summary="s",
        )
        view = self.admin_ai.ConfirmationView(self.make_transport(object()), state)
        labels = sorted(item.label for item in view.children)
        self.assertEqual(labels, ["Approve", "Cancel"])
        self.assertLessEqual(view.timeout, self.ai_orchestrator.CONFIRMATION_TTL_SECONDS)
        for item in view.children:
            self.assertNotIn("confirm_secret_value", item.custom_id or "")


# ---------------------------------------------------------------------------
# Exact, bounded confirmation preview (fail closed)
# ---------------------------------------------------------------------------


class ConfirmationPreviewTests(AdminAITestBase):
    PAGE_HEADER = "AI action plan - review page"

    def batch(self, contents, reason=None):
        calls = []
        for index, content in enumerate(contents):
            arguments = {"channel_id": "100", "content": content}
            if reason is not None:
                arguments["reason"] = reason
            calls.append(self.ai_platform.AIToolCall(call_id=f"c{index}", tool_name="send_message", arguments=arguments))
        return self.ai_platform.AIResponse(tool_calls=tuple(calls))

    def preview_pages(self, interaction):
        return [content for content, _ in interaction.followup.sent if self.PAGE_HEADER in content]

    def json_values(self, pages, argument):
        """Re-assemble every exact JSON value shown for `argument` from the pages."""
        values = []
        current = []
        pattern = re.compile(
            r"argument `\"" + argument + r"\"` \(exact JSON value(, part \d+, (continues|final)[^)]*)?\):\n```json\n(.*?)\n```",
            re.DOTALL,
        )
        for page in pages:
            for match in pattern.finditer(page):
                multipart, state, body = match.group(1), match.group(2), match.group(3)
                current.append(body)
                if multipart is None or state == "final":
                    values.append(json.loads("".join(current)))
                    current = []
        self.assertEqual(current, [])
        return values

    async def test_long_send_message_content_is_fully_reviewable_and_exact(self):
        content = "@everyone ```fence``` " + ("x" * 1700) + " END_OF_EXACT_MESSAGE_73921"
        self.assertLessEqual(len(content), 2000)
        self.assertGreater(len(content), 300)
        provider = FakeProvider(self.ai_platform, responses=[self.batch([content]), self.ai_platform.AIResponse(content="ok")])
        transport = self.make_transport(self.make_orchestrator(provider))
        interaction = await self.start(transport)

        pages = self.preview_pages(interaction)
        self.assertTrue(pages)
        self.assertIn("END_OF_EXACT_MESSAGE_73921", "".join(pages))
        self.assertEqual(self.json_values(pages, "content"), [content])
        for page in pages:
            self.assertLessEqual(len(page), self.admin_ai.AI_PREVIEW_PAGE_CHARS)
        self.assertEqual(self.guild.text_channel.sent, [])
        self.assert_no_mentions(interaction)

        view = self.views(interaction)[0]
        control = interaction.followup.sent[-1][0]
        self.assertNotIn("END_OF_EXACT_MESSAGE_73921", control)
        await self.decide(transport, view.state, view=view)
        self.assertEqual(self.guild.text_channel.sent[0][0], content)

    async def test_multi_tool_plan_spans_pages_without_clipping(self):
        contents = [f"TOOL_{index}_" + ("y" * 1500) for index in range(5)] + ["LAST_TOOL_DATA_5521 " + ("z" * 1500)]
        provider = FakeProvider(self.ai_platform, responses=[self.batch(contents)])
        transport = self.make_transport(self.make_orchestrator(provider))
        interaction = await self.start(transport)

        pages = self.preview_pages(interaction)
        self.assertGreater(len(pages), 1)
        joined = "\n".join(pages)
        for index in range(1, 7):
            self.assertIn(f"**Action {index} of 6:**", joined)
        self.assertIn("LAST_TOOL_DATA_5521", joined)
        self.assertEqual(self.json_values(pages, "content"), contents)
        for page_number, page in enumerate(pages, start=1):
            self.assertLessEqual(len(page), self.admin_ai.DISCORD_MESSAGE_LIMIT)
            self.assertIn(f"review page {page_number} of {len(pages)}", page)
        self.assertEqual(len(self.views(interaction)), 1)
        self.assertIn("view", interaction.followup.sent[-1][1])
        self.assertEqual(self.guild.text_channel.sent, [])
        self.assert_no_mentions(interaction)

    async def test_maximum_realistic_current_batch_stays_within_page_bound(self):
        limit = self.ai_orchestrator.MAX_TOOL_CALLS_PER_ROUND
        contents = [f"{index}" + ("w" * 1999) for index in range(limit)]
        reason = "r" * 512
        provider = FakeProvider(self.ai_platform, responses=[self.batch(contents, reason=reason)])
        transport = self.make_transport(self.make_orchestrator(provider))
        interaction = await self.start(transport)

        pages = self.preview_pages(interaction)
        self.assertGreater(len(pages), 1)
        self.assertLessEqual(len(pages), self.admin_ai.AI_MAX_PREVIEW_PAGES)
        self.assertEqual(self.json_values(pages, "content"), contents)
        self.assertEqual(self.json_values(pages, "reason"), [reason] * limit)
        self.assertEqual(len(self.views(interaction)), 1)

    async def test_plan_too_large_for_preview_fails_closed(self):
        limit = self.ai_orchestrator.MAX_TOOL_CALLS_PER_ROUND
        # Valid under the Admin Tool contract (2000 chars), but every backtick
        # must be shown as an exact JSON escape, so the exact preview exceeds the
        # production page bound.
        contents = ["`" * 2000 for _ in range(limit)]
        provider = FakeProvider(self.ai_platform, responses=[self.batch(contents)])
        orchestrator = self.make_orchestrator(provider)
        transport = self.make_transport(orchestrator)
        interaction = await self.start(transport)

        self.assertEqual(self.views(interaction), [])
        self.assertEqual(self.preview_pages(interaction), [])
        self.assertEqual(
            strip_engine_footer(interaction.followup.sent[-1][0]),
            self.admin_ai.unreviewable_plan_message(too_large=True, cancelled=True, earlier_actions=False),
        )
        self.assertEqual(orchestrator._pending_confirmations, {})
        self.assertEqual(self.guild.text_channel.sent, [])
        self.assert_no_mentions(interaction)
        with self.assertRaises(self.admin_ai.PlanPreviewTooLarge):
            self.admin_ai.render_tool_plan_pages(
                tuple(
                    self.ai_orchestrator.ValidatedToolCall(
                        f"c{index}", "send_message", {"channel_id": "100", "content": text}, self.ai_platform.ToolRisk.NORMAL
                    )
                    for index, text in enumerate(contents)
                )
            )

    async def test_unrenderable_plan_fails_closed_and_is_cancelled(self):
        provider = FakeProvider(self.ai_platform, responses=[self.batch(["hello"])])
        orchestrator = self.make_orchestrator(provider)
        transport = self.make_transport(orchestrator)
        original = self.ai_orchestrator.ValidatedToolCall.public_dict

        def broken_public_dict(call):
            raise RuntimeError("secret-render-detail")

        self.ai_orchestrator.ValidatedToolCall.public_dict = broken_public_dict
        self.addCleanup(setattr, self.ai_orchestrator.ValidatedToolCall, "public_dict", original)
        interaction = await self.start(transport)
        self.ai_orchestrator.ValidatedToolCall.public_dict = original

        texts = " ".join(interaction.all_texts())
        self.assertEqual(self.views(interaction), [])
        self.assertEqual(
            strip_engine_footer(interaction.followup.sent[-1][0]),
            self.admin_ai.unreviewable_plan_message(too_large=False, cancelled=True, earlier_actions=False),
        )
        self.assertNotIn("secret-render-detail", texts)
        self.assertNotIn("{}", texts)
        self.assertNotIn("?", texts)
        self.assertEqual(orchestrator._pending_confirmations, {})
        self.assertEqual(self.guild.text_channel.sent, [])
        self.assert_no_mentions(interaction)

    def test_renderer_rejects_malformed_public_data(self):
        class Bad:
            def __init__(self, payload):
                self.payload = payload

            def public_dict(self):
                return self.payload

        for payload in (
            {"tool_name": "", "risk": "NORMAL", "arguments": {}},
            {"tool_name": "send_message", "risk": None, "arguments": {}},
            {"tool_name": "send_message", "risk": "NORMAL", "arguments": None},
            {"tool_name": "send_message", "risk": "NORMAL", "arguments": {"x": float("nan")}},
        ):
            with self.subTest(payload=payload), self.assertRaises(self.admin_ai.PlanPreviewError):
                self.admin_ai.render_tool_plan_pages((Bad(payload),))
        with self.assertRaises(self.admin_ai.PlanPreviewError):
            self.admin_ai.render_tool_plan_pages(())

    def test_exact_json_display_round_trips_display_hostile_values(self):
        for value in ("a`b", "```", "x​y", "‮evil", "\U000e0041", "line\nbreak", "@here", 5, None, "\"\\"):
            with self.subTest(value=value):
                text = self.admin_ai.exact_json_display(value)
                self.assertEqual(json.loads(text), value)
                self.assertNotIn("`", text)
                self.assertNotIn("​", text)
                self.assertNotIn("‮", text)


class FailClosedHonestyTests(AdminAITestBase):
    def send_batch(self, contents):
        return self.ai_platform.AIResponse(
            tool_calls=tuple(
                self.ai_platform.AIToolCall(
                    call_id=f"s{index}", tool_name="send_message", arguments={"channel_id": "100", "content": content}
                )
                for index, content in enumerate(contents)
            )
        )

    def break_public_dict(self):
        original = self.ai_orchestrator.ValidatedToolCall.public_dict

        def broken(call):
            raise RuntimeError("secret-render-detail")

        self.ai_orchestrator.ValidatedToolCall.public_dict = broken
        self.addCleanup(setattr, self.ai_orchestrator.ValidatedToolCall, "public_dict", original)
        return original

    def assert_honest_current_plan_text(self, interaction, *, cancelled):
        texts = [content for content, _ in interaction.followup.sent]
        joined = "\n".join(texts)
        self.assertEqual(self.views(interaction), [])
        self.assertIn("Actions already executed:", texts[0])
        self.assertIn("`list_channels` ok", texts[0])
        final = texts[-1]
        self.assertTrue(final.startswith("The new AI action plan"))
        self.assertIn("no action from this plan was executed", final)
        self.assertIn("Earlier actions from this request may already have executed", final)
        self.assertIn("were not undone", final)
        self.assertNotIn("Nothing was executed", joined)
        self.assertNotIn("secret-render-detail", joined)
        if cancelled:
            self.assertIn("It was cancelled", final)
        else:
            self.assertNotIn("cancelled", final.lower())
            self.assertIn("will expire automatically", final)
        self.assert_no_mentions(interaction)

    async def test_earlier_read_then_unrenderable_plan_reports_honestly(self):
        provider = FakeProvider(
            self.ai_platform,
            responses=[self.call("r1", "list_channels"), self.send_batch(["hello"])],
        )
        orchestrator = self.make_orchestrator(provider)
        transport = self.make_transport(orchestrator)
        self.break_public_dict()
        interaction = await self.start(transport)

        self.assert_honest_current_plan_text(interaction, cancelled=True)
        self.assertEqual(self.audits[0][0], "/ai list_channels")
        self.assertEqual(len(self.audits), 1)
        self.assertEqual(self.guild.text_channel.sent, [])
        self.assertEqual(orchestrator._pending_confirmations, {})

    async def test_earlier_read_then_too_large_plan_reports_honestly(self):
        limit = self.ai_orchestrator.MAX_TOOL_CALLS_PER_ROUND
        provider = FakeProvider(
            self.ai_platform,
            responses=[self.call("r1", "list_channels"), self.send_batch(["`" * 2000] * limit)],
        )
        orchestrator = self.make_orchestrator(provider)
        transport = self.make_transport(orchestrator)
        interaction = await self.start(transport)

        self.assert_honest_current_plan_text(interaction, cancelled=True)
        self.assertIn("too large to review safely", interaction.followup.sent[-1][0])
        self.assertEqual(self.guild.text_channel.sent, [])
        self.assertEqual(orchestrator._pending_confirmations, {})

    async def test_unconfirmed_core_rejection_uses_expiry_wording(self):
        provider = FakeProvider(
            self.ai_platform,
            responses=[self.call("r1", "list_channels"), self.send_batch(["hello"])],
        )
        orchestrator = self.make_orchestrator(provider)
        original_approve = orchestrator.approve_confirmation
        attempts = []

        async def failing_approve(confirmation_id, *, approved, executor):
            attempts.append((confirmation_id, approved))
            raise RuntimeError("secret-cancel-detail")

        orchestrator.approve_confirmation = failing_approve
        transport = self.make_transport(orchestrator)
        self.break_public_dict()
        interaction = await self.start(transport)

        self.assert_honest_current_plan_text(interaction, cancelled=False)
        joined = "\n".join(interaction.all_texts())
        self.assertNotIn("secret-cancel-detail", joined)
        self.assertEqual(len(attempts), 1)
        confirmation_id = attempts[0][0]
        self.assertFalse(attempts[0][1])
        self.assertNotIn(confirmation_id, joined)
        self.assertEqual(self.guild.text_channel.sent, [])
        # The plan is still pending in the core but unreachable via this transport
        # (no view). It can never run without an explicit approval.
        self.assertIn(confirmation_id, orchestrator._pending_confirmations)
        orchestrator.approve_confirmation = original_approve

    async def test_cancel_unreviewable_success_and_failure_detection(self):
        provider = FakeProvider(self.ai_platform, responses=[self.send_batch(["hello"])])
        orchestrator = self.make_orchestrator(provider)
        transport = self.make_transport(orchestrator)
        transport.get_orchestrator()
        result = await orchestrator.orchestrate(
            self.ai_orchestrator.OrchestratorRequest(
                messages=(self.ai_platform.AIMessage(role=self.ai_platform.MessageRole.USER, content="x"),),
                task_class="ROUTINE",
            ),
            executor=transport.build_executor(self.interaction(), self.admin_ai.RequestBinding(OWNER_ID, self.guild.id, 100)),
            confirmation_policy=self.ai_orchestrator.ConfirmationPolicy(confirm_normal=True),
        )
        self.assertEqual(result.status, self.ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)

        self.assertTrue(await transport._cancel_unreviewable(result.confirmation_id))
        # Already consumed: the core now answers "unknown or expired", which is
        # not a confirmed rejection by this call.
        self.assertFalse(await transport._cancel_unreviewable(result.confirmation_id))
        executed = []

        async def recording_executor(tool_name, arguments):
            executed.append(tool_name)
            return self.admin_tools.ToolResult(True, tool_name, "ok")

        after = await orchestrator.approve_confirmation(result.confirmation_id, approved=True, executor=recording_executor)
        self.assertEqual(after.status, self.ai_orchestrator.OrchestratorStatus.CANCELLED)
        self.assertEqual(executed, [])
        self.assertEqual(self.guild.text_channel.sent, [])

        no_orchestrator = self.make_transport(factory=lambda: None)
        self.assertFalse(await no_orchestrator._cancel_unreviewable("confirm_x"))

    def test_unreviewable_wording_matrix(self):
        message = self.admin_ai.unreviewable_plan_message
        plain = message(too_large=False, cancelled=True, earlier_actions=False)
        self.assertTrue(plain.startswith("The AI action plan could not be displayed safely"))
        self.assertIn("It was cancelled", plain)
        self.assertNotIn("Earlier actions", plain)
        expiry = message(too_large=True, cancelled=False, earlier_actions=False)
        self.assertIn("too large to review safely", expiry)
        self.assertIn("will expire automatically", expiry)
        self.assertNotIn("cancelled", expiry.lower())
        for text in (plain, expiry, message(too_large=True, cancelled=True, earlier_actions=True)):
            self.assertNotIn("Nothing was executed", text)
            self.assertNotIn("undone", text.replace("were not undone", ""))

class UnexpectedFailureHonestyTests(AdminAITestBase):
    async def test_orchestrator_exception_never_claims_nothing_ran(self):
        class ExplodingOrchestrator:
            async def orchestrate(self, request, *, executor=None, confirmation_policy=None):
                raise RuntimeError("secret-core-detail")

            async def approve_confirmation(self, confirmation_id, *, approved, executor):
                raise RuntimeError("secret-core-detail")

        transport = self.make_transport(ExplodingOrchestrator())
        interaction = await self.start(transport)
        text = interaction.followup.sent[-1][0]
        self.assertEqual(text, self.admin_ai.UNEXPECTED_FAILURE_MESSAGE)
        self.assertNotIn("No action was executed", text)
        self.assertNotIn("secret-core-detail", " ".join(interaction.all_texts()))

        state = self.admin_ai.PendingConfirmation(
            confirmation_id="confirm_abc",
            binding=self.admin_ai.RequestBinding(OWNER_ID, self.guild.id, 100),
            summary="control",
        )
        decision = await self.decide(transport, state, approved=True)
        self.assertEqual(decision.followup.sent[-1][0], self.admin_ai.UNEXPECTED_FAILURE_MESSAGE)
        self.assertNotIn("confirm_abc", " ".join(decision.all_texts()))
        self.assertNotIn("secret-core-detail", " ".join(decision.all_texts()))

class StrictDecisionTests(AdminAITestBase):
    async def test_non_bool_decision_raises_without_side_effects(self):
        provider = FakeProvider(
            self.ai_platform,
            responses=[self.call("c1", "send_message", channel_id="100", content="hi"), self.ai_platform.AIResponse(content="ok")],
        )
        orchestrator = self.make_orchestrator(provider)
        transport = self.make_transport(orchestrator)
        view = self.views(await self.start(transport))[0]

        for invalid in ("false", "true", 1, 0, None):
            with self.subTest(approved=invalid):
                interaction = self.interaction()
                with self.assertRaises(ValueError):
                    await transport.handle_decision(interaction, view.state, approved=invalid, view=view)
                self.assertFalse(view.state.resolved)
                self.assertFalse(view.stopped)
                self.assertEqual(interaction.all_messages(), [])
                self.assertEqual(interaction.response.edited, [])
                self.assertEqual(len(orchestrator._pending_confirmations), 1)
                self.assertEqual(self.guild.text_channel.sent, [])

        approved = await self.decide(transport, view.state, approved=True, view=view)
        self.assertEqual(len(self.guild.text_channel.sent), 1)
        self.assertEqual(strip_engine_footer(approved.followup.sent[0][0]), "ok")
        again = await self.decide(transport, view.state, approved=True, view=view)
        self.assertEqual(again.response.sent[0][0], self.admin_ai.INACTIVE_MESSAGE)
        self.assertEqual(len(self.guild.text_channel.sent), 1)


# ---------------------------------------------------------------------------
# Ownership and execution-time authorization
# ---------------------------------------------------------------------------


class OwnershipTests(AdminAITestBase):
    async def pending(self, responses=None):
        provider = FakeProvider(
            self.ai_platform,
            responses=responses
            or [self.call("c1", "send_message", channel_id="100", content="hi"), self.ai_platform.AIResponse(content="ok")],
        )
        orchestrator = self.make_orchestrator(provider)
        transport = self.make_transport(orchestrator)
        view = self.views(await self.start(transport))[0]
        return transport, orchestrator, view

    async def test_other_user_wrong_guild_and_wrong_channel_are_denied_without_consuming(self):
        transport, orchestrator, view = await self.pending()
        intruder = self.guild.add_member(FakeMember(888, "intruder", roles=[self.ai_role], administrator=True))
        denied = await self.decide(transport, view.state, user=intruder, view=view)
        self.assertEqual(denied.response.sent[0][0], self.admin_ai.NOT_OWNER_MESSAGE)
        self.assertTrue(denied.response.sent[0][1]["ephemeral"])

        other_guild = FakeGuild(guild_id=11)
        await self.decide(transport, view.state, guild=other_guild, view=view)
        wrong_channel = await self.decide(transport, view.state, channel_id=999, view=view)
        self.assertEqual(wrong_channel.response.sent[0][0], self.admin_ai.NOT_OWNER_MESSAGE)

        self.assertEqual(len(orchestrator._pending_confirmations), 1)
        self.assertFalse(view.state.resolved)
        self.assertEqual(self.guild.text_channel.sent, [])

        owner = await self.decide(transport, view.state, view=view)
        self.assertEqual(len(self.guild.text_channel.sent), 1)
        self.assertEqual(strip_engine_footer(owner.followup.sent[0][0]), "ok")

    async def test_removed_whitelist_denies_without_consuming(self):
        transport, orchestrator, view = await self.pending()
        self.config["ai_allowed_role_ids"] = []
        denied = await self.decide(transport, view.state, view=view)
        self.assertEqual(denied.response.sent[0][0], self.admin_ai.ACCESS_DENIED_MESSAGE)
        self.assertEqual(len(orchestrator._pending_confirmations), 1)
        self.assertEqual(self.guild.text_channel.sent, [])

        self.config["ai_allowed_role_ids"] = [AI_ROLE_ID]
        await self.decide(transport, view.state, view=view)
        self.assertEqual(len(self.guild.text_channel.sent), 1)

    async def test_execution_time_recheck_uses_fresh_member_state(self):
        transport, orchestrator, view = await self.pending()
        # The button interaction still carries a stale member snapshot with the AI
        # role, but the guild's current member has lost it (and is an admin).
        stale = FakeMember(OWNER_ID, "owner", roles=[self.ai_role])
        stale.guild = self.guild
        self.guild.members[OWNER_ID] = FakeMember(OWNER_ID, "owner", roles=[], administrator=True, guild=self.guild)
        result = await self.decide(transport, view.state, user=stale, view=view)
        self.assertEqual(self.guild.text_channel.sent, [])
        texts = " ".join(result.all_texts())
        self.assertIn("failed", texts.lower())
        self.assertIn("no longer authorized", texts)
        self.assertEqual(self.audits, [])

    async def test_role_removed_during_generation_stops_read_execution(self):
        def drop_role(request_count):
            if request_count == 1:
                self.owner.roles = []

        provider = FakeProvider(
            self.ai_platform,
            responses=[self.call("c1", "list_channels"), self.ai_platform.AIResponse(content="x")],
            on_generate=drop_role,
        )
        transport = self.make_transport(self.make_orchestrator(provider))
        interaction = await self.start(transport)
        texts = " ".join(interaction.all_texts())
        self.assertIn("no longer authorized", texts)
        self.assertEqual(self.audits, [])

    async def test_expired_core_confirmation_executes_nothing(self):
        transport, orchestrator, view = await self.pending()
        orchestrator._pending_confirmations.clear()
        result = await self.decide(transport, view.state, view=view)
        self.assertEqual(result.followup.sent[-1][0], self.admin_ai.EXPIRED_MESSAGE)
        self.assertEqual(self.guild.text_channel.sent, [])


# ---------------------------------------------------------------------------
# Executor / audit
# ---------------------------------------------------------------------------


class ExecutorTests(AdminAITestBase):
    async def test_executor_context_and_audit_failure_does_not_fail_action(self):
        captured = []
        original = self.admin_tools.execute_tool

        async def recording_execute(context, tool_name, arguments=None):
            captured.append(context)
            return await original(context, tool_name, arguments)

        self.admin_tools.execute_tool = recording_execute
        self.addCleanup(setattr, self.admin_tools, "execute_tool", original)

        async def failing_audit(interaction, config, action, result):
            raise RuntimeError("audit down")

        transport = self.admin_ai.AITransport(
            load_config=lambda: dict(self.config),
            audit=failing_audit,
            orchestrator_factory=lambda: None,
            view_factory=RecordingView,
        )
        binding = self.admin_ai.RequestBinding(OWNER_ID, self.guild.id, 100)
        executor = transport.build_executor(self.interaction(), binding)
        result = await executor("send_message", {"channel_id": "100", "content": "hi"})
        self.assertTrue(result.ok)
        self.assertIn("audit logging failed", result.message)
        context = captured[0]
        self.assertEqual(context.source, "/ai")
        self.assertTrue(context.suppress_mentions)
        self.assertEqual(context.requesting_user_id, OWNER_ID)
        self.assertEqual(context.requesting_user_name, "owner")

    async def test_executor_denies_wrong_guild_or_user(self):
        transport = self.make_transport(object())
        binding = self.admin_ai.RequestBinding(OWNER_ID, self.guild.id, 100)
        wrong_guild = transport.build_executor(self.interaction(guild=FakeGuild(guild_id=12)), binding)
        result = await wrong_guild("send_message", {"channel_id": "100", "content": "hi"})
        self.assertFalse(result.ok)
        intruder = self.guild.add_member(FakeMember(888, "x", roles=[self.ai_role]))
        wrong_user = transport.build_executor(self.interaction(user=intruder), binding)
        self.assertFalse((await wrong_user("send_message", {"channel_id": "100", "content": "hi"})).ok)
        self.assertEqual(self.guild.text_channel.sent, [])


# ---------------------------------------------------------------------------
# Admin Tool mention behavior
# ---------------------------------------------------------------------------


class SendMessageMentionTests(AdminAITestBase):
    async def test_ai_context_suppresses_and_default_context_unchanged(self):
        ai_context = self.admin_tools.AdminToolContext(guild=self.guild, source="/ai", suppress_mentions=True)
        await self.admin_tools.execute_tool(ai_context, "send_message", {"channel_id": "100", "content": "@everyone"})
        content, kwargs = self.guild.text_channel.sent[-1]
        self.assert_mentions_none(kwargs["allowed_mentions"])

        default_context = self.admin_tools.AdminToolContext(guild=self.guild, source="/execute")
        self.assertFalse(default_context.suppress_mentions)
        await self.admin_tools.execute_tool(default_context, "send_message", {"channel_id": "100", "content": "@everyone"})
        content, kwargs = self.guild.text_channel.sent[-1]
        self.assertEqual(kwargs, {})


# ---------------------------------------------------------------------------
# Admin config validation
# ---------------------------------------------------------------------------


class AdminConfigValidationTests(unittest.TestCase):
    def load_admin(self):
        return import_admin_module()

    def base(self):
        return {
            "allow_server_administrators": True,
            "allowed_user_ids": ["1"],
            "allowed_role_ids": [],
            "audit_channel_id": None,
        }

    def test_ai_fields_validate_as_snowflake_lists_and_default_empty(self):
        admin = self.load_admin()
        legacy = admin.validate_config(self.base())
        self.assertEqual(legacy["ai_allowed_user_ids"], [])
        self.assertEqual(legacy["ai_allowed_role_ids"], [])
        self.assertEqual(legacy["allowed_user_ids"], [1])

        config = admin.validate_config({**self.base(), "ai_allowed_user_ids": ["123"], "ai_allowed_role_ids": [456]})
        self.assertEqual(config["ai_allowed_user_ids"], [123])
        self.assertEqual(config["ai_allowed_role_ids"], [456])
        for bad in ({"ai_allowed_user_ids": "123"}, {"ai_allowed_role_ids": ["abc"]}, {"ai_allowed_user_ids": [True]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                admin.validate_config({**self.base(), **bad})

    def test_defaults_and_schema_contain_ai_fields(self):
        defaults = json.loads((CORE_ROOT / "defaults" / "admin_config.json").read_text(encoding="utf-8"))
        self.assertEqual(defaults["ai_allowed_user_ids"], [])
        self.assertEqual(defaults["ai_allowed_role_ids"], [])
        self.assertTrue(defaults["allow_server_administrators"])
        schema = json.loads((PROJECT_ROOT / "bots" / "admin" / "config.schema.json").read_text(encoding="utf-8"))
        self.assertIn("ai_allowed_user_ids", schema["properties"])
        self.assertIn("ai_allowed_role_ids", schema["properties"])
        manifest = json.loads((PROJECT_ROOT / "bots" / "admin" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["config_version"], 1)


# ===========================================================================
# AI-5: natural control channel
# ===========================================================================

CONTROL_CHANNEL_ID = 555


class FakeChatMessage:
    _next_id = 9000

    def __init__(self, author, guild, channel, content, *, webhook_id=None):
        FakeChatMessage._next_id += 1
        self.id = FakeChatMessage._next_id
        self.author = author
        self.guild = guild
        self.channel = channel
        self.content = content
        self.webhook_id = webhook_id
        self.replies = []

    async def reply(self, content, **kwargs):
        self.replies.append((content, kwargs))
        self.channel.sent.append((content, dict(kwargs, _reply_to=self.id)))
        return FakeChatMessage(None, self.guild, self.channel, content)


class ControlChannelTestBase(AdminAITestBase):
    def setUp(self):
        super().setUp()
        self.control = FakeChannel(CONTROL_CHANNEL_ID, "kairo-control")
        self.guild.channels.append(self.control)
        self.config["ai_control_channel_id"] = CONTROL_CHANNEL_ID

    def natural_transport(self, orchestrator=None, *, factory=None):
        transport = self.make_transport(orchestrator, factory=factory)
        transport.set_control_channel(CONTROL_CHANNEL_ID)
        return transport

    async def say(self, transport, content, *, author=None, channel=None, guild=None, webhook_id=None):
        message = FakeChatMessage(
            author or self.owner,
            self.guild if guild is None else guild,
            channel or self.control,
            content,
            webhook_id=webhook_id,
        )
        await transport.handle_control_message(message)
        return message

    def public_sends(self):
        return list(self.control.sent)

    def assert_public_no_mentions(self):
        for _content, kwargs in self.control.sent:
            self.assertNotIn("ephemeral", kwargs)
            self.assert_mentions_none(kwargs.get("allowed_mentions"))
            if "_reply_to" in kwargs:
                self.assertIs(kwargs.get("mention_author"), False)


class ControlChannelConfigTests(unittest.TestCase):
    def test_control_channel_config_default_valid_and_invalid(self):
        admin = import_admin_module()
        base = {
            "allow_server_administrators": True,
            "allowed_user_ids": [],
            "allowed_role_ids": [],
            "audit_channel_id": None,
        }
        self.assertIsNone(admin.validate_config(dict(base))["ai_control_channel_id"])
        self.assertIsNone(admin.validate_config({**base, "ai_control_channel_id": None})["ai_control_channel_id"])
        self.assertEqual(admin.validate_config({**base, "ai_control_channel_id": "123"})["ai_control_channel_id"], 123)
        self.assertEqual(admin.validate_config({**base, "ai_control_channel_id": 456})["ai_control_channel_id"], 456)
        for bad in ("abc", True, 0, -5, [], "12a", 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                admin.validate_config({**base, "ai_control_channel_id": bad})

        defaults = json.loads((CORE_ROOT / "defaults" / "admin_config.json").read_text(encoding="utf-8"))
        self.assertIn("ai_control_channel_id", defaults)
        self.assertIsNone(defaults["ai_control_channel_id"])
        schema = json.loads((PROJECT_ROOT / "bots" / "admin" / "config.schema.json").read_text(encoding="utf-8"))
        self.assertIn("ai_control_channel_id", schema["properties"])
        self.assertNotIn("ai_control_channel_id", schema["required"])


class MessageContentIntentTests(unittest.TestCase):
    def run_main(self, admin, config, run_side_effect=None):
        seen = {}

        def fake_run(token):
            seen["message_content"] = admin.bot.intents.message_content
            seen["members"] = admin.bot.intents.members
            seen["control_channel"] = admin.ai_transport.control_channel_id
            if run_side_effect is not None:
                raise run_side_effect

        runtime = types.SimpleNamespace(instance_id="admin-main", config_path=Path("x"), token_path=Path("y"), lock_path=Path("z"))
        patches = {
            "resolve_runtime": lambda instance_id: runtime,
            "load_config": lambda runtime=None: admin.validate_config(dict(config)),
            "load_token": lambda runtime=None: "not-a-real-token",
            "acquire_single_instance_lock": lambda runtime=None: True,
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

    def base_config(self, **extra):
        return {
            "allow_server_administrators": True,
            "allowed_user_ids": [],
            "allowed_role_ids": [],
            "audit_channel_id": None,
            **extra,
        }

    def test_intent_off_without_control_channel_and_on_with_it(self):
        admin = import_admin_module()
        self.assertFalse(admin.bot.intents.message_content)
        code, seen = self.run_main(admin, self.base_config())
        self.assertEqual(code, 0)
        self.assertFalse(seen["message_content"])
        self.assertTrue(seen["members"])
        self.assertIsNone(seen["control_channel"])

        code, seen = self.run_main(admin, self.base_config(ai_control_channel_id="777"))
        self.assertEqual(code, 0)
        self.assertTrue(seen["message_content"])
        self.assertTrue(seen["members"])
        self.assertEqual(seen["control_channel"], 777)

        code, seen = self.run_main(admin, self.base_config(ai_control_channel_id=None))
        self.assertFalse(seen["message_content"])
        self.assertIsNone(seen["control_channel"])
        # /execute and /ai stay registered regardless of the intent.
        self.assertIsNotNone(admin.bot.tree.get_command("execute"))
        self.assertIsNotNone(admin.bot.tree.get_command("ai"))

    def test_privileged_intent_rejection_is_actionable_only_for_natural_ai(self):
        import discord

        admin = import_admin_module()
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code, _seen = self.run_main(
                admin,
                self.base_config(ai_control_channel_id="777"),
                run_side_effect=discord.PrivilegedIntentsRequired(None),
            )
        self.assertEqual(code, 1)
        self.assertIn("Message Content Intent", captured.getvalue())
        self.assertIn("AI control channel ID", captured.getvalue())
        with self.assertRaises(discord.PrivilegedIntentsRequired):
            self.run_main(admin, self.base_config(), run_side_effect=discord.PrivilegedIntentsRequired(None))


class ControlMessageFilterTests(ControlChannelTestBase):
    async def test_allowed_user_and_role_reach_provider_publicly(self):
        provider = FakeProvider(self.ai_platform, responses=[self.ai_platform.AIResponse(content="hi there")] * 2)
        transport = self.natural_transport(self.make_orchestrator(provider))
        await self.say(transport, "hello kairo")
        self.assertEqual(len(provider.requests), 1)

        user = self.guild.add_member(FakeMember(AI_USER_ID, "listed-user"))
        self.config["ai_allowed_user_ids"] = [AI_USER_ID]
        await self.say(transport, "hello again", author=user)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(len(self.control.sent), 2)
        self.assert_public_no_mentions()

    async def test_ignored_categories_never_construct_or_call_provider(self):
        provider = FakeProvider(self.ai_platform, responses=[self.ai_platform.AIResponse(content="x")])
        transport = self.natural_transport(self.make_orchestrator(provider))
        admin_only = self.guild.add_member(FakeMember(9, "admin", administrator=True))
        exec_only = self.guild.add_member(FakeMember(55, "exec", roles=[FakeRole(66)]))
        self.config["allowed_user_ids"] = [55]
        self.config["allowed_role_ids"] = [66]
        bot_member = self.guild.add_member(FakeMember(4321, "Kairo", roles=[self.ai_role]))
        bot_member.bot = True
        other_bot = self.guild.add_member(FakeMember(4322, "OtherBot", roles=[self.ai_role]))
        other_bot.bot = True
        other_channel = FakeChannel(456, "general")
        self.guild.channels.append(other_channel)

        cases = [
            ("administrator only", dict(author=admin_only)),
            ("/execute whitelist only", dict(author=exec_only)),
            ("wrong channel", dict(channel=other_channel)),
            ("self (bot)", dict(author=bot_member)),
            ("other bot", dict(author=other_bot)),
            ("webhook", dict(webhook_id=1234)),
            ("not a member", dict(author=types.SimpleNamespace(id=OWNER_ID, roles=[self.ai_role], bot=False))),
        ]
        for label, kwargs in cases:
            with self.subTest(case=label):
                await self.say(transport, "do something", **kwargs)
        dm = FakeChatMessage(self.owner, None, self.control, "dm text")
        await transport.handle_control_message(dm)
        for empty in ("", "   "):
            await self.say(transport, empty)

        self.assertEqual(self.factory_calls, 0)
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.control.sent, [])
        self.assertEqual(other_channel.sent, [])

    async def test_disabled_or_reconfigured_channel_is_ignored(self):
        provider = FakeProvider(self.ai_platform, responses=[self.ai_platform.AIResponse(content="x")])
        transport = self.make_transport(self.make_orchestrator(provider))
        await self.say(transport, "hello")  # control_channel_id never set at startup
        transport.set_control_channel(CONTROL_CHANNEL_ID)
        self.config["ai_control_channel_id"] = None  # cleared in config after startup
        await self.say(transport, "hello")
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.factory_calls, 0)
        self.assertEqual(self.control.sent, [])

    async def test_removed_access_takes_effect_immediately(self):
        provider = FakeProvider(self.ai_platform, responses=[self.ai_platform.AIResponse(content="x")] * 2)
        transport = self.natural_transport(self.make_orchestrator(provider))
        await self.say(transport, "first")
        self.config["ai_allowed_role_ids"] = []
        await self.say(transport, "second")
        self.assertEqual(len(provider.requests), 1)

    async def test_overlong_message_is_rejected_without_provider(self):
        provider = FakeProvider(self.ai_platform)
        transport = self.natural_transport(self.make_orchestrator(provider))
        await self.say(transport, "x" * (self.admin_ai.AI_PROMPT_MAX_CHARS + 1))
        self.assertEqual(provider.requests, [])
        self.assertEqual(len(self.control.sent), 1)
        self.assert_public_no_mentions()


class ControlMessageResponseTests(ControlChannelTestBase):
    async def test_routine_single_message_request_public_reply_with_engine_footer(self):
        class Recording:
            def __init__(self, result):
                self.result = result
                self.calls = []

            async def orchestrate(self, request, *, executor=None, confirmation_policy=None):
                self.calls.append((request, confirmation_policy))
                return self.result

        result = self.ai_orchestrator.OrchestratorResult(
            status=self.ai_orchestrator.OrchestratorStatus.COMPLETED,
            content="@everyone there are 3 channels",
            profile_id="groq-default",
            provider_id="groq",
            model_id="openai/gpt-oss-120b",
        )
        orchestrator = Recording(result)
        transport = self.natural_transport(orchestrator)
        message = await self.say(transport, "сколько у нас каналов?")

        request, policy = orchestrator.calls[0]
        self.assertEqual(request.task_class.value, "ROUTINE")
        self.assertEqual(len(request.messages), 1)
        self.assertEqual(request.messages[0].content, "сколько у нас каналов?")
        self.assertTrue(policy.confirm_normal)
        self.assertEqual(len(message.replies), 1)
        content, kwargs = message.replies[0]
        self.assertTrue(content.startswith("@everyone there are 3 channels"))
        self.assertTrue(content.endswith("-# Groq · groq-default · openai/gpt-oss-120b"))
        self.assertIs(kwargs["mention_author"], False)
        self.assert_public_no_mentions()

    async def test_no_cross_message_memory(self):
        provider = FakeProvider(
            self.ai_platform,
            responses=[self.ai_platform.AIResponse(content="a"), self.ai_platform.AIResponse(content="b")],
        )
        transport = self.natural_transport(self.make_orchestrator(provider))
        await self.say(transport, "SECRET_FIRST_PROMPT_1")
        await self.say(transport, "second request")
        second = provider.requests[1]
        user_messages = [m for m in second.messages if m.role is self.ai_platform.MessageRole.USER]
        self.assertEqual([m.content for m in user_messages], ["second request"])
        self.assertNotIn("SECRET_FIRST_PROMPT_1", json.dumps([m.public_dict() for m in second.messages]))
        self.assertEqual(self.factory_calls, 1)  # same per-process orchestrator

    async def test_provider_failure_is_contained_publicly(self):
        provider = FakeProvider(self.ai_platform, error=RuntimeError("Authorization: Bearer sk-secret"))
        transport = self.natural_transport(self.make_orchestrator(provider))
        await self.say(transport, "hello")
        text = " ".join(content for content, _ in self.control.sent)
        self.assertIn("unavailable", text.lower())
        self.assertNotIn("sk-secret", text)
        self.assertNotIn("Authorization", text)
        self.assert_public_no_mentions()

    async def test_read_tool_runs_automatically(self):
        provider = FakeProvider(
            self.ai_platform, responses=[self.call("c1", "list_channels"), self.ai_platform.AIResponse(content="2 channels")]
        )
        transport = self.natural_transport(self.make_orchestrator(provider))
        await self.say(transport, "list channels")
        self.assertEqual(self.audits[0][0], "/ai list_channels")
        self.assertTrue(self.control.sent[0][0].startswith("2 channels"))
        self.assertNotIn("view", self.control.sent[0][1])


class ControlChannelConfirmationTests(ControlChannelTestBase):
    async def pending(self, tool_response, extra=()):
        provider = FakeProvider(self.ai_platform, responses=[tool_response, *extra])
        orchestrator = self.make_orchestrator(provider)
        transport = self.natural_transport(orchestrator)
        await self.say(transport, "please do it")
        views = [kwargs["view"] for _content, kwargs in self.control.sent if "view" in kwargs]
        return transport, orchestrator, views

    def button_interaction(self, user=None, channel_id=CONTROL_CHANNEL_ID, guild=None):
        return self.interaction(user=user, guild=guild, channel_id=channel_id)

    async def test_normal_and_destructive_plans_get_public_preview_and_buttons(self):
        for response, risk in (
            (self.call("c1", "send_message", channel_id="100", content="@here hi"), "NORMAL"),
            (self.call("c2", "ban_member", member_id="777"), "DESTRUCTIVE"),
        ):
            with self.subTest(risk=risk):
                self.control.sent.clear()
                transport, orchestrator, views = await self.pending(response)
                self.assertEqual(len(views), 1)
                pages = [content for content, _ in self.control.sent if "review page" in content]
                self.assertTrue(pages)
                self.assertIn(f'risk `"{risk}"`', "\n".join(pages))
                control = self.control.sent[-1][0]
                self.assertIn(f"overall risk {risk}", control)
                self.assertIn("-# fake · routine · fake-model", control)
                self.assertEqual(self.guild.text_channel.sent, [])
                self.assertEqual(self.target.calls, [])
                self.assertEqual(views[0].state.delivery_mode, self.admin_ai.DeliveryMode.PUBLIC)
                self.assert_public_no_mentions()

    async def test_public_buttons_enforce_ownership_and_continue_publicly(self):
        transport, orchestrator, views = await self.pending(
            self.call("c1", "send_message", channel_id="100", content="hi"),
            extra=(self.ai_platform.AIResponse(content="sent"),),
        )
        view = views[0]
        intruder = self.guild.add_member(FakeMember(888, "intruder", roles=[self.ai_role], administrator=True))
        denied = self.button_interaction(user=intruder)
        await transport.handle_decision(denied, view.state, approved=True, view=view)
        self.assertEqual(denied.response.sent[0][0], self.admin_ai.NOT_OWNER_MESSAGE)
        self.assertTrue(denied.response.sent[0][1]["ephemeral"])
        self.assertEqual(denied.response.edited, [])
        wrong_channel = self.button_interaction(channel_id=999)
        await transport.handle_decision(wrong_channel, view.state, approved=True, view=view)
        wrong_guild = self.button_interaction(guild=FakeGuild(guild_id=11))
        await transport.handle_decision(wrong_guild, view.state, approved=True, view=view)
        self.config["ai_allowed_role_ids"] = []
        revoked = self.button_interaction()
        await transport.handle_decision(revoked, view.state, approved=True, view=view)
        self.assertEqual(revoked.response.sent[0][0], self.admin_ai.ACCESS_DENIED_MESSAGE)
        self.assertTrue(revoked.response.sent[0][1]["ephemeral"])
        self.assertFalse(view.state.resolved)
        self.assertEqual(len(orchestrator._pending_confirmations), 1)
        self.assertEqual(self.guild.text_channel.sent, [])

        self.config["ai_allowed_role_ids"] = [AI_ROLE_ID]
        owner = self.button_interaction()
        await transport.handle_decision(owner, view.state, approved=True, view=view)
        self.assertEqual(len(self.guild.text_channel.sent), 1)
        self.assert_mentions_none(self.guild.text_channel.sent[0][1]["allowed_mentions"])
        self.assertTrue(owner.followup.sent)
        for _content, kwargs in owner.followup.sent:
            self.assertIs(kwargs["ephemeral"], False)
            self.assert_mentions_none(kwargs["allowed_mentions"])
        self.assertEqual(strip_engine_footer(owner.followup.sent[0][0]), "sent")
        self.assertIsNone(owner.response.edited[0]["view"])

    async def test_slash_ai_continuation_stays_ephemeral(self):
        provider = FakeProvider(
            self.ai_platform,
            responses=[self.call("c1", "send_message", channel_id="100", content="hi"), self.ai_platform.AIResponse(content="done")],
        )
        transport = self.make_transport(self.make_orchestrator(provider))
        first = await self.start(transport)
        view = self.views(first)[0]
        self.assertEqual(view.state.delivery_mode, self.admin_ai.DeliveryMode.EPHEMERAL)
        approval = await self.decide(transport, view.state, approved=True, view=view)
        for _content, kwargs in approval.followup.sent:
            self.assertIs(kwargs["ephemeral"], True)
        self.assertEqual(self.control.sent, [])


class EngineFooterTests(AdminAITestBase):
    def result(self, **fields):
        return self.ai_orchestrator.OrchestratorResult(status=self.ai_orchestrator.OrchestratorStatus.COMPLETED, **fields)

    def test_engine_footer_formats(self):
        render = self.admin_ai.render_engine_footer
        self.assertEqual(
            render(self.result(provider_id="groq", profile_id="groq-default", model_id="llama-x")),
            "-# Groq · groq-default · llama-x",
        )
        self.assertEqual(
            render(self.result(provider_id="gemini", profile_id="gemini-default", model_id="gemini-2.5-flash", fallback_used=True)),
            "-# Gemini · gemini-default · gemini-2.5-flash · fallback",
        )
        self.assertEqual(render(self.result()), "")
        self.assertEqual(render(types.SimpleNamespace()), "")
        footer = render(self.result(provider_id="groq", profile_id="p", model_id="m" * 500))
        self.assertLessEqual(len(footer), 3 + self.admin_ai.AI_ENGINE_FOOTER_CHARS)
        long_text = self.admin_ai.with_footer("y" * 5000, footer)
        self.assertLessEqual(len(long_text), self.admin_ai.DISCORD_MESSAGE_LIMIT)

    def test_engine_footer_never_uses_attempts_or_credentials(self):
        attempt = self.ai_orchestrator.AttemptRecord("p", "groq", "m", "FAILED", False, "Authorization secret-ish")
        footer = self.admin_ai.render_engine_footer(
            self.result(provider_id="groq", profile_id="p", model_id="m", attempts=(attempt,))
        )
        self.assertEqual(footer, "-# Groq · p · m")

if __name__ == "__main__":
    unittest.main()
