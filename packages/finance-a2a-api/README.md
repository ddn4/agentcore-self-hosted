# finance-a2a-api: the Finance agent's published A2A-shaped contract

The **only** code shared between team Support (caller, namespace `agents`) and team
Finance (handler, namespace `agent-services`). Depends only on `nexus-rpc`;
pyright strict.

| Module | Contents |
|---|---|
| `finance_a2a_api.a2a` | `Part`, `A2AMessage`, `TaskStatus`/`TaskState`, `Artifact`, `A2ATask`, `AgentSkill`, `AgentCard`, `AgentCardRequest`; the `@nexusrpc.service FinanceAgentA2A` (`get_agent_card` sync, `send_message` workflow-backed); helpers (`refund_request`, `first_text`, `first_data`, `status_text`) |
| `finance_a2a_api.agent_card` | `FINANCE_AGENT_CARD`, `render_markdown()` (the Nexus endpoint description), `ENDPOINT = "finance-agent"`, `SERVICE = "FinanceAgentA2A"`, `AUTO_APPROVE_LIMIT_USD = 100` |

## Spec version: pinned to A2A **0.3.0**

These types are **A2A-shaped, not A2A wire-compatible** (runbook §1.4). The transport
is Nexus, and the payloads are JSON dataclasses. They follow the concepts and
field names of [A2A protocol 0.3.0](https://a2a-protocol.org/v0.3.0/specification/),
written in Python `snake_case`. 0.3.0 is the version the runbook's design and the
talk track use (`input-required`, `kind: "text"`). The current spec, A2A
**1.0.0**, renamed several of these, so they are listed below. Present the demo as
"A2A 0.3-shaped".

| A2A 0.3.0 (JSON) | Here | A2A 1.0.0 delta |
|---|---|---|
| `Message.role` `"user"`/`"agent"` | `A2AMessage.role` | `"ROLE_USER"`/`"ROLE_AGENT"` |
| `Message.messageId`, `contextId`, `taskId`, `parts` | `message_id`, `context_id`, `task_id`, `parts` | same names (camelCase) |
| `Message.kind: "message"` | — (not needed: the operation's type says it) | `kind` discriminators removed |
| `TextPart {kind:"text", text}`, `DataPart {kind:"data", data}` | `Part(kind, text, data)` | `Part` is a oneof of `text`/`data`/`raw`/`url`, no `kind` |
| `Task.id`, `contextId`, `status`, `artifacts` | `A2ATask.id`, `context_id`, `status`, `artifacts` | same; `history` not carried here |
| `TaskStatus.state`, `message`, `timestamp` | `TaskStatus.state`, `message`, `timestamp` | same fields |
| `TaskState` `submitted`, `working`, `input-required`, `completed`, `canceled`, `failed`, `rejected` (`auth-required`, `unknown` unused) | `TaskState` literal | `TASK_STATE_SUBMITTED`, … `TASK_STATE_INPUT_REQUIRED` |
| `Artifact.artifactId`, `name`, `parts` | `Artifact.artifact_id`, `name`, `parts` | same |
| `AgentSkill.id`, `name`, `description`, `tags` | same | same |
| `AgentCard.name`, `description`, `version`, `skills`, `url`, `protocolVersion`, `defaultInputModes`, `defaultOutputModes` | same, snake_case; `url = temporal-nexus://finance-agent/FinanceAgentA2A` | `supportedInterfaces` replaces `url`; `capabilities` required |
| JSON-RPC `message/send` | Nexus operation `send_message` | `SendMessage` |
| Agent Card at `/.well-known/agent-card.json` | sync Nexus operation `get_agent_card` + the Nexus endpoint description | — |

A non-Temporal A2A client would need a thin JSON-RPC gateway that maps these names and
calls the same Nexus endpoint. That is out of scope (runbook §1.4).

## Compatibility rules

Support (AgentCore) and Finance (EKS) deploy independently, so:

- never remove or rename a field, and never change its type;
- every new field has a default (it is optional for older peers);
- unknown fields are ignored on decode (both converters), so a newer peer can add fields.

`tests/test_schema_snapshot.py` enforces this against `tests/contract.schema.json`.
After an allowed change, run `UPDATE_CONTRACT_SNAPSHOT=1 uv run pytest packages/finance-a2a-api`.
`tests/test_roundtrip.py` round-trips every type through the SDK default converter
and the pydantic converter the StrandsPlugin installs, in all four pairings, and
parses runbook 7b.9's hand-typed CLI JSON. `tests/test_agent_card.py` keeps the
endpoint description (`finance-agent-card.golden.md`, `make agent-card-golden`) in
sync with `FINANCE_AGENT_CARD`.

**No `from __future__ import annotations` in `a2a.py`.** Inside a caller's Workflow
sandbox, the default converter could not resolve string annotations
(`NameError: name 'AgentSkill' is not defined` while decoding the Nexus result), so
the module uses real annotations.
