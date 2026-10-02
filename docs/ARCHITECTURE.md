# DarkAbyss Bot Manager Architecture

## Phase 2A Status

Phase 2A is complete. It introduced program-owned Bot Type manifests and user-owned Bot Instance storage under `DATA_ROOT/instances`.

## Phase 2B Status

Phase 2B migrates the live Admin Bot runtime to the instance architecture.

`Admin.py` now runs one selected Admin Bot instance per process:

```bat
python DarkAbyss_Core\Admin.py --instance admin-main
python DarkAbyss_Core\Admin.py --instance admin-second
```

If `--instance` is omitted, `admin-main` is selected.

Only `admin-main` receives special first-run bootstrap and Phase 1 migration behavior. Other instance IDs must already exist.

## Bot Type

A Bot Type is program-owned. It describes an available bot implementation and lives in the application/release tree.

Current manifest:

```text
bots/admin/manifest.json
```

Current manifest shape:

```json
{
  "schema_version": 1,
  "id": "admin",
  "display_name": "Admin Bot",
  "version": "1.0.0",
  "entrypoint": "DarkAbyss_Core/Admin.py",
  "default_config": "DarkAbyss_Core/defaults/admin_config.json"
}
```

Manifests must not contain user IDs, guild IDs, tokens, absolute local paths, credentials, logs, or mutable user data.

## Bot Instance

A Bot Instance is user-owned. It stores mutable configuration and secrets for one configured copy of a bot type.

Instance layout:

```text
<DATA_ROOT>/instances/<instance_id>/
    instance.json
    config.json
    secrets/
        token.txt
    runtime/
        admin_bot.lock
    logs/
    data/
```

These files are user-owned. Program updates must never overwrite them.

`display_name` is a mutable local Manager label. It can be changed from the GUI through `instance_store.update_instance_display_name()` without renaming the instance directory, instance ID, config, secrets, logs, runtime, or data directories. Metadata updates validate the existing instance first and atomically replace only `instance.json`.


- `instance.json` stores minimal metadata for the instance.
- `config.json` is mutable user configuration for that instance.
- `secrets/token.txt` stores that instance's Discord token.
- `runtime/` stores runtime state such as the instance-specific lock file.
- `logs/` is reserved for instance logs.
- `data/` is reserved for persistent bot-specific data.

Minimal instance metadata:

```json
{
  "schema_version": 1,
  "id": "admin-main",
  "bot_type": "admin",
  "display_name": "Admin Bot"
}
```

## Admin Runtime Selection

Startup resolves an immutable Admin runtime context:

- selected `instance_id`
- selected `config.json`
- selected `secrets/token.txt`
- selected `runtime/admin_bot.lock`

`load_config()` reloads config from the selected instance. This preserves the existing `/execute` config reload behavior without falling back to Phase 1 paths.

`load_token()` reads only the selected instance token and rejects missing, empty, or placeholder tokens.

The lock file is instance-specific, so `admin-main` and `admin-second` do not block each other. Two processes using the same instance are still prevented from running simultaneously.

## AI Administration Tool Layer

AI-1 introduces a transport-neutral Admin Tool Layer in:

```text
DarkAbyss_Core/admin_tools.py
```

The intended future flow is:

```text
User request
    -> Manager GUI or Discord transport
    -> future AI Orchestrator
    -> Admin Tool Layer
    -> Discord API
```

AI provider integration is not implemented in AI-1. There is no Gemini, Groq, OpenRouter, `/ai` command, dedicated AI control channel, Manager AI chat, API key storage, or cross-device locking in this phase.

The Admin Tool Layer is the safe capability boundary:

- AI will only be able to request registered tools.
- Provider-facing tool arguments are JSON-safe primitives and objects validated against provider-neutral JSON Schema. Discord snowflakes are represented as decimal strings to avoid JSON integer precision ambiguity.
- Program code owns tool names, argument validation, and risk classification.
- Program code determines whether a tool is read-only, normal write, or destructive.
- `discord.py` objects such as members, roles, text channels, and voice channels are resolved internally by the tool layer from validated IDs. They never come from a model/provider request.
- Destructive confirmations will live above this layer in a later orchestrator/transport phase.
- Discord message content, usernames, channel names, role names, and other server-provided content are untrusted data. They are not system instructions, authorization, tool definitions, confirmation, or permission to execute follow-up actions.
- The AI tool path has no shell/system execution and no arbitrary Python/code execution. It only calls explicit Discord admin capabilities registered in `admin_tools.py`.
- The schemas are intended for Groq, Gemini, and future OpenRouter tool-calling integration without provider-specific code inside `admin_tools.py`.

`Admin.py` still owns Discord bot startup, slash-command wiring, Discord-specific responses, and audit routing. Existing `/execute` behavior delegates to the Admin Tool Layer but keeps its current access policy and presentation.

Future transports may include:

- `/ai` in any Discord channel for explicitly whitelisted actors.
- A dedicated configured AI control channel.
- Manager GUI AI chat.

These transports do not exist yet.

## Optional AI Platform Principle

AI is optional. DarkAbyss Core must function with:

- zero AI providers configured
- zero AI API keys
- no available AI network service

Without AI configuration:

- Manager must still work.
- Bot lifecycle must still work.
- `/execute` must still work.
- Admin Tool Layer must still work.
- Updates, config, and logs must still work.

AI features should report a clear unavailable/not-configured state instead of crashing the application. Future provider failures must be isolated to AI requests. No provider is a startup dependency, and the application must not perform provider network checks automatically at startup. Provider-specific modules and dependencies should be isolated and lazy where practical.

Provider adapters expose separate local and network-facing operations:

- `get_local_availability(...)`: synchronous, local only, and must not perform network I/O. Routing may call only this method.
- `test_connection(...)`: asynchronous explicit user action; network is allowed.
- `generate(...)`: asynchronous future AI request; network is allowed.

There are no network calls on module import, registry construction, profile loading, routing, Manager startup, or bot startup.

AI request/response objects are provider-neutral and JSON-compatible. They contain messages, JSON-safe tool schemas, JSON-safe tool-call arguments, finish metadata, and no provider-specific raw response object. Admin Tool schemas can pass through this contract without `discord.py` objects.

AI-2B adds the first optional provider adapter:

- Provider: Groq
- Initial tested model metadata: `openai/gpt-oss-120b`
- API style: Groq OpenAI-compatible Chat Completions
- Manager panel: `AI Providers...`

Groq is not a core dependency. Core remains fully operational when Groq is not configured, the local key is missing, the optional adapter cannot load, Groq is offline, quota is exhausted, or credentials are rejected. Groq outage/key/quota failure affects only explicit AI requests.

The Groq adapter uses standard-library HTTPS and performs network I/O only from explicit provider calls:

- `test_connection(...)`
- `generate(...)`

No Groq request occurs on import, provider construction, registry construction, routing, Manager startup, or bot startup.

Groq tool calls are data until a later orchestrator phase validates, plans, confirms, and executes them. AI-2B does not execute Admin Tools, mutate Discord, decide confirmation policy, write bot config, or route task classes. Groq built-in browser search, code execution, remote MCP, and provider-hosted tool execution are not enabled.

For `openai/gpt-oss-120b`, DarkAbyss uses `reasoning_effort` (`low`, `medium`, `high`) and requests `include_reasoning = false` for all GPT-OSS requests, including normal text and local function-schema requests. DarkAbyss must not expose model chain-of-thought/reasoning in responses, Manager UI, Discord, logs, or audit output. AI-2B also applies a conservative local `max_completion_tokens <= 8192` safety/resource ceiling; this is a DarkAbyss limit, not a statement about the model's full provider-side maximum.

AI-2C adds the second optional provider adapter:

- Provider: Google Gemini
- Provider ID: `gemini`
- Default profile: `gemini-default`
- Credential ref: `gemini-default`
- Initial model metadata: `gemini-3.8-flash`, `gemini-3.5-flash-lite`
- API style: native Gemini `generateContent` REST
- API base: `https://generativelanguage.googleapis.com/v1beta`
- Authentication: `x-goog-api-key`, not `Authorization: Bearer`
- User-Agent: `DarkAbyssBotManager/AI-2C`

Gemini uses direct standard-library HTTPS. No `google-genai`, `google-generativeai`, `requests`, `httpx`, OpenAI SDK, or Google provider SDK is required. Gemini network I/O occurs only from explicit `test_connection(...)` and `generate(...)` calls. There is no online model discovery in AI-2C, and Gemini credentials are not validated by prefix because Google AI Studio keys are opaque non-empty secrets.

Gemini request mapping uses native `generateContent` structures:

- `SYSTEM` messages become ordered `systemInstruction.parts`.
- `USER` messages become `contents` entries with role `user`.
- `ASSISTANT` messages become Gemini role `model`.
- Assistant tool calls become `functionCall` parts.
- Tool results become role `user` `functionResponse` parts matched by `tool_call_id` to the preceding assistant `AIToolCall`.

Gemini local Admin Tool schemas are exposed only as `functionDeclarations`. Built-in Google Search, Google Maps, code execution, URL context, file search, computer use, provider-hosted MCP, and other hosted Gemini tools are not enabled. Function calls remain data only; AI-2C does not execute Admin Tools, mutate Discord, decide confirmation policy, or route task classes.

Gemini `reasoning_effort` maps to `generationConfig.thinkingConfig.thinkingLevel` (`low`, `medium`, `high`) with `includeThoughts = false`. Thinking level controls provider computation only. AI-2C exposes only Gemini 3.x model metadata, so the Gemini adapter allowlist is deliberately small: `reasoning_effort` and `max_output_tokens`. Gemini `temperature`, `top_p`, and `top_k` are not sent. User-configured `max_output_tokens` has a conservative local DarkAbyss safety/resource ceiling of `8192`; this is not a statement about Gemini 3.8's provider-side maximum. The Gemini Test Connection smoke request uses `reasoning_effort = low` but does not force `max_output_tokens`, because Gemini thinking tokens count against output budgets. DarkAbyss never requests, displays, logs, audits, or copies chain-of-thought/thought summaries into `AIResponse.content`, Manager UI, Discord, logs, or audit output.

Gemini 3 function-calling may return opaque encrypted `thoughtSignature` values. These are continuation state, not chain-of-thought text. In native Gemini REST JSON, `thoughtSignature` belongs to the `Part` as a sibling of `functionCall` or `text`; it is not nested inside `functionCall`. DarkAbyss preserves function-call signatures only inside JSON-safe `AIToolCall.metadata` as:

```json
{"gemini": {"thought_signature": "opaque-string"}}
```

When replaying Gemini assistant tool calls in a later turn, the Gemini adapter restores the signature onto the same outgoing `functionCall` Part. For parallel Gemini function calls, the first `functionCall` Part in a current model step normally carries the signature; later parallel `functionCall` Parts may have no signature. DarkAbyss preserves any later signatures if present, but it does not copy or manufacture signatures. Signature validation is current-turn-only: the current turn starts at the most recent ordinary `USER` message, `TOOL` does not start a turn, and histories with no `USER` are treated conservatively as current. Previous completed turns with missing Gemini signatures are tolerated and replayed if possible so older/provider-switched history does not invalidate the conversation.

Gemini text Parts may also carry an opaque `thoughtSignature`. DarkAbyss preserves the original visible Gemini text-Part boundaries in sanitized `AIResponse.metadata` so signed and unsigned Parts can be replayed exactly. `AIResponse.content` remains a convenient concatenated visible string, but replay uses the original visible Part list when present and fails locally if metadata no longer matches the assistant content. An empty visible text Part with a signature is still preserved and replayed as a real Part. Text from Parts marked `thought: true` is never copied into `AIResponse.content` or replay metadata.

When a Gemini model step emits multiple parallel function calls and the following provider-neutral `TOOL` messages contain their results, DarkAbyss matches responses only against the immediately preceding assistant function-call step, requires exactly one result per call, rejects stale/unknown/duplicate/incomplete result sets, and batches valid `functionResponse` Parts into one Gemini `role: user` content ordered by the original model call order. Sequential function-calling steps remain separate assistant/user content pairs with independent signatures. Function call IDs are exact provider IDs and Gemini 3 parsed function calls without a non-empty string ID are rejected. The adapter never stores raw provider responses or thought text for this purpose.

Gemini errors are normalized without raw response bodies: `401` or explicit invalid-key reasons become `CREDENTIAL_INVALID`; `403` becomes `ACCESS_FORBIDDEN`; quota/rate-limit, billing/credit prerequisites, model unavailable, 5xx, network, and timeout conditions become safe unavailable states/messages.

The Manager `AI Providers...` dialog is device-local and global. It is not tied to the selected Discord Bot Instance and can be opened even when no bot is selected.

Current AI provider settings behavior:

- The user enters Groq and/or Gemini API keys inside Manager.
- The key is stored device-locally via `CredentialStore`.
- Existing saved keys are never re-displayed, partially displayed, or copied back into the UI.
- Saving an empty key field preserves an existing key.
- Removing a key is an explicit action.
- Model/profile settings are stored separately in `<DATA_ROOT>/config/ai.json`.
- `ai.json` contains no raw API key.
- Editing Groq settings replaces only the `groq-default` profile. Editing Gemini settings replaces only the `gemini-default` profile. Both preserve other provider profiles and existing routing assignments.
- Malformed `ai.json` is preserved and normal Save is disabled until the user explicitly resolves the settings file.
- Another PC uses its own device-local credential and profile settings.
- `Test Connection` is an explicit user action and runs outside the GUI thread.

AI-3 will add the orchestrator loop that can interpret model plans/tool-call data, apply confirmation policy, and call the Admin Tool Layer when authorized. AI-2B/AI-2C only store settings, call providers explicitly, and parse model output into data.

AI-3A adds the provider-neutral backend orchestrator core in `ai_orchestrator.py`. It is still an optional accessory: Manager, bot lifecycle, Kairo/Admin, `/execute`, config, logs, and updates remain operational with zero providers, missing credentials, invalid AI settings, provider import failures, offline networks, rejected keys, or exhausted quotas. The orchestrator performs no provider network calls during import, construction, registry construction, settings loading, routing, Manager startup, or bot startup. Network access is limited to explicit orchestration requests.

The AI-3A orchestrator flow is:

```text
caller
  -> OrchestratorRequest
  -> TaskClass routing or configured-profile manual override
  -> selected AIProfile
  -> provider.generate(...)
  -> final visible response OR validated Admin Tool plan
  -> risk evaluation
  -> optional confirmation
  -> injected tool executor
  -> provider-neutral TOOL results
  -> same provider/profile continuation
```

Routing remains provider-neutral. `TaskClass` values (`DIRECT`, `ROUTINE`, `PLANNER`, `CREATIVE`) select configured profile IDs from `RoutingConfig`; `DIRECT` bypasses providers. Manual override selects a configured profile ID and does not accept raw provider/model/credential injection. Manual override does not fallback unless the caller explicitly opts in.

`RoutingConfig` schema version 1 is backward-compatible and now supports explicit ordered fallback lists:

- `routine_fallback_profile_ids`
- `planner_fallback_profile_ids`
- `creative_fallback_profile_ids`

Missing fallback fields load as empty tuples. Fallbacks are never auto-populated and never hardcode provider roles such as "Groq = hard" or "Gemini = easy". Fallback happens only before any Admin Tool has executed. After any side effect, provider failure terminates the run safely with no provider switch.

Admin Tool exposure uses data-only provider schemas from the authoritative Admin Tool Layer. Each schema contains only `name`, `description`, and JSON Schema `arguments`; no Discord objects, handlers, callables, contexts, secrets, or execution handles are exposed. Provider-returned tool calls are validated locally before confirmation or execution: unknown tools, disallowed tools, missing/unknown arguments, invalid snowflakes, duplicate/missing call IDs, and non-JSON-safe arguments are rejected without execution.

Tool risk is derived only from `ToolDefinition.risk` and maps to provider-neutral `ToolRisk` (`READ`, `NORMAL`, `DESTRUCTIVE`). `TaskClass` never changes tool risk. A multi-tool batch uses the maximum risk in the batch, so `READ + DESTRUCTIVE` is gated as `DESTRUCTIVE` and no read call executes early.

The default confirmation policy allows `READ` and `NORMAL` by default, can optionally confirm `NORMAL`, and always requires confirmation for `DESTRUCTIVE`. AI-3A has no option to disable destructive confirmation. Confirmation IDs are opaque, random, single-use, held only in memory, and tied to the exact stored profile/provider/model/tool order/arguments. Approval executes the stored immutable plan; callers cannot resubmit or edit tools or arguments. Rejection executes zero tools and invalidates the ID.

Tool execution is injected through an async executor callback. The orchestrator does not construct Discord contexts, does not import Qt widgets, and does not call Discord interaction/message APIs. AI-4 will adapt Discord authorization and Admin Tool execution to this interface. Batches execute in exact model order and fail fast; already executed calls are reported, remaining calls are skipped, and no rollback or fallback is attempted after partial execution.

Tool results are serialized into bounded JSON-safe provider `TOOL` messages with exact provider call IDs. Oversized or non-serializable results are replaced with safe bounded failure payloads. After successful tool execution, continuation uses the same selected provider/profile/model and appends the assistant response plus tool messages to history, preserving provider-neutral response metadata needed for Gemini/Groq continuation while never exposing hidden reasoning or opaque `thoughtSignature` values in public results.

AI-3A also adds provider-neutral compare mode. Compare mode requires at least two distinct configured profile IDs, performs one generate call per selected profile with the same messages and tool schema set, uses no fallback, and executes zero Admin Tools before candidate selection. Candidates contain safe visible content, validated tool plan data, batch risk, or sanitized failure state. Selecting a candidate uses the exact stored candidate, does not re-query all providers, and then follows the normal execution/confirmation/tool-loop rules on the same selected provider/profile.

AI-3A continuation state is explicit and bounded. Each orchestration fixes its tool allowlist from the original request (or compare request): `allowed_tool_names=None` keeps every registered Admin Tool available, `()` exposes zero tools, and an explicit list stays exactly that list across every tool round, confirmation resume, and selected compare continuation; provider schemas and local validation always use the same allowlist. The orchestrator SYSTEM instruction is inserted exactly once when raw caller messages become provider history; tool-loop continuation, confirmation resume, and compare-candidate continuation reuse that already-initialized history. `MAX_TOOL_ROUNDS` is a hard total cap for the selected-provider orchestration: every assistant response that yields a tool batch consumes one round, the counter is carried through confirmation pauses and compare selection, and at the cap the result is `LIMIT_REACHED` with no further execution or confirmation. The `ConfirmationPolicy` is also fixed run state: it is captured when `orchestrate()` or `select_compare_candidate()` starts and reused for every later round and confirmation resume. `approve_confirmation()` accepts only the confirmation ID, the approve/reject decision, and the executor; it cannot replace the policy, profile, provider, model, plan, arguments, order, or allowlist. `DESTRUCTIVE` gating is enforced independently of the policy object. Injected dependencies are honored by explicit `None` checks, so an explicitly supplied empty `ProviderRegistry()` stays empty and the default Groq/Gemini registry is created only when no registry is passed.

Pre-AI-4 hardening fixes the remaining AI-3A safety contract. `approve_confirmation()` requires `approved` to be exactly `True` or `False`; any other value (strings, integers, `None`, containers), a non-string confirmation ID, or approval without a callable executor raises `ValueError` before pending state is touched, so invalid input neither authorizes, rejects, nor consumes the stored plan. `ConfirmationPolicy` is a fixed-semantics value object: only the exact type is accepted, `confirm_normal` must be a real `bool`, and the orchestrator evaluates risk without calling any overridable method (`READ` never, `NORMAL` iff `confirm_normal`, `DESTRUCTIVE` always). `ValidatedToolCall.arguments` are recursively copied and frozen (mappings become read-only mappings, lists/tuples become tuples, non-JSON values and non-finite numbers are rejected); `public_dict()` and every executor invocation receive fresh ordinary `dict`/`list` copies, so neither callers, providers, nor executors can alter a stored confirmation plan. Pending confirmations and compare results are in-memory only, bounded in count (`MAX_PENDING_CONFIRMATIONS`, `MAX_PENDING_COMPARES`), and expire after a TTL measured on a monotonic, injectable clock (`CONFIRMATION_TTL_SECONDS`, `COMPARE_TTL_SECONDS`). Expired entries are purged lazily on every create and lookup without background threads; an expired ID behaves like an unknown one and executes nothing. When a bound is reached new state is refused with a safe result (compare refuses before spending any provider request) rather than evicting other users' pending state. A single compare request is also bounded: `ComparePlanRequest` stores an immutable copy of 2 to `MAX_COMPARE_PROFILES` (4) distinct, valid profile IDs and a validated `ToolRisk`, and `compare_plans()` re-checks that bound on a snapshot of the IDs before any provider request, so even a tampered request cannot fan out into more provider calls. Malformed provider output (a non-`AIResponse` object, unexpected validation exceptions, over-long call IDs) and broken local registry/credential lookups are contained as provider-unavailable or `INVALID_TOOL_PLAN` results. Public diagnostic messages are length-bounded; transport-specific rendering such as Discord mention escaping and per-user confirmation ownership belongs to the AI-4 transport.

Attempt history accumulates in routing order across the whole request: locally unavailable/skipped profiles (`UNAVAILABLE`), selections (`SELECTED`), and provider failures (`FAILED`) are all retained, so a fallback success still records why fallback happened. Attempt messages contain only safe text and exception type names, never raw exception strings, credentials, headers, or HTTP bodies. Unexpected exceptions raised by the injected tool executor are contained: the batch stops immediately, already-completed tool summaries are kept, the failing call is recorded as not successful, remaining calls are skipped, the result is `TOOL_EXECUTION_FAILED` with a safe message, and no fallback occurs.

AI-3A deliberately adds no Discord AI surface. There is no `/ai`, no natural-message listener, no Message Content Intent, no Manager AI chat, and no AI access-policy reuse of `/execute` admin settings. AI-4 will add an explicit-whitelist Discord `/ai` transport around this backend.

AI-4 adds that transport as `DarkAbyss_Core/admin_ai.py`, wired into `Admin.py` as the `/ai` slash command (`prompt`, at most 2000 characters, plus optional `mode`: `routine` (default), `planner`, `creative`; `DIRECT` is never exposed). `ai_orchestrator.py` stays Discord-free; everything Discord-specific lives in the transport.

- AI authorization is a separate, explicit whitelist: `ai_allowed_user_ids` and `ai_allowed_role_ids` (program defaults: empty lists; no config version bump, older instance overrides inherit them through defaults). Access requires a guild `Member` whose ID is listed or who currently holds a listed role. Discord Administrator, server ownership, `allow_server_administrators`, and the `/execute` `allowed_user_ids`/`allowed_role_ids` never grant `/ai`. An empty AI whitelist means nobody may use `/ai`. `/execute` authorization is unchanged.
- Optional-AI isolation: `admin_ai` imports no AI or provider module at import time, and `Admin.py` guards its import. A per-process orchestrator is created lazily on the first authorized `/ai` request (never for unauthorized requests); a construction/import failure returns a contained ephemeral "unavailable" reply and is retried on a later request. No provider connection test or network access happens at startup or command registration. Message Content Intent stays disabled.
- `/ai` uses `ConfirmationPolicy(confirm_normal=True)`: READ tools may run automatically, NORMAL and DESTRUCTIVE plans always need explicit approval. The executable plan is fully reviewable before approval: it is rendered only from the immutable validated stored plan (`ValidatedToolCall.public_dict()`), never model prose, and shows every tool, its risk, and every argument with its complete value as exact JSON (display-hostile characters such as backticks and invisible/format characters appear as JSON escapes, so the shown text decodes to exactly the executed value). Nothing is truncated: the plan may span bounded ephemeral preview pages (at most 1900 characters each, at most `AI_MAX_PREVIEW_PAGES` = 20 pages; long values are split only at JSON-escape boundaries into labelled consecutive parts), followed by one short control message with the overall risk and the Approve/Cancel buttons. If the exact plan cannot be rendered or would exceed the page bound, the transport fails closed: no Approve button is shown and nothing from that plan executes. The transport rejects the pending core confirmation and only reports the plan as cancelled when the public core result confirms the rejection (`CANCELLED`); otherwise it says no approval control was created and the pending request will expire automatically. Status wording always refers to the current plan: if earlier actions of the same request already ran, the reply lists them and states they were not undone, and it never claims that nothing at all was executed. The opaque confirmation ID is never displayed; it is held only in the in-memory view state.
- Confirmation ownership lives in the transport: each pending confirmation is bound to the requesting user, guild, and channel. Before `approve_confirmation()` is called, the transport verifies all three bindings and reloads the config to confirm the user is still AI-whitelisted; any failed check is denied ephemerally without consuming the confirmation. The decision must be a real `bool` (anything else raises before any state change). A valid Approve/Cancel resolves the view once (buttons removed, repeated clicks rejected). Follow-up rounds that need confirmation get a fresh view with the same binding. The view timeout (600 s) does not exceed the core confirmation TTL (900 s); an expired core confirmation executes nothing.
- The injected executor is built from the current Discord interaction (a fresh one for every approval). Before every Admin Tool call it reloads the config, checks the guild and requesting user, and re-evaluates the AI whitelist against the member's current guild state; if authorization is gone it returns a failed tool result and executes nothing. It runs `admin_tools.execute_tool()` with `source="/ai"` and `AdminToolContext.suppress_mentions=True`, and audits each executed tool through the existing audit channel (an audit failure never turns a completed action into a failure).
- Mention safety: every `/ai` transport message (responses, errors, confirmations, results) is ephemeral (AI-5 control-channel messages are public, see below) and every AI transport message is sent with `AllowedMentions.none()`, and model output is chunked/truncated to Discord limits in the transport. AI-originated `send_message` sends with `AllowedMentions.none()`; `/execute` keeps the default `suppress_mentions=False` behavior.

AI-5 adds an optional natural-message AI control channel to the same transport (`admin_ai.py`); slash `/ai` and control-channel messages share one orchestration path, executor, authorization, plan preview, fail-closed rendering, and mention handling, and use the same lazy per-process orchestrator.

- Config: `ai_control_channel_id` (snowflake or `null`, program default `null`; no config version bump, older overrides inherit `null`). `null` disables natural-message AI. It is edited in Manager Setup Bot as "AI control channel ID" and is distinct from `audit_channel_id`.
- Message Content Intent is optional: `Admin.main()` requests it before the gateway connection only when `ai_control_channel_id` is set; otherwise the bot connects exactly as before, so `/execute`, `/ai`, and Manager never depend on the privileged intent. If Discord refuses the privileged intent while the channel is enabled, startup prints an actionable message (enable the intent in the Developer Portal or clear the channel ID) instead of a traceback. Enabling/disabling the channel requires a bot restart.
- Message filter: a message becomes an AI request only if it is in a guild, in exactly the configured channel (gate fixed at startup and re-checked against the reloaded config), from a real guild member who is not a bot (including Kairo itself) and not a webhook, with non-empty text, and the author currently passes the explicit AI whitelist (`ai_allowed_user_ids` / `ai_allowed_role_ids`; Administrator and the `/execute` lists never grant access). Everything else is ignored without loading providers, calling a provider, or replying. Attachments are not interpreted.
- Natural messages always use `TaskClass.ROUTINE` (explicit modes stay available through `/ai mode:`) and are stateless: each Discord message is one independent orchestrator request containing only that message; there is no cross-message memory.
- Delivery: `/ai` stays ephemeral; control-channel conversations are public in that channel (the first reply references the user's message with `mention_author=False`). Every public message uses `AllowedMentions.none()`. A pending confirmation stores its delivery mode, so continuations after Approve/Cancel stay ephemeral for `/ai` and public for the control channel. Public confirmation buttons keep the user+guild+channel ownership checks: other users get an ephemeral denial and the confirmation is not consumed; execution-time authorization is rechecked before every tool call, tools still run with `source="/ai"`, `suppress_mentions=True`, and the existing audit path.
- Engine indicator: AI answers and confirmation controls end with a small `-# Provider · profile · model` line (plus `fallback` when used), built only from the public `OrchestratorResult` fields `provider_id`, `profile_id`, `model_id`, `fallback_used`; it is omitted when absent and never shows credential references, attempt errors, or provider payloads.

AI credentials are device-local and stored outside program versions, bot instance config, release artifacts, and the source tree:

```text
<DATA_ROOT>/secrets/ai/<provider_id>/<credential_ref>.secret
```

Gemini keys are stored at `<DATA_ROOT>/secrets/ai/gemini/gemini-default.secret`. Groq keys are stored at `<DATA_ROOT>/secrets/ai/groq/groq-default.secret`.

`CredentialReference` is only a logical pointer. The same `credential_ref` such as `groq-default` or `gemini-default` may resolve to different local secrets on different computers. AI profiles and routing preferences are also device-local by default in the current architecture and do not belong to the Discord Bot Instance's portable config. This allows PC A and PC B to use different providers/profiles while hosting the same logical bot at different times. Cloud sync is not implemented.

Routing distinguishes local availability states without network access: `NOT_CONFIGURED`, `PROVIDER_MISSING`, `CREDENTIAL_MISSING`, `CREDENTIAL_INVALID`, `ACCESS_FORBIDDEN`, `DISABLED`, `UNAVAILABLE`, and `AVAILABLE`.

Conceptual future architecture:

```text
CORE
    Manager
    Admin Bot
    Admin Tool Layer

OPTIONAL AI ACCESSORY BUS
    Provider Registry
    Credential Profiles
    AI Profiles
    Task Router
    Orchestrator
    Compare/Fallback
```

Provider choice and task class are independent. The task complexity classes are:

- `DIRECT`
- `ROUTINE`
- `PLANNER`
- `CREATIVE`

Tool risk remains separate:

- `READ`
- `NORMAL`
- `DESTRUCTIVE`

Routing should eventually allow:

- `ROUTINE` -> selected AI profile
- `PLANNER` -> selected AI profile
- `CREATIVE` -> selected AI profile

The system must not hardcode provider roles such as "Gemini = routine" or "Groq = planner". Those assignments are user-selectable.

Provider model task-class metadata is advisory only. A user may assign any profile to `ROUTINE`, `PLANNER`, or `CREATIVE` for testing. Hard capability checks, such as future tool-call support, belong in the orchestrator where that capability is actually required.

Future user controls should allow:

- automatic routing
- manual provider/profile override per request
- comparing multiple profiles on one request
- selecting one generated plan

Compare mode is plan-only until the user chooses a plan. A fallback-generated destructive plan requires fresh review/confirmation if it differs from the original plan or comes from another provider.

## Shared AI Access And Runtime Ownership

Access policy and runtime host ownership are separate problems.

Discord role membership is the preferred shared whitelist mechanism. Existing `allowed_role_ids` refer to roles stored by Discord; if a person gains or loses that Discord role, every active host observes the same membership without synchronizing a local user list. Explicit `allowed_user_ids` remain valid, but they are local configuration entries unless the instance configuration itself is copied or synchronized.

Future AI access uses:

```text
explicit allowed user
OR explicit allowed role
```

Discord Administrator alone must not grant AI access. Future UI should recommend role-based AI access for multi-PC use. AI-2A does not implement role provisioning UI.

Runtime lease is the different question of which PC may host one bot right now. Current local process/file locks protect only one machine.

Future protected shared-host mode requires an atomic remote lease:

```text
PC A owns active lease:
    PC A = LOCAL ACTIVE
    PC B = REMOTE ACTIVE / READ ONLY
    PC B Start disabled
    PC B Restart disabled
```

Lease state concept:

- public bot/application identity
- device id
- session id
- heartbeat
- lease expiry
- atomic claim
- renew
- release
- controlled takeover after expiry

The Discord token must never be written into lease state. Discord Application ID is a suitable future public shared bot identity and should eventually be persisted for this purpose. Strict cross-device exclusion must not pretend that a local file lock or a non-atomic Discord message is sufficient. The exact coordinator backend is not selected in AI-2A.

Normal local-only operation does not require a lease coordinator. If a future user explicitly enables Protected Shared-Host Mode and its lease coordinator is unavailable, startup must fail closed for that protected instance rather than risk two active hosts.

Runtime-1 is a release gate before supported multi-PC host switching, but it is not required to continue single-PC AI development.

Current AI roadmap:

- AI-1: done; Admin Tool Layer.
- AI-2A: provider-neutral optional AI platform foundation: provider interface/registry, credential references, AI profiles, routing configuration, and availability states. Zero configured providers is a valid normal state.
- AI-2B: Groq adapter.
- AI-2C: Gemini adapter.
- AI-3: Orchestrator, routing, manual override, fallback, compare mode, and confirmation policy.
- AI-4: `/ai` explicit-whitelist Discord command.
- Runtime-1: cross-device single-host protection, mandatory before supported multi-PC host switching.
- AI-5: dedicated AI control channel.
- AI-6: Manager GUI AI chat.
- AI-7: richer admin/design capabilities.
- Later: OpenRouter adapter, generic runtime/plugin work, and remote/server infrastructure.

Local inference is not part of the current core roadmap. It may only be added much later as another optional adapter.

## Phase 1 Migration

Legacy Phase 1 sources:

```text
<DATA_ROOT>/config/admin.json
<DATA_ROOT>/secrets/admin_bot_token.txt
```

Migration rules:

- If `admin-main` exists, it is authoritative and is not overwritten.
- If `admin-main` does not exist, it is atomically created from Phase 1 config/token when useful Phase 1 data exists.
- If no useful Phase 1 data exists, `admin-main` is created from program defaults and a placeholder token.
- Phase 1 source files are never deleted or modified automatically.
- A Phase 1 token equal to `PUT_DISCORD_BOT_TOKEN_HERE` is treated as a placeholder.

## Multiple Admin Instances

Multiple Admin Bot instances are independent:

```bat
python DarkAbyss_Core\instance_store.py create admin admin-second
Admin.bat admin-second
```

Each instance has separate:

- config
- token
- runtime directory
- lock file
- logs directory
- data directory

Running multiple instances means running multiple independent OS processes, one selected instance per process.

## Phase 3A Manager Core

Phase 3A adds a GUI-independent process supervision layer in:

```text
DarkAbyss_Core/manager_core.py
```

Manager Core owns process lifecycle only. Bot processes own bot functionality.

Current lifecycle API:

- `start(instance_id)`
- `stop(instance_id, timeout=...)`
- `restart(instance_id, timeout=...)`
- `status(instance_id)`
- `list_status()`
- `shutdown_all(timeout=...)`

One Bot Instance maps to one OS process. Multiple instances are independent processes:

```text
Manager Core
    ├── admin-main process
    └── admin-second process
```

Manager Core resolves launch targets through:

```text
Bot Instance -> bot_type -> Bot Type manifest -> entrypoint
```

It does not hardcode `Admin.py`.

## Entrypoint Contract

Managed bot entrypoints must accept:

```text
--instance <instance_id>
```

The current source-runtime launch strategy is:

```text
sys.executable <bot_type.entrypoint> --instance <instance_id>
```

Launch construction is isolated behind `LaunchSpec` so later packaged/runtime launch strategies can replace it without rewriting process supervision.

Manager Core launches children with argument lists only. It does not use `shell=True`, `cmd /c`, PowerShell, `os.system`, `eval`, or `exec`.

## Process Status Model

Manager Core exposes immutable process status snapshots:

- `instance_id`
- `bot_type`
- `state`
- `pid`
- `started_at`
- `uptime_seconds`
- `exit_code`

States:

- `STOPPED`: no running child is owned by this Manager Core session.
- `RUNNING`: the owned child is still running according to `process.poll()`.
- `EXITED`: the owned child exited naturally and its exit code was captured.

Before first start, valid instances report `STOPPED` with no PID and no exit code.

After explicit `stop()`, status is `STOPPED` and retains the final exit code. Natural child exit is detected by polling `status()` and reported as `EXITED`.

## Process Ownership Boundary

Phase 3A process ownership is in-memory only.

The Manager Core object owns only child processes that it started during the current Python session. It does not adopt arbitrary PIDs after restart, does not persist process handles, and is not a daemon or service.

Future persistent process adoption/service mode must be designed later.

## Manager Logs

Child stdout/stderr are redirected to instance-owned logs:

```text
<DATA_ROOT>/instances/<instance_id>/logs/process.stdout.log
<DATA_ROOT>/instances/<instance_id>/logs/process.stderr.log
```

Logs are opened in append mode. They are user-owned runtime data and must not be tracked by Git.

Manager Core does not put bot tokens in process command lines and does not log token contents.

## Phase 3A Non-Goals

Not implemented yet:

- persistent auto-start or auto-restart policy
- restart delay/limit settings
- GUI or tray icon
- daemon/service mode
- HTTP/WebSocket/remote control
- updater
- GitHub integration
- packaging
- Alehandro migration

## Phase 3B Manager Core Hardening

Phase 3B keeps Manager Core GUI-independent while adding safer read models and lifecycle hardening for future GUI/headless callers.

Public read APIs now include:

- `get_instance_info(instance_id)`
- `list_instance_info()`

`InstanceInfo` is an immutable GUI-facing snapshot. It includes identity, bot type metadata, current process status, config path, logs directory, and Manager-owned stdout/stderr log paths. It does not expose token contents, config contents, secret contents, or mutable user data.

`list_status()` and `list_instance_info()` return instances sorted by `instance_id`.

All public read APIs refresh owned process records before reporting state. If a child exited naturally, `status()`, `get_instance_info()`, `list_status()`, and `list_instance_info()` report `EXITED`, capture the exit code, and finalize Manager-owned log handles.

Public Manager APIs normalize storage and registry failures as `ManagerCoreError` subclasses or `ManagerCoreError` itself. Malformed instances are reported clearly and are not silently skipped during listing.

## Manager Core Concurrency

Manager Core uses:

- a small global lock for shared dictionaries
- per-instance lifecycle locks for `start()`, `stop()`, and `restart()`

Same-instance lifecycle operations are serialized, so two concurrent `start("admin-main")` calls cannot create duplicate managed children.

Different instances remain independent. A blocking stop for `admin-main` does not hold the global manager lock while waiting on the process, so status/read operations for `admin-second` can still complete.

## LaunchSpec Validation

Before spawning a child process, Manager Core validates:

- executable is a non-empty string
- args is a tuple of strings
- cwd exists and is a directory
- env maps strings to strings
- stdout/stderr log paths stay directly under the selected instance's `logs/` directory

Child processes are still launched with argument lists only. Manager Core does not use shell execution.

Manager Core copies the `LaunchSpec` environment before spawn and enforces `DARKABYSS_DATA_DIR` itself for the child process. Custom launch-spec builders cannot omit or override the selected Manager data root, and their original environment mapping is not mutated.

Manager-created process logs are limited to:

```text
<DATA_ROOT>/instances/<instance_id>/logs/process.stdout.log
<DATA_ROOT>/instances/<instance_id>/logs/process.stderr.log
```

Manager Core does not read token contents, does not edit config/token files, and does not place token values in command arguments.

## Shutdown and Failure Behavior

Invalid timeout values are rejected. A timeout of `0` is allowed and means terminate, immediately escalate to kill if the child is still running, then reap.

If stopping one instance fails, `shutdown_all()` still attempts all other managed records and returns a result or error per considered instance.

Process ownership remains in-memory only. Phase 3B still does not add PID files, persisted process state, auto-start, auto-restart, GUI, daemon/service mode, HTTP, WebSocket, updater, or packaging.

## Phase 4A Versioned Configuration

Phase 4A separates program-owned configuration defaults from user-owned overrides.

Program-owned files:

- bot type manifests
- default config files such as `DarkAbyss_Core/defaults/admin_config.json`
- declarative config schemas such as `bots/admin/config.schema.json`
- migration code

User-owned files:

- instance `config.json` override files
- instance `config.meta.json` version metadata
- config migration backups under `<DATA_ROOT>/backups`
- tokens, runtime files, logs, databases, and bot data

Runtime effective config is:

```text
bot type default config + instance config.json overrides
```

Dictionaries merge recursively. Scalar values replace defaults. Lists replace defaults. Missing override fields inherit program defaults. The merge returns a fresh dictionary for runtime consumers and never mutates program defaults or user override files.

New instances use an empty override file:

```json
{}
```

They also receive `config.meta.json` at the bot type's current `config_version`. This lets future program defaults flow into fields the user never overrode.

Existing Phase 2/3 full `config.json` files remain compatible. They are treated as explicit user overrides, so values already present in the old file keep their behavior when merged with newer defaults.

## Config Versions and Migrations

Bot type manifests declare:

- `config_schema`
- `config_version`

`config_schema` is validated as a safe program-owned file path. It must be relative, stay inside the program tree, stay outside `DATA_ROOT`, exist, and be a regular file.

Instance config metadata lives beside the user override file:

```text
<DATA_ROOT>/instances/<instance_id>/config.meta.json
```

Current metadata shape:

```json
{
  "schema_version": 1,
  "config_version": 1
}
```

An existing instance without `config.meta.json` is legacy config version `0`. The conservative `0 -> 1` migration validates that `config.json` is a usable JSON object, creates a backup, and atomically writes metadata. It does not rewrite config bytes when no transformation is needed.

Public effective-config loading always ensures the instance config is current before returning runtime values. Legacy `0 -> 1` migration therefore happens automatically on the first effective-config load, including Admin runtime config loads.

Stale configs are never interpreted with newer defaults/schema unless a supported migration path completes first. If no migration path exists, effective config is not returned.

The `0 -> 1` migration is idempotent. After metadata is current, repeated effective-config loads validate the current config but do not create additional migration backups.

If metadata reports a config version newer than the program supports, Manager/runtime code fails clearly and never downgrades automatically.

## Config Backups

Before a config migration marks an existing user config as current, it creates a backup under:

```text
<DATA_ROOT>/backups/instances/<instance_id>/config/<unique-backup-id>/
    config.json
    backup.json
```

The backed-up `config.json` is byte-for-byte identical to the original user config. `backup.json` contains safe metadata only: backup schema version, instance id, source/target config versions, UTC creation time, and the SHA-256 of the backed-up config bytes.

Backups never include token contents, secret files, environment variables, logs, runtime files, databases, or unrelated user data. Backup paths are generated internally and must remain under `app_paths.BACKUPS_DIR`.

Migration writes to config metadata are atomic. A failed migration must leave the original config, token, and user data untouched and must not leave a successful current-version marker behind.

If metadata writing fails after a backup was completed, the valid backup may remain. This is safe user data and is not treated as a successful migration marker.

## Phase 4B Config Editing API

Future frontends must use ConfigStore APIs instead of writing instance `config.json` directly.

The GUI-facing configuration APIs are:

- `get_config_snapshot(instance_id)`
- `load_config_overrides(instance_id)`
- `save_config_overrides(instance_id, overrides)`

`config.json` remains an overrides-only user file. `get_config_snapshot()` returns separate fresh dictionaries for defaults, user overrides, and effective runtime config. Runtime normalization never rewrites user files.

`save_config_overrides()` first ensures the instance config version is current, validates proposed overrides by merging them with bot type defaults, and writes the override file atomically only after validation succeeds. Invalid overrides, stale unsupported config versions, unsafe paths, malformed current config, and write failures leave the previous override bytes intact.

Ordinary config edits do not create migration backups. Backups are created only by supported config migrations before metadata is marked current.

## Phase 5A Minimal GUI

The Windows GUI is a frontend only. It must not host Discord bot execution and must not duplicate process lifecycle logic.

Phase 5A dependencies flow one way:

```text
GUI / Qt frontend
  -> Manager Core
  -> ConfigStore
  -> InstanceStore / BotRegistry
```

Bot children remain separate OS processes owned by `BotProcessManager`. The GUI owns one manager instance for its session and calls `list_instance_info()`, `get_instance_info()`, `start()`, `stop()`, `restart()`, and `shutdown_all()` instead of launching shells or running bot code in the GUI process.

Qt is isolated to the frontend layer. `manager_core.py`, `config_store.py`, `instance_store.py`, `bot_registry.py`, and `Admin.py` remain headless and must not import PySide6.

The GUI uses a lightweight `QTimer` for read-only status refresh. It does not poll Discord and does not implement auto-restart.

Potentially blocking lifecycle operations run through a small Qt worker/thread path so the UI thread is not blocked by `stop()`, `restart()`, or `shutdown_all()`. `start()` uses the same path for consistency. While an action is active for an instance, duplicate actions for that instance are disabled.

The config editor is JSON-based in Phase 5A. It displays user overrides as editable JSON and effective config as read-only JSON. Saves go through `ConfigStore.save_config_overrides()` only; the GUI does not write `config.json` directly and does not read or display token files.

Closing the GUI must not silently orphan managed running children. If managed instances are running, Phase 5A offers an explicit stop-all-and-exit path or cancellation. It does not offer "leave running" until process adoption/persistence exists.

## Phase 6A Local Update Engine

The local update engine owns program code only. It must never install release payloads into `DATA_ROOT` or modify user-owned tokens, config overrides, config metadata, databases, logs, runtime files, instance data, or backups.

Phase 6A uses a prepared local release directory, not network downloads and not archive extraction. A release directory contains:

```text
release.json
<program payload files>
```

The release manifest schema is:

```json
{
  "schema_version": 1,
  "version": "1.0.0",
  "files": [
    {
      "path": "DarkAbyss_Core/example.py",
      "sha256": "<64 hex characters>",
      "size": 123
    }
  ]
}
```

The engine validates manifests strictly: schema version, version string, unique normalized relative paths, exact file sizes, and SHA-256 hashes. Release paths must stay inside the release payload root and the resulting installed version directory. Absolute paths, Windows drive paths, `..` traversal, symlinks that could redirect outside containment, and reserved user-data roots such as `instances`, `secrets`, `runtime`, `logs`, `backups`, `downloads`, `user_data`, root `config`, and database roots are rejected.

The application-owned install layout is:

```text
<PROGRAM_INSTALL_ROOT>/
    versions/
        <version>/
    updates/
        staging/
    current.json
```

`PROGRAM_INSTALL_ROOT` is injectable for tests and future packaging. It is separate from `DATA_ROOT`. The source checkout is not moved or rewritten by Phase 6A.

`PROGRAM_INSTALL_ROOT` and `DATA_ROOT` must be disjoint trees. The updater rejects an install root equal to `DATA_ROOT`, inside `DATA_ROOT`, or containing `DATA_ROOT`. Program update state and user-owned runtime state must never overlap.

Staging copies a fully verified local release into a unique application-owned staging directory under `<PROGRAM_INSTALL_ROOT>/updates/staging/`. The staged bytes are verified before publication. Only after the full copy succeeds does the engine publish the staged directory into `<PROGRAM_INSTALL_ROOT>/versions/<version>/` using a same-filesystem rename. Existing installed versions are not overwritten silently, and incomplete staging is cleaned on handled failure.

Updater-owned structural directories are:

- `<PROGRAM_INSTALL_ROOT>/versions/`
- `<PROGRAM_INSTALL_ROOT>/updates/`
- `<PROGRAM_INSTALL_ROOT>/updates/staging/`

Before creating or writing through any of these paths, the engine validates every existing structural component. Existing structural components must be real directories, must not be symlinks, and must resolve inside the install root. This containment check happens before `mkdir`, temporary staging creation, payload copy, or version publication, so a pre-existing symlinked `updates`, `updates/staging`, or `versions` path cannot redirect writes outside the install root.

Activation changes only `<PROGRAM_INSTALL_ROOT>/current.json`:

```json
{
  "schema_version": 1,
  "version": "1.0.0"
}
```

The pointer write is atomic: temporary file in the same directory, flush/fsync, then `os.replace`. Activation verifies the installed version metadata before updating the pointer. If pointer writing fails, the previous `current.json` remains valid. No mutable `current/` program directory is populated.

Previous version directories are retained for future rollback support. Phase 6A does not implement automatic rollback, network downloading, GitHub integration, packaging, or config/database migration execution. It may install migration code as program files, but it does not run migrations or modify user data during activation.

## Phase 7A GitHub Releases Transport

Phase 7A adds a GUI-independent GitHub Releases transport layer in:

```text
DarkAbyss_Core/github_updates.py
```

GitHub integration is transport only:

```text
GitHub Releases
    -> downloaded ZIP artifact
    -> safe local extraction/preparation
    -> update_engine.inspect_release()
    -> update_engine.stage_release()
    -> explicit update_engine.activate_staged_release()
```

The GitHub layer does not duplicate Phase 6 manifest validation, per-file SHA-256 verification, version publication, or `current.json` activation. The Phase 6 `release.json` remains the authoritative payload manifest, and ZIP extraction success is never treated as equivalent to release verification.

Repository owner/name are explicit API inputs. Public GitHub Releases work without authentication. An optional GitHub token may be supplied by API parameter or `GITHUB_TOKEN` for API requests only; it is never persisted, never placed in URLs, and is not forwarded to unrelated redirected hosts.

Supported lookups:

- latest stable release from GitHub Releases, excluding drafts and prereleases by default
- prereleases only when explicitly allowed
- exact tag lookup

Version normalization is intentionally small: `v1.2.3` maps to `1.2.3`; unrelated tag formats are not silently remapped. The selected GitHub tag version, expected asset name, and extracted `release.json` version must match before staging.

The deterministic release asset name is:

```text
darkabyss-release-<version>.zip
```

The ZIP must contain the Phase 6 release layout directly at archive root:

```text
release.json
DarkAbyss_Core/...
bots/...
```

Archives with an extra parent directory are not accepted. ZIP extraction is explicit and hardened: absolute paths, `..` traversal, Windows drive paths, backslash separators, symlink entries, special files, duplicate normalized paths, and case-colliding paths are rejected. `extractall()` is not used.

Network policy:

- API requests use HTTPS GitHub API endpoints with an explicit `User-Agent` and finite timeout.
- Asset downloads require HTTPS and allow only GitHub/GitHubusercontent asset hosts.
- Redirects to HTTP or unrelated hosts are rejected.
- Authorization is removed before following a redirect to a different host.

Downloads are written to a unique temporary file first under:

```text
<PROGRAM_INSTALL_ROOT>/updates/downloads/
```

Prepared extractions are written under:

```text
<PROGRAM_INSTALL_ROOT>/updates/prepared/
```

These are program/update-owned locations, not `DATA_ROOT`. Existing updater structural directories such as `updates/downloads` and `updates/prepared` must be real directories, must not be symlinks, and must resolve inside the install root before any network artifact is written or extracted. GitHub transport never writes to instances, config, secrets, logs, backups, databases, or other user-owned `DATA_ROOT` paths.

Downloads are bounded and streamed. The configured maximum compressed artifact size is enforced from `Content-Length` when present and again while bytes are streamed. If GitHub asset metadata declares a size, the completed byte count must match it. If GitHub provides a SHA-256 digest, the transport validates it; if no digest exists, the layer relies on Phase 6 per-file SHA-256 verification after extraction.

ZIP preparation has independent resource limits for extracted payload bytes and archive entry count. Before extraction, the transport validates `ZipInfo` metadata: the entry count must be below the configured limit, each declared uncompressed file size must be valid and within the extracted-size limit, and the total declared uncompressed size must fit within the same limit. During extraction, bytes are copied in bounded chunks; the actual bytes written for each file must exactly match its declared `ZipInfo.file_size`, and the running total must not exceed the configured extracted-size limit. Rejected archives do not proceed to Phase 6 staging or activation.

Phase 7A does not add GUI automatic update installation, unattended updates, rollback/recovery, GitHub publishing, PyInstaller packaging, or Discord runtime behavior changes. Activation remains an explicit caller decision through `update_engine.activate_staged_release()`.

## Phase 8A Rollback and Recovery Backend

Phase 8A keeps rollback GUI-independent and local to the already installed program versions managed by `update_engine.py`.

Rollback is a pointer operation only:

```text
<PROGRAM_INSTALL_ROOT>/
    versions/
        1.0.0/
        1.1.0/
    current.json
```

No program files are copied, restored from backups, downloaded, or deleted during rollback. No user-owned data is rolled back or modified. Tokens, config overrides, config metadata, databases, instance data, logs, and backups remain byte-identical across activation, rollback, and recovery.

`current.json` remains schema version 1 and is extended compatibly with optional `previous_version`:

```json
{
  "schema_version": 1,
  "version": "1.1.0",
  "previous_version": "1.0.0"
}
```

Old Phase 6 pointers without `previous_version` remain valid. First activation records `previous_version` as `null`. `get_current_version()` continues to return only the active version, while `get_activation_state()` returns both current and previous pointer fields.

Activation records the former current version atomically in the same `current.json` write. There is no second state file. If pointer writing fails, the previous valid `current.json` remains byte-identical.

Explicit rollback APIs:

- `rollback_to_version(target_version, install_root)`
- `rollback_to_previous(install_root)`

`rollback_to_version()` requires the target version directory to already exist under `versions/`, pass full Phase 6 `inspect_release()` verification, and have a `release.json` version matching the requested target. A rollback from `1.1.0` to `1.0.0` writes `version = 1.0.0` and `previous_version = 1.1.0`, enabling an explicit forward switch later. Rolling back to the already-current version is a no-op and does not rewrite the pointer.

`rollback_to_previous()` never guesses. It reads `previous_version` from the activation state, requires it to be present, and delegates to `rollback_to_version()`.

Explicit recovery API:

```text
recover_current_pointer(target_version, install_root)
```

Recovery is for an unusable `current.json`: missing, malformed, pointing to a missing version, or pointing to a corrupt installed version. The caller must explicitly provide the target version. Recovery verifies the target installed version with the same Phase 6 release checks and atomically writes a fresh `current.json` with no inferred previous version. If the current pointer is already healthy, recovery is rejected as unnecessary.

No Phase 8A operation selects versions by lexical order, directory mtime, or "latest installed" heuristics. Corruption never triggers automatic repair or network download.

Health inspection is non-mutating:

```text
check_install_health(install_root)
```

Health states:

- `HEALTHY`: pointer parses, current version exists, release verification passes, and release version matches pointer.
- `NO_CURRENT_POINTER`: `current.json` is absent.
- `INVALID_CURRENT_POINTER`: `current.json` is malformed, unsupported, non-file, symlinked, or contains invalid version fields.
- `CURRENT_VERSION_MISSING`: pointer is valid but the selected version directory is absent.
- `CURRENT_VERSION_CORRUPT`: selected version exists but fails release manifest, size, hash, or version checks.

Rollback and recovery preserve Phase 6 path safety: `versions/` is a real structural directory, `current.json` symlinks are rejected, version directories remain inside the install root, and `PROGRAM_INSTALL_ROOT` stays disjoint from `DATA_ROOT`.

Phase 8A still does not delete old versions, prune versions, add automatic unattended rollback, add GUI updater controls, add PyInstaller packaging, or change Discord bot behavior.

## Phase 9A Windows Packaged Runtime Foundation

Phase 9A introduces the first packaged runtime shape without changing Discord bot behavior or adding CI automation.

The target Windows distribution layout is:

```text
DarkAbyssBotManager/
    Launcher.exe
    current.json
    versions/
        0.9.0/
            DarkAbyssApp.exe
            release.json
            _internal/
                bots/
                DarkAbyss_Core/defaults/
                PyInstaller support files
```

`Launcher.exe` is stable bootstrap code. It reads `current.json`, delegates pointer and installed-version validation to the existing update engine health checks, resolves `versions/<version>/DarkAbyssApp.exe`, and starts it as `DarkAbyssApp.exe --manager`. It does not download updates, mutate user data, repair unhealthy pointers, pick a random installed version, or use shell execution. If the pointer is missing, malformed, symlinked, points to a missing version, or points to a corrupt version, startup fails clearly.

`DarkAbyssApp.exe` is the versioned application. It has explicit modes:

```text
DarkAbyssApp.exe --manager
DarkAbyssApp.exe --bot-runner admin --instance admin-main
```

Manager GUI mode starts `manager_gui.main()`. Bot-runner mode calls the selected bot runtime in the current process and does not spawn another child from inside the runner. Each managed bot instance still remains a separate OS process because Manager Core launches a separate `DarkAbyssApp.exe --bot-runner ...` process per instance.

Source mode remains unchanged:

```text
python DarkAbyss_Core/manager_gui.py
python DarkAbyss_Core/Admin.py --instance admin-main
```

Manager Core now has two explicit launch-spec strategies:

- Source strategy: `sys.executable <bot_type.entrypoint> --instance <instance_id>`.
- Packaged strategy: `<version_dir>/DarkAbyssApp.exe --bot-runner <bot_type> --instance <instance_id>`.

Both strategies produce argument lists only. Manager Core keeps enforcing `shell=False`, instance log ownership, and `DARKABYSS_DATA_DIR = app_paths.DATA_ROOT.resolve()` for child processes. Tokens are still read from user-data files and are never passed on the command line or embedded into environment variables.

Program resource resolution is centralized in `runtime_layout.py`. Source checkouts resolve resources from the repository root. Frozen PyInstaller runtimes resolve program-owned resources from the bundle resource root. `app_paths.DATA_ROOT` remains controlled by `DARKABYSS_DATA_DIR` or OS user-data defaults and must not become the PyInstaller temporary directory or the versioned program directory.

The versioned application uses PyInstaller `--onedir` through `packaging/DarkAbyssApp.spec`. The version directory is already the atomic deployment unit, so onedir avoids onefile extraction semantics, improves startup, keeps resources inspectable, and fits rollback by pointer switch. `Launcher.exe` is a small separate bootstrap built from `packaging/Launcher.spec`.

Developer builds require an explicit version:

```text
build_windows.bat 0.9.0
```

The build script first creates raw PyInstaller output under `dist/DarkAbyssApp/` and `dist/Launcher.exe`, then `packaging/assemble_distribution.py` assembles the bootable tree:

```text
dist/DarkAbyssBotManager/
    Launcher.exe
    current.json
    versions/
        0.9.0/
            DarkAbyssApp.exe
            release.json
            _internal/...
```

The assembler validates the requested version, copies only the versioned app bundle under `versions/<version>/`, copies the stable launcher only to the distribution root, generates and verifies the version `release.json`, writes the initial pointer with `previous_version = null`, and validates that launcher resolution selects exactly the assembled app.

`release_manifest.py` generates deterministic `release.json` files from packaged version directories. It writes normalized relative paths, SHA-256 hashes, byte sizes, and stable ordering; rejects symlinks; excludes `release.json` itself; and rejects reserved user-data roots such as `secrets`, `instances`, `logs`, `backups`, `updates`, and databases. The generated manifest must pass `update_engine.inspect_release()`.

The first distributable archive should be pre-bootstrapped with a valid `current.json` and one installed version directory. A clean user machine should not need system Python, pip, setup.bat, or a source checkout to double-click `Launcher.exe` and open the Manager GUI.

Current limitation: token provisioning remains manual. The packaged Manager can initialize user-data directories and bot instances, but users still place their Discord token into the generated instance token file outside the program directory.

Phase 9A did not add GitHub Actions, automatic GUI update controls, automatic rollback policy, PyInstaller release publishing, or changes to Discord command behavior.

## Phase 10A GitHub Actions Release Pipeline

Phase 10A automates production Windows release artifacts without changing the packaged runtime architecture. A release tag is authoritative: `v1.2.3` maps exactly to version `1.2.3`, and malformed or mismatched tag/version/manifest values fail before publishing.

The Windows workflow lives at:

```text
.github/workflows/release.yml
```

The workflow has two jobs. `build-and-verify` runs on `windows-latest`, uses `permissions: contents: read`, sets up Python with `actions/setup-python@v5` before running any Python helper, installs `requirements-build.txt`, compiles core/build helper modules, runs the full unittest suite, executes `build_windows.bat <version>`, creates release artifacts, verifies both artifacts locally, performs artifact hygiene checks, and uploads only the four final release files as a workflow artifact.

`publish-release` depends on `build-and-verify`, uses `permissions: contents: write`, downloads the verified workflow artifact, and publishes the four assets without rebuilding. Its condition is limited to actual tag pushes where `github.event_name == 'push'`, `github.ref_type == 'tag'`, and `github.ref` starts with `refs/tags/v`. `workflow_dispatch` can build and verify artifacts for inspection, but it cannot publish a GitHub Release or synthesize a production release tag.

GitHub-controlled values such as `github.ref_type`, `github.ref_name`, and `inputs.version` are passed into the version-resolution step through environment variables. The PowerShell step reads those variables, and Python validation reads tag/version from environment variables rather than constructing executable source text from tag contents.

There are two distinct ZIP formats:

```text
darkabyss-release-<version>.zip
DarkAbyssBotManager-<version>-windows.zip
```

`darkabyss-release-<version>.zip` is the updater artifact consumed by Phase 7. Its archive root is the version payload directly:

```text
release.json
DarkAbyssApp.exe
_internal/...
```

It must not include `Launcher.exe` at archive root, `current.json`, a `versions/<version>/` wrapper, user-data roots, source-adjacent runtime token/config files, `.git`, logs, backups, databases, generated instances, or symlinks. Artifact construction is allowlist-oriented: the update ZIP contains exactly `release.json` plus files listed by that manifest. The helper validates the ZIP by feeding it through `github_updates.prepare_downloaded_release()` without network and then through `update_engine.stage_release()` in a temporary install root.

The update ZIP must also remain compatible with the Phase 7 default artifact download limit. Build and verify-only paths reject an update ZIP whose compressed size is greater than `github_updates.DEFAULT_MAX_ARTIFACT_BYTES`.

`DarkAbyssBotManager-<version>-windows.zip` is the fresh-install artifact. Its archive root contains a single `DarkAbyssBotManager/` directory:

```text
DarkAbyssBotManager/
    Launcher.exe
    current.json
    versions/<version>/
        DarkAbyssApp.exe
        release.json
        _internal/...
```

The fresh-install ZIP is validated by extracting to a temporary root and resolving `Launcher.exe -> current.json -> versions/<version>/DarkAbyssApp.exe --manager` through `launcher.resolve_current_app()`.

Fresh-install artifact construction is also allowlist-oriented. It contains exactly stable root files `Launcher.exe` and `current.json`, `versions/<version>/release.json`, and the manifest-listed payload files under `versions/<version>/`. The assembled distribution is rejected if it contains a second version directory, arbitrary root files, unmanifested version files, nested `.git` content, source runtime token files, user configs, databases, logs, backups, generated instances, or other user-data roots.

Release artifact creation is implemented in `packaging/build_release_artifacts.py` so CI remains a thin orchestration layer. ZIP member ordering is deterministic, member names use `/`, inputs containing symlinks are rejected, and checksum files are generated in the conventional `<sha256>  <filename>` format. The helper rejects artifact output paths that equal, contain, or sit inside the assembled distribution before any output cleanup can run. Fresh-install verification validates member names, duplicate/case collisions, symlink/special-file metadata, and root containment before extracting entries one by one; it does not use `extractall()` in the verification path. After safe extraction, the same exact distribution validator runs before launcher resolution, so verify-only rejects extra fresh-install files too.

A local developer can reproduce CI artifact assembly after a successful Windows build with:

```bat
python packaging\build_release_artifacts.py --version 0.9.0 --tag v0.9.0 --distribution dist\DarkAbyssBotManager --output dist\release-artifacts
```

Phase 10A does not add GUI updater controls, automatic update checks, release publishing outside GitHub Actions, Alehandro bot migration, rollback policy changes, or Discord runtime behavior changes.

## Future Phases

Future phases may add:

- GUI updater controls
- packaged release publishing
- token provisioning UX
- Windows service/systemd/Docker


## Guided Manager Setup

The Manager GUI provides a `Setup Bot` wizard for normal onboarding. A user can configure an Admin bot without opening README or manually editing files:

1. choose a local Manager display name;
2. open Discord Developer Portal;
3. enter the public Discord Application ID;
4. paste the Discord bot token;
5. acknowledge the external Server Members Intent toggle;
6. configure structured access settings (`allow_server_administrators`, `allowed_user_ids`, `allowed_role_ids`, `audit_channel_id`);
7. generate/copy/open the OAuth2 invite URL;
   The generated URL targets Guild Install with `integration_type=0`. Both private and shareable Discord application modes are supported: `Public Bot = OFF` limits installation to the owner/developer team and may require `Installation -> Install Link = None`; `Public Bot = ON` lets another authorized server owner use the Manager-generated invite link. Public Bot does not change DarkAbyss access control, commands, whitelist, token handling, or runtime behavior.
8. review a Ready checklist and optionally `Save && Start Bot`.

The wizard stores only user-owned instance data. The Discord bot token is written atomically to `instances/<instance_id>/secrets/token.txt`, is masked by default, is never displayed back after saving, and is not included in invite URLs.

Invite links are generated from named Discord permission bits for the documented granular permissions: View Channels, Send Messages, Read Message History, Manage Messages, Manage Channels, Manage Roles, Moderate Members, Kick Members, and Ban Members. The GUI does not request Discord Administrator permission.

Discord-side actions such as enabling Server Members Intent, choosing private or shareable Public Bot installation access, setting Installation -> Install Link to None when needed, and authorizing the invite are represented as user acknowledgements. Manager does not claim to verify those external Discord settings locally.

Raw JSON editing remains available as `Advanced JSON...` for developer/advanced overrides, but the normal Admin bot setup flow does not require it.
