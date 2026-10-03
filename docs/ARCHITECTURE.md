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

## Bot Types and Instances at a Glance

Every bot is an independent instance with its own Discord application/token,
process, config, data and logs. AI access is configured with shared provider
**connections** (provider + API key + model): a **base set** that every bot uses
by default, and an optional **own choice** per bot. Provider adapters,
orchestrator, safety checks and the Game Presence domain are shared code.

```text
Manager
├── AI connections (shared)            config/ai_connections.json + secrets/ai_connections/<provider>/<id>.secret
│   ├── Groq main     (GPT-OSS 120B)   ← base set: planning
│   ├── Gemini main   (3.8 Flash)      ← base set: execution
│   └── Groq backup   (GPT-OSS 120B)
├── Admin Bot instance (bot type "admin", Admin.py)
│   ├── Discord Token A                instances/<admin>/secrets/token.txt
│   ├── AI source: base set            instances/<admin>/data/ai_selection.json (absent = base set)
│   └── AI usage A                     instances/<admin>/data/ai_usage.json
├── Game Presence Bot instance (bot type "game_presence", GamePresence.py)
│   ├── Discord Token B                instances/<gp>/secrets/token.txt
│   ├── AI source: own → Groq backup   instances/<gp>/data/ai_selection.json
│   └── AI usage B                     instances/<gp>/data/ai_usage.json
└── Stream Director Bot instance (bot type "stream_director", StreamDirector.py) — no AI
    ├── Discord Token C                instances/<sd>/secrets/token.txt
    ├── Twitch OAuth tokens            instances/<sd>/secrets/twitch_oauth.json
    └── Sessions, community, inbox     instances/<sd>/data/stream_director_state.json
```

Both run at the same time as separate processes (`--bot-runner admin` /
`--bot-runner game_presence` in the packaged app, the manifest entrypoint in
source mode). Bots on the same connection share its key and provider limits;
a bot with its own connection (own key) has its own limits.

## Bot Type

A Bot Type is program-owned. It describes an available bot implementation and lives in the application/release tree.

Current manifests:

```text
bots/admin/manifest.json            Admin Bot (DarkAbyss_Core/Admin.py)
bots/game_presence/manifest.json    Game Presence Bot (DarkAbyss_Core/GamePresence.py)
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
        admin_bot.lock | game_presence_bot.lock
        bot_status.json, game_presence_status.json, ai_terminal/ ...
    logs/
    data/
        ai_selection.json       this bot's AI source: base set (default, file absent) or own connections
        ai_usage.json           this bot's provider-reported AI usage
        admin_features.json     Admin bot features | game_presence_state.json (Game Presence)
```

AI connections and their keys are shared by all bots (see the Manager `AI Providers` description below):

```text
<DATA_ROOT>/config/ai_connections.json
<DATA_ROOT>/secrets/ai_connections/<provider>/<connection id>.secret
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

The Manager `AI Providers` page manages connections, the base set and each bot's AI source:

- **Connections** (`ai_connections.ConnectionStore`, `config/ai_connections.json`): one provider account each — provider, name, model, options (reasoning). Several connections may use the same provider (e.g. "Groq main" and "Groq backup" with different keys). `Add Connection` / `Edit` open `ConnectionDialog`; the connection ID (e.g. `groq-1`) is both the profile ID and the credential reference.
- **Keys**: one per connection in `secrets/ai_connections/<provider>/<connection id>.secret`. A saved key is never re-displayed; a blank key field keeps it; removing a connection removes its key and is refused while the base set or a bot's own choice uses it. The connections file contains no key.
- **Base set**: planning, execution, an optional extra fallback and "if one fails the other takes over" (cross fallback). Every bot without its own choice uses it. The first connection saved into an empty setup becomes the base set's execution connection.
- **AI of a bot**: "Use the base set" (default; `ai_selection.json` absent) or "Choose connections for this bot" (own planning/execution/fallback). Switching back to the base set keeps the stored own route. A Game Presence bot uses only the execution connection, for wording.
- `Test Connection` runs outside the GUI thread with that connection's key; it is a real API request and is recorded as Manager usage of the connection. Result texts are sanitized (provider messages are never shown).
- Changes apply to running bots on their next AI request (the orchestrator re-reads the settings every request). The periodic Manager refresh never resets choices that are being edited but not saved yet.

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
- Message Content Intent is optional: `Admin.main()` requests it before the gateway connection only when `ai_control_channel_id` is set; otherwise the bot connects exactly as before, so `/execute`, `/ai`, and Manager never depend on the privileged intent. If Discord refuses a privileged intent while the channel is enabled, startup prints an actionable message instead of a traceback; because Discord does not say which intent was refused, it names both required privileged intents (Server Members Intent, always required, and Message Content Intent, required only for the control channel) and explains that clearing the channel ID removes only the Message Content requirement. Enabling/disabling the channel requires a bot restart.
- Message filter: a message becomes an AI request only if it is in a guild, in exactly the configured channel (gate fixed at startup and re-checked against the reloaded config), from a real guild member who is not a bot (including Kairo itself) and not a webhook, with non-empty text, and the author currently passes the explicit AI whitelist (`ai_allowed_user_ids` / `ai_allowed_role_ids`; Administrator and the `/execute` lists never grant access). Everything else is ignored without loading providers, calling a provider, or replying. Attachments are not interpreted.
- Natural messages always use `TaskClass.ROUTINE` (explicit modes stay available through `/ai mode:`) and are stateless: each Discord message is one independent orchestrator request containing only that message; there is no cross-message memory.
- Delivery: `/ai` stays ephemeral; control-channel conversations are public in that channel (the first reply references the user's message with `mention_author=False`). Every public message uses `AllowedMentions.none()`. A pending confirmation stores its delivery mode, so continuations after Approve/Cancel stay ephemeral for `/ai` and public for the control channel. Public confirmation buttons keep the user+guild+channel ownership checks: other users get an ephemeral denial and the confirmation is not consumed; execution-time authorization is rechecked before every tool call, tools still run with `source="/ai"`, `suppress_mentions=True`, and the existing audit path.
- Engine indicator: AI answers and confirmation controls end with a small `-# Provider · profile · model` line (plus `fallback` when used), built only from the public `OrchestratorResult` fields `provider_id`, `profile_id`, `model_id`, `fallback_used`; it is omitted when absent and never shows credential references, attempt errors, or provider payloads.


AI-6 expands the Discord-only Admin Tools and splits every AI request into a planning call and an execution call.

- Scope: everything stays Discord-only. Tools have no filesystem, process, shell or general network access; images (server icon/banner, emojis, stickers) come only from files attached to the request (`/ai file:`/`file2:` or control-channel message attachments, at most 5), checked for type, size and image signature and read from Discord. Webhook tokens/URLs are never returned to the model or shown.
- Tool modules: `admin_tools.py` keeps the registry, the validator, the legacy tools and the shared guards; `admin_tools_server.py` (channels/categories, permission overwrites, roles, members, moderation, server settings, welcome screen, onboarding, AutoMod), `admin_tools_content.py` (embeds, message edit/delete/pin/reactions, polls, publishing, threads/forums, webhooks, invites, emojis/stickers, scheduled events), `admin_blueprint.py` and `admin_features.py` return `(ToolDefinition, handler)` pairs from `build_tools(core)`; `admin_tools` imports them fresh by name and registers them (`register_tool`), so they are listed as PyInstaller hidden imports. Every tool has a capability `category`; `render_tool_catalog()` lists names and one-line purposes only.
- Validation: the local validator now supports the JSON-schema subset the tools use (type unions, enum, string length/pattern, numeric bounds, arrays with item schemas/min/max/unique, nested objects with required fields and `additionalProperties: false`). Gemini receives tool schemas through `parametersJsonSchema` (the OpenAPI-subset `parameters` field rejects `additionalProperties` and type unions).
- `/execute` keeps a fixed 17-action legacy set (`EXECUTE_TOOL_NAMES`, Discord allows at most 25 choices); legacy tools gained optional arguments (category/topic/slowmode/private roles for channel creation, color/hoist/mentionable/permissions for roles, purge filters, ban message deletion) that `/execute` does not send.
- Anti-escalation (AI requests: `AdminToolContext.enforce_hierarchy=True`): roles at or above the requester's highest role cannot be assigned, edited, moved or deleted; members at or above it (and the server owner and the bot) cannot be moderated; only permissions the requester holds can be granted (the server owner is exempt from these requester checks). Administrator is never granted by any tool. Roles that members obtain by themselves (role menus, verification, welcome auto roles, onboarding options) must not carry moderator/admin permissions; this is re-checked on every button click and join. Bot role hierarchy is checked for everyone.
- Server blueprint: `check_server_blueprint` (read) validates a JSON description of roles, categories, channels and overwrites (by role name) and reports create-vs-reuse; `apply_server_blueprint` creates only missing objects (never edits/deletes existing ones) after one confirmation and records the created IDs; `undo_last_blueprint` deletes exactly those. The confirmation shows a readable outline in addition to the exact JSON pages.
- Persistent features (no AI at runtime): `FeatureStore` is a JSON file `admin_features.json` in the instance `data` folder (atomic writes; an unreadable file is renamed aside). Role button menus and verification buttons (`dab:rm:<menu>:<role>` custom IDs handled by an `on_interaction` listener), welcome message + auto roles (`on_member_join`, mentions limited to the new member), repeating scheduled messages (60 s `tasks.loop`, minimum interval 10 minutes, mentions suppressed), server lockdown state and the blueprint undo record. AI tools only configure these through the normal confirmation path.
- Two-stage AI (production transport `planning=True`): the PLANNER-routed profile receives the agent planning instruction plus the compact tool catalog and NO tool schemas (`allowed_tool_names=()`), and returns JSON (`answer` or `act` with tools and steps). An `answer` is delivered directly; for `act` the executor (task class from `/ai mode:`, default ROUTINE) receives the executor instruction with the plan and only the planned tools plus core read tools. If no planner profile is configured planning uses ROUTINE; an unusable plan falls back to keyword tool routing. This keeps each provider request small (Groq free tier: 8K tokens/minute) and lets a stronger model plan while a cheaper one executes. All writes still go through the AI-4 confirmation path. Manager "AI Providers" has a Routing tab (planning engine, execution engine, optional cross-provider fallback).
- Bot invite permissions (still no Administrator) now include the permissions these tools and the roles/overwrites they create need (Manage Server, Manage Webhooks, Manage Expressions, Manage Events, Manage Threads, View Audit Log, Move/Mute/Deafen Members, Manage Nicknames, Create Invite, Mention Everyone, voice and thread permissions, Send Polls, Pin Messages). Already invited bots need their role permissions updated or a re-invite.

AI-6.1/6.2 harden the runtime and make the agent usable for multi-step work:

- Fixes: the single-instance lock always locks byte 0 (the `a+b` handle starts at EOF, so two processes used to lock different bytes and the second crashed with `PermissionError`); saving a provider in Manager fills only unset routing slots (saving only Gemini left routing empty); role-menu clicks are acknowledged before the role change.
- Providers: 60 s timeout. `retry_delays` (off by default, so Manager Test Connection fails fast) retries 429/5xx/network failures and Groq `tool_use_failed`; the provider's own hint ("try again in Xs", `RetryInfo.retryDelay`) is honoured up to 30 s, longer waits are not retried. `build_default_provider_registry()` enables `DEFAULT_RETRY_DELAYS` for bot requests.
- Error recovery: `OrchestratorRequest.recover_errors=True` (the Discord transport sets it) returns per-call validation errors and failed tool results to the model as TOOL messages instead of ending the run, at most `MAX_RECOVERED_ERRORS` (3) times per orchestration (carried across confirmation pauses like the round counter). Nothing from a batch with an invalid call executes; after a failed call the rest of that batch is skipped and reported as skipped; later writes need confirmation again under the fixed policy. Executor crashes and results flagged `data={"fatal": True}` (revoked AI authorization) always end the run. Without the flag the strict AI-3 contract is unchanged. `MAX_TOOL_ROUNDS` is 15; `MAX_TOOL_CALLS_PER_ROUND` stays 8 because the exact confirmation preview is bounded by it.
- Confirmation modes (instance config `ai_confirmation_mode`, default `plan`; a missing/unknown value in the transport means `strict`): in `plan` mode a planner plan that uses write tools is shown once (steps, allowed write tools, which destructive tools will still ask) with Approve plan/Cancel bound to the requester, guild and channel with fresh authorization checks. After approval the executor runs with only the plan's tools (plus core reads) and `ConfirmationPolicy(confirm_normal=False)`: NORMAL actions run, DESTRUCTIVE batches still get the exact AI-4 preview and confirmation. Read-only plans run directly; requests without a usable plan use `strict`. `/execute` now also sets `enforce_hierarchy=True`.
- Request context (`ai_context.py`): requester (owner/admin/highest role), current channel (so "here" works), bot top role, server size and a bounded channel tree and role list - names only for the planner (1600 chars), with IDs for the executor (3200 chars). Names are flattened to one printable line, backticks replaced, and labelled as user-written data.
- Memory (`ai_memory.py`): per (guild, channel, user), RAM only, last 6 exchanges, 3000 chars, 30 min TTL, at most 500 conversations; both stages receive it as prior USER/ASSISTANT messages. The final answer plus action outcomes is recorded when a request ends (pending confirmations carry the key). `/ai_reset` or `reset`/`сброс` in the control channel clears it. This replaces the AI-5 "every message is independent" rule.
- Engine switching (AI-6.3/6.4): the Discord transport sends `OrchestratorRequest(auto_fallback=False)` so routing fallbacks never switch engines silently. When the planning or execution engine fails (rate limit, outage), the transport asks `AIOrchestrator.alternative_profile(task_class, exclude)` (local checks only) and shows the failed engine, its sanitized reason (provider adapter error texts are kept; other exceptions stay type-name only), the actions already executed, and a "Continue with <engine>" / Cancel prompt bound to the requester. Continuing re-plans with the chosen engine (`manual_profile_id`) or restarts the executor on it with the same plan, the same confirmation policy and a system note listing the actions that already ran so they are not repeated. Failed engines are not offered again within the request. With no alternative the reply includes the reason. `auto_fallback=True` callers keep automatic fallback, which now also applies after read-only tools (they have no side effects). The bot exits with an actionable message (no traceback dialog) when Discord rejects the token.
- Manager AI terminal (`admin_terminal.py`, `manager_terminal.py`): a file mailbox in the instance `runtime` folder (`ai_terminal/requests`, `ai_terminal/decisions`, `ai_terminal/events/<id>.jsonl`, `bot_status.json`); the bot polls it every 0.5 s (`terminal_loop`) and opens no port. Requests run through `AITransport.handle_manager_request` as the local operator (binding user ID 0, only for `ManagerInteraction` objects, which Discord can never produce): no AI whitelist and no requester hierarchy, bot hierarchy and every plan/destructive/engine-switch confirmation still apply. `ManagerInteraction` imitates the interaction API, so views become `approval` events and Manager clicks become decision files dispatched by `dispatch_view_decision`. Stale requests (>5 min) are refused; event files are purged after 24 h. `bot_status.json` lists servers and channels for the Manager pickers (refreshed on ready and every 30 s).
- @mention requests: `ai_mention_enabled` + `ai_mention_channel_ids` (empty = every channel). A message pinging the bot user or its managed role (`guild.self_role`) starts the same public flow as the control channel (mention text stripped, buttons owner-only); non-whitelisted members get one refusal per 60 s. Requests the Message Content Intent (role pings carry no text without it).
- Game Presence (separate bot type `game_presence` with its own Discord application, token and process; see "Game Presence Bot" below): `game_presence.py` is the pure domain (no discord import): `GameNormalizer` (trim/casefold key + display name, alias hook), `ActivityTracker` (current game + voice per member, index guild+game -> users, so selection is never O(n^2)), `SessionTracker` (session start per member+game, 120 s restart grace, Discord activity start used as hint), `CandidateSelector` (groups of >= 2 per guild+game, allow/ignore lists), pending candidates (created on first sight, re-checked against the CURRENT state after the delay, then re-armed; a group that falls apart is cancelled silently), replaceable `SuggestionPolicy` (default: >= 2 players settled for the delay, guild cooldown, opt-out + per-user cooldown filter the mention targets, all eligible in one voice -> nothing, group cooldown by >= 2-member overlap, optional voice-aware "join" suggestion, max 10 mentions), `PreferenceBook` and `CooldownLedger` over a `PresenceStore` (backed by the Game Presence instance's own `data/game_presence_state.json`, key `game_presence`, scoped per guild + user; the Admin bot's `admin_features.json` is not used). `game_presence_discord.py` reads `Member.activities` (Playing) and voice state, renders deterministic templates in the instance language, builds mentions only from approved IDs and sends with `AllowedMentions(users=targets, everyone=False, roles=False)`; persistent `dab:gp:mute`/`dab:gp:allow` buttons change only `interaction.user`'s state (ephemeral replies, idempotent); the optional AI rewrite may only rephrase a template with fixed placeholders and is validated by the runtime (fallback to the template on any failure). The settings are the Game Presence instance's top-level config (validated fail-closed by `normalize_bot_config`, which only differs from `normalize_config_dict` in treating "on but no server/channel yet" as "not configured"); live settings reload every 15 s. The bot writes `runtime/game_presence_status.json` for the Manager "Game Presence" page (settings, status, required intents, Save & Restart through the normal lifecycle path). Session state is rebuilt from presence after a restart; cooldowns/history persist, and pending delays always restart, so a restart never re-posts. Future large-server work: sampling, per-pool policies, sharding the tracker, batching status.
- Manager bot groups (`manager_groups.py`): Manager-only tabs stored in `config/manager_groups.json`; bots without an assignment are in "Main".
- Message text: `ai_read_message_content` (default false) requests the Message Content Intent without a control channel. When the intent is off, `AdminToolContext.message_content=False`: text/link purge filters refuse with an actionable message instead of silently matching nothing, `get_recent_messages` adds a `content_note`, and the executor instruction says message text is unreadable.

AI credentials are device-local, stored outside program versions, instance `config.json` files, release artifacts and the source tree:

```text
<DATA_ROOT>/config/ai_connections.json                         connections + base set (no keys)
<DATA_ROOT>/secrets/ai_connections/<provider>/<connection>.secret
<DATA_ROOT>/instances/<instance_id>/data/ai_selection.json     the bot's choice (absent = base set)
```

`ai_storage.for_instance()` builds the stores of one bot: a read-only `ai_connections.BotSettingsView` that contains ONLY the connections of that bot's route (base set or own choice), the shared connection `CredentialStore`, and the bot's own usage store. Because the view never contains other connections, the orchestrator cannot fall back to a connection the bot was not given (not even through engine switching). The instance ID is re-validated and the bot's own files must resolve inside `instances/<instance_id>`; connection IDs are `^[a-z0-9][a-z0-9_-]{0,63}$` and `CredentialStore.path_for()` validates provider/ref components and containment. An unreadable connections file or `ai_selection.json` fails closed (AI unavailable, file preserved, Manager shows the problem); a route that names a deleted connection simply loses it.

Adding a provider later: write an adapter module with the contract described in `ai_providers.py` (constructor `(credential_store, *, usage_recorder=None, retry_delays=())`, `metadata` with models, `get_local_availability`, async `test_connection(credential_ref)` and `generate(request, credential_ref)`, recording one `ai_usage.UsageEvent` per received response), add one `ProviderSpec` to `ai_providers.PROVIDERS` and list the module in the PyInstaller hidden imports. The Manager connection editor, model lists, logos, the bots' provider registry (`build_default_provider_registry`) and the usage display all read the catalog. Only Groq and Gemini exist today.

AI usage (`ai_usage.py`): every provider HTTP response a bot actually received is recorded in that bot's `instances/<id>/data/ai_usage.json` (Manager Test Connection calls in `config/ai_usage_manager.json`), aggregated per provider / connection / model / local day (requests, failed requests = non-2xx, input, output, thinking and total tokens; 31 days kept) plus the latest rate-limit snapshot per provider/connection/model (limits belong to a key, so the connection is part of the key; version 1 files without it are read as connection `<provider>-default`). The adapters record inside their retry loop, so every attempt counts: retries, intermediate and final 429/5xx answers, Groq `tool_use_failed` re-samples and Manager Test Connection calls (the Manager passes the selected bot's recorder to the providers it creates). A network failure without any response is not counted. Only provider-reported numbers are used: Groq `usage` (prompt/completion/total tokens) and its `x-ratelimit-*` headers (remaining/limit/reset of tokens per minute and requests per day, taken from every response that carries them, intermediate 429s included); Gemini `usageMetadata` (prompt, candidates, thoughts, total). Tokens of a failed response are counted only if the provider reported them; nothing is estimated, no percentages or account quota are invented, and no prompt, response or key is stored. Several processes may write the same file, so writes are serialized with an inter-process lock file (`.ai_usage.json.lock`) and replaced atomically. The orchestrator passes `InstanceAIStores.usage` through `build_default_provider_registry(credentials, usage_store=...)`; recording runs off the event loop and a recorder error never affects a request. The Manager shows, per connection, today's total of every bot plus Manager tests (AI page) and the selected bot's own usage of its connections (dashboard), with the freshest rate-limit snapshot of that key across all bots, e.g. `GPT-OSS 120B · 18 req (2 failed) · 41k tok` / `34k in / 6.8k out · TPM 6.1k left`, or `No usage yet`; a remaining value is shown only while its reported reset window is still open. An unreadable usage file is shown as "Usage data unreadable" and set aside on the next write.

Migrations (`ai_storage.run_migrations()`, at Manager and Admin start; copy only, idempotent, nothing deleted, no secret in any message or marker):

1. Very first global store (`config/ai.json`, `secrets/ai/...`) -> the single Admin instance's old per-bot files (ambiguous with several Admin bots: nothing copied; corrupted: stopped without a marker; marker `config/ai.migrated.json`).
2. Old per-bot files (`instances/<id>/data/ai.json` + `instances/<id>/secrets/ai/...`) -> connections. If exactly one bot has keys and the base set is empty, its keys become the base set ("Groq main", "Gemini main"; routing incl. the "try the other one" fallback is kept) and every bot uses it by default. Otherwise each bot's keys become its own connections ("Groq (<bot name>)") and the bot gets them as its own choice. An existing base set, an existing connection key or a choice already saved in the Manager is never overwritten; unreadable per-bot settings are skipped and retried; converted bots are listed in `migrated_instances`.

`CredentialReference` is only a logical pointer: the connection ID, resolved to a local secret on each computer. AI profiles and routing preferences are device-local and do not belong to the Discord Bot Instance's portable config. This allows PC A and PC B to use different providers/profiles while hosting the same logical bot at different times. Cloud sync is not implemented.

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

## Game Presence Bot

Bot type `game_presence` (`bots/game_presence/manifest.json`, entrypoint `DarkAbyss_Core/GamePresence.py`, defaults `DarkAbyss_Core/defaults/game_presence_config.json`, schema with `additionalProperties: false`). It is created like any bot (Manager -> Bots -> Add Bot -> Game Presence Bot) and needs its own Discord application and token; the Manager never creates one automatically.

- Process: `discord.Client` with intents guilds, members, presences and voice_states only (no Message Content). `PrivilegedIntentsRequired` exits with a message naming Presence Intent + Server Members Intent (also written to the status file); `LoginFailure` exits with an actionable message; no traceback dialog.
- Data: `data/game_presence_state.json` (opt-outs, history, cooldowns per guild) through `PresenceStateStore`, a FeatureStore that fails closed: an unreadable file pauses posting and buttons instead of resetting everyone to "allowed", and is never rewritten. Diagnostics: `GamePresenceBot.state_problem()` is checked at start (message in the log), on ready and before every tick; the reason is written to `game_presence_status.json` (`GamePresenceRuntime.status()` still reports when the engine state cannot be read). The Manager page also checks the file read-only, so "State file problem" is shown even while the bot is stopped. Runtime: own lock, `bot_status.json` (servers/channels for the Manager pickers) and `game_presence_status.json`.
- Diagnosis: every tick stores one reason in `game_presence_status.json` (`diagnosis` = code + text) without changing any decision. The engine reports `no_activity`, `single_player`, `game_filtered`, `waiting_delay`, `suggesting` and the policy's "no" reasons (`DefaultSuggestionPolicy.decide()`, which `evaluate()` delegates to: `settling`, `guild_cooldown`, `user_muted`, `user_cooldown`, `same_voice`, `group_cooldown`); the runtime overrides them with `not_configured`, `paused`, `config_problem`, `state_problem`, `intent_missing`, `guild_unavailable`, `channel_unavailable`, `permission_denied` (checked with `channel.permissions_for(guild.me)` BEFORE posting, so no suggestion is consumed while it cannot be sent; a 403 on send is reported the same way), `send_failed` and `posted`. Problems and posts are logged once per change. The status also carries `visible_members` / `online_members` / `playing_members` / `other_activities` (non-bot members in the cache, how many are online, how many show a Playing activity, and the other activity types such as custom status) so "no member data" (Server Members Intent), "everyone offline" (Presence Intent) and "online but nobody shares a game" (Discord activity privacy / game detection on the members' side) look different; `no_activity` names the online count. Bot processes line-buffer stdout/stderr (`runtime_layout.line_buffered_output`), so these lines reach `logs/process.stdout.log` while the bot runs. The Manager page shows the reason as "Now: ...", the dashboard as "Game Presence: ...".
- Server/channel choice: until a server and a channel are saved, the Manager page shows "— choose a server —" / "— choose a channel —" instead of pre-selecting the first ones (pre-selection made an unconfigured, online bot look configured), and the status says "Not configured — nothing is posted".
- Buttons: persistent `dab:gp:mute` / `dab:gp:allow`, handled by this bot's `on_interaction` against its own state; only `interaction.user` changes, replies are ephemeral.
- AI: optional wording only, through `InstanceAI` (this instance's `ai_storage` stores) and `TextOnlyAI`, which strips every tool from the request and never passes a tool executor, so Admin tools are unreachable.
- Admin separation: `Admin.py` has no Game Presence import, listener, loop, intent or status. An old Admin config with a `game_presence` section still starts (root schema allows unknown keys) and the section is ignored, never deleted. The Manager "Game Presence" page lists only Game Presence instances, shows such leftover sections and imports them (settings; opt-outs/cooldowns only while the Game Presence bot is stopped, never overwriting its existing state) when the user clicks Import.
- Manager: Bot Setup for this type has Name, Token, Presence + Server Members intents, Invite (View Channels + Send Messages, scope `bot`) and Ready; the AI terminal lists only Admin bots; the dashboard shows each type's capabilities.

## Stream Director Bot

Bot type `stream_director` (`bots/stream_director/manifest.json`, entrypoint `DarkAbyss_Core/StreamDirector.py`, defaults `DarkAbyss_Core/defaults/stream_director_config.json`). Its own Discord application, token, process, config, data and runtime; it shares only infrastructure (instance/config stores, `admin_terminal` status files and server/channel pickers, packaging) with the other types and imports nothing from Admin or Game Presence. AI is not used.

Modules (each can change independently):

```text
stream_director_config.py   settings: defaults, limits, features, normalization (the whole instance config)
stream_director.py          domain: sessions, moments (+clustering), challenges, polls/predictions, inbox,
                            community points/levels/seasons, goals, recap, next-stream poll — no Discord, no network
stream_director_store.py    persistence: one JSON state, atomic writes, backup, fail closed
stream_director_twitch.py   Twitch: OAuth Device Code flow, token store, Helix, EventSub WebSocket supervisor
stream_director_discord.py  Discord: live card + session thread, buttons/modals/slash commands, rendering
StreamDirector.py           process: discord.Client wiring, 5 s tick, status file, single-instance lock
manager_stream_director.py  Manager page: Twitch connect, Discord place, features, limits, status, inbox, recap
```

Domain contract: inputs are `StreamEvent` (normalized EventSub notification with its message id), `LiveStream` (Helix state for reconciliation) and `Actor` (Discord member or Twitch chatter, `team` = owner / Manage Server / Administrator / configured team role, or Twitch broadcaster/moderator badge). Outputs are `Effect`s that the Discord adapter turns into posts and edits, and `Outcome` (ok + private reply + effects) for member actions. The state is saved after every change.

Session lifecycle: `stream.online` (or `/stream start` without Twitch) → `live`; `stream.offline` → `ending` for `end_grace_minutes`; a stream that comes back within the grace — even with a new Twitch stream id — resumes the same session; after the grace the session is finalized (points, counters, recap) and moved to history. Helix polling (every 2 minutes and right after every EventSub (re)connect) reconciles missed events: live without a session starts one with the real `started_at`, offline with a live session begins the grace, a different stream id after a long gap ends the old session and starts a new one, and a Twitch session not confirmed for 12 h is closed.

Duplicates and replays: EventSub message ids are stored (24 h, at most 2000) in the state, so a repeated notification — also after a restart — is ignored; notifications older than 10 minutes are dropped (Twitch replay guidance); `stream.online` for a stream id the session already has is a no-op.

Twitch transport: a public Twitch application (no client secret anywhere). The Manager runs the Device Code flow (`/oauth2/device`, polling `/oauth2/token`, `/oauth2/validate`) and writes `secrets/twitch_oauth.json` with a new `generation`; the bot refreshes tokens (rotating refresh token) and writes them only if the generation is unchanged (`save_if_current`), so a reconnect in the Manager is never overwritten. Scopes: `moderator:read:followers` (follower count), `channel:read:subscriptions`, `bits:read`, `user:read:chat` (chat commands and activity); a missing scope only disables its subscription. `TwitchSupervisor` keeps EventSub connected (welcome within 10 s → subscriptions with the session id; `session_reconnect` is followed without resubscribing; close, keepalive loss or errors reconnect with backoff and resubscribe; revocation of the stream subscriptions asks for a reconnect) and never raises: no Client ID, no account, auth failure or no network only change its `status` (`not_configured`, `not_connected`, `connecting`, `connected`, `auth_failed`, `error`). After an auth failure it waits for a new token instead of retrying the rejected one. HTTP goes only to `id.twitch.tv` / `api.twitch.tv`; tokens never appear in logs, status files, `repr` or the config.

Discord surfaces: the configured stream channel gets one card per session (edited at most every 20 s: status, title, category, uptime, moments, challenges, inbox, community level) with a public thread for the session. Only useful events go to the thread (category changes, raids, challenge suggestions and decisions, poll results, goals and level-ups, the full recap); follows, subs, bits and chat are counted for the recap. At the end the card becomes the short recap without buttons and the thread is renamed. All posts use `AllowedMentions.none()` except the optional go-live role ping (exactly that role); user text is cleaned (control characters, length) on input and markdown/mention-escaped (`stream_director.md`) in posts, so masked links or fake `@everyone` cannot be created. Buttons and modals use custom ids that carry the entity id (`sd:card:moment`, `sd:ch:<id>:accept`, `sd:poll:<id>:vote:<i>`, `sd:inbox:<id>:done`) and are routed in `on_interaction`, so they work after a restart without in-memory views. Slash commands (`/moment`, `/challenge`, `/suggest`, `/poll`, `/prediction`, `/inbox`, `/community`, `/stream start|end`, `/goal add|remove`, `/nextstream`) are synced to the configured server. Intents: guilds + guild messages (activity count in the thread only; no Message Content, nothing privileged). Invite: View Channels, Send Messages, Embed Links, Read Message History, Create Public Threads, Send Messages in Threads, Manage Events; scopes `bot applications.commands`.

Community mechanics: moments store the offset from the stream start minus a reaction lag; marks within `moment_cluster_seconds` are one cluster, notable when at least `notable_moment_min_users` different people (or the team) marked it; the recap links clusters to the VOD (`/helix/videos` by stream id) when the streamer keeps VODs. Challenges: suggest (duplicates become support), support, team accept/reject, then completed/failed; accepted ones carry over to the next stream (posted again at its start). Polls and predictions have up to 5 options, one vote per person (changeable), predictions lock and are resolved by the team — no points, no currency, no stakes. The inbox is one queue of questions, game suggestions, clips/links and topics plus waiting challenges (duplicates become votes), visible to the team with `/inbox` and on the Manager page. Progression is shared, not per user: streams, minutes live, completed (and attempted) challenges, polls, notable moments, raids and new followers add XP to the community level (level L starts at 50·L·(L−1) XP) and to the monthly season; goals (weekly / season / long-term, metrics from `stream_director.METRICS`) award a bonus once per period. After a stream with ≥2 game suggestions a 24 h "next stream" poll is posted in the channel; `/nextstream` creates a Discord Scheduled Event that mentions its result.

Persistence and failure: `stream_director_state.json` is written atomically after every change, a backup is refreshed from a state that loaded fine (at start and every 5 minutes); an unreadable or malformed file makes the director unavailable (fail closed): nothing changes, buttons answer "not ready", the status and the Manager page explain how to restore the backup. A config problem is reported the same way; an unconfigured bot tracks nothing visible and posts nothing. Status for the Manager: `runtime/stream_director_status.json` (diagnosis, Twitch status without tokens, missing channel permissions, command sync, session/community/inbox summary, last recap).

Manager: Add Bot → Stream Director Bot; Bot Setup has Name, Token, an intents step stating that nothing privileged is needed, Invite and Ready; the Stream Director page has the status, the Twitch connection (Client ID, Connect with the device code, Cancel, Disconnect with token revocation), the server/stream channel, team roles, go-live role, the seven feature switches, eight limits, the inbox and the last recap. Stream Director instances are not listed on the AI page or the AI terminal.

## Bot Language (per instance)

Every instance config has `language` (`"en"` or `"ru"`, schema pattern `^(en|ru)$`). It is a bot setting, not a
program setting: two bots — also two of the same type — speak different languages at the same time. Defaults keep
what each type did before languages existed: Admin `en`, Game Presence `ru`, Stream Director `en`; an old config
without the key gets that default through the normal defaults merge.

`bot_i18n.py` translates by the English text in the code (the key) into the Russian catalogs
`bot_i18n_ru_admin.py`, `bot_i18n_ru_game_presence.py`, `bot_i18n_ru_stream_director.py`; placeholders are
`str.format` names and a missing translation falls back to English. Stream Director and Game Presence pass their
instance language explicitly (`Director._t`, `tr(language, …)`, render functions take `language`). The Admin bot,
whose output is spread over many modules (commands, AI transport, admin tools, features, blueprint), sets its process
language whenever it loads its config (`Admin.load_config`, `AITransport._try_load_config`) and uses `t(...)`; one
bot process serves exactly one instance. Tool results and errors are translated where they are created, so `/execute`
replies, the "actions already executed" list and plan previews follow the language. Slash command descriptions:
Stream Director builds them in its language, Admin translates them through `BotLanguageTranslator` (descriptions only,
for every Discord locale); both are registered at start, so a language change shows there after a restart while every
reply, button and post switches live.

AI: `OrchestratorRequest.response_language` puts `bot_i18n.ai_language_rule(language)` as a SYSTEM message right after
the core rules into every provider call of the request (planning, tool rounds, final answer; also `compare_plans`).
The Admin planner/executor instructions refer to that rule instead of "the user's language"; Game Presence passes
its language to the wording rewrite. Tests: `tests/test_bot_languages.py` (catalog completeness, deterministic en/ru
output of all three types, the rule in every provider call, old configs) and the Manager round-trip tests.

## Manager Settings Feedback

Pages: Bot Setup (all types), Kairo (Admin: language, Social Awareness), Game Presence, Stream Director. Showing a page
again keeps unsaved edits of the same bot (still marked unsaved); Refresh asks before discarding them.

Every settings form shows one line (`manager_dashboard.SaveIndicator`): the saved settings of which bot are shown,
"● Unsaved changes" as soon as a control differs from the stored value, "✓ Saved at HH:MM:SS" plus when it applies
(live within 15 s / next command / when the bot starts / restart required for a new token) or "✗ Not saved: …".
After Save the form is reloaded from storage, so it always shows what is really stored. Switching to another bot or
closing Bot Setup with unsaved changes asks first. A config that cannot be read locks the form (defaults are never
shown as saved or written over it). Bot Setup also remembers the steps that happen outside the program — Discord
Application ID, "intents enabled", "bot invited" — in `config/manager_setup.json` (Manager-side, no secrets), so
reopening the wizard no longer shows them unchecked.

## Group Up: Whole-Voice Invitations

Voice-aware "join" (Game Presence): a player of game X is in a voice channel and another player of X is outside voice
-> the outsider is invited to that channel. With `ping_whole_voice` (default on, Manager checkbox) the engine also puts
the rest of that voice channel into `Suggestion.voice_crew_ids`: everyone the tracker sees there (bots are never
tracked), whether Discord shows them playing or not - not showing "Playing" never excludes a voice member. Opted-out
members and members under the per-user cooldown are left out, more than `MAX_VOICE_CREW` (10) are not pinged one by one,
and the crew gets the per-user cooldown after the post. Semantics kept from before: everyone outside voice -> "gather",
several voice channels -> "split_voice", all in one channel -> nothing, third player during the delay -> one message.

The notification is plain user mentions (`allowed_mentions.users` = exactly `Suggestion.mentioned_user_ids`, roles and
everyone off). The earlier temporary-role variant was replaced: it needed Manage Roles (which the Game Presence invite
does not ask for, so it always fell back), its stale cleanup deleted any role whose name merely started with
"Group Up: " (also roles of people or of another Game Presence instance), it left "@deleted-role" in the posted message,
and a crash could leave the role behind. No role is created now, so there is nothing to clean up and the invite stays
View Channels + Send Messages.

AI wording (optional, CREATIVE route, no tools) only rephrases the template: each call gets a random tone and the last
accepted wordings to avoid; the result must keep exactly the template's placeholders (`{targets}` within the first 60
characters), contain no mention/link syntax, and be in the bot's language (Cyrillic check), otherwise the deterministic
template is posted. Targets, mentions, timing and cooldowns never come from the AI.

## Bot Event Bus (`bot_events.py`)

DarkAbyss bots are separate processes; `bot_events` lets one bot understand what another just did. Each producing
instance owns one file `<data root>/runtime/bot_events/<instance>.json` with its last 50 events (6 h TTL), replaced
atomically on each publish; readers poll the folder (file signature check), skip their own file and keep seen IDs.
Events carry Discord IDs, names and facts only (no tokens, keys, paths or message text); the reader validates every
field, the file name must equal the producer (a file cannot speak for another instance), sizes are capped and invalid
data is dropped. Producers today: Game Presence (`group_up.suggested`: kind, game, invited/voice/crew IDs, voice channel,
message ID) and Stream Director (`stream.started`, `stream.ended`, `stream.level_up`, `stream.goal_completed`). Publishing
never raises; nothing depends on another bot running.

## Kairo Social Awareness (`social_awareness.py`)

Optional per Admin instance: `social_awareness_enabled` (null = never chosen = off; the Manager asks once when such a
bot is started and saves the answer), `social_awareness_channel_ids` (empty = every channel Kairo can read) and
`social_awareness_replies_per_hour` (1-20, default 4). It needs the Message Content Intent (`message_content_requested`
includes it); switched on while the bot runs without that intent, the status says "restart".

Pipeline: messages (and bot events) -> RAM-only timeline per server (45 min, nothing persisted, nothing kept while off)
-> cheap local triggers: Kairo's name without a mention, a reply to Kairo, the person Kairo just answered, a member
involved in a recent bot event or anyone in that channel in the first minutes after it, rarely a lively conversation.
Messages the normal AI flow answers (control channel, @mention requests) and bot messages never trigger. A trigger
schedules ONE analysis after a short debounce (6-40 s, bursts merge), with per-channel gaps per trigger type.

Analysis: one orchestrator request on the PLANNER route of this bot's own AI connections, `reasoning_effort="high"`
(new `OrchestratorRequest.reasoning_effort`: overrides the profile option for that request only, where the provider
catalog supports the level), no tools, the bot language rule. The timeline goes in as JSON data with short refs (m1, e2),
member texts never reach the system prompt. The model answers ignore / reply_now / wait with a hypothesis, confidence,
target ref and intent. Policy: confidence >= 0.6, analyses/hour = 3 x replies/hour (6-40), replies/hour cap, 3 min
between two replies in a channel, 5 min backoff after AI failures. "wait" stores a thought (60-1800 s); any new message,
bot event or Kairo message in that channel drops it as outdated. The final message is written on the normal ROUTINE
route (normal effort), sanitized (no mention syntax, no links, @everyone neutralized, 400 chars) and sent as a reply
with `AllowedMentions.none()`, only where the bot may post. Ordinary commands, /ai and @mentions are unchanged. Status:
`runtime/social_awareness_status.json` (counts, last decision, problem) for the Manager "Kairo" page. Admin re-reads its
config every 15 s (`refresh_live_config`), so language and Social Awareness changes apply without a command; an
unreadable config switches Social Awareness off.

## Kairo's Social Life: Reactions, Server Lore, Feedback, Quiet, the DarkAbyss System

Everything below happens inside Social Awareness (autonomous interventions only) and adds no AI call of its own:
lore proposals, the reaction choice and "stay out for a while" come from the same high-effort analysis; feedback,
quiet wishes and heartbeats are deterministic.

- Reactions: `react` is a fourth decision next to ignore / reply_now / wait. The analysis names a member message
  (exact ref, never a guess) and one emoji from `social_signals.KAIRO_REACTIONS` (unicode only). Threshold 0.65
  (moved by feedback), reactions/hour = 2 x replies/hour (max 30), 45 s per channel, never twice on one message, never
  on a bot's message, never older than 20 min. `Admin.social_react` checks View Channel, Add Reactions and Read Message
  History.
- Server Lore (`social_memory.py`, `instances/<id>/data/social_memory.json`, per guild): durable things that help
  understand later conversations (kinds meme, nickname, joke, relation, event, norm, fact). The analysis may propose at
  most 2 ops (remember / update / forget by L-ref of the lore it was shown). A new item is a candidate until a later
  analysis (>= 10 min) proposes a similar text again; text is validated (12-180 chars, no mention syntax, links, long
  IDs, e-mail, phone numbers); at most 30 active and 20 candidates per server, candidates expire after 7 days, weakly
  confirmed lore fades after 120 days. Config `social_awareness_lore_enabled` (default true). The Manager lists,
  forgets and clears it.
- Feedback: each autonomous reply/reaction is watched for 10 min: replies to it, being addressed again, reactions
  (positive/negative sets in `social_signals`), negative phrases ("no one asked", "кринж"), and whether people just kept
  talking. The outcome (engaged / positive / neutral / ignored / negative) goes to the memory file (last 40 per server).
  The confidence bar for replies and reactions in that channel moves within 0.55-0.85 with the last 10 outcomes, and the
  analysis sees the counts. Two negative outcomes in a channel within 2 h make Kairo step back there (auto quiet 45 min,
  doubling per repeat within 24 h, max 6 h); one bad joke only raises the bar. Personality and direct commands are not
  touched.
- Quiet wishes (`social_signals.parse_quiet_request`, RU/EN): only for messages addressed to Kairo (name, reply to it,
  mention, or right after its own intervention with no other addressee). Scopes: channel (default; 1 h, or 30 days for
  "не отвечай в этом канале" / "stay out of this channel"), whole server ("везде"), one member ("мне не отвечай", 30
  days); explicit durations ("на 2 часа", "for 10 minutes", "до завтра", "навсегда" = 30 days max). Kairo nods with a
  reaction (🤐 / 👌, at most once a minute per channel) unless the normal AI flow answers that message. "Можешь снова
  говорить" lifts it: a member's own wish only by that member, a channel/server wish by whoever set it, a server
  manager, or anyone for an automatic step-back. Wishes persist across restarts; the Manager shows and lifts them.
  Mutes never reach /ai, @mention requests, the control channel or admin commands. An unreadable memory file means
  silence (the wishes are unknown) until it is fixed or reset.
- The DarkAbyss system: every bot writes a heartbeat into its `bot_events` file (type, Manager name, Discord account,
  servers, once a minute, "stopped" on clean exit; 3 min without one = not running). Kairo combines heartbeats, the
  instance list (metadata only) and the bus events into `darkabyss_bots` for the analysis (name, type, running, in this
  server, last actions here), labels messages of those bot accounts in the timeline, and treats talk about them (their
  names, mentions, or "Group Up"/"Stream Director" when that bot acted here within 2 h) as a trigger, so "Group Up опять
  охуел" is linked to that instance's latest invitation. Awareness only: Kairo cannot control another bot through it.

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
- AI-6: implemented (live Discord smoke pending); expanded Discord-only Admin Tools (structure, permissions, roles, members, moderation, server settings, AutoMod, content, threads, webhooks, invites, expressions, events), server blueprint, persistent bot features, two-stage planner/executor routing.
- AI-7: Manager GUI AI chat.
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

## Manager Self-Update

`DarkAbyss_Core/app_updates.py` connects the update pieces for the installed app. Releases come from
`Grollex/DarkAbyss-Bot-Manager` (`UPDATE_OWNER`/`UPDATE_REPO`).

```text
current_install()             packaged layout only: <install>/versions/<v>/DarkAbyssApp.exe -> (install, v, Launcher.exe)
check_for_update(current)     github_updates.fetch_latest_release (no drafts/prereleases) + semver compare
install_update(update, root)  download_and_stage_release(tag) -> activate_staged_release -> clean updates/ -> prune
save_resume / take_resume     <DATA_ROOT>/config/resume_after_update.json (instance ids, read once, 15 min max age)
start_launcher(installed)     <install>/Launcher.exe, which starts versions/<current.json>
```

Version order is semantic (`1.0.1 > 1.0.0 > 1.0.0-rc2 > 0.9.0-ai7-rc4`); an unparsable version sorts lowest.

Manager flow (`ManagerMainWindow`): from the packaged app a silent background check runs 4 s after start and
every 6 h; the sidebar shows `Check for updates`, the status line, and `⬆ Update to v…` when a newer release
exists. Source runs show "Updates: packaged app only". Installing asks for confirmation (listing the running
bots and the DATA_ROOT that is kept), then in a worker: install + activate first (a failure changes nothing
and the bots keep running), then save the running instance ids, `shutdown_all()`, start the Launcher and close.
The next Manager calls `resume_bots_after_update()` in `main()` and starts those instances again.

Housekeeping after a successful activation only touches program files: `updates/downloads` and
`updates/prepared` are emptied, and installed versions other than the current, the previous one (for
`rollback_to_previous`) and the running one (`keep_versions`) are removed. A version is renamed into
`updates/trash` before it is deleted; Windows refuses that rename while a process runs from the folder,
so a version in use is skipped whole instead of being left half deleted. A version already staged by an
interrupted attempt is activated without downloading again, and a version that is already active is not
re-activated (re-activation would overwrite `previous_version` with itself). If listing the bots fails
after the switch, the Manager still restarts into the new version.

Data safety: the update payload holds only program files; DATA_ROOT (instances, tokens, AI connections and
keys, configs, groups, logs, runtime state) is never written by the updater, and `update_engine` refuses an
install root overlapping DATA_ROOT. New bot types ship in `bots/<type>/manifest.json` inside the version
directory and are discovered from there; existing instances keep their files and configs are normalized with
defaults on load. Release artifacts are built by GitHub Actions from the repository on a clean runner.

## Future Phases

Future phases may add:

- rollback controls in the GUI
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

The wizard adapts to the bot type of the selected instance. A Game Presence bot has its own Discord application, so its wizard asks only for the display name, ITS token and Application ID, acknowledges Presence Intent + Server Members Intent, and generates an invite with scope `bot` and only View Channels + Send Messages; it never writes Admin access settings (Game Presence options live on the Manager "Game Presence" page). New instances of either type are created with `Bots -> Add Bot`, which lists every registered bot type.
