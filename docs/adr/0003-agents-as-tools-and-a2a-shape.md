# ADR-0003: Nested agents as tools; A2A as a tool vs handoff

- Status: **Part 1 accepted (v0.3).** Part 2 is proposed and is decided at v0.5.
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

## Part 2: A2A to Finance as a tool vs an explicit handoff (proposed; decided at v0.5)

The Finance agent belongs to another team and lives in another namespace, so it
is **not** a sub-agent and **not** a child Workflow (ADR-0004). The proposed
default is `ask_finance_agent`, a hand-written async in-workflow `@tool` that
awaits `workflow.create_nexus_client(...).execute_operation(FinanceAgentA2A.send_message, ...)`.
That is the same mechanism Part 1 proved for nested agents, now awaiting a Nexus
operation instead of a sub-agent. The fallback, `SUPPORT_A2A_MODE=handoff`, uses an
unforced capture tool `submit_refund_request` (ADR-0001 §6) and then makes the
Nexus call in `run()`. v0.5 answers Q19a and fills in this part.
