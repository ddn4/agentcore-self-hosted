# ADR-0001: Model pinning and effort

- Status: Accepted (v0.1); amended v0.2 (effort field, structured output). The live
  Bedrock confirmation of the v0.2 amendments is **pending** (see Evidence (v0.2)).
- Runbook: §3.2 (Bedrock model), 5.6, 7b.5. Plan: §3.1.

## Context

- Every agent in the demo (Support Supervisor, its sub-agents, Finance) uses
  **Claude Opus 5.5 on Bedrock**, through the inference profile
  `global.anthropic.claude-opus-5-5` in `us-east-1` (or `us.` for US-only routing).
- `StrandsPlugin()` with no `models` mapping registers Strands' default
  `BedrockModel()`, which is a **different model** (the plugin source,
  `temporalio/strands_agents/_plugin.py`, `_default_bedrock_model`). A Workflow that
  forgets to pin would silently run on it.
- Opus 5.5 on Bedrock rejects `temperature`, `top_p`, `top_k`, a disabled-thinking
  configuration and a forced `tool_choice` (only `auto` and `none`) with HTTP 400.
- Temporal owns retries of the plugin's `invoke_model` Activity, so the boto client
  must not retry inside an attempt (the plugin's own default factory sets
  `retries={"max_attempts": 0}`).

## Decision

1. Each service builds its own plugin in its own `config.py` (no shared code
   between teams except `finance_a2a_api`):
   ```python
   StrandsPlugin(models={"bedrock": bedrock_model})
   def bedrock_model() -> BedrockModel:
       return BedrockModel(model_id=os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-opus-5-5"),
                           region_name=os.environ.get("AWS_REGION", "us-east-1"),
                           boto_client_config=BotocoreConfig(read_timeout=DEFAULT_READ_TIMEOUT,
                                                             retries={"max_attempts": 0}))
   ```
   This is the fixed form `lambda: BedrockModel(model_id=os.environ["BEDROCK_MODEL_ID"],
   region_name=os.environ["AWS_REGION"])` with code defaults (plan §3.2 precedence:
   runtime env > `.env` > profile > code defaults) and the plugin's no-retry client config.
2. **Every** `TemporalAgent` passes `model="bedrock"` (from v0.2).
3. **Never** pass `temperature`, `top_p`, `top_k`, disabled thinking or a forced
   `tool_choice`. Quality/latency is traded with **effort** only
   (`BEDROCK_EFFORT`: `low|medium|high|xhigh|max`, default `medium`).
   - Field (v0.2): `additional_request_fields={"output_config": {"effort": <level>}}`,
     which Converse forwards as `additionalModelRequestFields`. This is the Claude
     API's `output_config.effort` (GA, no beta header); Opus 5.5's default is
     `medium`.
   - At `medium` **nothing is sent** (`effort_fields("medium") is None`): the default
     request carries no `additionalModelRequestFields`, exactly as in v0.1's verified
     wire shape. Only a non-default level adds the field, so a wrong field name can
     only affect someone who opts out of the default.
   - An unknown `BEDROCK_EFFORT` fails fast at Worker start (`ValueError`).
4. Tests register a scripted model under the same `"bedrock"` key, so the pinning
   mechanism is also the test seam (v0.2: `demo_testing.ScriptedModel`, injected via
   `StrandsPlugin(models={"bedrock": lambda: scripted})`; the samples' fallback of
   monkeypatching the plugin's `BedrockModel` is not needed).
5. Switching to US-only routing is `BEDROCK_MODEL_ID=us.anthropic.claude-opus-5-5`:
   no code change, different IAM (runbook §3.2).
6. **Structured output on Opus 5.5 (v0.2): do not use
   `TemporalAgent(structured_output_model=...)`.** Use an **unforced capture tool**
   (plan fallback 1): an in-workflow `@tool submit_refund_request(order_id,
   amount_usd, reason)` with `toolChoice` auto, an explicit prompt instruction to
   call it, and pydantic validation of its input. If the model ends its turn
   without calling it, the Workflow re-prompts once (v0.5 handoff mode, the only
   consumer). JSON text + pydantic (fallback 2) stays the last resort.

   Why, from the installed source (strands-agents 1.62.0, temporalio-strands-agents 0.1.0):
   - `structured_output_model` adds the output model as a tool with `toolChoice`
     auto. If the model answers in text instead, Strands retries with
     `toolChoice {"tool": {"name": <model>}}` (`event_loop.py`, `set_forced_mode`).
   - Plain Strands survives that on models that refuse forced tool use:
     `BedrockModel._converse_structured_output` falls back forced → native
     `outputConfig` → auto. But that fallback only runs when `stream()` receives
     `structured_output_model`, and the plugin's `invoke_model` Activity input
     (`_InvokeModelInput`) has no such field; `TemporalModel` also lacks
     `_supports_structured_output`, so the forced request (only the forced tool
     spec, `toolChoice` tool) goes to Bedrock unchanged.
   - Opus 5.5 rejects forced `tool_choice` (`any`/`tool`) with a 400 (Claude API
     model notes; runbook 3.2). The `invoke_model` Activity would then fail on every
     attempt until `MODEL_RETRY` gives up and the Workflow fails.
   - So `structured_output_model` works only when the model happens to call the
     output tool on its own: a latent failure, not a mechanism.

## Evidence (v0.1)

- `make bedrock-ping`: both `global.` and `us.` answer `end_turn`; the captured
  Converse request has only `messages` (no `inferenceConfig`, no `toolConfig`).
- `make test-live`: both services' `bedrock_model()` factories stream a reply.
- `services/*/tests/test_config.py`: default model ID/region, env override, no
  inference params configured on the model.

## Evidence (v0.2)

- Offline, in `make check`: `test_config.py` (no extra fields at `medium`;
  `output_config.effort` for other levels; unknown level rejected);
  `test_workflow.py` (every `invoke_model` input has `model_name: "bedrock"` and
  `tool_choice: null`; the model receives no forced `tool_choice`; an unknown key
  fails the Activity instead of falling back to a default model).
- Offline, in `make check`: `test_structured_output_path.py` runs
  `TemporalAgent(structured_output_model=...)` (`demo_testing.probes`) against a
  ScriptedModel behind the plugin. When the model answers in text, the request the
  Worker-side model receives next is `toolChoice {"tool": {"name": ...}}` with only
  the output tool: the forced request Opus 5.5 refuses.
- Live, **pending**: the AWS SSO session had expired when v0.2 was built
  (`InvalidClientTokenId` / `TokenRetrievalError`), and `aws sso login` needs a
  browser. `make spikes` (= `spikes/opus_params.py` + `spikes/opus_structured_output.py`)
  checks, through the plugin's own `invoke_model` Activity with the pinned
  factory: the ConverseStream body (pinned `modelId`, no `inferenceConfig`
  sampling keys, no thinking config, `toolChoice` auto); 400 for `temperature` and
  for disabled thinking; `output_config.effort` `low` accepted and a bogus level
  refused; per-turn latency at `low` vs `medium`; forced `toolChoice` refused both
  raw and on the plugin's forced-retry request; the capture tool returning a valid
  `RefundRequest`. If the effort field is refused, change `effort_fields()` only
  (the default path sends nothing).

## Consequences

- A model change is an env change, recorded in `docs/versions.md`.
- v0.5 handoff mode uses the capture tool, never `structured_output_model`.
- The plugin gap (structured output's provider fallback is lost across the
  Activity boundary) is reported in `docs/ask-temporal.md`.
