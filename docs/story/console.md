# Demo console: every scenario from one page, while it happens

**Goal.** A web page that a presenter drives every scenario from and that an audience can
read on a projector. It covers Case A, Case B (approve or reject), the crash demo, the
serverless shapes (invoke-local, the dev server's WCI, real AgentCore), the failure demo and
load. It shows both workflows side by side as they run: Support in `agents`, Finance in
`agent-services`, the Nexus `send_message` crossing between them, the A2A task state, and
Support's Worker or AgentCore lifetimes. It works on every profile (`local`, `kind`,
`kind-plaintext`, `eks-pf`) without new terminals or typed workflow IDs.

**Why now.** Everything through v0.13 runs from make targets, each in its own terminal, and
the evidence (`make timeline`, `serverless_report.py`, the Web UI) appears only after the
fact. The four things the audience must see are a state machine, a boundary, compute that goes
away, and a pinned build that comes back. They are easier to see live than in a history dump.
v1.0.0 needs a demo that one person can run.

**Result (2026-11-02): working on `local` with scripted models; kind and eks-pf not yet run
live.** `make check` is green offline. Every local scenario below was driven end to end
through the console's own API and buttons (headless Chrome clicking the page), with no
terminal except `make console`. No AWS call and no Bedrock token. The kind and eks-pf code
paths are unit-tested against fakes and wait for a real run after `make resume`.

## What it adds

| Area | What |
|---|---|
| `services/demo-console` (package `demo_console`) | Starlette + uvicorn on `127.0.0.1:8099`. Both libraries were already in `uv.lock`, so nothing new is downloaded. Static ES modules, no build step and no npm. A new workspace member; Support, Finance and the contract are untouched, so the Build ID stays `4ee7f87` |
| `make console` | `uv run --frozen python -m demo_console --port $(CONSOLE_PORT)`. It takes the same `TEMPORAL_PROFILE` as every other target. `CONSOLE_ALLOW_AWS=1` enables the AWS-backed actions |
| Run view | Lanes `time / compute rail / Support / Nexus seam / Finance`, with gaps over 4 s compressed into bands. The A2A state is the headline word. Three facts sit under it: Support workers running, Nexus call held open (a live clock), and lifetimes. A guided stepper ticks itself from Temporal's own facts. On `input-required` the approval card sits in Finance's lane on the live "now" row. A report strip appears when the run closes. Presenter notes have the talk line, what to point at, and the equivalent make command |
| Stack bar and Manage stack | One pip per component (Temporal, Web UI, endpoint, Finance, Support, AgentCore app, WCI emulator, payments). Each laptop process can be started, stopped or have its log read. Recipes: local plain, local invoke-local, local WCI emulator, kind laptop side, teardown. One-shot jobs: nexus-endpoint, register-version, invoke-local, smoke, card, load, wci-history, and the kind cluster lifecycle (confirm first) |
| Process control | Each process is `make -s <target>` in its own process group, recorded in `.temporal/console/procs/`. After a console restart it is adopted again. Children never inherit the parent make's `MAKEFLAGS`, so `make console TEMPORAL_PROFILE=kind` cannot pin later children to kind |
| Cost gate | Every action is `free`, `tokens` or `aws`. `tokens` asks first. `aws` is refused (`403 aws_disabled`) unless the console was started with `CONSOLE_ALLOW_AWS=1` |
| Ledger 72 | The console never queries a Support Workflow: on `support-tq` a query would wake an AgentCore session. Support's state comes from history and describe only. `AgentsClient` raises on any query in `agents`, and a unit test checks every route |
| `make console-shots` | `scripts/console-shot.sh` / `console_shot.py` drive headless Chrome over the DevTools protocol, because a plain `--screenshot` fires before the modules render. They write the fixture views at 1440×900 and 390×844, dark and light, to `.temporal/console/shots/`. `?fixture=<name>` and `?demo=1` (a recorded Case B replay) need no Temporal, for rehearsal |
| Tests | 95 unit tests (fakes only). The integration test drives Case A, Case B approve and reject, and a 409 through the console's API on the two-namespace dev server with in-process scripted Workers. For Case A it asserts the hop both ways (`send_message` out to `task.started`, `task.state completed` back to `nexus.completed`) and zero queries in `agents` |

Design and API contract: [`docs/console/DESIGN.md`](../console/DESIGN.md). Look:
[`docs/console/BRAND.md`](../console/BRAND.md) (dark, Temporal brand).

## How to run it

```bash
make console                                   # local; then Manage stack -> "Local, plain Support" (scripted)
TEMPORAL_BUILD_ID=4ee7f87 make console         # while uv.lock is uncommitted (build-id.sh refuses)
TEMPORAL_PROFILE=kind make console             # after make kind-up kind-temporal kind-workers certs-pull pf
TEMPORAL_PROFILE=eks-pf make console           # after make resume and make certs-pull pf TEMPORAL_PROFILE=eks-pf
TEMPORAL_PROFILE=eks-pf CONSOLE_ALLOW_AWS=1 make console   # + CloudWatch drain confirm, failure demo, --agentcore-auto
```

The README table under "Demo console" maps each permutation to the recipe and scenario to
use. Stop the console with Ctrl-C. Its processes keep running, and the next console adopts
them. Stop them with *Tear down local* in Manage stack.

## The run (2026-11-02, local, scripted, via the console only)

The dev server was reset first, so no earlier runs show. The console started with
`TEMPORAL_BUILD_ID=4ee7f87 make console` and was restarted five times during the session,
the last time without the variable. Each time it adopted the dev server, payments-mock and
the Workers. With no override, the Build ID then came from the deployment's current version.

| Step | Through the console | What Temporal showed |
|---|---|---|
| Stack | `POST /api/actions/start-recipe {recipe: local-plain, variant: scripted}` | dev server (WCI=0), payments-mock, `worker-finance-scripted`, `nexus-endpoint` (created), `worker-support-scripted`; all pips up |
| Case A | **Start Case A** on the scenario card (clicked) → `support-1042-c2s8` | `get_agent_card` on the seam, `send_message` out (`nx-64`) and back (`nx-64-back`), `completed`, `approved_by auto`, refund `R-10001` quoted in the reply; held open 2.1 s; 5 of 5 steps |
| Case B, approve | **Start Case B** → `support-2077-f5r7`; at `input-required`, **Stop Support worker (plain)** from the notes; **Approve refund** on the card; **Start Support worker** | Support workers 0 with the held-open clock running. Finance `completed`, `R-10002`, `approved_by dana.finance`. A new worker identity finished the same run. Held open 114 s, approval to reply 21 s, two worker identities |
| Case B, reject | `POST /api/runs` → `support-2077-rj-e2e1`; **Reject refund** on the card | `rejected`, no refund, Support's reply apologises with Finance's reason |
| Serverless shape | Recipe `local-invoke`: `agentcore-local-scripted`, invoke-local (`lifetime 1`, HealthyBusy), `register-version MODE=--poller` (current, PINNED) | Shape "serverless, invoke-local"; Build `4ee7f87 current, pinned` |
| | **Start** on *AgentCore, invoke-local* → `support-2077-sl-hurc` | The recipe's lifetime had drained after its 20 s debounce. The run waits with "No Support worker has picked this up yet", and the step is *Invoke* |
| | **Invoke AgentCore app** (notes button) | The lifetime picks the run up, `input-required`, then the app log's `drained` 20 s later: the rail is empty and dashed, Support 0, and the seam's held line keeps glowing |
| | **Approve refund**, then **Invoke AgentCore app** again | Finance `completed` while Support has no compute. Lifetime 2 finishes the reply: `R-10003`, 2 lifetimes, Build IDs `[4ee7f87]`, PINNED. `serverless_report.py` agrees (`agentcore-lifetime-2` and `-3`, `support-supervisor:4ee7f87`, `VERSIONING_BEHAVIOR_PINNED`) |
| Failure demo | **Start Support worker** (plain), `POST /api/runs {scenario: failure}` → `support-1042-fail-e2e1`, **Stop** the Finance worker, then **Start Finance worker** from the notes | `send_message` stays pending on the seam: "attempt 4 failed (upstream timeout)" with attempt 5 next, and the gap reads "the server tries to deliver Support's call". With Finance back, the same call completes: `R-10005`, held open 1:12, 6 of 6 steps |
| The dev server's WCI | Recipe `local-wci`: waits for `/ping` Healthy, restarts the dev server with `WCI=1` (runs survive in `dev.db`), starts the emulator, `register-version --local-wci` | Shape "serverless, WCI emulator"; the emulator pip is up |
| | **Start** on *AgentCore, server's WCI* → `support-2077-wci-ga74`; wait for the drain; **Approve refund** | No button for the second lifetime: the WCI invoked it on its own. After the Nexus completion: task-add-signal +0.006 s, InvokeWorker +0.01 s, first Workflow Task +0.02 s, reply +0.36 s. 2 lifetimes, Build `4ee7f87`, `R-10004`, held open 46 s |

## Fixed while integrating

- **Scenario order for invoke-local.** `register-version --poller` needs a lifetime polling,
  so the recipe invokes first, and that lifetime drains after 20 s, before anyone has started
  the run. The scenario is now *start → Invoke (lifetime 1)*, with the step done when this
  run has a lifetime. A stale "invoked earlier" no longer ticks it, and the presenter gets the
  Invoke button at the moment it matters.
- **Support count after a drain.** The AgentCore app outlives each lifetime, so a drained
  lifetime's poller stayed listed for the server's minute. After a drain the stack bar said
  Support 1 while the run view said 0. On the WCI run, right after lifetime 2 started, both
  said 2. The console now drops this laptop's `…/agentcore-lifetime-N` pollers once `/ping`
  is `Healthy`, and while one lifetime runs it keeps only that one (taken from the app log).
- **Failure demo attempts.** A `nexus.attempt_failed` row's `attempt` is now the attempt that
  failed, which matches its title and the page's "attempt N failed" tag; describe's attempt
  is the next one. The gap before it reads "the server tries to deliver Support's call", not
  "while Finance works".
- **A process the console stopped** is `stopped`, not `exited` ("exited with code -15, open
  the log" read as a crash).
- **Page fixes.** The make command in the notes now quotes the form's or the run's
  workflow ID, not the catalog default. The live clocks render their current value, so
  they no longer flash `0:00` on every update. When Temporal is down there is one banner, not
  two with a raw gRPC error. The AgentCore app and emulator pips are "off" (not red) when the
  Support shape does not use them. The stepper stays on one line with 8 steps. Missing Q15
  deltas (invoke-local has no WCI) are hidden instead of showing `+–`. A rejection no longer
  sends the approver's default "photos checked" comment as its reason.

## Still open

- **kind and eks-pf live.** The detection, mTLS probe, per-identity certs, the `aws` cost
  gate, the estimated drain and the CloudWatch adapter are unit-tested with fakes only. Run
  scenario 4 and `cloud-b` from the console after `make resume`.
- **Lifetime labels.** The run view numbers lifetimes per run ("lifetime 1, 2"). The app's
  identities count since the app started (`agentcore-lifetime-2, -3`), and so does the
  AgentCore pip ("lifetime 2 running").
- **Smaller gaps.** Runs-list paging (`next_page_token` is always null). The list's
  `build_id` is often null because visibility does not fill it. Load batches have no view of
  their own. On a 900 px laptop the approval card's lower edge needs a scroll of the lanes.
  On phones it is a bottom sheet over the page.
