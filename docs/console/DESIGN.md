# Demo console: design

A web console on `http://127.0.0.1:8099` that a presenter uses to run every scenario of this
demo and that an audience watches on a projector. The presenter drives Case A, Case B
(approve or reject), the crash demo, the serverless shapes (invoke-local, the WCI emulator,
real AgentCore), the failure demo and load. The console shows what is happening in both
namespaces while it happens. It works for every permutation (`TEMPORAL_PROFILE=local`,
`kind`, `kind-plaintext`, `eks-pf`; scripted or live models) and derives everything it
shows from Temporal, so it can be started before or after the stack and restarted at any
time.

- Look: [`BRAND.md`](BRAND.md) (dark, Temporal brand styling; authoritative for visuals).
- Visual target: [`mockup.html`](mockup.html) (self-contained, fake data, dark brand styling; the run view midway
  through Case B on the WCI emulator, with a working Approve button that plays the rest of the
  run). Open it directly in a browser. URL switches: `?theme=light` (dark is the default), `?projector=on`,
  `?notes=off`, `?approved=1` (jump to the completed state). Keys: `N` notes, `P` projector.
- New workspace member: `services/demo-console` (package `demo_console`). It does not modify
  `services/support-agent`, `services/finance-agent` or `packages/finance-a2a-api`.

Contents: [1 Audience and story](#1-audience-and-story) ·
[2 Information architecture](#2-information-architecture-and-layout) ·
[3 Visual system](#3-visual-system) · [4 Tech](#4-tech) ·
[5 API contract](#5-api-contract) · [6 Test plan](#6-test-plan) ·
[7 Files and make targets](#7-files-and-make-targets) ·
[8 Follow-ups](#8-follow-ups-out-of-scope)

---

## 1. Audience and story

**Presenter**: a Temporal SE or the author, on a laptop. They may also have the projector as
a second screen. They know the make targets. What they need is fewer terminals, no typed
workflow IDs, and a screen that explains itself while they talk.

**Audience**: platform and AI engineers at a customer. Most have never seen Temporal history.
They must leave with four pictures in their heads. Each picture is a visual element of the
console that has to work when projected from the back of the room:

| The audience must see | What the console shows | Where |
|---|---|---|
| **Two teams, one contract.** Support (namespace `agents`) asks Finance (namespace `agent-services`) for a refund. They share an A2A contract and a named Nexus endpoint, not code, workers or a child workflow. | Two swimlanes with a vertical **boundary seam** between them, labelled with the endpoint `finance-agent`. Every Nexus call is drawn as a connector that crosses the seam. Nothing else crosses it. | Run view, lanes |
| **The A2A task has a state, and a human owns one of them.** `submitted -> working -> input-required -> completed / rejected / failed`. | The **state track**, with the current protocol token set very large (`input-required`). The approval card sits *inside Finance's lane*, at the moment the task went to input-required. | Run view, header and Finance lane |
| **Support's compute is gone, and nothing is lost.** While Finance waits, Support has no worker at all (scale to zero). The server holds the Nexus call open and the conversation stays in history. | The **compute rail** at the left of the Support lane is lit while a worker or AgentCore lifetime runs and **goes dark** when it drains. Beside the dark stretch, the seam shows a violet "held open" bar, with a running clock, for the pending `send_message`. A large counter reads "Support workers running: 0". | Run view, compute rail and seam |
| **A new lifetime picks up, pinned to the same build.** After approval, the Nexus completion makes the WCI (or the presenter, with invoke-local) start lifetime 2. It finishes the reply on the same Build ID. | The rail lights again as "lifetime 2". A `wci InvokeWorker` row appears on the rail edge. The run header always shows `pinned to support-supervisor:4ee7f87`. The report strip shows the Q15 deltas (approval to InvokeWorker to first Workflow Task to reply). | Run view, rail and report strip |

The secondary stories (crash demo: kill the worker mid-thought; failure demo: Finance
disappears and the Nexus call retries; handoff mode; load) reuse the same view. Each adds
one visible thing: a lifetime that ends abruptly, an attempt counter on the seam, or a grid of
small runs.

**Presenter flow** (for every scenario):
1. Pick the scenario in the launcher. Its preconditions are checked against the live stack,
   and each missing piece has a fix button (local) or the exact make command (kind/eks-pf).
2. Press **Start**. The console starts the workflow with a fresh ID and opens the run view.
3. Follow the guided step list. Each step ticks itself when Temporal shows it happened
   (derived from history, task state and the compute signals; never from button clicks). The
   current step carries the talk-track line in presenter notes.
4. Act where a human is needed: approve or reject, invoke-local, kill or restart a worker.
5. Open the Temporal Web UI from any row (deep links), and finish on the report strip.

---

## 2. Information architecture and layout

### 2.1 Regions

```
┌ Stack bar ─────────────────────────────────────────────────────────────────────────────┐
│ AgentCore demo console   [local | kind | eks-pf]   Support: serverless via WCI emulator │
│ Model: scripted   ● Temporal ● Web UI ● Endpoint ● Finance 1 ○ Support 0 ● AgentCore    │
│ ● Emulator ● Payments   Build 4ee7f87 (current, pinned)     [Manage stack] [N] [P] [◐]  │
├──────────────────────┬─────────────────────────────────────────────────┬───────────────┤
│ Scenarios            │ Run header: support-2077-wci-k3f9               │ Presenter     │
│  Case A  ($40)       │ "Order #2077: my laptop arrived with a cracked…"│ notes         │
│  Case B  ($450) ●    │ Case B on the WCI emulator; pinned 4ee7f87      │ (toggle N)    │
│  Crash demo          │ [Support in Temporal UI] [Finance task in UI]   │               │
│  Serverless B        ├─────────────────────────────────────────────────┤ talk line for │
│  B on WCI emulator   │ submitted  working  INPUT-REQUIRED  completed…  │ the current   │
│  B on AgentCore      │ Support workers 0 · held open 46 s · lifetimes 1│ step; what to │
│  Failure demo        ├─────────────────────────────────────────────────┤ point at; the │
│  Smoke + card        │ 1 ✓ 2 ✓ 3 ✓ 4 ✓ 5 ✓ [6 Approve] 7 8  (stepper)  │ make command  │
│  Load                ├────┬──────────────┬────────┬────────────────────┤               │
│                      │rail│ Support      │ seam   │ Finance            │               │
│ Runs                 │ ██ │ started      │        │                    │               │
│  support-2077-wci… ◐ │ ██ │ model turn   │        │                    │               │
│  support-1042-k2x  ✓ │ ██ │ send_message ├──────▶ │ task started       │               │
│  load-…  10/10     ✓ │ ██ │              │ ┃      │ fraud_score        │               │
│                      │ ░░ │   46 s: Support has no compute ┃ input-req.  │               │
│                      │ ░░ │              │ ┃held  │ ┌ approval card ┐  │               │
│                      │ ░░ │              │ ┃open  │ │approve/reject │  │               │
│                      │    │              │        │ └───────────────┘  │               │
├──────────────────────┴────┴──────────────┴────────┴────────────────────┴───────────────┤
│ Report strip (after close): lifetimes 2 · Build IDs 1 · approval → reply 9.1 s · R-10015│
└────────────────────────────────────────────────────────────────────────────────────────┘
```

(The `·` separators in this sketch are only for the ASCII drawing. In the UI each fact is its own
labelled cell.)

- **Stack bar** (always visible, one or two lines, sticky). It shows the profile (a segmented
  control; switching asks first if managed processes are running), Support's shape (plain
  worker / serverless invoke-local / serverless WCI emulator / AgentCore in AWS), the model
  mode, and one status pip per component. A pip is a small shape and a word, never colour
  alone. **Manage stack** opens the stack sheet: processes with start/stop/log, stack recipes,
  one-shot actions, kind and EKS commands to copy. `N` toggles presenter notes, `P` toggles
  projector mode and `◐` switches between the dark theme (default) and light.
- **Scenario launcher** (left column). Each scenario shows its title, the amount, and a fit
  marker for the current profile: ready, needs N things, or not on this profile. Selecting one
  opens its card in the main column if no run is focused. The card shows preconditions with
  fix buttons, options (A2A mode tool|handoff, Nexus budget, workflow ID, approver default),
  cost labels and **Start**.
- **Runs** (left column, below). This is a visibility list of recent `SupportSupervisorWorkflow`
  runs (plus load batches grouped by `load-<epoch>`). Each entry shows status and the A2A task
  state glyph. Selecting one focuses the run view (URL `#/runs/<wf>`), so a console restart or
  reload returns to the same run.
- **Run view** (main column), top to bottom:
  1. *Run header*: the ticket as a quote on one line with the Temporal UI deep links
     ("Support", "Finance task") beside it, then one meta line: the workflow ID (mono), the
     scenario name, queue, `pinned to support-supervisor:<build>` (from describe
     `versioning_info`), A2A mode and budget. Header, hero and stepper together are one compact
     strip so the lanes get most of the height (about 60% of a 900 px screen).
  2. *State track* (the hero): the five A2A states as a track, with the current state token set
     large. Beside it are three facts: **Support workers running** (0 is shown in the
     "dark" colour), **Nexus call held open** (a live clock while pending) and
     **Lifetimes** (count), with the task's message under or between them. Once the run has
     closed, the report (held open, lifetimes, Build IDs, approval wait and to reply, approver,
     refund) takes the facts' place, and the all-done stepper is hidden.
  3. *Guided steps*: a horizontal stepper (numbered, because it is a sequence) kept to one
     line. Done steps collapse to a check (full title in the tooltip), the current step shows
     its short label in a pill, and the steps still to come show short labels. Its action
     button and talk line live in presenter notes and in the lanes, not in the stepper.
  4. *Lanes*: `time | compute rail | Support | seam (Nexus) | Finance`. Time flows down, in event
     order, with **compressed gaps**: a gap longer than 4 s between consecutive rows is shown as
     a band carrying its duration ("2:00 with no Support compute"). This keeps a 24 h approval
     wait readable. Rows carry their agent attribution ("Order Lookup agent") and, on hover or
     focus, the worker identity and a deep link. Nexus rows pair up into connectors across the
     seam (when the two ends are within 0.2 s, they render as one row with a wire across).
     On wide screens the page itself does not scroll: header, hero and stepper stay fixed, and
     the lanes are the only scroller. They **follow the run**: the newest Nexus hop row
     (send_message out, the answer back, a failed attempt) stays at the top with as much after
     it as fits, while the newest row (the reply, once finished) keeps at least its title in
     view; the approval card always shows in full. Scrolling away pauses following and shows
     a "Follow the run" button. Projector mode hides the side column, the stepper and (unless
     turned on there) the notes, so the lanes keep about 60% of a 1280x720 screen. The last row is always a live
     **"now" row**. While the task is `input-required`, the **approval card sits in Finance's
     cell of the "now" row**, so the card, the dark rail and the held-open bar are on screen
     together. The earlier `input-required` row stays in the history as "Finance asks a
     human". (The first mockup put the card at that earlier row; with tail-follow it scrolled
     out of view on a 900 px laptop.)
  5. *Reply and report strip*: once the run closes, Support's final reply (from the
     `WorkflowExecutionCompleted` payload) plus the report: lifetimes, distinct Build IDs,
     approval wait, approval-to-reply, refund ID, and the Q15 deltas on serverless runs.
- **Presenter notes** (right column, toggle `N`; or a separate window at `/#/notes` for the
  presenter's own screen). This holds the scenario talk track from the README story index,
  the talk line of the current step, what to point at, and the equivalent make command
  (useful when the audience asks "what did that button do").
- **Projector mode** (`P`): type scale ×1.25, hides the scenario column, process logs and
  identities, and keeps the stack bar to one line. Use it on the projected window.

### 2.2 Scenario catalog

Steps are a sequence and are numbered. "Done when" is a predicate the backend evaluates on the
run view (section 5.4), so steps survive reloads and console restarts. *Cost*: `free` (no AWS,
no tokens), `tokens` (Bedrock), `aws` (any AWS API, including kubectl to EKS).

| id | Title | Profiles | Support shape | Steps (abridged) | Cost |
|---|---|---|---|---|---|
| `case-a` | Case A: $40 headphones, auto-approved | all | plain (support-dev-tq) or serverless (support-tq) | start → sub-agents answer → agent card discovered → Nexus send_message → Finance completed `approved_by auto` → reply | free (scripted) / tokens (live) / aws on eks-pf serverless |
| `case-b` | Case B: $450 laptop, a human approves | all | plain or serverless | start → input-required → (optional) stop Support worker → approve/reject → restart worker → reply | as above |
| `crash` | Durable agent: kill the worker mid-thought | local, kind | plain | start Case A → wait for an `invoke_model` in flight → **Kill Support worker** (SIGKILL to its process group) → start it again → same run finishes; the rail shows two lifetimes, one ending abruptly | free / tokens |
| `serverless-b` | Case B, AgentCore shape, invoke-local | local, kind | serverless invoke-local | AgentCore app up, Invoke and register version `--poller` (the recipe) → start on support-tq → **Invoke** (lifetime 1 picks the run up; the recipe's lifetime has usually drained by then) → input-required → drained (`/ping` Healthy) → approve → **Invoke** (lifetime 2) → reply → report | free / tokens |
| `wci-b` | Case B, the server's WCI starts the worker | local | serverless WCI emulator | dev server `WCI=1` (restart only when `/ping` is Healthy) → emulator → register version `--local-wci` → start → input-required → drained → approve → WCI InvokeWorker → lifetime 2 → reply → report | free / tokens |
| `cloud-b` | Case B on AgentCore in AWS | eks-pf | AgentCore (real) | start on support-tq → WCI invokes session 1 → input-required → drained (estimated from debounce; CloudWatch confirm is opt-in) → approve → InvokeWorker → session 2 → reply → report | aws + tokens |
| `failure` | Finance disappears mid-call | local (stop the Finance worker), eks-pf (`failure.sh`, scales pods) | plain | start Case A → agent card done → **Stop Finance** → seam shows `send_message` attempt 2, 3… with lastAttemptFailure → **Start Finance** → completes | free locally / aws on eks-pf |
| `smoke` | The hop with no AI | all | smoke worker | `A2ASmokeWorkflow` on nexus-smoke-tq, then a standalone `get_agent_card` | free |
| `load` | N conversations at once | local, kind, eks-pf | serverless (support-tq) | `load.sh N LOAD_KIND=ping|case-a` as a job; the runs list groups the batch; the report shows completed/failed and InvokeWorker count | free locally (scripted) / aws on eks-pf |
| `cleanup` | Leftovers | all | — | lists runs still in `input-required` (e.g. `support-2077-v013-…` after resume) with Reject and Terminate | free |

Default workflow IDs follow the make defaults with a short base-36 suffix so that repeated runs
do not collide: `support-1042-<sfx>`, `support-2077-sl-<sfx>`, `support-2077-wci-<sfx>`,
`support-2077-v1-<sfx>` (cloud). The suffix is never 12 digits (scrub-check).

### 2.3 Stack recipes (local profiles)

One button per recipe brings the stack to a known shape as a job with a visible checklist.
Each line is the make target the console runs, so the presenter can say what happened.

| Recipe | Starts (in order) | Stops |
|---|---|---|
| Local, plain Support | `dev-server-up` (WCI=0), `payments-mock`, `worker-finance[-scripted]`, `nexus-endpoint`, `worker-support[-scripted]` | `agentcore-local*`, emulator |
| Local, serverless (invoke-local) | as above without `worker-support`, then `agentcore-local[-scripted]`, `invoke-local`, `register-version MODE=--poller` | `worker-support*` |
| Local, serverless (WCI emulator) | waits for `/ping` Healthy, `dev-server-down`, `dev-server-up WCI=1`, `agentcore-emulator`, `agentcore-local[-scripted]`, `register-version` (`--local-wci`) | `worker-support*` |
| kind (laptop side only) | `nexus-endpoint`, `agentcore-local[-scripted]`, `invoke-local`, `register-version MODE=--poller` | — |
| Tear down local | stops every managed process, `dev-server-down` | everything |

The cluster lifecycle on kind (`kind-up kind-temporal kind-workers FINANCE_MODEL=scripted
certs-pull pf`, `kind-down`) is offered as confirm-first jobs: Docker only, several minutes,
and `kind-down` is destructive. On eks-pf the sheet shows the commands (`make resume`,
`make pf TEMPORAL_PROFILE=eks-pf`, `make pause`) as copyable text and never runs them.

### 2.4 Phone width (≤ 640 px)

The stack bar collapses to profile + a single "n of m up" pip (tap to open the sheet). The
launcher and runs become a drawer. The lanes become **one interleaved column**: each row keeps
a 4 px lane edge in its lane colour and a lane word ("Support", "Finance"). Seam crossings
become full-width violet rows ("Nexus send_message to Finance"). The compute rail turns into a
6 px strip on the left edge of every row, lit or dark. The state track shows only the current
state and the next one. The approval card is a sticky bottom sheet. The side gutter is 16 px,
and nothing scrolls horizontally.

---

## 3. Visual system

**[`BRAND.md`](BRAND.md) is authoritative** for the look: a dark theme styled after
temporal.io/brand. It overrides this section wherever they conflict. This section records
how the console applies it; `static/styles.css` holds the tokens and
[`mockup.html`](mockup.html) uses the same stylesheet.

### 3.1 Direction

**A dark brand panel with one loud element: the Nexus seam.** Space Black ground, Off White
type in a light grotesk, UV for everything you can press. The boldness is spent between the
two teams' lanes: a glowing purple-to-UV band (the Temporal server and its Nexus endpoint),
with the held-open `send_message` call as a bright core line that leaves the outgoing wire and
joins the answer's wire. Beside it, the Support compute rail tells the scale-to-zero story:
**alive** is a mint bar with a glow, **scaled to zero** is an empty, dashed, dim lane. The
difference has to read from the back of a room on a projector. Everything else is quiet:
hairline rules, sentence-case labels, no decoration.

Earlier direction (superseded 2026-11-02): a light-first "signal-box mimic panel" with Archivo
and amber Finance. The layout, information architecture and API contract are unchanged; only
the visual layer moved to the brand.

### 3.2 Colour tokens

Dark is the default and needs no preference: tokens are on `:root`, and `index.html` sets
`data-theme="dark"` unless `?theme=light` or a stored choice says light. Light is secondary,
behind the theme toggle (`:root[data-theme="light"]`, Off White `#F8FAFC` ground). `body` and
`html` have an explicit background.

| Token | Dark (default) | Light | Use |
|---|---|---|---|
| `--page` | `#141414` Space Black | `#F8FAFC` | page background |
| `--panel` / `--raised` / `--well` | `#1C1C1F` / `#26262B` / `#101011` | `#FFFFFF` / `#EEF1F5` / `#F1F3F7` | lanes and cards / approval card, current item / inputs, notes |
| `--ink` / `--muted` / `--faint` | `#F8FAFC` / `#A9ABAD` / `#8F9195` | `#141414` / `#55585E` / `#676A71` | text: primary, ~65 %, ~52 % |
| `--rule`, `--hair` | `#2F2F35`, 9 % white | `#D9DCE2`, 10 % black | borders, row hairlines |
| `--nexus-fill`, `--nexus` | UV `#444CE7`, text `#A3A8FF` | `#444CE7` | buttons, selected, focus; Nexus labels, WCI rows, held clock |
| `--band`, `--g-nexus` | purple→UV `#B664FF → #444CE7` | same, lighter | the Nexus seam band and its held-open core line |
| `--support-fill`, `--support` | blue `#0066FF`, text `#4A93FF` | `#0066FF`, `#0057D9` | Support lane accent, reply bubble |
| `--finance` | pink `#FF6BFF` | `#B4189E` | Finance lane accent (pink keeps it apart from the UV seam) |
| `--ok`, `--alive` | mint `#1FF1A5` | `#06714C`, `#12C98A` | completed, healthy pips, compute alive (glow) |
| `--hold` | lime `#C3FF62` | `#4A7000` | input-required: lime stripes on black under the state word and down the approval card |
| `--bad` | red `#FF5555` | `#C42929` | rejected, failed, Reject, AWS cost |
| `--dead` | 40 % Off White, dashed | 32 % black | compute scaled to zero: dashed outline, no fill |
| `--wire` | `#8E7BFF` | `#5F55E8` | Nexus connectors across the seam |
| `--focus` | `#8C92FF` | `#444CE7` | 2 px focus ring, offset 2 px |

Gradients (brand stops, used sparingly): purple→UV for the seam and the held line;
lime→mint for **Approve refund** and the `completed` underline; red→purple for the
`rejected`/`failed` underline. Approve is the only gradient button; other actions are solid UV.

States map to colour plus shape plus word: `submitted` and `working` use Finance pink with an
open and a filled dot; `input-required` a lime lozenge; `completed` a mint check; `rejected` a
red cross; `failed` a red broken ring; `canceled` is muted. Every pip has a text label.
**Contrast**: every text token is AA on `#141414`, `#1C1C1F` and `#26262B` (lowest: `--faint`
4.8:1 on `--raised`, `--bad` 4.8:1 on `--raised`; Support text 6.1:1, Nexus text 8.4:1,
Finance 7.7:1, mint 12.4:1, lime 15.6:1 on the page). White on UV is 6.1:1, Space Black on
lime or mint is above 12:1. The light theme's text tokens are AA on all three light surfaces.

### 3.3 Typography

- **Hanken Grotesk** (variable `wght` 100–900, OFL) stands in for Aeonik, which is commercial
  and is neither copied nor linked. All UI text. Headings are light (300–400) at large sizes
  with tight tracking: the hero A2A state word (`input-required`, in the protocol's own
  spelling) is weight 300, `min(5.1rem, 11.4cqi)`, tracking −0.045em; lane headers, the
  approval title and dialog titles are 400; body 400; labels 500; buttons 600.
- **IBM Plex Mono** (400/500, OFL), for identifiers only: workflow IDs, Build IDs, task queues,
  worker identities, make commands.
- **Vendored, no CDN**: `static/vendor/fonts/` holds the unmodified Latin-subset woff2 files
  with their OFL texts and a README with sources and SHA-256. `index.html` preloads the sans.
  The mockup loads the same files by relative path. Fallback stacks: `"Hanken Grotesk",
  system-ui, -apple-system, "Segoe UI", sans-serif` and `"IBM Plex Mono", ui-monospace,
  "SF Mono", Menlo, monospace`.
- Base 15 px (× 1.25 in projector mode), line-height 1.45 for body, about 1 for the hero.
  Sentence case everywhere, with no all-caps labels and no eyebrows. Lines stay under 80
  characters in notes and replies.

### 3.4 Motion

Motion is used only for state changes caused by the run or the presenter, plus a slow pulse on
what is live. Every animation is disabled under `prefers-reduced-motion: reduce` (the changes
then happen instantly).

| Moment | Motion | Duration |
|---|---|---|
| A2A state changes | the new word fades in from weight 100 and loose tracking to weight 300 and tight tracking (variable-font interpolation) | 520 ms, `cubic-bezier(.2,.7,.2,1)` |
| Compute drains | the rail goes from the mint bar to the dashed empty lane | 900 ms |
| A lifetime starts | the rail segment lights with a single brief mint glow | 600 ms, once |
| Nexus call pending | the held-open line in the seam glows and pulses slowly, the only looping animation, and it stops when the call completes | 2.4 s loop |
| New row | the row's ground flashes UV-tinted and settles | 900 ms |
| Approve pressed | the card collapses into the signal row it produced | 250 ms |

Crossing rows keep their labels off the held-open line: the outgoing `send_message` row puts
its label above its wire (the line starts at the wire and runs down), the answer row puts its
label below its wire (the line runs down into the wire), and a stub inside a held stretch
(`started, held`, `attempt N failed`) is a pill tag on a solid ground, pinned on the line.

### 3.5 Iconography

Inline SVG symbols in one `<svg hidden>` sprite in `index.html` (no icon font, no CDN), with a
1.5 px stroke, round caps and a 16/20 px grid: lane glyphs (Support headset, Finance ledger,
Nexus seam), state glyphs (see 3.2), process glyphs (play, stop, log), cost marks (token coin
= Bedrock, cloud = AWS), external-link and copy. Icons always sit next to a word, never alone,
except the three toggles in the stack bar, which carry `aria-label` and a tooltip.

### 3.6 Copy rules

Name things by what the audience sees ("Support workers running", "Nexus call held open",
"Finance asks a human"). Show the Temporal term once beside the plain one (`input-required`,
`send_message`). Buttons say exactly what happens: **Start Case B**, **Approve refund**,
**Reject refund**, **Invoke AgentCore app**, **Stop Support worker**. Results reuse the
button's verb ("Refund approved by dana.finance"). An error says what happened and how to fix
it: "Temporal is not reachable at 127.0.0.1:7233. Start the dev server, or run `make pf` for
kind." An empty state invites the next action: "No runs yet. Start Case A to see the hop."

---

## 4. Tech

### 4.1 Shape

- **Backend**: Python 3.12, **Starlette 1.10 + uvicorn 0.54** (both already in `uv.lock`
  transitively, through bedrock-agentcore and mcp; no new download). SSE is a plain Starlette
  `StreamingResponse` with `text/event-stream` (no sse-starlette needed). It talks to Temporal
  through the Python SDK, via the same envconfig as make (`TEMPORAL_CONFIG_FILE`,
  `TEMPORAL_PROFILE`). It reuses `demo_testing.history`, `demo_testing.a2a_tasks`,
  `demo_testing.versioning`, `finance_a2a_api` and `support_agent.config`, all read-only.
  Make targets and scripts are used only where there is no SDK path: compute-config
  registration, dev-server, kind/eks pf, load, failure.
- **Frontend**: static files served by the backend: `index.html`, `styles.css`, and ES
  modules under `static/js/`. There is no build step, no bundler, no npm and no vendored
  minified library (scrub-check scans every file). Lanes are DOM (CSS grid), connectors are
  one absolutely positioned inline SVG overlay, and there is no chart library.
- **Live updates**: one SSE stream per page (`/api/stream?wf=<focused>`). The server polls
  Temporal (stack every 3 s, runs every 3 s, the focused run every 1 s; adaptive back-off when
  nothing changes) and emits only on change (hash per snapshot). On reconnect the browser just
  re-renders from the next snapshots, so there is no replay protocol.

### 4.2 Connection and profiles

- `Settings.profile` comes from `--profile` or else `TEMPORAL_PROFILE` (the make precedence:
  runtime env > `.env` > `local`). `TEMPORAL_CONFIG_FILE` defaults to
  `<repo>/config/temporal.toml`. The console runs with cwd = repo root (`make console`
  guarantees it), because cert paths in the toml are relative to the root. `__main__`
  `chdir`s to `git rev-parse --show-toplevel` defensively.
- Clients:
  - `agents`: `ClientConfig.load_client_connect_config(profile=…, config_file=…)` with
    namespace `agents`, plus `plugins=[support_agent.config.build_strands_plugin()]` for the
    pydantic converter (lazy, no AWS).
  - `agent-services`: the same connect config with namespace `agent-services`.
- **Non-local profiles**: the console removes `TEMPORAL_TLS` and `TEMPORAL_ADDRESS` from its
  own environment before loading envconfig, and from every child it spawns. This mirrors the
  Makefile's `unexport`; otherwise `.env` would silently turn mTLS off.
- **Profile switch at runtime** (`POST /api/profile`): close the clients, reconnect with the
  new profile, and refuse while managed processes started under another profile are running
  (the response lists them). Profiles all dial `127.0.0.1:7233`, so only one can be live.
  **Detection** suggests the right one: `.temporal/dev-server.pid` → local;
  `.temporal/kind-pf-*.pid` → kind or kind-plaintext (a plaintext `GetSystemInfo` that fails
  the handshake means mTLS); `.temporal/eks-pf-*.pid` → eks-pf. Every pidfile is checked with
  `kill -0`. A mismatch shows in the stack bar: "A kind port-forward is running, but the
  console is on profile local. Switch to kind."
- **Startup order**: the clients connect lazily, with 1 s → 10 s back-off. Until Temporal
  answers, the stack bar shows "Temporal is not reachable", plus a **Start dev server** button
  on local or the `make pf` command on kind/eks-pf. The page itself always loads.

### 4.3 Ledger 72: never query Support

A query on a `SupportSupervisorWorkflow` on `support-tq` makes the WCI start an AgentCore
session: a paid session on EKS, and an extra lifetime locally. With no poller, the query hangs
instead. The console therefore **never queries Support**. Support's state comes only from
`fetch_history` and `describe`: status, `pendingNexusOperations` (attempt,
lastAttemptFailure, scheduleToCloseTimeout), `versioning_info`, worker identities, Nexus
input/result payloads, and the result payload. `clients.AgentsClient` wraps the SDK client and
raises `SupportQueryForbidden` on any query in namespace `agents`. A unit test proves the run
view and every route are built without one.

Querying Finance is fine (Finance workers are always up): `get_task` with a 5 s RPC timeout.
On a timeout or with no Finance poller, the console falls back to the state inferred from the
A2A task's own history (timer started = `input-required`, signal, completion payload), labelled
`source: "history"`. (Describe does not carry "current details": the Web UI reads them with
the built-in `__temporal_workflow_metadata` query, which hangs the same way. See 5.7.)

### 4.4 What is derived where (no AWS)

| Signal | local | kind | eks-pf |
|---|---|---|---|
| Support rows | history (`demo_testing.history` + own row builder) | same | same |
| Finance rows and task state | history of `a2a-task-<wf>` + `get_task` | same | same |
| Lifetimes (compute rail) | WFT-completed / activity-started identities `…/agentcore-lifetime-N` from history, plus the managed AgentCore app's log markers (`lifetime N: connected`, `idle for`, `drained`, `async task released`) and `/ping` | identities + app log (the app is on the laptop) | identities + WCI InvokeWorker rows; CloudWatch sessions only with `CONSOLE_ALLOW_AWS=1` (`demo_testing.agentcore_logs`) |
| Plain worker "lifetimes" | identity changes in history (`<pid>@<host>`): a new pid = a restart (crash demo) | same | same |
| Drain | `/ping` HealthyBusy → Healthy, or the app log's `drained` | same | **estimated**: the last Support WFT + debounce (30 s) + 10 s margin, labelled "estimated"; CloudWatch confirm is opt-in (aws) |
| WCI invokes | emulator JSONL `.temporal/agentcore-emulator.jsonl` + the WCI system workflow | the WCI system workflow | the WCI system workflow (Temporal only, free) |
| WCI system workflow | `temporal-sys-worker-controller-instance:support-supervisor:<build>` in `agents`, read by ID with `fetch_history` and walked back through `continued_execution_run_id` (SDK; fallback: `scripts/wci-history.sh` subprocess) | same | same |
| Pollers | `DescribeTaskQueue` (workflow + activity) on support-dev-tq, support-tq, agent-services-tq, nexus-smoke-tq; entries are kept only if `last_access_time` is within 70 s | same | same |
| Build ID | `TEMPORAL_BUILD_ID` → the deployment's `currentVersion` build (SDK `describe_worker_deployment`) → `scripts/build-id.sh` → none | same | same |

**Build ID and uv.lock**: adding `services/demo-console` changes `uv.lock`, and
`scripts/build-id.sh` refuses while `uv.lock` is uncommitted. The console therefore always
passes `TEMPORAL_BUILD_ID` to children (resolved as above). When none can be resolved,
serverless actions are disabled with the reason: "No Build ID. Commit uv.lock, or start with
TEMPORAL_BUILD_ID=<id>." The content hash covers only support-agent's exports, so it stays
`4ee7f87` after commit.

### 4.5 Process control

- **Allowed to start or stop** (laptop processes only, never cluster or AWS resources):
  dev server (through `scripts/dev-server.sh start|stop` with `WCI=0|1`, never `aws`),
  `payments-mock`, `worker-finance[-scripted]`, `worker-support[-scripted]`,
  `worker-support-versioned`, `agentcore-local[-scripted]`, `agentcore-emulator`,
  `worker-smoke`. On kind and eks-pf only the laptop-side Support processes are offered
  (`agentcore-local*`, `worker-support*`; hybrid). This is a deliberate reading of "observe
  only": scenario 4 needs the laptop AgentCore app. Cluster lifecycle stays out except the
  confirm-first kind jobs in 2.3.
- **How**: each process is `make -s <target>` (so the Makefile stays the single source of truth
  for args and per-identity client certs, Makefile 452–470), spawned with
  `asyncio.create_subprocess_exec(..., start_new_session=True)`. It gets stdout/stderr to
  `.temporal/console/logs/<name>.log`, an env built by `Settings.child_env()` (profile,
  `TEMPORAL_BUILD_ID`, TLS vars stripped outside local), and a record at
  `.temporal/console/procs/<name>.json` holding `{pid, pgid, target, variant, profile,
  started_at}`. Stop is `SIGTERM` to the process group, then `SIGKILL` after 8 s. The crash
  demo uses `SIGKILL` immediately.
- **Restart-safe**: on startup the manager adopts every record whose pgid is alive
  (`os.killpg(pgid, 0)`) and drops the rest. A process started outside the console (another
  terminal) appears as `external`, detected from its port or its poller identity. The console
  shows it but cannot stop it.
- **Live variants** (`-scripted` dropped) cost Bedrock tokens. They are offered only when the
  model selector is "live", and a start needs `confirm: true`.
- **Jobs** (one-shots: nexus-endpoint, register-version, invoke-local, smoke, card, load,
  wci-history, failure, recipes, kind lifecycle) run the same way and are persisted at
  `.temporal/console/jobs/<id>.json` with a log. invoke-local is done in-process with httpx
  (POST `/invocations` with the session header, then GET `/ping`), since it is trivial.
- **Cost gate**: every action has `cost ∈ {free, tokens, aws}`. `tokens` requires
  `confirm: true`. `aws` additionally requires the console to be started with
  `CONSOLE_ALLOW_AWS=1`, otherwise it is `403 aws_disabled`. The UI shows the cost mark on the
  button and asks for confirmation.

### 4.6 Safety

- Binds `127.0.0.1` only. Requests whose `Host` header is not `127.0.0.1:<port>` or
  `localhost:<port>` are refused (DNS rebinding). Mutating requests require
  `Content-Type: application/json` and the header `X-Console: 1` (a cross-site form cannot send
  either), so another tab cannot drive the console.
- No secrets in responses: identities and paths only, with cert contents never read.
- Logs served by the API are tailed files under `.temporal/console/`. Paths are never taken
  from the request.

### 4.7 Running inside EKS (follow-up, not in scope)

The backend is container-ready: no laptop state is required beyond `.temporal/console/`.
The follow-up adds `services/demo-console/Dockerfile` (the finance-agent pattern: python
3.12.12-slim, uv 0.12.15, `uv sync --frozen --no-dev --package demo-console`, non-root),
`deploy/k8s/base/demo-console/` (Deployment + Service + NetworkPolicy in `temporal-workers`,
readiness `/healthz`, an operator client cert from cert-manager), an `eks` overlay with the
image tag set by make, and `CONSOLE_MODE=in-cluster`, which hides process control and reads
the frontend at `temporal-frontend.temporal-customer-support.internal:7233`. Access would go
through `kubectl port-forward` (no Ingress). This stays out of v1 because it needs a new
image, IAM review and an AWS deploy.

---

## 5. API contract

Base URL `http://127.0.0.1:8099`. JSON is UTF-8, times are RFC 3339 UTC with milliseconds,
and durations are seconds (float) unless the field name ends in `_text` (Temporal duration
strings like `"25h"`). Mutating requests need `Content-Type: application/json` and `X-Console: 1`.

### 5.0 Errors

Every non-2xx response:

```json
{ "error": { "code": "not_input_required",
             "message": "The A2A task is completed; approve and reject only apply to input-required.",
             "hint": "Open the run to see who decided." } }
```

| HTTP | code | When |
|---|---|---|
| 400 | `bad_request` | invalid JSON / missing field |
| 403 | `forbidden_origin` | Host or `X-Console` check failed |
| 403 | `aws_disabled` | `cost: aws` action without `CONSOLE_ALLOW_AWS=1` |
| 404 | `not_found` | unknown run / scenario / action / process / job |
| 409 | `not_input_required` | approve/reject outside input-required |
| 409 | `confirm_required` | `tokens`/`aws` action without `confirm: true` |
| 409 | `precondition_failed` | e.g. no Build ID, process already running, profile mismatch |
| 422 | `invalid_param` | e.g. empty approver, `n` outside 1..20 |
| 503 | `temporal_unreachable` | Temporal not reachable (reads that need it) |

### 5.1 Config and stack

`GET /healthz` → `{"status": "ok"}` (no Temporal call).

`GET /api/config`

```json
{ "profile": "local", "profiles": ["local", "kind", "kind-plaintext", "eks-pf"],
  "ui_base": "http://127.0.0.1:8233", "allow_aws": false, "console_version": "0.1.0",
  "debounce_s": 20, "auto_approve_limit_usd": 100, "default_approver": "dana.finance" }
```

`POST /api/profile` with body `{"profile": "kind"}` → `200` with the new `/api/config` body, or
`409 precondition_failed` with `{"error": {..., "processes": ["worker-support"]}}`.

`GET /api/stack` → **StackSnapshot**

```json
{
  "checked_at": "2026-11-02T14:03:55.120Z",
  "profile": { "configured": "local", "detected": "local", "mismatch": null, "tls": false },
  "shape": "serverless-wci",
  "model": { "mode": "scripted", "source": "managed" },
  "build_id": { "value": "4ee7f87", "source": "deployment" },
  "deployment": { "name": "support-supervisor", "current_build": "4ee7f87",
                  "compute": "local-wci", "behavior": "PINNED" },
  "wci": { "mode": "emulator", "invokes_total": 3 },
  "agentcore_app": { "status": "Healthy", "lifetime": 1, "url": "http://127.0.0.1:8080" },
  "components": [
    { "id": "temporal",  "label": "Temporal",        "status": "up",   "detail": "127.0.0.1:7233, server 1.32", "group": "temporal" },
    { "id": "web-ui",    "label": "Web UI",          "status": "up",   "detail": "http://127.0.0.1:8233", "group": "temporal" },
    { "id": "endpoint",  "label": "Nexus endpoint",  "status": "up",   "detail": "finance-agent → agent-services/agent-services-tq", "group": "temporal" },
    { "id": "finance",   "label": "Finance workers", "status": "up",   "detail": "1 poller", "count": 1, "group": "finance", "process": "worker-finance" },
    { "id": "support",   "label": "Support workers", "status": "zero", "detail": "0 on support-tq", "count": 0, "group": "support" },
    { "id": "agentcore", "label": "AgentCore app",   "status": "up",   "detail": "Healthy (idle)", "group": "support", "process": "agentcore-local" },
    { "id": "emulator",  "label": "WCI emulator",    "status": "up",   "detail": ":4599", "group": "support", "process": "agentcore-emulator" },
    { "id": "payments",  "label": "Payments",        "status": "up",   "detail": "4 refunds", "group": "finance", "process": "payments-mock" }
  ],
  "pollers": {
    "agents/support-dev-tq": [],
    "agents/support-tq": [],
    "agent-services/agent-services-tq": [ { "identity": "5120@laptop", "last_access": "2026-11-02T14:03:51.004Z" } ],
    "agents/nexus-smoke-tq": []
  },
  "port_forward": null
}
```

- `components[].status` ∈ `up | down | degraded | zero | unknown | external`. `zero` means
  up by design with nothing running (Support between lifetimes); it renders in `--off`, not red.
- `shape` ∈ `plain | serverless-invoke | serverless-wci | agentcore-aws | unknown`. It is
  derived from the current deployment compute config, the WCI mode, the pollers and the
  managed processes.
- `model.mode` ∈ `scripted | live | mixed | unknown`; `source` ∈ `managed | unknown`.
- `port_forward`: `null` on local, else `{"kind": "kind|eks", "frontend": true, "web": true}`.
- On eks-pf, `components` adds `{"id": "agentcore-aws", "status": "unknown", "detail":
  "not checked (AWS)"}` unless `allow_aws`.

### 5.2 Scenarios

`GET /api/scenarios` → `{"scenarios": [Scenario, ...]}` with preconditions evaluated now.

```json
{
  "id": "wci-b", "title": "Case B, the server's WCI starts the worker",
  "summary": "Finance waits for a human while Support's compute is gone; the server restarts it.",
  "story": { "tag": "v0.7-serverless-shape", "doc": "docs/story/v0.7-serverless-shape.md" },
  "profiles": ["local"], "fit": "ready",
  "cost": "free", "cost_note": "scripted model",
  "case": "B", "order_id": "2077", "amount_usd": 450.0,
  "options": [
    { "id": "a2a_mode", "label": "How Support asks Finance", "type": "choice", "choices": ["tool", "handoff"], "default": "tool" },
    { "id": "nexus_send_timeout", "label": "Support's budget for Finance's answer", "type": "duration", "default": "25h" },
    { "id": "workflow_id", "label": "Workflow ID", "type": "text", "default": "support-2077-wci-k3f9" }
  ],
  "preconditions": [
    { "id": "temporal", "label": "Temporal is up", "ok": true },
    { "id": "wci", "label": "Dev server runs with the WCI (WCI=1)", "ok": true },
    { "id": "emulator", "label": "WCI emulator on :4599", "ok": true },
    { "id": "agentcore", "label": "AgentCore app on :8080", "ok": true },
    { "id": "version", "label": "support-supervisor:4ee7f87 is current with local WCI compute", "ok": false,
      "fix": { "kind": "action", "action": "register-version", "params": { "mode": "--local-wci" }, "label": "Register version" } },
    { "id": "finance", "label": "A Finance worker polls agent-services-tq", "ok": true },
    { "id": "endpoint", "label": "Nexus endpoint finance-agent", "ok": true }
  ],
  "steps": [
    { "id": "start", "n": 1, "title": "Start Case B on support-tq", "kind": "action", "action": "start-run",
      "done_when": "run.started", "talk": "A customer asks for $450 back on a laptop.", "make": "make case-b-serverless WF=support-2077-wci-k3f9" },
    { "id": "invoke1", "n": 2, "title": "The WCI starts lifetime 1", "kind": "watch",
      "done_when": "compute.lifetimes>=1", "talk": "Nothing was polling. The server's Worker Controller invoked AgentCore." },
    { "id": "input-required", "n": 3, "title": "Finance asks a human", "kind": "watch",
      "done_when": "task.state==input-required", "talk": "Over $100, Finance's agent says 'I need a human'." },
    { "id": "drained", "n": 4, "title": "Support's compute drains", "kind": "watch",
      "done_when": "compute.drain==drained", "talk": "Support's worker is gone. Nothing runs and nothing is billed." },
    { "id": "approve", "n": 5, "title": "Approve the refund", "kind": "human",
      "done_when": "task.state in [completed, rejected]", "talk": "I approve as dana.finance." },
    { "id": "invoke2", "n": 6, "title": "The WCI starts lifetime 2", "kind": "watch",
      "done_when": "compute.lifetimes>=2", "talk": "Finance's answer landed in Support's history, so the server starts a worker." },
    { "id": "reply", "n": 7, "title": "Support replies with the refund ID", "kind": "watch",
      "done_when": "run.status==COMPLETED", "talk": "Same build, pinned: support-supervisor:4ee7f87." },
    { "id": "report", "n": 8, "title": "Read the report", "kind": "watch", "done_when": "run.closed",
      "talk": "Two lifetimes, one Build ID, reply seconds after the approval." }
  ],
  "notes": "Finance is waiting for a human, and Support's compute is gone. Nothing runs and nothing is billed, but the conversation is still there. I approve, and Temporal starts a fresh worker to finish the reply."
}
```

- `fit` ∈ `ready | needs_setup | wrong_profile`.
- `steps[].kind` ∈ `action | watch | human | manual`. `manual` steps carry `make` text only
  (e.g. eks-pf `make resume`) and can be ticked by hand (the tick is kept client-side only,
  in localStorage).
- `done_when` uses a closed vocabulary that the backend evaluates (5.4). The frontend never
  parses it; it reads `RunView.steps[].status`.
- `steps[].action` (with optional `steps[].params`) is one of: `start-run`; `approve` (the
  frontend focuses the approval card); `process.start` / `process.stop` with
  `params: {"name": "<process>", "signal": "TERM|KILL"}`; or any action id from 5.5, sent as
  `POST /api/actions/{id}` with `params`. The presenter-notes column shows it as the step's
  button.
- `preconditions[].fix.kind` is one of `action` (`{action, params, label}` → 5.5), `process`
  (`{process, label}` → `POST /api/processes/{process}/start`) or `command`
  (`{command, label}`: shown with a copy button, never run).
- Mutating bodies may carry `confirm: true` after the frontend's own cost confirmation
  (`POST /api/runs` included), so a `409 confirm_required` round trip is only a fallback.

### 5.3 Runs

`GET /api/runs?limit=25&page_token=` → recent Support conversations (visibility
`WorkflowType='SupportSupervisorWorkflow'`, newest first), plus load batches.

```json
{
  "runs": [
    { "workflow_id": "support-2077-wci-k3f9", "run_id": "0b1e…", "scenario": "wci-b", "case": "B",
      "status": "RUNNING", "task_state": "input-required", "task_state_source": "history",
      "task_queue": "support-tq", "build_id": "4ee7f87", "amount_usd": 450.0,
      "started_at": "2026-11-02T14:02:40.004Z", "closed_at": null, "group": null }
  ],
  "groups": [ { "id": "load-1793500000", "kind": "ping", "n": 10, "completed": 10, "failed": 0, "running": 0 } ],
  "next_page_token": null
}
```

The list's `task_state` is inferred from each running A2A task's history (no query;
`task_state_source: "history"`). Closed tasks are cached (`"cache"`).

`POST /api/runs`: start a conversation.

```json
{ "scenario": "case-b", "workflow_id": "support-2077-wci-k3f9", "queue": "versioned",
  "a2a_mode": "tool", "nexus_send_timeout": "25h", "ticket": null }
```

- `queue` ∈ `dev` (support-dev-tq) | `versioned` (support-tq). The default comes from the
  scenario: `case-*` uses `dev` unless the stack shape is serverless, and `serverless-b`,
  `wci-b` and `cloud-b` use `versioned`. `ticket: null` uses the case default (README text).
- The start uses `SupportSupervisorWorkflow.run` args `[ticket, a2a_mode, nexus_send_timeout]`
  and `id_conflict_policy=USE_EXISTING`, like case-a.sh. It never waits for the result.
- `201` →
  ```json
  { "workflow_id": "support-2077-wci-k3f9", "run_id": "0b1e…", "attached": false,
    "links": { "support": "http://127.0.0.1:8233/namespaces/agents/workflows/support-2077-wci-k3f9",
               "finance": "http://127.0.0.1:8233/namespaces/agent-services/workflows/a2a-task-support-2077-wci-k3f9" } }
  ```
  `attached: true` when an open run with that ID already existed.
- Cost: `tokens` if the stack's model mode is `live` or `unknown` on a non-scripted shape;
  `aws` on eks-pf with `queue: versioned` (starts an AgentCore session).

`GET /api/runs/{wf}` → **RunView**

```json
{
  "workflow_id": "support-2077-wci-k3f9", "run_id": "0b1e…",
  "scenario": "wci-b", "case": "B", "order_id": "2077", "amount_usd": 450.0,
  "ticket": "Order #2077: my laptop arrived with a cracked screen. I want a refund.",
  "a2a_mode": "tool", "nexus_send_timeout_text": "25h", "task_queue": "support-tq",
  "status": "RUNNING", "started_at": "2026-11-02T14:02:40.004Z", "closed_at": null,
  "versioning": { "deployment": "support-supervisor", "build_id": "4ee7f87", "behavior": "PINNED" },
  "support": {
    "pending_nexus": [ { "operation": "send_message", "state": "STARTED", "attempt": 1,
                         "last_failure": null, "schedule_to_close_s": 90000,
                         "scheduled_at": "2026-11-02T14:03:03.074Z" } ],
    "agent_card_discovered": true
  },
  "task": {
    "id": "a2a-task-support-2077-wci-k3f9", "state": "input-required", "source": "query",
    "message": "Refund of $450.00 for order 2077 needs a human: over the $100 auto-approve limit. Risk LOW.",
    "updated_at": "2026-11-02T14:03:14.550Z",
    "approval": { "timeout_text": "24h", "deadline": "2026-11-03T14:03:14.550Z" },
    "artifact": null
  },
  "compute": {
    "running": false,
    "drain": { "state": "drained", "at": "2026-11-02T14:03:35.060Z", "estimated": false, "source": "app-log" },
    "lifetimes": [
      { "n": 1, "label": "lifetime 1", "identity": "4711@laptop/agentcore-lifetime-1",
        "start": "2026-11-02T14:02:44.840Z", "end": "2026-11-02T14:03:35.060Z",
        "end_reason": "drained", "workflow_tasks": 6, "activities": 9, "source": "history+app-log" }
    ]
  },
  "wci": { "invokes": 1, "rows_source": "wci-history" },
  "reply": null,
  "metrics": { "held_open_s": 52.4, "approval_wait_s": null, "approval_to_reply_s": null,
               "lifetimes": 1, "build_ids": ["4ee7f87"], "q15": null },
  "steps": [ { "id": "start", "status": "done" }, { "id": "invoke1", "status": "done" },
             { "id": "input-required", "status": "done" }, { "id": "drained", "status": "done" },
             { "id": "approve", "status": "current" }, { "id": "invoke2", "status": "todo" },
             { "id": "reply", "status": "todo" }, { "id": "report", "status": "todo" } ],
  "links": {
    "support": "http://127.0.0.1:8233/namespaces/agents/workflows/support-2077-wci-k3f9/0b1e…/history",
    "finance": "http://127.0.0.1:8233/namespaces/agent-services/workflows/a2a-task-support-2077-wci-k3f9",
    "nexus_endpoints": "http://127.0.0.1:8233/nexus"
  },
  "rows_version": 31
}
```

- After close: `reply` is `{"text": "...", "refund_id": "R-10015"}` (the refund ID comes
  from the Finance artifact); `task.artifact` is `{"refund_id", "order_id", "amount_usd",
  "approved_by", "risk", "handled_by"}`; `metrics.q15` is
  `{"task_add_signal_s": 0.03, "invoke_worker_s": 0.06, "first_wft_s": 5.7, "reply_s": 11.1}`
  (relative to `send_message COMPLETED`, serverless only).
- `scenario` is inferred: the ticket's order (1042/2077) gives the case, the queue and stack
  shape give the scenario. The `cleanup` scenario is never inferred.
- `steps[].status` ∈ `done | current | todo | skipped | blocked`.

`GET /api/runs/{wf}/rows?after=<seq>` → **lane rows** (all rows when `after` is omitted)

```json
{
  "workflow_id": "support-2077-wci-k3f9", "t0": "2026-11-02T14:02:40.004Z", "rows_version": 31,
  "rows": [
    { "seq": 1,  "at": "2026-11-02T14:02:40.004Z", "t": 0.0,  "lane": "support", "kind": "run.started",
      "title": "Ticket received", "detail": "Order #2077: my laptop arrived with a cracked screen…",
      "event_id": 1, "link": "http://127.0.0.1:8233/namespaces/agents/workflows/support-2077-wci-k3f9" },
    { "seq": 2,  "at": "…", "t": 0.03, "lane": "wci", "kind": "wci.invoke", "title": "Server invokes AgentCore",
      "detail": "InvokeWorker (WCI)", "source": "wci-history" },
    { "seq": 3,  "at": "…", "t": 4.84, "lane": "compute", "kind": "lifetime.start", "title": "Lifetime 1 starts",
      "lifetime": 1, "identity": "4711@laptop/agentcore-lifetime-1", "source": "app-log" },
    { "seq": 7,  "at": "…", "t": 5.34, "lane": "support", "kind": "model.turn", "title": "Support Supervisor thinks",
      "agent": "Support Supervisor", "identity": "4711@laptop/agentcore-lifetime-1", "event_id": 11 },
    { "seq": 14, "at": "…", "t": 23.07, "lane": "nexus", "kind": "nexus.scheduled", "title": "Asks Finance for a refund",
      "detail": "send_message (refund order 2077 $450) via ask_finance_agent tool", "pair": "nx-2", "from": "support", "to": "finance" },
    { "seq": 15, "at": "…", "t": 23.09, "lane": "finance", "kind": "task.started", "title": "A2A task started",
      "pair": "nx-2", "link": "http://127.0.0.1:8233/namespaces/agent-services/workflows/a2a-task-support-2077-wci-k3f9" },
    { "seq": 21, "at": "…", "t": 34.55, "lane": "finance", "kind": "task.state", "title": "Finance asks a human",
      "state": "input-required", "detail": "Approval timer started (24h)" }
  ]
}
```

- `lane` ∈ `compute | support | nexus | finance | wci | human`. (`nexus` rows render on the
  seam; `wci` rows render on the rail edge; `human` rows are signals and render in Finance's
  lane with the human glyph.)
- `kind` vocabulary:
  `run.started, run.completed, run.failed, run.terminated, model.turn, activity, wft`
  (shown only when its identity differs from the previous one),
  `nexus.scheduled, nexus.started, nexus.attempt_failed, nexus.completed, nexus.failed`,
  `task.started, task.state, timer.started, timer.fired, signal.approve, signal.reject`,
  `refund.issued, lifetime.start, lifetime.connected, lifetime.idle, lifetime.drained,
  lifetime.released, lifetime.killed, wci.signal, wci.invoke, wci.failed, session.start`
  (CloudWatch, opt-in).
- `pair` links the two ends of a seam crossing: `nexus.scheduled` ↔ `task.started` (out), and
  `task.state completed|rejected|failed` ↔ `nexus.completed|nexus.failed` (back). Discovery
  (`get_agent_card`, sync) is one pair with both ends on the seam.
- `seq` is stable for a given run (deterministic ordering by `at`, then lane order). New
  sources that arrive late (app log, WCI) can insert rows with a lower `at`. In that case
  `rows_version` changes and the client refetches without `after`.
- Gap compression is a client concern: a gap > 4 s between consecutive rows renders as a band
  showing the duration and what was running ("46 s with no Support compute" when
  `compute.running` was false across it).

`POST /api/runs/{wf}/approve` and `POST /api/runs/{wf}/reject`

```json
{ "approver": "dana.finance", "comment": "photos checked" }
```

- `{wf}` may be the Support ID or `a2a-task-…`. Steps: query `get_task` → if not
  `input-required`, `409 not_input_required` (with the current state in `message`); else
  signal `approve|reject` with `{approver, comment}` → poll `get_task` up to 10 s.
- An empty approver gives `422`. The default approver comes from `/api/config`.
- `200` →
  `{"workflow_id": "...", "task_id": "a2a-task-...", "verdict": "approve",
    "state_before": "input-required", "state_after": "completed", "settled": true}`.
  `settled: false` if it is still `working` after 10 s; the stream carries the rest.
- No AWS involved on any profile (signals go to Finance), so the cost is `free`.

`POST /api/runs/{wf}/terminate` with body `{"reason": "console cleanup", "confirm": true}`
→ `200 {"terminated": true}` (used by the `cleanup` scenario).

`GET /api/runs/{wf}/report` → the approval + serverless report as data (the
`approval_report.py` and `serverless_report.py` logic, reimplemented on the history helpers):

```json
{ "send_message": { "scheduled": "…", "started": "…", "ended": "…", "budget_s": 90000, "open_s": 52.4 },
  "support_during_wait": { "workflow_tasks": 0, "longest_silence_s": 50.1, "identities": 2, "same_worker_after": false },
  "finance": { "timer_started": "…", "timer": "canceled", "signal": { "verdict": "approve", "approver": "dana.finance", "comment": "photos checked" }, "final_state": "completed" },
  "lifetimes": [ { "identity": "…/agentcore-lifetime-1", "first": "…", "last": "…", "wft": 6, "activities": 9 },
                 { "identity": "…/agentcore-lifetime-2", "first": "…", "last": "…", "wft": 2, "activities": 1 } ],
  "build_ids": ["4ee7f87"], "behaviors": ["PINNED"], "q15": { "...": "see RunView.metrics.q15" } }
```

### 5.4 Step predicates (backend vocabulary)

`run.started`, `run.closed`, `run.status==<STATUS>`, `task.state==<s>`,
`task.state in [<s>, ...]`, `compute.lifetimes>=<n>`, `compute.drain==drained`,
`compute.running==true|false`, `nexus.attempt>=<n>`, `nexus.completed`, `card.discovered`,
`process.<name>==running|stopped`, `wci.invokes>=<n>`. Unknown predicates are a startup error
(a unit test checks every catalog step parses). A step's status is `done` if its predicate holds,
`current` for the first step that is not done, and `todo` after it. `blocked` means the
current step's action is unavailable (with `why` in the step), and `skipped` marks an
optional step passed over (e.g. "stop the Support worker").

### 5.5 Actions, processes, jobs

`GET /api/actions` → `{"actions": [Action, ...]}`

```json
{ "id": "register-version", "label": "Register Support's version", "cost": "free",
  "profiles": ["local", "kind"], "available": true, "why_not": null,
  "params": [ { "id": "mode", "type": "choice", "choices": ["--poller", "--local-wci"], "default": "--local-wci" } ],
  "make": "make register-version MODE=--local-wci", "long": false }
```

Action ids: `start-recipe` (`recipe` = `local-plain | local-invoke | local-wci | kind-laptop |
teardown`), `nexus-endpoint`, `register-version`, `invoke-local`, `agentcore-ping`, `smoke`,
`card`, `load` (`n` 1..20, `kind` = `ping|case-a`), `wci-history`, `failure`
(eks-pf, `aws`), `drain-cloudwatch` (eks-pf, `aws`), `kind-up`, `kind-temporal`,
`kind-workers`, `certs-pull`, `pf`, `pf-stop`, `kind-down` (long, confirm).

`POST /api/actions/{id}` with body `{"params": {"mode": "--local-wci"}, "confirm": false}` →
`202` **Job**:

```json
{ "id": "j-20261102-140301-register-version", "action": "register-version", "status": "running",
  "command": "make -s register-version MODE=--local-wci", "cost": "free",
  "started_at": "2026-11-02T14:03:01.000Z", "ended_at": null, "exit_code": null,
  "log_tail": ["worker deployment support-supervisor exists", "create-version support-supervisor:4ee7f87 …"],
  "result": null }
```

`invoke-local` and `agentcore-ping` are synchronous (`200`) with
`result: {"message": "worker starting", "lifetime": 2, "ping": "HealthyBusy"}`.

`GET /api/jobs?limit=20` → `{"jobs": [Job, ...]}`; `GET /api/jobs/{id}` → Job (log tail of 200 lines).

`GET /api/processes` → `{"processes": [Process, ...]}`

```json
{ "name": "agentcore-local", "label": "Local AgentCore app", "variant": "scripted",
  "variants": ["scripted", "live"], "status": "running", "pid": 4711, "started_at": "…",
  "exit_code": null, "make": "make agentcore-local-scripted", "cost": "free",
  "ports": [8080], "profiles": ["local", "kind"], "needs": ["build_id"], "managed": true }
```

- `status` ∈ `running | stopped | exited | external | unavailable`; `unavailable` comes with
  a `why`.
- Names: `dev-server`, `payments-mock`, `worker-finance`, `worker-support`,
  `worker-support-versioned`, `agentcore-local`, `agentcore-emulator`, `worker-smoke`.

`POST /api/processes/{name}/start` with body `{"variant": "scripted", "wci": 1, "confirm": false}`
(`wci` applies to `dev-server` only) → `200` Process.

`POST /api/processes/{name}/stop` with body `{"signal": "TERM"}` (`"KILL"` is the crash demo) →
`200` Process.

`GET /api/processes/{name}/log?tail=200` → `{"name": "...", "lines": ["..."]}` (`text/plain`
with `?raw=1`).

### 5.6 SSE stream

`GET /api/stream?wf=<workflow_id>` (`wf` is optional). The response is `text/event-stream`, with
`retry: 2000` and a comment heartbeat (`: ping`) every 15 s. Every event has an `id` (a
process-local counter) and JSON `data`. The client renders whole snapshots and keeps no event
log.

| event | data | When |
|---|---|---|
| `hello` | `/api/config` body | on connect |
| `stack` | StackSnapshot | on connect, then on change (polled every 3 s) |
| `runs` | `/api/runs` body (first page) | on connect, then on change (3 s) |
| `run` | RunView of `wf` | on connect if `wf`, then on change (1 s) |
| `rows` | `{"workflow_id", "rows_version", "reset": bool, "rows": [Row]}` | new rows (`reset: true` = replace all) |
| `job` | Job | on job start, on each new log line (≤ 4/s), on end |
| `process` | Process | on start / stop / exit / adoption |
| `alert` | `{"level": "info|warn|error", "message": "...", "hint": "..."}` | e.g. Temporal unreachable, profile mismatch, process exited non-zero |

The client changes focus by reopening the stream with a new `wf`, which is cheap.

### 5.7 Backend notes and contract deltas (as implemented)

The backend (`services/demo-console`, everything except `static/`) follows 5.0 to 5.6, with
these additions and clarifications:

- **Static files** are served at `/` (`index.html`, `styles.css`, `app.js`, `js/*`,
  `fixtures/*`); `/api/*` and `/healthz` take precedence. Without `static/index.html` the
  backend serves a small placeholder page.
- **A2A task source**: `task.source` and `task_state_source` are `query` (Finance's
  `get_task`, 3 s RPC timeout), `history` (inferred from the task's history) or `cache`
  (list only, closed tasks). There is no `details` source (see 4.3).
- **`POST /api/runs`** also takes `confirm` (cost gate: `tokens` with a live/mixed model,
  `tokens` on eks-pf with `queue: dev` because Finance on EKS uses Bedrock, `aws` on eks-pf
  with `queue: versioned`) and validates `nexus_send_timeout` (`25h`, `90m`, `30s`) and the
  workflow ID (`[A-Za-z0-9][A-Za-z0-9._:-]*`, never `a2a-task-…`). Scenario `options` also
  carry `queue` (`dev|versioned`) and, for Case B scenarios, `approver`. A run the console
  starts is recorded in `.temporal/console/runs/<wf>.json`, so its scenario (e.g. `crash`,
  `failure`) survives restarts; other runs are inferred.
- **Approve/reject without a Finance poller**: the state check falls back to history, so a
  signal can still be sent (the failure demo); the response then has `settled: false`.
- **`POST /api/runs/{wf}/terminate`** also terminates the running A2A task and answers
  `{"terminated": true, "finance_terminated": bool}`.
- **Step predicates** add `job.<action>==<status>` (the latest job of that action, for the
  run-less `smoke` and `load` scenarios). Steps may carry `optional: true`. Status rule: the
  last non-optional step whose predicate holds is the frontier; every non-optional step up to
  it is `done` (a later fact implies the earlier ones) and optional ones are `done` or
  `skipped`; after it, steps are `done` in order while they hold, then one `current`
  (`blocked` + `why` when its action cannot run now: approve outside `input-required`,
  invoke-local without a Build ID or app, a process that is unavailable or external), then
  `todo`. A step seen `done` stays done for that run (sticky), so "stop the Support worker"
  stays ticked after the worker is started again.
- **RunView `compute`**: `drain.state` ∈ `running | drained | unknown`; `drain.source` ∈
  `process | pollers` (plain worker), `ping | app-log | no-app` (local/kind AgentCore app),
  `estimate | cloudwatch` (eks-pf). `lifetimes[].end_reason` ∈ `drained | killed | stopped |
  ended | null`. A lifetime can start before `t0` (the app was invoked before the run), so
  rows can have a negative `t`.
- **`rows_version`** increases on every change of the row list; the SSE `rows` event says
  `reset: true` when rows were inserted or changed (not only appended).
- **Lane rows** add `nexus.started` ("Finance accepted the task", seam) and use `operation`
  on nexus rows; `nexus.attempt_failed` rows come from describe (`lastAttemptFailure`), not
  history. `signal.*` rows carry `approver` and `effective` (false for a decision Finance
  ignored).
- **StackSnapshot**: `components[].status` is `external` for a laptop process that is up but
  not started by the console (including Temporal itself on local); a top-level
  `temporal_error` is present while Temporal is down. Pollers of this laptop
  (`<pid>@<host>`) whose pid is gone are dropped at once (the server keeps listing them for
  about a minute), so a stopped worker shows as stopped.
- **Process status after a stop**: a process the console stopped (TERM, or KILL in the crash
  demo) is `stopped`, with its `exit_code`; `exited` means it ended on its own.
- **Support pollers after a drain**: once the local AgentCore app's `/ping` is `Healthy`, this
  laptop's `…/agentcore-lifetime-N` pollers are dropped from `pollers` and the Support count
  (the app process outlives each lifetime, so the server would list them for a minute).
- **Child env**: `MAKEFLAGS`, `MFLAGS`, `MAKELEVEL` and `MAKEOVERRIDES` are removed, so the
  variables given to `make console` never override the profile the console switched to.
- **Process**: `dev-server` adds `wci` (`"0"`, `"1"`, `"aws"` or `null`); `unavailable` comes
  with `why` (wrong profile, no Build ID, or no client certificate under `.certs/…`).
- **Job**: `status` ∈ `running | succeeded | failed`. `start-recipe` takes
  `params: {recipe, variant: "scripted"|"live"}` (live = `tokens`). On eks-pf, `pf`,
  `pf-stop`, `certs-pull` and `load` are `aws` (kubectl to EKS needs an AWS token;
  `load.sh` reads CloudWatch there), and `register-version` defaults to `--agentcore-auto`
  (`aws`).
- **Errors** may carry extra fields next to `code/message/hint` (e.g. `processes` on a
  refused profile switch).
- **Tests**: the fakes are in `demo_console.fakes` (pytest's `--import-mode=importlib` cannot
  import helpers from a `conftest.py`); `tests/conftest.py` only holds fixtures.

---

## 6. Test plan

All tests stay offline and run under `make check` (markers `unit` and `integration` only; no
new markers).

### 6.1 Unit (`pytestmark = pytest.mark.unit`)

Fakes live in `services/demo-console/tests/conftest.py`: `FakeAgents` / `FakeAgentServices`
(canned `WorkflowHistory` objects loaded from small JSON fixtures, describe responses, a
`get_task` result, and a record of every call), `FakeRunner` (records argv/env, returns
scripted stdout/exit codes), `FakeHttp` (ping/invocations), and a tmp `.temporal/console`.

| Test file | Covers |
|---|---|
| `test_rows.py` | Support/Finance/WCI/lifetime row builders on recorded histories (Case A, B approve, B reject, crash with two pids, failure with attempt 3); pairs across the seam; stable `seq` |
| `test_runview.py` | RunView fields from history + describe; scenario inference; `held_open_s`; Q15 deltas; reply + refund ID; `get_task` timeout → `source: "history"` |
| `test_ledger72.py` | building every RunView/route against `FakeAgents` issues **zero** queries in namespace `agents`; `AgentsClient.query` raises `SupportQueryForbidden` |
| `test_steps.py` | every catalog `done_when` parses; statuses done/current/todo for each fixture |
| `test_stack.py` | component statuses from fake pollers/pidfiles/ports; `zero` vs `down`; shape and model inference; Build ID resolution order (env → deployment → build-id.sh refusing on dirty uv.lock → none) |
| `test_detect.py` | profile detection from pidfiles (tmp dir, `kill -0` faked) and the TLS probe outcome |
| `test_env.py` | `child_env()` strips `TEMPORAL_TLS`/`TEMPORAL_ADDRESS` outside local, sets `TEMPORAL_PROFILE`, `TEMPORAL_CONFIG_FILE` (absolute), `TEMPORAL_BUILD_ID` |
| `test_procs.py` | start spawns `make -s <target>` in a new session; record written; adoption after "restart"; stop TERM→KILL; `external` detection; live variant needs confirm |
| `test_actions.py` | cost gate (`confirm_required`, `aws_disabled`); eks-pf `failure`/`drain-cloudwatch` build the right argv **with FakeRunner only**; `register-version` argv per mode; recipes order |
| `test_cloud.py` | `agentcore_logs` adapter with faked `sessions_for`/`wait_drained` (no boto3 client created) |
| `test_routes.py` | Starlette `TestClient`: Host check, `X-Console` check, error shapes, approve 409/422/200 paths, `POST /api/runs` arguments match case-a.sh (ticket, mode, budget, `USE_EXISTING`) |
| `test_stream.py` | the SSE generator emits `hello`, `stack`, `run`, `rows` and only on change (hash); heartbeat |
| `test_static.py` | `index.html` references only local assets (fonts are vendored); every JS module imported exists; no `localhost` |

### 6.2 Integration (`pytestmark = pytest.mark.integration`)

`test_console_integration.py` uses the existing session fixtures `two_namespace_env`,
`agents_client` and `agent_services_client` (dev server via `start_local`; no AWS). It runs
the scripted Support and Finance workers **in-process** with `demo_testing.scripted_model` /
`support_script` / `finance_script` (the pattern of the existing integration tests) and
creates the endpoint with `demo_testing.nexus`. The console app is built with
`create_app(clients=…)` and driven through `httpx.ASGITransport`:

1. `POST /api/runs` Case A → poll `GET /api/runs/{wf}` until `COMPLETED`; assert rows contain
   a `nexus.scheduled`/`task.started` pair, `task.artifact.approved_by == "auto"`, the reply
   quotes the refund ID.
2. Case B → wait for `task.state == "input-required"` → `POST approve` (`dana.finance`) →
   `state_after == "completed"` → run completes; `report.finance.signal.approver` matches.
3. Case B reject → `rejected`; the reply apologises (scripted text).
4. `approve` on a completed task → `409 not_input_required`.
5. Throughout: the spy on the agents client recorded zero queries.

### 6.3 Visual check

- `scripts/console-shot.sh <url-path> <out.png> [width] [height] [--full] [--click SEL]`
  (new; the existing `ui-screenshot.sh` pre-checks the Temporal UI) runs
  `scripts/console_shot.py`, which drives headless Chrome over the DevTools protocol: set the
  viewport (a true 390 px phone width), load, wait, optionally click, capture. (A plain
  `--headless=new --screenshot` with `--virtual-time-budget` captured the page before the ES
  modules had rendered.) `--all` serves `static/` itself on a free port, so it needs neither
  the console nor Temporal.
- **Fixture mode** for deterministic shots: `index.html?fixture=case-b-input-required` makes
  `app.js` read `static/fixtures/<name>.json` (StackSnapshot + RunView + rows) instead of the
  API and stream. It needs no Temporal, and presenters can use it to rehearse offline.
  Fixtures: `case-a-completed`, `case-b-input-required`, `case-b-completed-serverless`,
  `failure-retrying`, `empty-stack`, plus `case-b-replay`. A fixture file is
  `{fixture, description, now, focus, route?, config, stack, runs, scenarios, actions,
  processes, jobs, run, rows, report, extra_runs: {wf: {run, rows}}, replay?}`, each value in
  its API shape. `replay` is `{frames: [{t, rows?, run?, stack?, runs?}], gate: {after, on,
  recorded_at}, branches: {approve: [...], reject: [...]}, reports: {...}}`: frames play at
  their offset `t` (seconds), pause at the gate, and Approve/Reject plays that branch.
  Every timestamp is shifted so the recording's `now` is the page's now.
  `index.html?demo=1` replays `case-b-replay` live from the start (`&at=gate` jumps to the
  approval, `&at=end&verdict=approve|reject` to the end). A "Recorded data" badge in the
  stack bar marks both modes. In `case-b-input-required` the Approve and Reject buttons also
  play the recorded rest of the run.
- `make console-shots` writes 1440×900 and 390×844, in dark (the default) and light
  (`&theme=light`), for each fixture into `.temporal/console/shots/`. Review is manual (look at
  them); no pixel diffing in CI. `docs/console/mockup.html` is the reference the shots should
  resemble.
- Accessibility pass on each shot set: focus ring visible on Approve/Reject (keyboard-only run
  through), reduced-motion run (`--force-prefers-reduced-motion`), and contrast spot checks of
  `--muted` on `--panel` in both themes.

### 6.4 Manual acceptance (by the user, after `make resume` for eks-pf)

Run each scenario from the console only (no terminal except `make console`) on: local plain
scripted; local invoke-local scripted; local WCI scripted; kind scripted; eks-pf `cloud-b`
(with `CONSOLE_ALLOW_AWS=1` for the CloudWatch confirm and the failure demo). Restart the
console mid-Case-B; the run view, steps and managed processes come back.

---

## 7. Files and make targets

```
services/demo-console/
  pyproject.toml                 demo-console 0.1.0; deps: starlette>=1.10, uvicorn>=0.54, httpx>=0.27,
                                 temporalio>=1.34.0,<2, support-agent, finance-a2a-api, demo-testing
                                 (workspace sources); hatchling; packages=["src/demo_console"]
  src/demo_console/
    __init__.py
    __main__.py                  python -m demo_console [--host 127.0.0.1] [--port 8099] [--profile P]
    settings.py                  Settings (repo root, profile, ui_base, allow_aws, debounce), child_env()
    app.py                       create_app(settings, clients=None, runner=None) -> Starlette; static mount
    routes.py                    handlers for section 5; error helper; Host/X-Console middleware
    clients.py                   Clients (agents w/ strands converter, agent-services), lazy connect,
                                 AgentsClient guard (SupportQueryForbidden)
    detect.py                    profile detection (pidfiles, kill -0, TLS probe)
    stack.py                     StackProbe -> StackSnapshot (pollers, endpoint, deployment, ports, ping)
    rows.py                      Row model + builders: support, finance, nexus pairs, wci, lifetimes
    runs.py                      list_runs, run_view, report (approval + serverless + q15)
    lifetimes.py                 identities -> lifetimes; app-log parser; emulator jsonl; WCI chain walk
    scenarios.py                 catalog loader + predicate evaluator
    data/scenarios.toml          scenarios, steps, talk tracks, make equivalents
    actions.py                   action registry, cost gate, recipes
    procs.py                     ProcessManager (make targets, process groups, records, adoption)
    jobs.py                      JobStore (.temporal/console/jobs)
    runner.py                    CommandRunner protocol + asyncio implementation
    cloud.py                     opt-in AWS adapters (agentcore_logs, eks failure) behind allow_aws
    stream.py                    SSE hub: pollers, change hashing, heartbeat
    static/
      index.html                 shell + SVG sprite + font preload
      styles.css                 tokens (section 3.2, BRAND.md), layout, lanes, motion
      vendor/fonts/              Hanken Grotesk + IBM Plex Mono woff2, OFL texts, README (3.3)
      app.js                     entry: router (#/runs/<wf>, #/notes), stream, store, fixture mode
      js/api.js  js/store.js  js/stackbar.js  js/launcher.js  js/runs.js  js/runview.js
      js/lanes.js (rows, gaps, rail, seam connectors)  js/approval.js  js/steps.js
      js/notes.js  js/sheet.js (processes, jobs, recipes)  js/format.js
      fixtures/*.json            fixture-mode data (6.3)
  tests/                         section 6 (no __init__.py), conftest.py with fakes, fixtures/*.json
scripts/console-shot.sh          headless Chrome screenshot of a console URL
scripts/console_shot.py          its DevTools-protocol driver (websockets, already in uv.lock)
```

Root `pyproject.toml`: add `"demo-console"` to dependencies and `demo-console = { workspace =
true }` to sources, and `demo_console` to ruff `known-first-party`. Run `uv lock --offline`
(no new PyPI packages). `docs/versions.md`: add starlette and uvicorn as direct deps.

Makefile (new section `# --- v1.0: demo console ---`, all in `.PHONY`):

```make
console: ## Demo console on http://127.0.0.1:8099 (TEMPORAL_PROFILE=local|kind|kind-plaintext|eks-pf; CONSOLE_ALLOW_AWS=1 enables AWS-backed actions)
	$(UV) python -m demo_console --port $(or $(CONSOLE_PORT),8099)

console-shots: ## Screenshots of the console's fixture views (light/dark, laptop/phone) -> .temporal/console/shots/
	scripts/console-shot.sh --all
```

Usage: `make console`, `TEMPORAL_PROFILE=kind make console`,
`TEMPORAL_PROFILE=eks-pf CONSOLE_ALLOW_AWS=1 make console`. The Makefile's per-profile
`unexport` already applies to the console process.

---

## 8. Follow-ups (out of scope)

- Running the console in EKS (4.7).
- Event-level deep links into the Temporal UI (`…/history/events/<id>`), once verified on UI 2.x
  for server 1.32.
- `make up` / `demo-reset` (plan 3.4) can call the console's recipes instead of duplicating them.
- A CloudWatch session lane on eks-pf by default (today opt-in because it calls AWS).
