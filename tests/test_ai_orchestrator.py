import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT / "DarkAbyss_Core"


def load_modules():
    sys.path.insert(0, str(CORE_ROOT))
    for name in ("ai_orchestrator", "ai_platform", "admin_tools"):
        sys.modules.pop(name, None)
    import ai_platform
    import ai_orchestrator
    import admin_tools

    return ai_platform, ai_orchestrator, admin_tools


class FakeProvider:
    def __init__(self, ai_platform, provider_id="fake", responses=None, error=None, local_state=None):
        self._metadata = ai_platform.ProviderMetadata(
            provider_id=provider_id,
            display_name=f"{provider_id} provider",
            models=(ai_platform.ProviderModel("fake-model", "Fake Model", supports_tool_calls=True),),
        )
        self.responses = list(responses or [])
        self.error = error
        self.local_state = local_state or ai_platform.Availability(ai_platform.AvailabilityState.AVAILABLE, "available")
        self.requests = []

    @property
    def metadata(self):
        return self._metadata

    def get_local_availability(self, *, credential_ref=None, credential_available=False):
        return self.local_state

    async def test_connection(self, credential_ref=None):
        raise AssertionError("orchestrator must not call test_connection")

    async def generate(self, request, credential_ref=None):
        self.requests.append((request, credential_ref))
        if self.error is not None:
            raise self.error
        if not self.responses:
            return sys.modules["ai_platform"].AIResponse(content="default")
        next_item = self.responses.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item


class FakeExecutor:
    def __init__(self, admin_tools, results=None):
        self.admin_tools = admin_tools
        self.results = list(results or [])
        self.calls = []

    async def __call__(self, tool_name, arguments):
        self.calls.append((tool_name, dict(arguments)))
        if self.results:
            return self.results.pop(0)
        return self.admin_tools.ToolResult(True, tool_name, f"{tool_name} ok", {"seen": True})


class RaisingExecutor:
    SECRET = "Bearer sk-executor-secret-value"

    def __init__(self, admin_tools, raise_on):
        self.admin_tools = admin_tools
        self.raise_on = raise_on
        self.calls = []

    async def __call__(self, tool_name, arguments):
        index = len(self.calls)
        self.calls.append(tool_name)
        if index == self.raise_on:
            raise RuntimeError(f"unexpected executor crash {self.SECRET}")
        return self.admin_tools.ToolResult(True, tool_name, f"{tool_name} ok", {"seen": True})


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self, ai_platform, settings):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "config" / "ai.json"
        store = ai_platform.AISettingsStore(path)
        store.save(settings)
        credentials = ai_platform.CredentialStore(Path(temp_dir.name) / "secrets" / "ai")
        return store, credentials

    def make_orchestrator(self, ai_platform, ai_orchestrator, settings, providers):
        store, credentials = self.make_store(ai_platform, settings)
        registry = ai_platform.ProviderRegistry(providers)
        return ai_orchestrator.AIOrchestrator(
            settings_store=store,
            provider_registry=registry,
            credential_store=credentials,
        )

    def profile(self, ai_platform, profile_id, provider_id="fake", credential_ref=None, enabled=True):
        return ai_platform.AIProfile(profile_id, provider_id, "fake-model", credential_ref=credential_ref, enabled=enabled)

    def user_message(self, ai_platform, text="hello"):
        return ai_platform.AIMessage(role=ai_platform.MessageRole.USER, content=text)

    async def test_zero_profiles_direct_and_invalid_settings_are_contained(self):
        ai_platform, ai_orchestrator, _admin_tools = load_modules()
        settings = ai_platform.AISettings()
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {})

        unavailable = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(
                messages=(self.user_message(ai_platform),),
                task_class=ai_platform.TaskClass.ROUTINE,
            )
        )
        self.assertEqual(unavailable.status, ai_orchestrator.OrchestratorStatus.UNAVAILABLE)

        direct = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(
                messages=(self.user_message(ai_platform, "direct"),),
                task_class=ai_platform.TaskClass.DIRECT,
            )
        )
        self.assertEqual(direct.status, ai_orchestrator.OrchestratorStatus.DIRECT)
        self.assertEqual(direct.content, "direct")

        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "ai.json"
            settings_path.write_text("{bad", encoding="utf-8")
            invalid = ai_orchestrator.AIOrchestrator(
                settings_store=ai_platform.AISettingsStore(settings_path),
                provider_registry=ai_platform.ProviderRegistry(),
                credential_store=ai_platform.CredentialStore(Path(temp_dir) / "secrets"),
            )
            result = await invalid.orchestrate(
                ai_orchestrator.OrchestratorRequest(
                    messages=(self.user_message(ai_platform),),
                    task_class=ai_platform.TaskClass.ROUTINE,
                )
            )
            self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.UNAVAILABLE)
            self.assertEqual(settings_path.read_text(encoding="utf-8"), "{bad")

    async def test_routing_manual_override_and_explicit_fallback(self):
        ai_platform, ai_orchestrator, _admin_tools = load_modules()
        primary = FakeProvider(ai_platform, "primary", error=RuntimeError("down"))
        fallback = FakeProvider(ai_platform, "fallback", responses=[ai_platform.AIResponse(content="fallback ok")])
        manual = FakeProvider(ai_platform, "manual", responses=[ai_platform.AIResponse(content="manual ok")])
        settings = ai_platform.AISettings(
            profiles=(
                self.profile(ai_platform, "primary", "primary"),
                self.profile(ai_platform, "fallback", "fallback"),
                self.profile(ai_platform, "manual", "manual"),
            ),
            routing=ai_platform.RoutingConfig(
                routine_profile_id="primary",
                routine_fallback_profile_ids=("fallback",),
                planner_profile_id="manual",
            ),
        )
        orchestrator = self.make_orchestrator(
            ai_platform,
            ai_orchestrator,
            settings,
            {"primary": primary, "fallback": fallback, "manual": manual},
        )

        routed = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE")
        )
        self.assertEqual(routed.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual(routed.profile_id, "fallback")
        self.assertTrue(routed.fallback_used)
        fallback_calls_after_routed = len(fallback.requests)

        manual_result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(
                messages=(self.user_message(ai_platform),),
                task_class="PLANNER",
                manual_profile_id="manual",
            )
        )
        self.assertEqual(manual_result.content, "manual ok")
        self.assertEqual(manual_result.profile_id, "manual")

        no_manual_fallback = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(
                messages=(self.user_message(ai_platform),),
                task_class="ROUTINE",
                manual_profile_id="primary",
            )
        )
        self.assertEqual(no_manual_fallback.status, ai_orchestrator.OrchestratorStatus.UNAVAILABLE)
        self.assertEqual(len(fallback.requests), fallback_calls_after_routed)

        fallback.responses.append(ai_platform.AIResponse(content="manual fallback ok"))
        manual_fallback = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(
                messages=(self.user_message(ai_platform),),
                task_class="ROUTINE",
                manual_profile_id="primary",
                allow_fallback_on_manual_override=True,
            )
        )
        self.assertEqual(manual_fallback.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual(manual_fallback.profile_id, "fallback")

    async def test_missing_credential_and_disabled_profiles_are_skipped_for_fallback(self):
        ai_platform, ai_orchestrator, _admin_tools = load_modules()
        fallback = FakeProvider(ai_platform, "fallback", responses=[ai_platform.AIResponse(content="ok")])
        settings = ai_platform.AISettings(
            profiles=(
                self.profile(ai_platform, "primary", "missing-provider", credential_ref="missing"),
                self.profile(ai_platform, "disabled", "fallback", enabled=False),
                self.profile(ai_platform, "fallback", "fallback"),
            ),
            routing=ai_platform.RoutingConfig(
                routine_profile_id="primary",
                routine_fallback_profile_ids=("disabled", "fallback"),
            ),
        )
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"fallback": fallback})

        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE")
        )

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual(result.profile_id, "fallback")

    async def test_tool_validation_risk_confirmation_and_exact_approval(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        destructive = ai_platform.AIResponse(
            content="plan",
            tool_calls=(
                ai_platform.AIToolCall(
                    call_id="call-1",
                    tool_name="list_channels",
                    arguments={},
                ),
                ai_platform.AIToolCall(
                    call_id="call-2",
                    tool_name="ban_member",
                    arguments={"member_id": "123", "reason": None},
                ),
            ),
        )
        provider = FakeProvider(ai_platform, responses=[destructive, ai_platform.AIResponse(content="done")])
        settings = ai_platform.AISettings(
            profiles=(self.profile(ai_platform, "routine"),),
            routing=ai_platform.RoutingConfig(routine_profile_id="routine"),
        )
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"fake": provider})
        executor = FakeExecutor(admin_tools)

        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=executor,
        )

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)
        self.assertEqual(result.tool_risk, ai_platform.ToolRisk.DESTRUCTIVE)
        self.assertEqual(executor.calls, [])

        approved = await orchestrator.approve_confirmation(result.confirmation_id, approved=True, executor=executor)
        self.assertEqual(approved.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual([call[0] for call in executor.calls], ["list_channels", "ban_member"])
        self.assertEqual(approved.executed_tools[0].tool_name, "list_channels")
        reused = await orchestrator.approve_confirmation(result.confirmation_id, approved=True, executor=executor)
        self.assertEqual(reused.status, ai_orchestrator.OrchestratorStatus.CANCELLED)

    async def test_confirmation_reject_and_normal_confirm_policy(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        response = ai_platform.AIResponse(
            tool_calls=(
                ai_platform.AIToolCall(
                    call_id="call-1",
                    tool_name="send_message",
                    arguments={"channel_id": "123", "content": "hi", "reason": None},
                ),
            ),
        )
        provider = FakeProvider(ai_platform, responses=[response])
        settings = ai_platform.AISettings(
            profiles=(self.profile(ai_platform, "routine"),),
            routing=ai_platform.RoutingConfig(routine_profile_id="routine"),
        )
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"fake": provider})
        executor = FakeExecutor(admin_tools)

        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=executor,
            confirmation_policy=ai_orchestrator.ConfirmationPolicy(confirm_normal=True),
        )

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)
        rejected = await orchestrator.approve_confirmation(result.confirmation_id, approved=False, executor=executor)
        self.assertEqual(rejected.status, ai_orchestrator.OrchestratorStatus.CANCELLED)
        self.assertEqual(executor.calls, [])

    async def test_invalid_tool_plans_do_not_execute(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        cases = [
            ai_platform.AIResponse(tool_calls=(ai_platform.AIToolCall(call_id="x", tool_name="unknown_tool", arguments={}),)),
            ai_platform.AIResponse(
                tool_calls=(
                    ai_platform.AIToolCall(call_id="x", tool_name="send_message", arguments={"channel_id": "123"}),
                )
            ),
            ai_platform.AIResponse(
                tool_calls=(
                    ai_platform.AIToolCall(
                        call_id="x",
                        tool_name="send_message",
                        arguments={"channel_id": "123", "content": "hi", "extra": "bad"},
                    ),
                )
            ),
            ai_platform.AIResponse(
                tool_calls=(
                    ai_platform.AIToolCall(call_id="x", tool_name="send_message", arguments={"channel_id": "bad", "content": "hi"}),
                )
            ),
            ai_platform.AIResponse(
                tool_calls=(
                    ai_platform.AIToolCall(call_id="dup", tool_name="list_channels", arguments={}),
                    ai_platform.AIToolCall(call_id="dup", tool_name="list_roles", arguments={}),
                )
            ),
        ]
        for response in cases:
            provider = FakeProvider(ai_platform, responses=[response])
            settings = ai_platform.AISettings(
                profiles=(self.profile(ai_platform, "routine"),),
                routing=ai_platform.RoutingConfig(routine_profile_id="routine"),
            )
            orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"fake": provider})
            executor = FakeExecutor(admin_tools)
            with self.subTest(response=response):
                result = await orchestrator.orchestrate(
                    ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
                    executor=executor,
                )
                self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.INVALID_TOOL_PLAN)
                self.assertEqual(executor.calls, [])

        disallowed_provider = FakeProvider(
            ai_platform,
            responses=[
                ai_platform.AIResponse(
                    tool_calls=(ai_platform.AIToolCall(call_id="x", tool_name="send_message", arguments={"channel_id": "123", "content": "hi"}),)
                )
            ],
        )
        settings = ai_platform.AISettings(
            profiles=(self.profile(ai_platform, "routine"),),
            routing=ai_platform.RoutingConfig(routine_profile_id="routine"),
        )
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"fake": disallowed_provider})
        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(
                messages=(self.user_message(ai_platform),),
                task_class="ROUTINE",
                allowed_tool_names=("list_channels",),
            )
        )
        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.INVALID_TOOL_PLAN)

    async def test_tool_loop_preserves_ids_metadata_and_same_provider(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        metadata = {"gemini": {"visible_text_parts": [{"text": "plan", "thought_signature": None}]}}
        response = ai_platform.AIResponse(
            content="plan",
            metadata=metadata,
            tool_calls=(ai_platform.AIToolCall(call_id="call-1", tool_name="list_channels", arguments={}),),
        )
        provider = FakeProvider(ai_platform, responses=[response, ai_platform.AIResponse(content="final")])
        settings = ai_platform.AISettings(
            profiles=(self.profile(ai_platform, "routine"),),
            routing=ai_platform.RoutingConfig(routine_profile_id="routine"),
        )
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"fake": provider})

        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=FakeExecutor(admin_tools),
        )

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual(len(provider.requests), 2)
        second_messages = provider.requests[1][0].messages
        assistant = [message for message in second_messages if message.role is ai_platform.MessageRole.ASSISTANT][0]
        tool = [message for message in second_messages if message.role is ai_platform.MessageRole.TOOL][0]
        self.assertEqual(assistant.tool_calls[0].call_id, "call-1")
        self.assertEqual(assistant.metadata["gemini"]["visible_text_parts"][0]["text"], "plan")
        self.assertEqual(tool.tool_call_id, "call-1")
        self.assertNotIn("metadata", result.public_dict())

    async def test_tool_execution_failure_and_oversized_result_are_bounded(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        provider = FakeProvider(
            ai_platform,
            responses=[
                ai_platform.AIResponse(
                    tool_calls=(
                        ai_platform.AIToolCall(call_id="call-1", tool_name="list_channels", arguments={}),
                        ai_platform.AIToolCall(call_id="call-2", tool_name="list_roles", arguments={}),
                    )
                )
            ],
        )
        settings = ai_platform.AISettings(
            profiles=(self.profile(ai_platform, "routine"),),
            routing=ai_platform.RoutingConfig(routine_profile_id="routine"),
        )
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"fake": provider})
        executor = FakeExecutor(
            admin_tools,
            results=[
                admin_tools.ToolResult(False, "list_channels", "failed", {"huge": "x" * (ai_orchestrator.MAX_TOOL_RESULT_BYTES + 1)}),
            ],
        )

        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=executor,
        )

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.TOOL_EXECUTION_FAILED)
        self.assertEqual([call[0] for call in executor.calls], ["list_channels"])
        self.assertNotIn("x" * 1000, json.dumps(result.public_dict()))

    async def test_provider_failure_after_tool_execution_does_not_fallback(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        primary = FakeProvider(
            ai_platform,
            "primary",
            responses=[
                ai_platform.AIResponse(
                    tool_calls=(
                        ai_platform.AIToolCall(
                            call_id="call-1", tool_name="send_message", arguments={"channel_id": "100", "content": "hi"}
                        ),
                    )
                ),
                RuntimeError("after side effect"),
            ],
        )
        fallback = FakeProvider(ai_platform, "fallback", responses=[ai_platform.AIResponse(content="fallback")])
        orchestrator = self.read_fallback_orchestrator(ai_platform, ai_orchestrator, primary, fallback)

        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=FakeExecutor(admin_tools),
        )

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.UNAVAILABLE)
        self.assertEqual(result.executed_tools[0].tool_name, "send_message")
        self.assertEqual(fallback.requests, [])

    async def test_provider_failure_after_only_read_tools_falls_back_from_scratch(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        primary = FakeProvider(
            ai_platform,
            "primary",
            responses=[
                ai_platform.AIResponse(
                    tool_calls=(ai_platform.AIToolCall(call_id="call-1", tool_name="list_channels", arguments={}),)
                ),
                RuntimeError("503 high demand"),
            ],
        )
        fallback = FakeProvider(ai_platform, "fallback", responses=[ai_platform.AIResponse(content="fallback done")])
        orchestrator = self.read_fallback_orchestrator(ai_platform, ai_orchestrator, primary, fallback)

        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=FakeExecutor(admin_tools),
        )

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual(result.content, "fallback done")
        self.assertTrue(result.fallback_used)
        # The fallback starts over from the original messages only.
        self.assertFalse(any(m.role.value == "tool" for m in fallback.requests[0][0].messages))

    def read_fallback_orchestrator(self, ai_platform, ai_orchestrator, primary, fallback):
        settings = ai_platform.AISettings(
            profiles=(self.profile(ai_platform, "primary", "primary"), self.profile(ai_platform, "fallback", "fallback")),
            routing=ai_platform.RoutingConfig(
                routine_profile_id="primary",
                routine_fallback_profile_ids=("fallback",),
            ),
        )
        return self.make_orchestrator(
            ai_platform,
            ai_orchestrator,
            settings,
            {"primary": primary, "fallback": fallback},
        )

    async def test_limits_stop_unbounded_tool_loops_and_large_batches(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        looping_responses = [
            ai_platform.AIResponse(tool_calls=(ai_platform.AIToolCall(call_id=f"call-{index}", tool_name="list_channels", arguments={}),))
            for index in range(ai_orchestrator.MAX_TOOL_ROUNDS + 1)
        ]
        provider = FakeProvider(ai_platform, responses=looping_responses)
        settings = ai_platform.AISettings(
            profiles=(self.profile(ai_platform, "routine"),),
            routing=ai_platform.RoutingConfig(routine_profile_id="routine"),
        )
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"fake": provider})
        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=FakeExecutor(admin_tools),
        )
        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.LIMIT_REACHED)

        too_many = ai_platform.AIResponse(
            tool_calls=tuple(
                ai_platform.AIToolCall(call_id=f"call-{index}", tool_name="list_channels", arguments={})
                for index in range(ai_orchestrator.MAX_TOOL_CALLS_PER_ROUND + 1)
            )
        )
        provider = FakeProvider(ai_platform, responses=[too_many])
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"fake": provider})
        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=FakeExecutor(admin_tools),
        )
        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.LIMIT_REACHED)

    async def test_compare_mode_is_plan_only_and_selection_is_single_use(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        provider_a = FakeProvider(ai_platform, "a", responses=[ai_platform.AIResponse(content="text")])
        provider_b = FakeProvider(
            ai_platform,
            "b",
            responses=[
                ai_platform.AIResponse(
                    tool_calls=(ai_platform.AIToolCall(call_id="call-1", tool_name="list_channels", arguments={}),)
                ),
                ai_platform.AIResponse(content="after read"),
            ],
        )
        provider_c = FakeProvider(
            ai_platform,
            "c",
            responses=[
                ai_platform.AIResponse(
                    tool_calls=(ai_platform.AIToolCall(call_id="call-2", tool_name="ban_member", arguments={"member_id": "123"}),)
                )
            ],
        )
        settings = ai_platform.AISettings(
            profiles=(
                self.profile(ai_platform, "pa", "a"),
                self.profile(ai_platform, "pb", "b"),
                self.profile(ai_platform, "pc", "c"),
            )
        )
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"a": provider_a, "b": provider_b, "c": provider_c})
        executor = FakeExecutor(admin_tools)

        compare = await orchestrator.compare_plans(
            ai_platform.ComparePlanRequest(("pa", "pb"), ai_platform.ToolRisk.READ),
            messages=(self.user_message(ai_platform),),
        )

        self.assertEqual(compare.status, ai_orchestrator.CompareStatus.COMPLETED)
        self.assertEqual(len(compare.candidates), 2)
        self.assertEqual(executor.calls, [])

        selected = await orchestrator.select_compare_candidate(compare.compare_id, "pb", executor=executor)
        self.assertEqual(selected.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual(executor.calls[0][0], "list_channels")
        reused = await orchestrator.select_compare_candidate(compare.compare_id, "pa", executor=executor)
        self.assertEqual(reused.status, ai_orchestrator.OrchestratorStatus.CANCELLED)

        compare_destructive = await orchestrator.compare_plans(
            ai_platform.ComparePlanRequest(("pa", "pc"), ai_platform.ToolRisk.DESTRUCTIVE),
            messages=(self.user_message(ai_platform),),
        )
        selected_destructive = await orchestrator.select_compare_candidate(
            compare_destructive.compare_id,
            "pc",
            executor=executor,
        )
        self.assertEqual(selected_destructive.status, ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)
        self.assertEqual(len(executor.calls), 1)

    async def test_compare_failure_isolation_and_no_fallback(self):
        ai_platform, ai_orchestrator, _admin_tools = load_modules()
        failing = FakeProvider(ai_platform, "a", error=RuntimeError("down"))
        ok = FakeProvider(ai_platform, "b", responses=[ai_platform.AIResponse(content="ok")])
        fallback = FakeProvider(ai_platform, "c", responses=[ai_platform.AIResponse(content="fallback")])
        settings = ai_platform.AISettings(
            profiles=(
                self.profile(ai_platform, "pa", "a"),
                self.profile(ai_platform, "pb", "b"),
                self.profile(ai_platform, "pc", "c"),
            ),
            routing=ai_platform.RoutingConfig(routine_fallback_profile_ids=("pc",)),
        )
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"a": failing, "b": ok, "c": fallback})

        compare = await orchestrator.compare_plans(
            ai_platform.ComparePlanRequest(("pa", "pb"), ai_platform.ToolRisk.READ),
            messages=(self.user_message(ai_platform),),
        )

        self.assertEqual(compare.status, ai_orchestrator.CompareStatus.COMPLETED)
        self.assertEqual(compare.candidates[0].status, ai_orchestrator.CompareStatus.UNAVAILABLE)
        self.assertEqual(compare.candidates[1].status, ai_orchestrator.CompareStatus.COMPLETED)
        self.assertEqual(fallback.requests, [])

    # ------------------------------------------------------------------
    # AI-3A review-fix regression tests
    # ------------------------------------------------------------------

    def single_profile_orchestrator(self, ai_platform, ai_orchestrator, provider):
        settings = ai_platform.AISettings(
            profiles=(self.profile(ai_platform, "routine"),),
            routing=ai_platform.RoutingConfig(routine_profile_id="routine"),
        )
        return self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"fake": provider})

    @staticmethod
    def tool_names(request):
        return [schema["name"] for schema in request.tools]

    @staticmethod
    def system_count(ai_platform, request):
        return sum(1 for message in request.messages if message.role is ai_platform.MessageRole.SYSTEM)

    @staticmethod
    def call(ai_platform, call_id, tool_name, **arguments):
        return ai_platform.AIResponse(
            tool_calls=(ai_platform.AIToolCall(call_id=call_id, tool_name=tool_name, arguments=arguments),)
        )

    async def test_explicit_allowlist_survives_tool_round_continuation(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        allowed = ("list_channels", "get_member_details")
        provider = FakeProvider(
            ai_platform,
            responses=[
                self.call(ai_platform, "call-1", "list_channels"),
                self.call(ai_platform, "call-2", "get_member_details", member_id="123"),
                ai_platform.AIResponse(content="final"),
            ],
        )
        orchestrator = self.single_profile_orchestrator(ai_platform, ai_orchestrator, provider)
        executor = FakeExecutor(admin_tools)

        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(
                messages=(self.user_message(ai_platform),),
                task_class="ROUTINE",
                allowed_tool_names=allowed,
            ),
            executor=executor,
        )

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual([call[0] for call in executor.calls], ["list_channels", "get_member_details"])
        self.assertEqual(len(provider.requests), 3)
        for request, _credential in provider.requests:
            self.assertEqual(sorted(self.tool_names(request)), sorted(allowed))

    async def test_explicit_allowlist_survives_confirmation_continuation(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        allowed = ("ban_member", "get_member_details")
        provider = FakeProvider(
            ai_platform,
            responses=[
                self.call(ai_platform, "call-1", "ban_member", member_id="123"),
                self.call(ai_platform, "call-2", "get_member_details", member_id="123"),
                self.call(ai_platform, "call-3", "send_message", channel_id="123", content="hi"),
            ],
        )
        orchestrator = self.single_profile_orchestrator(ai_platform, ai_orchestrator, provider)
        executor = FakeExecutor(admin_tools)

        pending = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(
                messages=(self.user_message(ai_platform),),
                task_class="ROUTINE",
                allowed_tool_names=allowed,
            ),
            executor=executor,
        )
        self.assertEqual(pending.status, ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)
        result = await orchestrator.approve_confirmation(pending.confirmation_id, approved=True, executor=executor)

        # get_member_details was not in the previous batch but is in the ORIGINAL allowlist.
        self.assertEqual([call[0] for call in executor.calls], ["ban_member", "get_member_details"])
        # send_message is outside the original allowlist and must stay rejected.
        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.INVALID_TOOL_PLAN)
        self.assertEqual(len(provider.requests), 3)
        for request, _credential in provider.requests:
            self.assertEqual(sorted(self.tool_names(request)), sorted(allowed))

    async def test_allowlist_none_keeps_all_tools_through_confirmation_continuation(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        provider = FakeProvider(
            ai_platform,
            responses=[
                self.call(ai_platform, "call-1", "ban_member", member_id="123"),
                self.call(ai_platform, "call-2", "send_message", channel_id="123", content="hi"),
                ai_platform.AIResponse(content="final"),
            ],
        )
        orchestrator = self.single_profile_orchestrator(ai_platform, ai_orchestrator, provider)
        executor = FakeExecutor(admin_tools)

        pending = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=executor,
        )
        result = await orchestrator.approve_confirmation(pending.confirmation_id, approved=True, executor=executor)

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual([call[0] for call in executor.calls], ["ban_member", "send_message"])
        all_tools = sorted(schema["name"] for schema in admin_tools.list_provider_tool_schemas())
        self.assertEqual(len(provider.requests), 3)
        for request, _credential in provider.requests:
            self.assertEqual(sorted(self.tool_names(request)), all_tools)

    async def test_empty_allowlist_exposes_zero_tools(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        provider = FakeProvider(
            ai_platform,
            responses=[ai_platform.AIResponse(content="text only"), self.call(ai_platform, "call-1", "list_channels")],
        )
        orchestrator = self.single_profile_orchestrator(ai_platform, ai_orchestrator, provider)
        executor = FakeExecutor(admin_tools)
        request = ai_orchestrator.OrchestratorRequest(
            messages=(self.user_message(ai_platform),),
            task_class="ROUTINE",
            allowed_tool_names=(),
        )

        completed = await orchestrator.orchestrate(request, executor=executor)
        rejected = await orchestrator.orchestrate(request, executor=executor)

        self.assertEqual(completed.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual(rejected.status, ai_orchestrator.OrchestratorStatus.INVALID_TOOL_PLAN)
        self.assertEqual(executor.calls, [])
        for sent, _credential in provider.requests:
            self.assertEqual(tuple(sent.tools), ())

        compare_provider = FakeProvider(ai_platform, "b", responses=[self.call(ai_platform, "call-1", "list_channels")])
        text_provider = FakeProvider(ai_platform, "a", responses=[ai_platform.AIResponse(content="text")])
        settings = ai_platform.AISettings(profiles=(self.profile(ai_platform, "pa", "a"), self.profile(ai_platform, "pb", "b")))
        compare_orchestrator = self.make_orchestrator(
            ai_platform, ai_orchestrator, settings, {"a": text_provider, "b": compare_provider}
        )
        compare = await compare_orchestrator.compare_plans(
            ai_platform.ComparePlanRequest(("pa", "pb"), ai_platform.ToolRisk.READ),
            messages=(self.user_message(ai_platform),),
            allowed_tool_names=(),
        )
        self.assertEqual(compare.candidates[1].status, ai_orchestrator.CompareStatus.UNAVAILABLE)
        self.assertEqual(tuple(compare_provider.requests[0][0].tools), ())
        self.assertEqual(tuple(text_provider.requests[0][0].tools), ())

    async def test_confirmation_resume_has_exactly_one_system_instruction(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        provider = FakeProvider(
            ai_platform,
            responses=[
                self.call(ai_platform, "call-1", "list_channels"),
                self.call(ai_platform, "call-2", "ban_member", member_id="123"),
                ai_platform.AIResponse(content="final"),
            ],
        )
        orchestrator = self.single_profile_orchestrator(ai_platform, ai_orchestrator, provider)
        executor = FakeExecutor(admin_tools)

        pending = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=executor,
        )
        self.assertEqual(pending.status, ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)
        result = await orchestrator.approve_confirmation(pending.confirmation_id, approved=True, executor=executor)

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual(len(provider.requests), 3)
        for request, _credential in provider.requests:
            self.assertEqual(self.system_count(ai_platform, request), 1)
            self.assertIs(request.messages[0].role, ai_platform.MessageRole.SYSTEM)
        final_messages = provider.requests[2][0].messages
        self.assertEqual(
            [message.role for message in final_messages],
            [
                ai_platform.MessageRole.SYSTEM,
                ai_platform.MessageRole.USER,
                ai_platform.MessageRole.ASSISTANT,
                ai_platform.MessageRole.TOOL,
                ai_platform.MessageRole.ASSISTANT,
                ai_platform.MessageRole.TOOL,
            ],
        )

    async def test_compare_selected_continuation_has_one_system_and_original_allowlist(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        allowed = ("list_channels", "get_member_details", "ban_member")
        text_provider = FakeProvider(ai_platform, "a", responses=[ai_platform.AIResponse(content="text")])
        tool_provider = FakeProvider(
            ai_platform,
            "b",
            responses=[
                self.call(ai_platform, "call-1", "list_channels"),
                self.call(ai_platform, "call-2", "get_member_details", member_id="123"),
                self.call(ai_platform, "call-3", "ban_member", member_id="123"),
                ai_platform.AIResponse(content="final"),
            ],
        )
        settings = ai_platform.AISettings(profiles=(self.profile(ai_platform, "pa", "a"), self.profile(ai_platform, "pb", "b")))
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"a": text_provider, "b": tool_provider})
        executor = FakeExecutor(admin_tools)

        compare = await orchestrator.compare_plans(
            ai_platform.ComparePlanRequest(("pa", "pb"), ai_platform.ToolRisk.READ),
            messages=(self.user_message(ai_platform),),
            allowed_tool_names=allowed,
        )
        self.assertEqual(executor.calls, [])
        pending = await orchestrator.select_compare_candidate(compare.compare_id, "pb", executor=executor)
        self.assertEqual(pending.status, ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)
        result = await orchestrator.approve_confirmation(pending.confirmation_id, approved=True, executor=executor)

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual([call[0] for call in executor.calls], ["list_channels", "get_member_details", "ban_member"])
        self.assertEqual(len(tool_provider.requests), 4)
        for request, _credential in tool_provider.requests:
            self.assertEqual(self.system_count(ai_platform, request), 1)
            self.assertEqual(sorted(self.tool_names(request)), sorted(allowed))
        self.assertEqual(len(text_provider.requests), 1)

    async def test_tool_round_cap_cannot_be_reset_through_repeated_confirmations(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        max_rounds = ai_orchestrator.MAX_TOOL_ROUNDS
        provider = FakeProvider(
            ai_platform,
            responses=[
                self.call(ai_platform, f"call-{index}", "ban_member", member_id="123")
                for index in range(max_rounds + 3)
            ],
        )
        orchestrator = self.single_profile_orchestrator(ai_platform, ai_orchestrator, provider)
        executor = FakeExecutor(admin_tools)

        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=executor,
        )
        confirmations = 0
        while result.status is ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION:
            confirmations += 1
            self.assertLessEqual(confirmations, max_rounds)
            result = await orchestrator.approve_confirmation(result.confirmation_id, approved=True, executor=executor)

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.LIMIT_REACHED)
        self.assertIsNone(result.confirmation_id)
        self.assertEqual(confirmations, max_rounds)
        self.assertEqual(len(executor.calls), max_rounds)
        self.assertEqual(len(result.executed_tools), max_rounds)
        self.assertEqual(len(provider.requests), max_rounds + 1)
        self.assertEqual(orchestrator._pending_confirmations, {})

    async def test_tool_round_count_persists_across_mixed_rounds_and_confirmations(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        max_rounds = ai_orchestrator.MAX_TOOL_ROUNDS
        responses = []
        for index in range(max_rounds + 2):
            if index % 2 == 0:
                responses.append(self.call(ai_platform, f"call-{index}", "list_channels"))
            else:
                responses.append(self.call(ai_platform, f"call-{index}", "ban_member", member_id="123"))
        provider = FakeProvider(ai_platform, responses=responses)
        orchestrator = self.single_profile_orchestrator(ai_platform, ai_orchestrator, provider)
        executor = FakeExecutor(admin_tools)

        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=executor,
        )
        confirmations = 0
        while result.status is ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION:
            confirmations += 1
            result = await orchestrator.approve_confirmation(result.confirmation_id, approved=True, executor=executor)

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.LIMIT_REACHED)
        self.assertEqual(confirmations, max_rounds // 2)
        self.assertEqual(len(executor.calls), max_rounds)
        self.assertEqual(len(provider.requests), max_rounds + 1)

    async def test_compare_selection_consumes_tool_round_budget(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        max_rounds = ai_orchestrator.MAX_TOOL_ROUNDS
        text_provider = FakeProvider(ai_platform, "a", responses=[ai_platform.AIResponse(content="text")])
        tool_provider = FakeProvider(
            ai_platform,
            "b",
            responses=[self.call(ai_platform, f"call-{index}", "list_channels") for index in range(max_rounds + 3)],
        )
        settings = ai_platform.AISettings(profiles=(self.profile(ai_platform, "pa", "a"), self.profile(ai_platform, "pb", "b")))
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"a": text_provider, "b": tool_provider})
        executor = FakeExecutor(admin_tools)

        compare = await orchestrator.compare_plans(
            ai_platform.ComparePlanRequest(("pa", "pb"), ai_platform.ToolRisk.READ),
            messages=(self.user_message(ai_platform),),
        )
        result = await orchestrator.select_compare_candidate(compare.compare_id, "pb", executor=executor)

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.LIMIT_REACHED)
        self.assertEqual(len(executor.calls), max_rounds)
        self.assertEqual(len(tool_provider.requests), max_rounds + 1)

    async def test_fallback_success_preserves_primary_failure_attempt(self):
        ai_platform, ai_orchestrator, _admin_tools = load_modules()
        primary = FakeProvider(ai_platform, "primary", error=RuntimeError("Authorization: Bearer sk-secret-value"))
        fallback = FakeProvider(ai_platform, "fallback", responses=[ai_platform.AIResponse(content="fallback ok")])
        settings = ai_platform.AISettings(
            profiles=(self.profile(ai_platform, "primary", "primary"), self.profile(ai_platform, "fallback", "fallback")),
            routing=ai_platform.RoutingConfig(routine_profile_id="primary", routine_fallback_profile_ids=("fallback",)),
        )
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"primary": primary, "fallback": fallback})

        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE")
        )

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertTrue(result.fallback_used)
        self.assertEqual(
            [(attempt.profile_id, attempt.category, attempt.fallback) for attempt in result.attempts],
            [("primary", "SELECTED", False), ("primary", "FAILED", False), ("fallback", "SELECTED", True)],
        )
        public = json.dumps(result.public_dict())
        self.assertNotIn("sk-secret-value", public)
        self.assertNotIn("Authorization", public)
        self.assertIn("RuntimeError", result.attempts[1].message)

    async def test_skipped_and_unavailable_attempt_history_is_ordered_and_sanitized(self):
        ai_platform, ai_orchestrator, _admin_tools = load_modules()
        fallback = FakeProvider(ai_platform, "fallback", responses=[ai_platform.AIResponse(content="ok")])
        settings = ai_platform.AISettings(
            profiles=(
                self.profile(ai_platform, "primary", "missing-provider", credential_ref="missing"),
                self.profile(ai_platform, "disabled", "fallback", enabled=False),
                self.profile(ai_platform, "fallback", "fallback"),
            ),
            routing=ai_platform.RoutingConfig(
                routine_profile_id="primary",
                routine_fallback_profile_ids=("disabled", "fallback"),
            ),
        )
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"fallback": fallback})

        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE")
        )

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual(
            [(attempt.profile_id, attempt.category, attempt.fallback) for attempt in result.attempts],
            [("primary", "UNAVAILABLE", False), ("disabled", "UNAVAILABLE", True), ("fallback", "SELECTED", True)],
        )
        json.dumps(result.public_dict())

        failing = FakeProvider(ai_platform, "primary", error=RuntimeError("x-goog-api-key: secret-key-value"))
        settings = ai_platform.AISettings(
            profiles=(
                self.profile(ai_platform, "primary", "primary"),
                self.profile(ai_platform, "gone", "missing-provider"),
            ),
            routing=ai_platform.RoutingConfig(routine_profile_id="primary", routine_fallback_profile_ids=("gone",)),
        )
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"primary": failing})
        unavailable = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE")
        )
        self.assertEqual(unavailable.status, ai_orchestrator.OrchestratorStatus.UNAVAILABLE)
        self.assertEqual(
            [(attempt.profile_id, attempt.category) for attempt in unavailable.attempts],
            [("primary", "SELECTED"), ("primary", "FAILED"), ("gone", "UNAVAILABLE")],
        )
        public = json.dumps(unavailable.public_dict())
        self.assertNotIn("secret-key-value", public)
        self.assertNotIn("x-goog-api-key", public)

    def fallback_orchestrator(self, ai_platform, ai_orchestrator, primary_responses):
        primary = FakeProvider(ai_platform, "primary", responses=primary_responses)
        fallback = FakeProvider(ai_platform, "fallback", responses=[ai_platform.AIResponse(content="fallback")])
        settings = ai_platform.AISettings(
            profiles=(self.profile(ai_platform, "primary", "primary"), self.profile(ai_platform, "fallback", "fallback")),
            routing=ai_platform.RoutingConfig(routine_profile_id="primary", routine_fallback_profile_ids=("fallback",)),
        )
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"primary": primary, "fallback": fallback})
        return orchestrator, primary, fallback

    async def test_executor_exception_on_first_tool_is_contained(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        batch = ai_platform.AIResponse(
            tool_calls=(
                ai_platform.AIToolCall(call_id="call-1", tool_name="list_channels", arguments={}),
                ai_platform.AIToolCall(call_id="call-2", tool_name="list_roles", arguments={}),
            )
        )
        orchestrator, primary, fallback = self.fallback_orchestrator(ai_platform, ai_orchestrator, [batch])
        executor = RaisingExecutor(admin_tools, raise_on=0)

        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=executor,
        )

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.TOOL_EXECUTION_FAILED)
        self.assertEqual(executor.calls, ["list_channels"])
        self.assertEqual([(tool.tool_name, tool.ok) for tool in result.executed_tools], [("list_channels", False)])
        self.assertEqual(result.profile_id, "primary")
        self.assertFalse(result.fallback_used)
        self.assertEqual(fallback.requests, [])
        self.assertEqual(len(primary.requests), 1)
        public = json.dumps(result.public_dict())
        self.assertNotIn(RaisingExecutor.SECRET, public)
        self.assertNotIn("Traceback", public)
        self.assertIn("Tool executor failed unexpectedly.", result.message)

    async def test_executor_exception_mid_batch_preserves_partial_execution(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        batch = ai_platform.AIResponse(
            tool_calls=(
                ai_platform.AIToolCall(call_id="call-1", tool_name="list_channels", arguments={}),
                ai_platform.AIToolCall(call_id="call-2", tool_name="list_roles", arguments={}),
                ai_platform.AIToolCall(call_id="call-3", tool_name="get_guild_summary", arguments={}),
            )
        )
        orchestrator, primary, fallback = self.fallback_orchestrator(ai_platform, ai_orchestrator, [batch])
        executor = RaisingExecutor(admin_tools, raise_on=1)

        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=executor,
        )

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.TOOL_EXECUTION_FAILED)
        self.assertEqual(executor.calls, ["list_channels", "list_roles"])
        self.assertEqual(
            [(tool.call_id, tool.tool_name, tool.ok) for tool in result.executed_tools],
            [("call-1", "list_channels", True), ("call-2", "list_roles", False)],
        )
        self.assertEqual(fallback.requests, [])
        self.assertEqual(len(primary.requests), 1)
        public = json.dumps(result.public_dict())
        self.assertNotIn(RaisingExecutor.SECRET, public)

    async def test_executor_exception_after_confirmation_is_contained(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        batch = ai_platform.AIResponse(
            tool_calls=(
                ai_platform.AIToolCall(call_id="call-1", tool_name="list_channels", arguments={}),
                ai_platform.AIToolCall(call_id="call-2", tool_name="ban_member", arguments={"member_id": "123"}),
                ai_platform.AIToolCall(call_id="call-3", tool_name="list_roles", arguments={}),
            )
        )
        orchestrator, primary, fallback = self.fallback_orchestrator(ai_platform, ai_orchestrator, [batch])
        executor = RaisingExecutor(admin_tools, raise_on=1)

        pending = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=executor,
        )
        self.assertEqual(executor.calls, [])
        result = await orchestrator.approve_confirmation(pending.confirmation_id, approved=True, executor=executor)

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.TOOL_EXECUTION_FAILED)
        self.assertEqual(executor.calls, ["list_channels", "ban_member"])
        self.assertEqual([tool.ok for tool in result.executed_tools], [True, False])
        self.assertEqual(fallback.requests, [])
        self.assertEqual(len(primary.requests), 1)
        self.assertNotIn(RaisingExecutor.SECRET, json.dumps(result.public_dict()))

    # ------------------------------------------------------------------
    # AI-3A final-review regression tests
    # ------------------------------------------------------------------

    async def test_explicit_empty_provider_registry_is_honored(self):
        ai_platform, ai_orchestrator, _admin_tools = load_modules()
        for name in ("ai_groq", "ai_gemini"):
            sys.modules.pop(name, None)
        default_calls = []
        original_default = ai_orchestrator.build_default_provider_registry

        def forbidden_default(*args, **kwargs):
            default_calls.append(True)
            return original_default(*args, **kwargs)

        ai_orchestrator.build_default_provider_registry = forbidden_default
        self.addCleanup(setattr, ai_orchestrator, "build_default_provider_registry", original_default)

        settings = ai_platform.AISettings(
            profiles=(
                ai_platform.AIProfile("groq-default", "groq", "fake-model"),
                ai_platform.AIProfile("gemini-default", "gemini", "fake-model"),
            ),
            routing=ai_platform.RoutingConfig(
                routine_profile_id="groq-default",
                routine_fallback_profile_ids=("gemini-default",),
            ),
        )
        store, credentials = self.make_store(ai_platform, settings)
        empty_registry = ai_platform.ProviderRegistry()
        self.assertEqual(len(empty_registry), 0)
        self.assertFalse(bool(empty_registry))

        orchestrator = ai_orchestrator.AIOrchestrator(
            settings_store=store,
            provider_registry=empty_registry,
            credential_store=credentials,
        )
        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE")
        )

        self.assertEqual(default_calls, [])
        self.assertIs(orchestrator._providers, empty_registry)
        self.assertEqual(len(empty_registry), 0)
        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.UNAVAILABLE)
        self.assertEqual(
            [(attempt.profile_id, attempt.category) for attempt in result.attempts],
            [("groq-default", "UNAVAILABLE"), ("gemini-default", "UNAVAILABLE")],
        )
        self.assertNotIn("ai_groq", sys.modules)
        self.assertNotIn("ai_gemini", sys.modules)
        json.dumps(result.public_dict())

    def test_default_provider_registry_created_only_when_registry_is_none(self):
        ai_platform, ai_orchestrator, _admin_tools = load_modules()
        created = []
        original_default = ai_orchestrator.build_default_provider_registry

        def counting_default(credential_store=None):
            created.append(credential_store)
            return ai_platform.ProviderRegistry()

        ai_orchestrator.build_default_provider_registry = counting_default
        self.addCleanup(setattr, ai_orchestrator, "build_default_provider_registry", original_default)
        store, credentials = self.make_store(ai_platform, ai_platform.AISettings())

        empty_registry = ai_platform.ProviderRegistry()
        explicit = ai_orchestrator.AIOrchestrator(
            settings_store=store, provider_registry=empty_registry, credential_store=credentials
        )
        self.assertEqual(created, [])
        self.assertIs(explicit._providers, empty_registry)

        ai_orchestrator.AIOrchestrator(settings_store=store, provider_registry=None, credential_store=credentials)
        self.assertEqual(len(created), 1)
        self.assertIs(created[0], credentials)

    def normal_call(self, ai_platform, call_id):
        return self.call(ai_platform, call_id, "send_message", channel_id="123", content="hi")

    async def test_confirm_normal_policy_persists_across_repeated_approvals(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        provider = FakeProvider(
            ai_platform,
            responses=[
                self.normal_call(ai_platform, "call-1"),
                self.normal_call(ai_platform, "call-2"),
                ai_platform.AIResponse(content="final"),
            ],
        )
        orchestrator = self.single_profile_orchestrator(ai_platform, ai_orchestrator, provider)
        executor = FakeExecutor(admin_tools)

        first = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=executor,
            confirmation_policy=ai_orchestrator.ConfirmationPolicy(confirm_normal=True),
        )
        self.assertEqual(first.status, ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)
        self.assertEqual(first.tool_risk, ai_platform.ToolRisk.NORMAL)
        self.assertEqual(executor.calls, [])

        second = await orchestrator.approve_confirmation(first.confirmation_id, approved=True, executor=executor)
        self.assertEqual(second.status, ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)
        self.assertEqual(second.tool_risk, ai_platform.ToolRisk.NORMAL)
        self.assertEqual([call[0] for call in executor.calls], ["send_message"])
        self.assertNotEqual(second.confirmation_id, first.confirmation_id)

        final = await orchestrator.approve_confirmation(second.confirmation_id, approved=True, executor=executor)
        self.assertEqual(final.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual(final.content, "final")
        self.assertEqual([call[0] for call in executor.calls], ["send_message", "send_message"])

    async def test_approval_api_does_not_accept_replacement_policy(self):
        import inspect

        ai_platform, ai_orchestrator, admin_tools = load_modules()
        parameters = inspect.signature(ai_orchestrator.AIOrchestrator.approve_confirmation).parameters
        self.assertNotIn("confirmation_policy", parameters)
        self.assertEqual(set(parameters), {"self", "confirmation_id", "approved", "executor"})

        provider = FakeProvider(ai_platform, responses=[self.normal_call(ai_platform, "call-1")])
        orchestrator = self.single_profile_orchestrator(ai_platform, ai_orchestrator, provider)
        executor = FakeExecutor(admin_tools)
        pending = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=executor,
            confirmation_policy=ai_orchestrator.ConfirmationPolicy(confirm_normal=True),
        )
        with self.assertRaises(TypeError):
            await orchestrator.approve_confirmation(
                pending.confirmation_id,
                approved=True,
                executor=executor,
                confirmation_policy=ai_orchestrator.ConfirmationPolicy(),
            )
        self.assertEqual(executor.calls, [])

    async def test_compare_selection_policy_persists_across_approvals(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        text_provider = FakeProvider(ai_platform, "a", responses=[ai_platform.AIResponse(content="text")])
        tool_provider = FakeProvider(
            ai_platform,
            "b",
            responses=[
                self.normal_call(ai_platform, "call-1"),
                self.normal_call(ai_platform, "call-2"),
                ai_platform.AIResponse(content="final"),
            ],
        )
        settings = ai_platform.AISettings(profiles=(self.profile(ai_platform, "pa", "a"), self.profile(ai_platform, "pb", "b")))
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"a": text_provider, "b": tool_provider})
        executor = FakeExecutor(admin_tools)

        compare = await orchestrator.compare_plans(
            ai_platform.ComparePlanRequest(("pa", "pb"), ai_platform.ToolRisk.NORMAL),
            messages=(self.user_message(ai_platform),),
        )
        self.assertEqual(executor.calls, [])
        first = await orchestrator.select_compare_candidate(
            compare.compare_id,
            "pb",
            executor=executor,
            confirmation_policy=ai_orchestrator.ConfirmationPolicy(confirm_normal=True),
        )
        self.assertEqual(first.status, ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)
        self.assertEqual(executor.calls, [])

        second = await orchestrator.approve_confirmation(first.confirmation_id, approved=True, executor=executor)
        self.assertEqual(second.status, ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)
        self.assertEqual(second.tool_risk, ai_platform.ToolRisk.NORMAL)
        self.assertEqual([call[0] for call in executor.calls], ["send_message"])

        final = await orchestrator.approve_confirmation(second.confirmation_id, approved=True, executor=executor)
        self.assertEqual(final.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual([call[0] for call in executor.calls], ["send_message", "send_message"])
        self.assertEqual(len(text_provider.requests), 1)

    async def test_default_policy_run_keeps_normal_unconfirmed_after_destructive_approval(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        provider = FakeProvider(
            ai_platform,
            responses=[
                self.call(ai_platform, "call-1", "ban_member", member_id="123"),
                self.normal_call(ai_platform, "call-2"),
                self.call(ai_platform, "call-3", "ban_member", member_id="456"),
                ai_platform.AIResponse(content="final"),
            ],
        )
        orchestrator = self.single_profile_orchestrator(ai_platform, ai_orchestrator, provider)
        executor = FakeExecutor(admin_tools)

        first = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=executor,
        )
        self.assertEqual(first.status, ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)
        second = await orchestrator.approve_confirmation(first.confirmation_id, approved=True, executor=executor)
        # NORMAL executed without confirmation (default policy), then DESTRUCTIVE gated again.
        self.assertEqual(second.status, ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)
        self.assertEqual(second.tool_risk, ai_platform.ToolRisk.DESTRUCTIVE)
        self.assertEqual([call[0] for call in executor.calls], ["ban_member", "send_message"])
        final = await orchestrator.approve_confirmation(second.confirmation_id, approved=True, executor=executor)
        self.assertEqual(final.status, ai_orchestrator.OrchestratorStatus.COMPLETED)

    async def test_destructive_confirmation_cannot_be_disabled_by_policy_object(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()

        class PermissivePolicy(ai_orchestrator.ConfirmationPolicy):
            def requires_confirmation(self, risk):
                return False

        provider = FakeProvider(ai_platform, responses=[self.call(ai_platform, "call-1", "ban_member", member_id="123")])
        orchestrator = self.single_profile_orchestrator(ai_platform, ai_orchestrator, provider)
        executor = FakeExecutor(admin_tools)
        request = ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE")

        # Subclasses are rejected outright: ConfirmationPolicy is a value object.
        with self.assertRaises(ValueError):
            await orchestrator.orchestrate(request, executor=executor, confirmation_policy=PermissivePolicy())
        self.assertEqual(provider.requests, [])
        self.assertEqual(executor.calls, [])

        with self.assertRaises(ValueError):
            await orchestrator.orchestrate(request, executor=executor, confirmation_policy=object())
        with self.assertRaises(ValueError):
            ai_orchestrator.ConfirmationPolicy(confirm_normal="yes")
        self.assertEqual(executor.calls, [])

    # ------------------------------------------------------------------
    # Pre-AI-4 hardening regression tests
    # ------------------------------------------------------------------

    def clocked_orchestrator(self, ai_platform, ai_orchestrator, settings, providers):
        store, credentials = self.make_store(ai_platform, settings)
        clock = FakeClock()
        orchestrator = ai_orchestrator.AIOrchestrator(
            settings_store=store,
            provider_registry=ai_platform.ProviderRegistry(providers),
            credential_store=credentials,
            clock=clock,
        )
        return orchestrator, clock

    def routine_settings(self, ai_platform):
        return ai_platform.AISettings(
            profiles=(self.profile(ai_platform, "routine"),),
            routing=ai_platform.RoutingConfig(routine_profile_id="routine"),
        )

    def patch_module(self, module, name, value):
        original = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, original)

    async def destructive_pending(self, ai_platform, ai_orchestrator, admin_tools, extra_responses=()):
        provider = FakeProvider(
            ai_platform,
            responses=[self.call(ai_platform, "call-1", "ban_member", member_id="123"), *extra_responses],
        )
        orchestrator, clock = self.clocked_orchestrator(
            ai_platform, ai_orchestrator, self.routine_settings(ai_platform), {"fake": provider}
        )
        executor = FakeExecutor(admin_tools)
        pending = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=executor,
        )
        self.assertEqual(pending.status, ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)
        return orchestrator, clock, executor, pending, provider

    # --- Blocker 1: strict boolean approval -----------------------------------

    async def test_non_bool_approval_raises_and_keeps_confirmation_usable_for_approval(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        orchestrator, _clock, executor, pending, _provider = await self.destructive_pending(
            ai_platform, ai_orchestrator, admin_tools, extra_responses=(ai_platform.AIResponse(content="done"),)
        )
        for invalid in ("false", "true", 1, 0, None, [], {}, 1.0):
            with self.subTest(approved=invalid):
                with self.assertRaises(ValueError):
                    await orchestrator.approve_confirmation(pending.confirmation_id, approved=invalid, executor=executor)
                self.assertEqual(executor.calls, [])
                self.assertEqual(len(orchestrator._pending_confirmations), 1)

        with self.assertRaises(ValueError):
            await orchestrator.approve_confirmation(pending.confirmation_id, approved=True, executor=None)
        with self.assertRaises(ValueError):
            await orchestrator.approve_confirmation(123, approved=True, executor=executor)
        self.assertEqual(executor.calls, [])

        approved = await orchestrator.approve_confirmation(pending.confirmation_id, approved=True, executor=executor)
        self.assertEqual(approved.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual([call[0] for call in executor.calls], ["ban_member"])
        again = await orchestrator.approve_confirmation(pending.confirmation_id, approved=True, executor=executor)
        self.assertEqual(again.status, ai_orchestrator.OrchestratorStatus.CANCELLED)
        self.assertEqual(len(executor.calls), 1)

    async def test_non_bool_approval_keeps_confirmation_usable_for_rejection(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        orchestrator, _clock, executor, pending, _provider = await self.destructive_pending(
            ai_platform, ai_orchestrator, admin_tools
        )
        with self.assertRaises(ValueError):
            await orchestrator.approve_confirmation(pending.confirmation_id, approved="false", executor=executor)
        rejected = await orchestrator.approve_confirmation(pending.confirmation_id, approved=False, executor=executor)
        self.assertEqual(rejected.status, ai_orchestrator.OrchestratorStatus.CANCELLED)
        self.assertEqual(rejected.message, "Confirmation rejected.")
        after_reject = await orchestrator.approve_confirmation(pending.confirmation_id, approved=True, executor=executor)
        self.assertEqual(after_reject.status, ai_orchestrator.OrchestratorStatus.CANCELLED)
        self.assertEqual(executor.calls, [])

    # --- Blocker 2: fixed confirmation-policy semantics ------------------------

    def test_confirmation_policy_fixed_semantics(self):
        ai_platform, ai_orchestrator, _admin_tools = load_modules()
        requires = ai_orchestrator._requires_confirmation
        Risk = ai_platform.ToolRisk
        strict = ai_orchestrator.ConfirmationPolicy(confirm_normal=True)
        default = ai_orchestrator.ConfirmationPolicy()
        self.assertFalse(requires(strict, Risk.READ))
        self.assertFalse(requires(default, Risk.READ))
        self.assertTrue(requires(strict, Risk.NORMAL))
        self.assertFalse(requires(default, Risk.NORMAL))
        self.assertTrue(requires(strict, Risk.DESTRUCTIVE))
        self.assertTrue(requires(default, Risk.DESTRUCTIVE))
        self.assertTrue(default.requires_confirmation(Risk.DESTRUCTIVE))
        for invalid in (1, 0, "true", None):
            with self.subTest(confirm_normal=invalid), self.assertRaises(ValueError):
                ai_orchestrator.ConfirmationPolicy(confirm_normal=invalid)

        tampered = ai_orchestrator.ConfirmationPolicy()
        object.__setattr__(tampered, "confirm_normal", "yes")
        with self.assertRaises(ValueError):
            ai_orchestrator._validate_confirmation_policy(tampered)

    async def test_policy_subclass_rejected_for_compare_selection_without_consuming(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()

        class PermissivePolicy(ai_orchestrator.ConfirmationPolicy):
            def requires_confirmation(self, risk):
                return False

        text_provider = FakeProvider(ai_platform, "a", responses=[ai_platform.AIResponse(content="text")])
        tool_provider = FakeProvider(ai_platform, "b", responses=[self.call(ai_platform, "call-1", "ban_member", member_id="1")])
        settings = ai_platform.AISettings(profiles=(self.profile(ai_platform, "pa", "a"), self.profile(ai_platform, "pb", "b")))
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"a": text_provider, "b": tool_provider})
        executor = FakeExecutor(admin_tools)
        compare = await orchestrator.compare_plans(
            ai_platform.ComparePlanRequest(("pa", "pb"), ai_platform.ToolRisk.DESTRUCTIVE),
            messages=(self.user_message(ai_platform),),
        )
        with self.assertRaises(ValueError):
            await orchestrator.select_compare_candidate(compare.compare_id, "pb", executor=executor, confirmation_policy=PermissivePolicy())
        self.assertEqual(executor.calls, [])
        selected = await orchestrator.select_compare_candidate(compare.compare_id, "pb", executor=executor)
        self.assertEqual(selected.status, ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)
        self.assertEqual(executor.calls, [])

    # --- Blocker 3: recursively immutable stored plans -------------------------

    def test_validated_tool_call_arguments_are_recursively_immutable(self):
        ai_platform, ai_orchestrator, _admin_tools = load_modules()
        source = {"outer": {"target": "A"}, "items": ["A", {"value": 1}]}
        call = ai_orchestrator.ValidatedToolCall("call-1", "list_channels", source, ai_platform.ToolRisk.READ)

        source["outer"]["target"] = "B"
        source["items"][1]["value"] = 2
        source["items"].append("C")
        source["new"] = True
        self.assertEqual(call.arguments["outer"]["target"], "A")
        self.assertEqual(call.arguments["items"][1]["value"], 1)
        self.assertEqual(len(call.arguments["items"]), 2)
        self.assertNotIn("new", call.arguments)

        with self.assertRaises(TypeError):
            call.arguments["outer"]["target"] = "X"
        with self.assertRaises(TypeError):
            call.arguments["items"][1]["value"] = 9
        with self.assertRaises(TypeError):
            call.arguments["new"] = 1
        with self.assertRaises(AttributeError):
            call.arguments["items"].append("X")

        public = call.public_dict()
        self.assertIs(type(public["arguments"]), dict)
        self.assertIs(type(public["arguments"]["outer"]), dict)
        self.assertIs(type(public["arguments"]["items"]), list)
        self.assertIs(type(public["arguments"]["items"][1]), dict)
        json.dumps(public)
        public["arguments"]["outer"]["target"] = "Z"
        public["arguments"]["items"][1]["value"] = 99
        public["arguments"]["items"].append("Z")
        self.assertEqual(call.arguments["outer"]["target"], "A")
        self.assertEqual(call.arguments["items"][1]["value"], 1)
        self.assertEqual(call.public_dict()["arguments"], {"outer": {"target": "A"}, "items": ["A", {"value": 1}]})

        for bad in ({"x": object()}, {"x": float("nan")}, {1: "x"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ai_orchestrator.ValidatedToolCall("call-1", "list_channels", bad, ai_platform.ToolRisk.READ)

    async def test_executor_receives_fresh_thawed_containers(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        call = ai_orchestrator.ValidatedToolCall(
            "call-1",
            "list_channels",
            {"outer": {"target": "A"}, "items": ["A", {"value": 1}]},
            ai_platform.ToolRisk.READ,
        )
        received = []

        async def mutating_executor(tool_name, arguments):
            received.append(arguments)
            self.assertIs(type(arguments), dict)
            self.assertIs(type(arguments["outer"]), dict)
            self.assertIs(type(arguments["items"]), list)
            self.assertIs(type(arguments["items"][1]), dict)
            arguments["outer"]["target"] = "MUTATED"
            arguments["items"].append("MUTATED")
            return admin_tools.ToolResult(True, tool_name, "ok")

        await ai_orchestrator._execute_tool_batch((call,), mutating_executor)
        await ai_orchestrator._execute_tool_batch((call,), mutating_executor)
        self.assertIsNot(received[0], received[1])
        self.assertEqual(received[1]["outer"]["target"], "MUTATED")
        self.assertEqual(call.arguments["outer"]["target"], "A")
        self.assertEqual(len(call.arguments["items"]), 2)

    async def test_caller_cannot_alter_stored_confirmation_plan_through_public_data(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        orchestrator, _clock, executor, pending, _provider = await self.destructive_pending(
            ai_platform, ai_orchestrator, admin_tools, extra_responses=(ai_platform.AIResponse(content="done"),)
        )
        public = pending.public_dict()
        public["tool_plan"][0]["arguments"]["member_id"] = "999"
        public["tool_plan"][0]["tool_name"] = "kick_member"
        with self.assertRaises(TypeError):
            pending.tool_plan[0].arguments["member_id"] = "999"
        with self.assertRaises(Exception):
            pending.tool_plan[0].tool_name = "kick_member"

        await orchestrator.approve_confirmation(pending.confirmation_id, approved=True, executor=executor)
        self.assertEqual(executor.calls, [("ban_member", {"member_id": "123"})])

    # --- Pending-state TTL and bounds ------------------------------------------

    async def test_expired_confirmation_cannot_execute(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        orchestrator, clock, executor, pending, _provider = await self.destructive_pending(
            ai_platform, ai_orchestrator, admin_tools
        )
        clock.advance(ai_orchestrator.CONFIRMATION_TTL_SECONDS + 1)
        result = await orchestrator.approve_confirmation(pending.confirmation_id, approved=True, executor=executor)
        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.CANCELLED)
        self.assertEqual(executor.calls, [])
        self.assertEqual(orchestrator._pending_confirmations, {})

    async def test_confirmation_before_ttl_still_works(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        orchestrator, clock, executor, pending, _provider = await self.destructive_pending(
            ai_platform, ai_orchestrator, admin_tools, extra_responses=(ai_platform.AIResponse(content="done"),)
        )
        clock.advance(ai_orchestrator.CONFIRMATION_TTL_SECONDS - 1)
        result = await orchestrator.approve_confirmation(pending.confirmation_id, approved=True, executor=executor)
        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.COMPLETED)

    async def test_confirmation_limit_is_bounded_and_recovers_after_expiry(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        self.patch_module(ai_orchestrator, "MAX_PENDING_CONFIRMATIONS", 2)
        provider = FakeProvider(
            ai_platform,
            responses=[self.call(ai_platform, f"call-{index}", "ban_member", member_id="123") for index in range(4)],
        )
        orchestrator, clock = self.clocked_orchestrator(
            ai_platform, ai_orchestrator, self.routine_settings(ai_platform), {"fake": provider}
        )
        executor = FakeExecutor(admin_tools)
        request = ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE")
        first = await orchestrator.orchestrate(request, executor=executor)
        second = await orchestrator.orchestrate(request, executor=executor)
        third = await orchestrator.orchestrate(request, executor=executor)
        self.assertEqual(first.status, ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)
        self.assertEqual(second.status, ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)
        self.assertEqual(third.status, ai_orchestrator.OrchestratorStatus.LIMIT_REACHED)
        self.assertIsNone(third.confirmation_id)
        self.assertEqual(len(orchestrator._pending_confirmations), 2)

        clock.advance(ai_orchestrator.CONFIRMATION_TTL_SECONDS + 1)
        fourth = await orchestrator.orchestrate(request, executor=executor)
        self.assertEqual(fourth.status, ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)
        self.assertEqual(list(orchestrator._pending_confirmations), [fourth.confirmation_id])
        self.assertEqual(executor.calls, [])

    async def test_confirmation_limit_after_executed_tools_reports_them(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        self.patch_module(ai_orchestrator, "MAX_PENDING_CONFIRMATIONS", 0)
        provider = FakeProvider(
            ai_platform,
            responses=[
                self.call(ai_platform, "call-1", "list_channels"),
                self.call(ai_platform, "call-2", "ban_member", member_id="123"),
            ],
        )
        orchestrator, _clock = self.clocked_orchestrator(
            ai_platform, ai_orchestrator, self.routine_settings(ai_platform), {"fake": provider}
        )
        executor = FakeExecutor(admin_tools)
        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=executor,
        )
        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.LIMIT_REACHED)
        self.assertEqual([tool.tool_name for tool in result.executed_tools], ["list_channels"])
        self.assertEqual([call[0] for call in executor.calls], ["list_channels"])
        self.assertEqual(result.profile_id, "routine")

    async def compare_setup(self, ai_platform, ai_orchestrator, second_responses):
        text_provider = FakeProvider(ai_platform, "a", responses=[ai_platform.AIResponse(content=f"text-{i}") for i in range(5)])
        tool_provider = FakeProvider(ai_platform, "b", responses=list(second_responses))
        settings = ai_platform.AISettings(profiles=(self.profile(ai_platform, "pa", "a"), self.profile(ai_platform, "pb", "b")))
        orchestrator, clock = self.clocked_orchestrator(
            ai_platform, ai_orchestrator, settings, {"a": text_provider, "b": tool_provider}
        )
        return orchestrator, clock, text_provider, tool_provider

    async def test_expired_compare_cannot_be_selected(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        orchestrator, clock, _text, _tool = await self.compare_setup(
            ai_platform, ai_orchestrator, [self.call(ai_platform, "call-1", "list_channels")]
        )
        executor = FakeExecutor(admin_tools)
        compare = await orchestrator.compare_plans(
            ai_platform.ComparePlanRequest(("pa", "pb"), ai_platform.ToolRisk.READ),
            messages=(self.user_message(ai_platform),),
        )
        clock.advance(ai_orchestrator.COMPARE_TTL_SECONDS + 1)
        result = await orchestrator.select_compare_candidate(compare.compare_id, "pb", executor=executor)
        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.CANCELLED)
        self.assertEqual(executor.calls, [])
        self.assertEqual(orchestrator._pending_compares, {})

    async def test_compare_limit_is_bounded_before_provider_requests(self):
        ai_platform, ai_orchestrator, _admin_tools = load_modules()
        self.patch_module(ai_orchestrator, "MAX_PENDING_COMPARES", 2)
        orchestrator, clock, text_provider, tool_provider = await self.compare_setup(
            ai_platform, ai_orchestrator, [ai_platform.AIResponse(content=f"b-{i}") for i in range(5)]
        )
        request = ai_platform.ComparePlanRequest(("pa", "pb"), ai_platform.ToolRisk.READ)
        messages = (self.user_message(ai_platform),)
        first = await orchestrator.compare_plans(request, messages=messages)
        second = await orchestrator.compare_plans(request, messages=messages)
        requests_before = (len(text_provider.requests), len(tool_provider.requests))
        third = await orchestrator.compare_plans(request, messages=messages)
        self.assertEqual(first.status, ai_orchestrator.CompareStatus.COMPLETED)
        self.assertEqual(second.status, ai_orchestrator.CompareStatus.COMPLETED)
        self.assertEqual(third.status, ai_orchestrator.CompareStatus.UNAVAILABLE)
        self.assertIsNone(third.compare_id)
        self.assertEqual((len(text_provider.requests), len(tool_provider.requests)), requests_before)
        self.assertEqual(len(orchestrator._pending_compares), 2)

        clock.advance(ai_orchestrator.COMPARE_TTL_SECONDS + 1)
        fourth = await orchestrator.compare_plans(request, messages=messages)
        self.assertEqual(fourth.status, ai_orchestrator.CompareStatus.COMPLETED)
        self.assertEqual(list(orchestrator._pending_compares), [fourth.compare_id])

    async def test_compare_selection_input_validation_does_not_consume(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        orchestrator, _clock, _text, tool_provider = await self.compare_setup(
            ai_platform,
            ai_orchestrator,
            [self.call(ai_platform, "call-1", "list_channels"), ai_platform.AIResponse(content="after")],
        )
        executor = FakeExecutor(admin_tools)
        compare = await orchestrator.compare_plans(
            ai_platform.ComparePlanRequest(("pa", "pb"), ai_platform.ToolRisk.READ),
            messages=(self.user_message(ai_platform),),
        )
        with self.assertRaises(ValueError):
            await orchestrator.select_compare_candidate(compare.compare_id, "pb", executor=None)
        with self.assertRaises(ValueError):
            await orchestrator.select_compare_candidate(None, "pb", executor=executor)
        with self.assertRaises(ValueError):
            await orchestrator.select_compare_candidate(compare.compare_id, 7, executor=executor)
        self.assertEqual(executor.calls, [])
        self.assertEqual(len(orchestrator._pending_compares), 1)

        selected = await orchestrator.select_compare_candidate(compare.compare_id, "pb", executor=executor)
        self.assertEqual(selected.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual(selected.profile_id, "pb")
        self.assertEqual(executor.calls, [("list_channels", {})])

    async def test_compare_unknown_profile_executes_nothing_and_consumes_compare(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        orchestrator, _clock, _text, _tool = await self.compare_setup(
            ai_platform, ai_orchestrator, [self.call(ai_platform, "call-1", "list_channels")]
        )
        executor = FakeExecutor(admin_tools)
        compare = await orchestrator.compare_plans(
            ai_platform.ComparePlanRequest(("pa", "pb"), ai_platform.ToolRisk.READ),
            messages=(self.user_message(ai_platform),),
        )
        result = await orchestrator.select_compare_candidate(compare.compare_id, "pz", executor=executor)
        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.CANCELLED)
        again = await orchestrator.select_compare_candidate(compare.compare_id, "pb", executor=executor)
        self.assertEqual(again.status, ai_orchestrator.OrchestratorStatus.CANCELLED)
        self.assertEqual(executor.calls, [])

    # --- Containment of malformed provider/model output ------------------------

    async def test_invalid_provider_response_object_is_contained_and_falls_back(self):
        ai_platform, ai_orchestrator, _admin_tools = load_modules()
        primary = FakeProvider(ai_platform, "primary", responses=[{"content": "not an AIResponse"}])
        fallback = FakeProvider(ai_platform, "fallback", responses=[ai_platform.AIResponse(content="fallback ok")])
        settings = ai_platform.AISettings(
            profiles=(self.profile(ai_platform, "primary", "primary"), self.profile(ai_platform, "fallback", "fallback")),
            routing=ai_platform.RoutingConfig(routine_profile_id="primary", routine_fallback_profile_ids=("fallback",)),
        )
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"primary": primary, "fallback": fallback})
        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE")
        )
        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual(result.profile_id, "fallback")
        self.assertEqual(result.attempts[1].category, "FAILED")

    async def test_unexpected_validation_error_is_contained(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()

        def exploding_validate(tool_name, arguments):
            raise TypeError("secret-validation-detail")

        self.patch_module(admin_tools, "validate_tool_arguments", exploding_validate)
        provider = FakeProvider(ai_platform, responses=[self.call(ai_platform, "call-1", "list_channels")])
        orchestrator = self.single_profile_orchestrator(ai_platform, ai_orchestrator, provider)
        executor = FakeExecutor(admin_tools)
        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=executor,
        )
        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.INVALID_TOOL_PLAN)
        self.assertNotIn("secret-validation-detail", json.dumps(result.public_dict()))
        self.assertEqual(executor.calls, [])

    async def test_broken_registry_lookup_is_contained(self):
        ai_platform, ai_orchestrator, _admin_tools = load_modules()

        class BrokenRegistry(ai_platform.ProviderRegistry):
            def get(self, provider_id):
                raise RuntimeError("registry exploded")

        store, credentials = self.make_store(ai_platform, self.routine_settings(ai_platform))
        orchestrator = ai_orchestrator.AIOrchestrator(
            settings_store=store, provider_registry=BrokenRegistry(), credential_store=credentials
        )
        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE")
        )
        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.UNAVAILABLE)
        self.assertNotIn("exploded", json.dumps(result.public_dict()))

    async def test_public_diagnostics_and_call_ids_are_bounded(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        limit = ai_orchestrator.MAX_PUBLIC_MESSAGE_CHARS
        cases = [
            self.call(ai_platform, "call-1", "x" * 5000),
            self.call(ai_platform, "c" * (ai_orchestrator.MAX_TOOL_CALL_ID_CHARS + 1), "list_channels"),
        ]
        for response in cases:
            provider = FakeProvider(ai_platform, responses=[response])
            orchestrator = self.single_profile_orchestrator(ai_platform, ai_orchestrator, provider)
            executor = FakeExecutor(admin_tools)
            with self.subTest(tool=response.tool_calls[0].tool_name[:10]):
                result = await orchestrator.orchestrate(
                    ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
                    executor=executor,
                )
                self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.INVALID_TOOL_PLAN)
                self.assertLessEqual(len(result.message), limit)
                self.assertNotIn("c" * (ai_orchestrator.MAX_TOOL_CALL_ID_CHARS + 1), json.dumps(result.public_dict()))
                self.assertEqual(executor.calls, [])

        provider = FakeProvider(
            ai_platform,
            responses=[self.call(ai_platform, "call-1", "list_channels"), ai_platform.AIResponse(content="done")],
        )
        orchestrator = self.single_profile_orchestrator(ai_platform, ai_orchestrator, provider)
        executor = FakeExecutor(admin_tools, results=[admin_tools.ToolResult(True, "list_channels", "m" * 10000)])
        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=executor,
        )
        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertLessEqual(len(result.executed_tools[0].message), limit)
        self.assertLessEqual(len(ai_orchestrator.AttemptRecord("p", None, None, "X", False, "a" * 9999).message), limit)

    async def test_non_true_tool_result_ok_is_failure(self):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        provider = FakeProvider(
            ai_platform,
            responses=[self.call(ai_platform, "call-1", "list_channels"), ai_platform.AIResponse(content="done")],
        )
        orchestrator = self.single_profile_orchestrator(ai_platform, ai_orchestrator, provider)
        executor = FakeExecutor(admin_tools, results=[admin_tools.ToolResult("false", "list_channels", "odd")])
        result = await orchestrator.orchestrate(
            ai_orchestrator.OrchestratorRequest(messages=(self.user_message(ai_platform),), task_class="ROUTINE"),
            executor=executor,
        )
        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.TOOL_EXECUTION_FAILED)
        self.assertFalse(result.executed_tools[0].ok)

    def test_allowed_tool_names_must_not_be_a_string(self):
        ai_platform, ai_orchestrator, _admin_tools = load_modules()
        for invalid in ("list_channels", ("list_channels", 5), ("",)):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                ai_orchestrator.OrchestratorRequest(
                    messages=(self.user_message(ai_platform),),
                    task_class="ROUTINE",
                    allowed_tool_names=invalid,
                )

    # --- Compare request resource bound ---------------------------------------

    def max_compare_setup(self, ai_platform, ai_orchestrator, count):
        providers = {}
        profiles = []
        for index in range(count):
            provider_id = f"prov{index}"
            providers[provider_id] = FakeProvider(
                ai_platform, provider_id, responses=[ai_platform.AIResponse(content=f"text-{index}")]
            )
            profiles.append(self.profile(ai_platform, f"p{index}", provider_id))
        settings = ai_platform.AISettings(profiles=tuple(profiles))
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, providers)
        return orchestrator, providers

    async def test_compare_accepts_two_and_max_profiles(self):
        ai_platform, ai_orchestrator, _admin_tools = load_modules()
        maximum = ai_orchestrator.MAX_COMPARE_PROFILES
        self.assertEqual(maximum, ai_platform.MAX_COMPARE_PROFILES)
        for count in (2, maximum):
            with self.subTest(count=count):
                orchestrator, providers = self.max_compare_setup(ai_platform, ai_orchestrator, count)
                compare = await orchestrator.compare_plans(
                    ai_platform.ComparePlanRequest(tuple(f"p{i}" for i in range(count)), ai_platform.ToolRisk.READ),
                    messages=(self.user_message(ai_platform),),
                )
                self.assertEqual(compare.status, ai_orchestrator.CompareStatus.COMPLETED)
                self.assertEqual([candidate.profile_id for candidate in compare.candidates], [f"p{i}" for i in range(count)])
                self.assertEqual(sum(len(provider.requests) for provider in providers.values()), count)

    async def test_oversized_compare_is_refused_before_any_provider_request(self):
        ai_platform, ai_orchestrator, _admin_tools = load_modules()
        maximum = ai_orchestrator.MAX_COMPARE_PROFILES
        orchestrator, providers = self.max_compare_setup(ai_platform, ai_orchestrator, maximum + 2)

        with self.assertRaises(ValueError):
            ai_platform.ComparePlanRequest(tuple(f"p{i}" for i in range(maximum + 1)), ai_platform.ToolRisk.READ)

        # Tampered frozen request: bypasses __post_init__ via object.__setattr__.
        oversized = ai_platform.ComparePlanRequest(("p0", "p1"), ai_platform.ToolRisk.READ)
        object.__setattr__(oversized, "profile_ids", tuple(f"p{i}" for i in range(maximum + 1)))
        with self.assertRaises(ValueError):
            await orchestrator.compare_plans(oversized, messages=(self.user_message(ai_platform),))

        mutable = ai_platform.ComparePlanRequest(("p0", "p1"), ai_platform.ToolRisk.READ)
        object.__setattr__(mutable, "profile_ids", ["p0", "p1"])
        with self.assertRaises(ValueError):
            await orchestrator.compare_plans(mutable, messages=(self.user_message(ai_platform),))

        duplicated = ai_platform.ComparePlanRequest(("p0", "p1"), ai_platform.ToolRisk.READ)
        object.__setattr__(duplicated, "profile_ids", ("p0", "p0"))
        with self.assertRaises(ValueError):
            await orchestrator.compare_plans(duplicated, messages=(self.user_message(ai_platform),))

        self.assertEqual(sum(len(provider.requests) for provider in providers.values()), 0)
        self.assertEqual(orchestrator._pending_compares, {})

    async def test_compare_iterates_a_stable_snapshot_of_profile_ids(self):
        ai_platform, ai_orchestrator, _admin_tools = load_modules()
        orchestrator, providers = self.max_compare_setup(ai_platform, ai_orchestrator, 3)
        source_ids = ["p0", "p1"]
        request = ai_platform.ComparePlanRequest(source_ids, ai_platform.ToolRisk.READ)

        original_generate = providers["prov0"].generate

        async def mutating_generate(sent_request, credential_ref=None):
            source_ids.append("p2")
            return await original_generate(sent_request, credential_ref)

        providers["prov0"].generate = mutating_generate
        compare = await orchestrator.compare_plans(request, messages=(self.user_message(ai_platform),))

        self.assertEqual([candidate.profile_id for candidate in compare.candidates], ["p0", "p1"])
        self.assertEqual(request.profile_ids, ("p0", "p1"))
        self.assertEqual(providers["prov2"].requests, [])


class DefaultRegistryTests(unittest.TestCase):
    def test_default_registry_uses_lazy_factories_and_is_resilient(self):
        ai_platform, ai_orchestrator, _admin_tools = load_modules()
        with tempfile.TemporaryDirectory() as temp_dir:
            registry = ai_orchestrator.build_default_provider_registry(
                ai_platform.CredentialStore(Path(temp_dir) / "secrets")
            )
            self.assertEqual(len(registry), 0)


class RecoverErrorsTests(unittest.IsolatedAsyncioTestCase):
    """recover_errors=True: mistakes go back to the model instead of ending the run."""

    make_store = OrchestratorTests.make_store
    make_orchestrator = OrchestratorTests.make_orchestrator
    profile = OrchestratorTests.profile
    user_message = OrchestratorTests.user_message

    def setup_run(self, responses, executor_results=None):
        ai_platform, ai_orchestrator, admin_tools = load_modules()
        provider = FakeProvider(ai_platform, responses=responses(ai_platform))
        settings = ai_platform.AISettings(
            profiles=(self.profile(ai_platform, "routine"),),
            routing=ai_platform.RoutingConfig(routine_profile_id="routine"),
        )
        orchestrator = self.make_orchestrator(ai_platform, ai_orchestrator, settings, {"fake": provider})
        executor = FakeExecutor(admin_tools, results=(executor_results or (lambda _tools: []))(admin_tools))
        return ai_platform, ai_orchestrator, provider, orchestrator, executor

    def request(self, ai_platform, ai_orchestrator, recover=True):
        return ai_orchestrator.OrchestratorRequest(
            messages=(self.user_message(ai_platform),), task_class="ROUTINE", recover_errors=recover
        )

    @staticmethod
    def call(ai_platform, call_id, tool_name, **arguments):
        return ai_platform.AIToolCall(call_id=call_id, tool_name=tool_name, arguments=arguments)

    @staticmethod
    def tool_results(provider_request):
        return [
            (message.tool_call_id, json.loads(message.content))
            for message in provider_request.messages
            if message.role.value == "tool"
        ]

    def test_recover_errors_must_be_boolean(self):
        ai_platform, ai_orchestrator, _ = load_modules()
        with self.assertRaises(ValueError):
            ai_orchestrator.OrchestratorRequest(
                messages=(self.user_message(ai_platform),), task_class="ROUTINE", recover_errors="yes"
            )

    async def test_invalid_arguments_are_returned_and_the_model_corrects_them(self):
        ai_platform, ai_orchestrator, provider, orchestrator, executor = self.setup_run(
            lambda p: [
                p.AIResponse(tool_calls=(self.call(p, "c1", "get_channel_details", channel_id="general"),)),
                p.AIResponse(tool_calls=(self.call(p, "c2", "get_channel_details", channel_id="100"),)),
                p.AIResponse(content="Channel found."),
            ]
        )

        result = await orchestrator.orchestrate(self.request(ai_platform, ai_orchestrator), executor=executor)

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual(result.content, "Channel found.")
        self.assertEqual(executor.calls, [("get_channel_details", {"channel_id": "100"})])
        rejected = self.tool_results(provider.requests[1][0])
        self.assertEqual(rejected[0][0], "c1")
        self.assertFalse(rejected[0][1]["ok"])
        self.assertIn("Not executed", rejected[0][1]["message"])

    async def test_unknown_or_disallowed_tool_is_reported_not_fatal(self):
        ai_platform, ai_orchestrator, provider, orchestrator, executor = self.setup_run(
            lambda p: [
                p.AIResponse(tool_calls=(self.call(p, "c1", "list_roles"), self.call(p, "c2", "list_channels"))),
                p.AIResponse(content="done"),
            ]
        )
        request = ai_orchestrator.OrchestratorRequest(
            messages=(self.user_message(ai_platform),),
            task_class="ROUTINE",
            allowed_tool_names=("list_channels",),
            recover_errors=True,
        )

        result = await orchestrator.orchestrate(request, executor=executor)

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual(executor.calls, [])  # nothing from a rejected batch runs
        results = dict(self.tool_results(provider.requests[1][0]))
        self.assertIn("not allowed", results["c1"]["message"])
        self.assertIn("another call in the same batch", results["c2"]["message"])

    async def test_failed_tool_result_lets_the_model_continue_and_skips_rest_of_batch(self):
        ai_platform, ai_orchestrator, provider, orchestrator, executor = self.setup_run(
            lambda p: [
                p.AIResponse(tool_calls=(self.call(p, "c1", "list_channels"), self.call(p, "c2", "list_roles"))),
                p.AIResponse(content="The channel list failed; roles were not read."),
            ],
            lambda tools: [tools.ToolResult(False, "list_channels", "Discord API error: 500")],
        )

        result = await orchestrator.orchestrate(self.request(ai_platform, ai_orchestrator), executor=executor)

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.COMPLETED)
        self.assertEqual([name for name, _ in executor.calls], ["list_channels"])
        self.assertEqual([(tool.tool_name, tool.ok) for tool in result.executed_tools], [("list_channels", False)])
        results = dict(self.tool_results(provider.requests[1][0]))
        self.assertEqual(results["c1"]["message"], "Discord API error: 500")
        self.assertIn("earlier action in this batch failed", results["c2"]["message"])

    async def test_error_budget_is_bounded(self):
        def bad(p, index):
            return p.AIResponse(tool_calls=(self.call(p, f"c{index}", "get_channel_details", channel_id="bad"),))

        ai_platform, ai_orchestrator, provider, orchestrator, executor = self.setup_run(
            lambda p: [bad(p, index) for index in range(10)]
        )

        result = await orchestrator.orchestrate(self.request(ai_platform, ai_orchestrator), executor=executor)

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.INVALID_TOOL_PLAN)
        self.assertEqual(len(provider.requests), ai_orchestrator.MAX_RECOVERED_ERRORS + 1)
        self.assertEqual(executor.calls, [])

    async def test_fatal_result_still_ends_the_run(self):
        ai_platform, ai_orchestrator, provider, orchestrator, executor = self.setup_run(
            lambda p: [p.AIResponse(tool_calls=(self.call(p, "c1", "list_channels"),)), p.AIResponse(content="unused")],
            lambda tools: [tools.ToolResult(False, "list_channels", "not authorized", {"fatal": True})],
        )

        result = await orchestrator.orchestrate(self.request(ai_platform, ai_orchestrator), executor=executor)

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.TOOL_EXECUTION_FAILED)
        self.assertEqual(len(provider.requests), 1)

    async def test_without_recover_errors_the_strict_contract_is_unchanged(self):
        ai_platform, ai_orchestrator, provider, orchestrator, executor = self.setup_run(
            lambda p: [p.AIResponse(tool_calls=(self.call(p, "c1", "get_channel_details", channel_id="bad"),))]
        )

        result = await orchestrator.orchestrate(
            self.request(ai_platform, ai_orchestrator, recover=False), executor=executor
        )

        self.assertEqual(result.status, ai_orchestrator.OrchestratorStatus.INVALID_TOOL_PLAN)
        self.assertEqual(len(provider.requests), 1)

    async def test_failure_after_approval_is_recovered_and_next_writes_need_approval_again(self):
        ai_platform, ai_orchestrator, provider, orchestrator, executor = self.setup_run(
            lambda p: [
                p.AIResponse(tool_calls=(self.call(p, "c1", "delete_role", role_id="200"),)),
                p.AIResponse(tool_calls=(self.call(p, "c2", "delete_role", role_id="201"),)),
            ],
            lambda tools: [tools.ToolResult(False, "delete_role", "Role 200 was not found.")],
        )

        first = await orchestrator.orchestrate(self.request(ai_platform, ai_orchestrator), executor=executor)
        self.assertEqual(first.status, ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)
        second = await orchestrator.approve_confirmation(first.confirmation_id, approved=True, executor=executor)

        self.assertEqual(second.status, ai_orchestrator.OrchestratorStatus.NEEDS_CONFIRMATION)
        self.assertEqual(second.tool_plan[0].arguments["role_id"], "201")
        self.assertEqual([(tool.tool_name, tool.ok) for tool in second.executed_tools], [("delete_role", False)])
        self.assertEqual(len(executor.calls), 1)


if __name__ == "__main__":
    unittest.main()
