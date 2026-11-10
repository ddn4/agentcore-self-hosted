// The run view: a one-line header (ticket, state track and word, held-open clock, deep links),
// a quiet line with the task's message and "Run details", the lanes, and the report once the
// run closes (DESIGN 2.1). The scaffold is built once per focused run; parts update.
import { updateApproval } from "./approval.js";
import { TERMINAL, dur, h, icon, sinceText } from "./format.js";
import { lanesKey, renderLanes } from "./lanes.js";
import { mergedSteps, renderSteps } from "./steps.js";

let mounted = null;   // workflow id the scaffold belongs to
let lastKey = null;
let lastState = undefined;
let autoScrollAt = 0;
let lastTarget = 0;

const PEEK = 52; // px of the newest row that always stays in view (its title line)

/**
 * Where the lanes should rest. The Nexus hop is the point of the page, so the newest hop row
 * (send_message out, its answer back, or a failed attempt) stays at the top with as much
 * after it as fits, rather than plain tail-following. The approval card, while the task is
 * input-required, always shows in full; otherwise the newest row (the reply, once finished)
 * keeps at least its title line in view, and wins over the hop only when both can't fit.
 * Before the first hop: the newest row.
 */
export function followTarget(lanes) {
  const head = lanes.querySelector(".lhead");
  const hh = head ? head.offsetHeight : 0;
  const view = lanes.clientHeight;
  const bottom = Math.max(0, lanes.scrollHeight - view);
  const now = lanes.querySelector(".row.now");
  const card = now && now.querySelector(".approval");
  if (card && now.offsetHeight > view - hh) return now.offsetTop - hh;
  const hops = lanes.querySelectorAll(".row.cross.out, .row.cross.back, .row.stub");
  const hop = hops[hops.length - 1];
  if (!hop) return bottom;
  let target = Math.max(0, hop.offsetTop - hh - 6);
  // Finished: the reply row (drain rows after it are the tail); running: the newest row.
  const rows = lanes.querySelectorAll("#rows > .row"); // folded members are not the newest row
  const newest = (!now && lanes.querySelector(".row.end")) || rows[rows.length - 1];
  if (card) target = Math.max(target, now.offsetTop + now.offsetHeight - view);
  else if (newest) target = Math.max(target, newest.offsetTop + Math.min(newest.offsetHeight, PEEK) - view);
  return Math.min(bottom, target);
}

function follow(lanes) {
  if (lanes.scrollHeight <= lanes.clientHeight) return;
  autoScrollAt = Date.now();
  lastTarget = followTarget(lanes);
  lanes.scrollTop = lastTarget;
}

function scaffold(el, wf) {
  // One line over the lanes (ticket, the state track with the current state as its word, the
  // held-open clock, deep links) and one quiet line under it, so the lanes get about 85% of a
  // 900 px screen: they are the hero (the hop, the held call, the compute rail).
  el.innerHTML = String(h`<div class="run">
    <div class="runhead" id="rv-head">
      <p class="ticket" id="rv-ticket"><span class="wf id">${wf}</span></p>
      <div class="state" role="group" aria-label="A2A task state" id="rv-hero">
        <ol class="dots" id="rv-before" aria-hidden="true"></ol>
        <p class="statebig" id="rv-state" data-s="none" aria-live="polite">loading</p>
        <ol class="dots" id="rv-after" aria-hidden="true"></ol>
        <span class="sr" id="rv-track-sr"></span>
      </div>
      <dl class="facts" id="rv-facts"></dl>
      <div class="links" id="rv-links"></div>
    </div>
    <div class="subline">
      <p class="statesub" id="rv-sub"></p>
      <div id="rv-report"></div>
      <details class="rundetails" id="rv-details"><summary>Run details</summary><div class="meta" id="rv-meta"></div></details>
    </div>
    <ol class="steps" id="rv-steps" aria-label="Guided steps"></ol>
    <section class="lanes" id="lanes" aria-label="Both workflows over time">
      <div class="lhead" aria-hidden="true">
        <div></div><div class="railh">compute</div>
        <div class="ln sup"><b>${icon("headset")}Support</b><span>namespace <span class="id">agents</span></span></div>
        <div class="seamh"><b>Nexus</b><span class="id">finance-agent</span></div>
        <div class="ln fin"><b>${icon("ledger")}Finance</b><span>namespace <span class="id">agent-services</span></span></div>
      </div>
      <div id="rows" role="list"></div>
      <button class="tbtn follow" type="button" id="follow">${icon("down")}Follow the run</button>
    </section>
    <div id="rv-q15"></div>
  </div>`);
  const lanes = el.querySelector("#lanes");
  lanes.addEventListener("scroll", () => {
    if (Date.now() - autoScrollAt < 200) return;
    // Paused once the presenter scrolls away from where following put the lanes.
    lanes.classList.toggle("paused", Math.abs(lanes.scrollTop - lastTarget) > 24);
  }, { passive: true });
  if (typeof ResizeObserver === "function") {
    // Fonts, the approval card and the reply change heights after render: keep following.
    new ResizeObserver(() => { if (!lanes.classList.contains("paused")) follow(lanes); }).observe(el.querySelector("#rows"));
  }
  el.querySelector("#follow").addEventListener("click", () => {
    lanes.classList.remove("paused");
    follow(lanes);
  });
  mounted = wf;
  lastKey = null;
  lastState = undefined;
}

function meta(run, state) {
  const scen = (state.scenarios || []).find((x) => x.id === run.scenario);
  const v = run.versioning;
  return h`<span class="wf id">${run.workflow_id}</span>
    <span>${scen?.run_label || scen?.title || (run.case ? `Case ${run.case}` : "Conversation")}</span>
    <span>Queue <b class="id">${run.task_queue}</b></span>
    ${v && v.build_id ? h`<span>${v.behavior === "PINNED" ? "Pinned to" : "Version"} <b class="id">${v.deployment}:${v.build_id}</b></span>` : h`<span>Unversioned</span>`}
    <span>A2A mode <b>${run.a2a_mode}</b></span>
    <span>Budget <b>${run.nexus_send_timeout_text}</b></span>`;
}

function links(run) {
  const l = run.links || {};
  return h`${l.support ? h`<a class="lbtn" href="${l.support}" target="_blank" rel="noopener" title="Support's workflow in the Temporal UI (namespace agents)"><span class="sw" style="background:var(--support)"></span>Support${icon("ext")}</a>` : ""}
    ${l.finance ? h`<a class="lbtn" href="${l.finance}" target="_blank" rel="noopener" title="Finance's A2A task workflow in the Temporal UI (namespace agent-services)"><span class="sw" style="background:var(--finance)"></span>Finance${icon("ext")}</a>` : ""}`;
}

function hadInputRequired(state) {
  return (state.rows?.rows || []).some((r) => r.state === "input-required") || !!state.run?.task?.approval;
}

const ORDER = ["submitted", "working", "input-required"];

/** The state track around the word: a small dot per other state, past filled, to come open. */
function track(run, state) {
  const s = run.task?.state || null;
  const idx = s ? (TERMINAL.has(s) ? 3 : ORDER.indexOf(s)) : -1;
  const skipped = TERMINAL.has(s) && !hadInputRequired(state);
  const dot = (name, i) => {
    const cls = i < idx ? (i === 2 && skipped ? "skip" : "past") : "next";
    return h`<li class="${cls}" title="${name}${cls === "skip" ? " (passed over)" : ""}"></li>`;
  };
  const before = ORDER.slice(0, Math.max(idx, 0)).map(dot);
  const after = ORDER.map((n, i) => (i > idx ? dot(n, i) : "")).filter(Boolean);
  if (!TERMINAL.has(s)) after.push(h`<li class="next" title="completed, or rejected or failed"></li>`);
  const words = ORDER.map((n, i) => `${n}${i < idx ? (i === 2 && skipped ? " passed over" : " done") : i === idx ? " now" : ""}`);
  words.push(TERMINAL.has(s) ? `${s} now` : "then completed, rejected or failed");
  return { before: h`${before}`, after: h`${after}`, sr: `States: ${words.join(", ")}.` };
}

function sub(run) {
  const t = run.task;
  const text = subText(run);
  // Finance has answered but Support's run is still open: say what it is waiting for.
  if (t && TERMINAL.has(t.state) && run.status === "RUNNING") {
    return `${text} Support has not replied yet: ${run.compute?.running ? "its worker is picking the answer up" : "no Support worker is running to pick the answer up"}.`;
  }
  return text;
}

function subText(run) {
  const t = run.task;
  const pend = (run.support?.pending_nexus || []).find((p) => p.operation !== "get_agent_card");
  if (!t) {
    if (pend && pend.attempt > 1) return `Finance has no task yet: Support's call is on attempt ${pend.attempt}, and the server keeps retrying it.`;
    if (run.status !== "RUNNING") return "This run ended before Finance created a task.";
    return "Support's agents are reading the ticket. Finance has no A2A task yet.";
  }
  const a = t.artifact;
  if (t.state === "completed" && a) {
    return a.approved_by === "auto"
      ? `Finance approved on its own: under the limit, risk ${a.risk}.`
      : "A human approved, so Finance issued the refund and answered Support's held call.";
  }
  return t.message || "";
}

const serverless = (run) => run.task_queue === "support-tq" || (run.compute?.lifetimes || []).length > 1;

/** The live facts beside the state: the held-open clock, and lifetimes when they tell a story. */
function facts(run) {
  const pend = (run.support?.pending_nexus || []).find((p) => p.operation !== "get_agent_card");
  const m = run.metrics || {};
  const held = pend
    ? h`<span data-since="${pend.scheduled_at}">${sinceText(pend.scheduled_at)}</span>${pend.attempt > 1 ? h`<small>attempt ${pend.attempt}</small>` : ""}`
    : m.held_open_s !== null && m.held_open_s !== undefined ? dur(m.held_open_s) : "–";
  const lt = m.lifetimes ?? (run.compute?.lifetimes || []).length;
  return h`<div><dt>Nexus call held open</dt><dd class="held">${held}</dd></div>
    ${serverless(run) ? h`<div><dt>Lifetimes</dt><dd>${lt}</dd></div>` : ""}`;
}

/** Once the run has closed: what only the end knows, once (the header keeps held open and
 *  lifetimes), on the quiet line beside the task's message. */
function report(run) {
  if (run.status === "RUNNING") return "";
  const m = run.metrics || {};
  const a = run.task?.artifact;
  const refund = run.reply?.refund_id || a?.refund_id;
  const has = (v) => v !== null && v !== undefined;
  const cells = [
    a ? ["Approved by", a.approved_by] : run.task?.state ? ["Finance said", run.task.state] : null,
    ["Refund", refund ? h`<span class="id">${refund}</span>` : "none"],
    has(m.approval_wait_s) ? ["Approval wait", dur(m.approval_wait_s)] : null,
    has(m.approval_to_reply_s) ? [run.task?.state === "rejected" ? "Decision to reply" : "Approval to reply", dur(m.approval_to_reply_s)] : null,
    ["Build", (m.build_ids || []).length ? h`<span class="id">${m.build_ids.join(", ")}</span>` : "unversioned"],
  ].filter(Boolean);
  return h`<dl class="report" aria-label="Report">${cells.map(([k, v]) => h`<div><dt>${k}</dt><dd>${v}</dd></div>`)}</dl>`;
}

/** The Q15 deltas under the lanes, on serverless runs once they have closed. */
function q15(run) {
  const q = run.status === "RUNNING" ? null : run.metrics?.q15;
  return q ? h`<ol class="q15" aria-label="After Finance answered">
      <li class="lead">After Finance answered:</li>
      ${q15Items(q).map(([k, v]) => h`<li>${k} <b>+${dur(v)}</b></li>`)}</ol>` : "";
}

// Only what this run has: with invoke-local there is no WCI, so no task-add-signal or InvokeWorker.
function q15Items(q) {
  return [["task-add-signal", q.task_add_signal_s], ["InvokeWorker", q.invoke_worker_s],
    ["first Workflow Task", q.first_wft_s], ["reply", q.reply_s]]
    .filter(([, v]) => v !== null && v !== undefined);
}

export function renderRunView(state, el) {
  const wf = state.route.wf;
  if (mounted !== wf || !el.querySelector("#rv-head")) scaffold(el, wf);
  const run = state.run && state.run.workflow_id === wf ? state.run : null;
  if (!run) {
    if (state.runMissing === wf) {
      el.querySelector("#rv-state").textContent = "not found";
      el.querySelector("#rv-sub").textContent = `There is no conversation called ${wf} in namespace agents. Pick one from Runs, or start a scenario.`;
    }
    return;
  }
  const ticket = el.querySelector("#rv-ticket");
  ticket.innerHTML = String(h`<q>${run.ticket || ""}</q>`);
  ticket.title = run.ticket || "";
  el.querySelector("#rv-links").innerHTML = String(links(run));
  el.querySelector("#rv-meta").innerHTML = String(meta(run, state));
  const tr = track(run, state);
  el.querySelector("#rv-before").innerHTML = String(tr.before);
  el.querySelector("#rv-after").innerHTML = String(tr.after);
  el.querySelector("#rv-track-sr").textContent = tr.sr;
  const big = el.querySelector("#rv-state");
  const s = run.task?.state || "none";
  if (s !== lastState) {
    big.textContent = s === "none" ? "no task yet" : s;
    big.dataset.s = s;
    if (lastState !== undefined) { big.classList.remove("enter"); void big.offsetWidth; big.classList.add("enter"); }
    lastState = s;
  }
  const closed = run.status !== "RUNNING";
  // Each fact once: while the approval card shows, it carries amount, limit and risk; once
  // Finance has answered and the run closed, the report beside this line names the outcome.
  const subEl = el.querySelector("#rv-sub");
  const quiet = (!closed && s === "input-required") || (closed && !!run.task?.artifact);
  subEl.textContent = quiet ? "" : sub(run);
  subEl.title = subEl.textContent;
  el.querySelector("#rv-facts").innerHTML = String(facts(run));
  const scen = (state.scenarios || []).find((x) => x.id === run.scenario);
  const steps = mergedSteps(scen, run);
  const stepsEl = el.querySelector("#rv-steps");
  // The stepper is presenter navigation: it lives in the notes; here only when notes are off.
  renderSteps(stepsEl, steps);
  if (closed && steps.every((x) => x.status === "done" || x.status === "skipped")) stepsEl.hidden = true;

  updateApproval(state);
  const key = lanesKey(state);
  const lanes = el.querySelector("#lanes");
  if (key !== lastKey) {
    lastKey = key;
    renderLanes(state, el.querySelector("#rows"));
    if (!lanes.classList.contains("paused")) follow(lanes);
  }
  el.querySelector("#rv-report").innerHTML = String(report(run));
  el.querySelector("#rv-q15").innerHTML = String(q15(run));
}

export function unmountRunView() { mounted = null; }
