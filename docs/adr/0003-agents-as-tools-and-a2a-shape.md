# ADR-0003: Nested agents as tools; A2A as a tool vs handoff

- Status: **Part 1 accepted (v0.3). Part 2 accepted (v0.5).**
- Runbook: §1.4, 5.6 ("Agents as tools", "Where the A2A tool runs"), §7 Q19, Q20.
- Plan: v0.3, v0.5; verify ledger items 10 and 15.

## Part 1: Support's sub-agents (v0.3)

### Context

The Support Supervisor needs two helpers that belong to the same team: an
**Order Lookup agent** and a **Return Policy agent**. Runbook 5.6 sketches them
as `TemporalAgent`s awaited from in-workflow Strands `@tool`s, which is a nested
agent loop inside the Supervisor's Workflow. It flagged Q20 ("is a nested
`invoke_async` inside a tool supported?") with a fallback: run each sub-agent
as a separate step in `run()`.

Three ways to build "agents as tools" exist with Strands 1.62 and
`temporalio-strands-agents` 0.1.0:

| Option | What it is |
|---|---|
| A. Hand-written `@tool`, **fresh** sub-agent per call | `@tool async def order_lookup_agent(question)` builds a `TemporalAgent` and awaits `invoke_async(question)` |
| B. Hand-written `@tool`, **one** sub-agent built in `__init__` | Runbook 5.6's sketch |
| C. Strands' own `Agent.as_tool()` | `sub.as_tool(name=..., description=...)` in the Supervisor's tools |
| Fallback | Sequential sub-agent steps in `run()` |

### Evidence

`spikes/nested_agents.py` (offline, `make spike-nested`) and the tests named below.

- **Nesting works (A).** The plugin runs plain `@tool` functions on the Workflow
  event loop. A nested `invoke_async` schedules the sub-agent's own
  `invoke_model` Activities, and the sub-agent's `activity_as_tool` tools run as
  Activities too. With `summary=<agent name>` on every `TemporalAgent`, the Web UI
  and the history show who was thinking:
  `Supervisor → Order Lookup → lookup_order → Order Lookup → Supervisor → Return Policy → get_return_policy → Return Policy → Supervisor`
  (`test_workflow.py::test_case_a_nests_each_sub_agent_loop_between_supervisor_turns`).
  It replays (`test_scripted_history_replays_deterministically`, `test_replay.py`
  over dev-server recordings). It also survives a `kill -9` *inside* a sub-agent's
  model turn: only that turn is retried, and the finished sub-agent loop is
  replayed from history, not re-run (`test_crash_safety.py`, verify step 4).
- **Option C writes durable timers.** `as_tool()` forwards the parent's
  `cancel_signal`, and `Agent.stream_async` mirrors that `threading.Event` with a
  watcher task that polls `asyncio.sleep(0.05)` (`strands/agent/agent.py`,
  `_link_cancel_signal`). Inside a Workflow, `asyncio.sleep` is a durable timer.
  - Dev server: about one `TimerStarted`/`TimerFired` pair, plus a Workflow Task,
    per second of sub-agent model time. A 50 ms timer fires about 1 s late. With
    1 s turns, 2 timers and 30 events in 3.1 s of wall time; with 5 s turns,
    6 timers and 50 events in 15.1 s.
  - Time-skipping test server: the timers advance virtual time *while the model
    Activity is still running*. In 3 runs while building the spike, one finished
    with 18 timers for a single 1 s turn, and two hung. The hangs fit the
    sub-agent's model Activity being timed out in virtual time, but that cause
    was not confirmed. The probe therefore runs on the dev server only.
  - Option A writes 0 timers (asserted).
  - `test_agents_as_tools.py::test_strands_as_tool_writes_timers_into_the_workflow_history`
- **Option B refuses parallel calls.** Strands runs a turn's tool calls
  concurrently, and an `Agent` allows one invocation at a time. Two calls to the
  same shared sub-agent in one turn return
  `ConcurrencyException - Agent is already processing a request` to the model as
  a tool error
  (`test_agents_as_tools.py::test_one_shared_sub_agent_refuses_a_second_concurrent_call`).
  B also carries the sub-agent's conversation from one question to the next,
  which a stateless lookup should not do.

### Decision

**Option A.** Each sub-agent is a fresh `TemporalAgent` built inside a
hand-written in-workflow `@tool` (`support_agent/agents.py`). The fallback was
not needed. Specifically:

- Same pinned model key (`"bedrock"`, Opus 5.5) and the same model Activity
  options as the Supervisor (120 s start-to-close, 10 s heartbeat, at most 5
  attempts). `summary` is the agent's name.
- Sub-agents own their Activity tools: Order Lookup has `lookup_order`, and
  Return Policy has `get_return_policy`. The Supervisor no longer calls those
  Activities directly; its only tools are the two sub-agents (and, from v0.5,
  `ask_finance_agent`).
- A sub-agent failure (its model Activity fails for good) is a **tool error**
  for the Supervisor, because Strands catches `Exception` from a `@tool`. The
  Supervisor can apologise instead of the Workflow failing. Workflow
  cancellation is an `asyncio.CancelledError`, which Strands does not catch, so
  it still cancels the in-flight sub-agent Activity
  (`test_a_failing_sub_agent_is_a_tool_error_for_the_supervisor`).
- The tools are called without a `cancel_signal`, which is what keeps option A
  free of timers.
- `strands` imports in Workflow code go inside `workflow.unsafe.imports_passed_through()`,
  as in runbook 5.6. Without them, a Worker without the StrandsPlugin (the v0.1
  hello tests) fails sandbox validation on `urllib3`/`http.client`.
- The `sub_agent_runs` query exposes each sub-agent's question and answer. The
  `transcript` query stays the Supervisor's own conversation.

### Consequences

- **v0.2 histories do not replay** on this code: the command sequence changed.
  `tests/histories/v0.2/` keeps the v0.2 fixture, and `test_replay.py` asserts
  that it raises `NondeterminismError`. On the unversioned dev queue, open v0.2
  runs must finish or be terminated before a v0.3 Worker starts (the verify script
  counts open runs). From v0.7, PINNED Worker Versioning handles this. That is
  the point of the Build ID.
- Each case costs 7 model calls instead of 3 (3 Supervisor, 2 per sub-agent),
  which means more latency and more tokens. It is acceptable for the demo, and it
  makes the "team of agents" visible.
- The Return Policy agent's answer carries `FINANCE_MANAGER_APPROVAL_REQUIRED`
  for refunds over $100 (policy rule 5). The Supervisor turns it into "escalated
  for manager approval" (Case B). From v0.5 the Supervisor will instead call the
  Finance agent, and Finance will decide and approve.
- Reported to Temporal (`docs/ask-temporal.md`): `Agent.as_tool()` inside the
  plugin writes timers. Either the plugin should neutralise the cancel watcher, or
  the docs should say "use a hand-written `@tool`".
- If a later Strands or plugin release changes either behaviour,
  `test_agents_as_tools.py` fails, and this ADR should be revisited.

## Part 2: A2A to Finance as a tool vs an explicit handoff (v0.5)

### Context

The Finance Refund agent belongs to another team and lives in another namespace,
so it is **not** a sub-agent and **not** a child Workflow (child Workflows must
be in the caller's namespace; ADR-0004). Runbook 5.6 sketches
`ask_finance_agent`, a plain async in-workflow `@tool` that awaits
`workflow.create_nexus_client(...).execute_operation(FinanceAgentA2A.send_message, ...)`,
and flags Q19a: does the plugin await an async in-workflow tool on the Workflow
event loop, so it can await a Nexus operation? Its fallback is an explicit
handoff: the Supervisor stops, `run()` makes the Nexus call, and the Supervisor
is invoked again with Finance's reply. Runbook §6 adds prompt drift (the
Supervisor "approves" the refund itself).

| Option | What it is |
|---|---|
| A. Tool (`SUPPORT_A2A_MODE=tool`) | `ask_finance_agent(order_id, amount_usd, reason, summary)`, an async `@tool` that awaits `send_message` inside the Supervisor's turn |
| B. Handoff (`SUPPORT_A2A_MODE=handoff`) | The Supervisor captures the request with `submit_refund_request` (unforced capture tool, ADR-0001 §6; never `structured_output_model`, which forces `tool_choice` and Opus 5.5 refuses it). `run()` sends it, then invokes the Supervisor again with `HANDOFF_DECISION` |
| C. `nexus_operation_as_tool` | Exists only in the OpenAI Agents integration; not in `temporalio-strands-agents` 0.1.0 (Q19, second half, asked of Temporal) |

### Evidence

- **Q19a: yes.** Strands awaits a coroutine tool directly
  (`strands/tools/decorator.py`: `await self._tool_func(...)`); the plugin runs
  plain tools in Workflow context, so `execute_operation` schedules a
  `NexusOperationScheduled` command from inside the Supervisor's turn. Caller
  history: `get_agent_card` → Supervisor/sub-agent turns → Supervisor turn →
  `send_message` (Scheduled, Started, Completed, linked to
  `agent-services/a2a-task-<id>`) → Supervisor's reply turn.
  - Time-skipping, stub Finance behind the real contract:
    `test_workflow.py::test_case_a_tool_mode_asks_finance_from_inside_the_supervisor_turn`.
  - Two namespaces, the real Finance Worker: `tests/test_supervisor_a2a.py`.
  - Dev server, real processes: `scripts/verify/v0.5-a2a-over-nexus.sh` steps 5–8,
    including a `kill -9` of the Support Worker mid-run.
  - Replay: the recorded runs replay with no model and no Finance; Nexus results
    come from history like Activity results (`test_replay.py`).
  - A Worker killed *after* Finance answered does not ask Finance again
    (`test_crash_safety.py::test_worker_dies_after_finance_answered_and_the_a2a_call_is_not_repeated`).
  - No timers are written (unlike `Agent.as_tool()`, part 1).
- **Handoff gives the same result.** Same A2A message (`test_both_modes_send_finance_the_same_a2a_message`),
  same task state and refund; one extra Supervisor turn. Verify step 7.
- **Q19b preview** (decided at v0.6): Strands' tool executors have no timeout of
  their own (`strands/tools/executors/*.py`); the only bound is the operation's
  `schedule_to_close_timeout`, 25 h here.
- **Tool errors.** Strands catches `Exception` from a tool and gives the model an
  error result. So `FinancePeer.ask` catches `NexusOperationError` itself and
  returns an explicit "no refund was issued; apologise; Finance will follow up"
  result (`test_finance_failure_is_reported_not_raised`). Workflow cancellation
  is `CancelledError`, which still propagates.
- **Sync tools don't work in a Workflow** (found here): Strands runs a sync tool
  with `asyncio.to_thread`, which the Workflow event loop refuses; the model gets
  `NotImplementedError` as a tool error and the Workflow carries on
  (`test_tool_kinds.py`; reported to Temporal). Every in-workflow tool here is
  `async def`.
- **Prompt drift** is guarded in code: if the Return Policy agent said ELIGIBLE
  and the Supervisor ended its turn without contacting Finance, `run()` sends one
  `GUARD_REPROMPT`. Tested in both modes, including a model that drifts twice
  (exactly one re-prompt, no loop). The live 5-run check is pending AWS (ledger 17, 49).

### Decision

**Option A, `tool`, is the default; option B, `handoff`, stays one env var away.**
`SUPPORT_A2A_MODE` is read by the starter (`scripts/demo/case-a.sh`, the e2e test)
and passed as the Workflow's second input, so the mode is recorded in history and
replays deterministically (Workflow code can't read the environment). Gate G1
(v0.6) is not triggered by Q19a.

- One Workflow-side client, `support_agent/finance_peer.py` (`FinancePeer`),
  makes both Nexus calls in both modes, so the hop looks the same in history:
  summaries `A2A → Finance Refund Agent: get_agent_card (discovery)` and
  `… send_message (refund order 1042 $40.00) via ask_finance_agent tool|handoff` (Q21 applied).
- **Discovery is real.** `run()` calls `get_agent_card` before the Supervisor's
  first turn, and the card's name, version and skills are given to the Supervisor
  in its first message.
- `context_id` = the Support Workflow ID, so Finance's task is
  `a2a-task-<support id>`; a repeated call joins the same task (`USE_EXISTING`,
  ADR-0004) and payments' idempotency key prevents a second refund.
- The `a2a` query reports the mode, the Agent Card, every exchange (state, reply,
  refund ID) and the guard's re-prompts.

### Consequences

- **v0.3 histories do not replay** on v0.5 code (the first command is now a Nexus
  operation). `tests/histories/v0.3/` keeps them and `test_replay.py` asserts
  `NondeterminismError`. Same rule as v0.2 → v0.3; from v0.7, PINNED versioning.
- Support's tests no longer run without a Finance peer:
  `demo_testing.finance_stub` implements the published contract (the real card,
  Finance's v0.4 limit rule) behind the real endpoint name, so Support's tests still
  import no Finance code. Cross-team tests live in `tests/`.
- A Case A conversation costs 8 Support model calls (4 Supervisor, 2 per
  sub-agent) in tool mode, 9 in handoff mode, plus Finance's 2.
- If a Strands or plugin release adds a Nexus tool helper (Q19, second half), the
  hand-written tool can be replaced; `finance_peer.py` would shrink to the
  discovery call and the A2A message builder.
