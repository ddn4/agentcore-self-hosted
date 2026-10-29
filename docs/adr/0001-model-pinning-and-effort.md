# ADR-0001: Model pinning and effort

- Status: Accepted (v0.1). The exact Bedrock request field for effort is verified in v0.2.
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
   (`BEDROCK_EFFORT`, default `medium`). The effort request field is added to
   `bedrock_model()` once v0.2 has verified it on the wire; until then no
   `additional_request_fields` are sent.
4. Tests register a scripted model under the same `"bedrock"` key, so the pinning
   mechanism is also the test seam (v0.2).
5. Switching to US-only routing is `BEDROCK_MODEL_ID=us.anthropic.claude-opus-5-5`:
   no code change, different IAM (runbook §3.2).

## Evidence (v0.1)

- `make bedrock-ping`: both `global.` and `us.` answer `end_turn`; the captured
  Converse request has only `messages` (no `inferenceConfig`, no `toolConfig`).
- `make test-live`: both services' `bedrock_model()` factories stream a reply.
- `services/*/tests/test_config.py`: default model ID/region, env override, no
  inference params configured on the model.

## Consequences

- A model change is an env change, recorded in `docs/versions.md`.
- v0.2 must close: effort field name, structured output without forced tool use,
  and a botocore hook test that inspects what Strands puts on the wire.
