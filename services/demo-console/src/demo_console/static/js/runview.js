// The run view: header, the A2A state hero with three facts, the stepper, the lanes and the
// reply/report strip (DESIGN 2.1). The scaffold is built once per focused run; parts update.
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
  const rows = lanes.querySelectorAll(".row");
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

const SCEN_NAME = {
  "case-a": "Case A, auto-approved", "case-b": "Case B, a human approves", crash: "Crash demo",
  "serverless-b": "Case B, AgentCore shape with invoke-local", "wci-b": "Case B, the server's WCI starts the worker",
  failure: "Failure demo",
};

function scaffold(el, wf) {
  // One compact strip above the lanes (ticket, meta, state word and three facts) so the lanes
  // get most of the height: they are the hero (the hop, the held call, the compute rail).
  el.innerHTML = String(h`<div class="run">
    <div class="runhead" id="rv-head"><p class="ticket"><span class="wf id">${wf}</span></p></div>
    <section class="hero" aria-label="A2A task state" id="rv-hero">
      <ol class="track" id="rv-track" aria-label="A2A task states"></ol>
      <p class="statebig" id="rv-state" data-s="none" aria-live="polite">loading</p>
      <p class="statesub" id="rv-sub"></p>
      <dl class="facts" id="rv-facts"></dl>
    </section>
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
    <div id="rv-report"></div>
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

function head(run, state) {
  const scen = (state.scenarios || []).find((s) => s.id === run.scenario);
  const v = run.versioning;
  const links = run.links || {};
  return h`<p class="ticket" title="${run.ticket || ""}"><q>${run.ticket || ""}</q></p>
    <div class="meta">
      <span class="wf id">${run.workflow_id}</span>
      <span>${SCEN_NAME[run.scenario] || scen?.title || (run.case ? `Case ${run.case}` : "Conversation")}</span>
      <span>Queue <b class="id">${run.task_queue}</b></span>
      ${v && v.build_id ? h`<span>${v.behavior === "PINNED" ? "Pinned to" : "Version"} <b class="id">${v.deployment}:${v.build_id}</b></span>` : h`<span>Unversioned</span>`}
      <span>A2A mode <b>${run.a2a_mode}</b></span>
      <span>Budget <b>${run.nexus_send_timeout_text}</b></span>
    </div>
    <div class="links">
      ${links.support || links.finance ? h`<span class="lead">Temporal UI</span>` : ""}
      ${links.support ? h`<a class="lbtn" href="${links.support}" target="_blank" rel="noopener" title="Support's workflow in the Temporal UI (namespace agents)"><span class="sw" style="background:var(--support)"></span>Support${icon("ext")}</a>` : ""}
      ${links.finance ? h`<a class="lbtn" href="${links.finance}" target="_blank" rel="noopener" title="Finance's A2A task workflow in the Temporal UI (namespace agent-services)"><span class="sw" style="background:var(--finance)"></span>Finance task${icon("ext")}</a>` : ""}
    </div>`;
}

function hadInputRequired(state) {
  return (state.rows?.rows || []).some((r) => r.state === "input-required") || !!state.run?.task?.approval;
}

function track(run, state) {
  const s = run.task?.state || null;
  const order = ["submitted", "working", "input-required"];
  const idx = s ? (TERMINAL.has(s) ? 3 : order.indexOf(s)) : -1;
  const skipped = TERMINAL.has(s) && !hadInputRequired(state);
  const li = (name, i, extra = "") => {
    const cls = [i < idx ? "past" : i === idx ? "now" : "", i === 2 && skipped ? "passed-over" : "",
      Math.abs(i - Math.max(idx, 0)) > 1 || (idx >= 0 && i < idx) ? "far" : ""].filter(Boolean).join(" ");
    return h`<li class="${cls}">${name}${extra}</li>`;
  };
  const last = TERMINAL.has(s) ? h`<li class="now">${s}</li>`
    : h`<li class="${idx === 2 ? "" : "far"}">completed <span class="alt">or rejected, failed</span></li>`;
  return h`${li("submitted", 0)}${li("working", 1)}${li("input-required", 2)}${last}`;
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
      ? `Finance approved on its own: under the limit, risk ${a.risk}. Refund ${a.refund_id}.`
      : `Approved by ${a.approved_by}. Finance issued refund ${a.refund_id} and answered Support's held call.`;
  }
  return t.message || "";
}

function facts(run, state) {
  const c = run.compute || {};
  const sup = (state.stack?.components || []).find((x) => x.id === "support");
  const n = c.running ? Math.max(1, sup?.count || 1) : 0;
  const pend = (run.support?.pending_nexus || []).find((p) => p.operation !== "get_agent_card");
  const held = pend
    ? h`<span data-since="${pend.scheduled_at}">${sinceText(pend.scheduled_at)}</span>${pend.attempt > 1 ? h`<small>attempt ${pend.attempt}</small>` : ""}`
    : run.metrics?.held_open_s !== null && run.metrics?.held_open_s !== undefined ? dur(run.metrics.held_open_s) : "–";
  const lt = run.metrics?.lifetimes ?? (c.lifetimes || []).length;
  return h`<div><dt>Support workers running</dt><dd class="${n ? "" : "zero"}"><span class="lamp ${n ? "lit" : ""}" aria-hidden="true"></span>${n}</dd></div>
    <div><dt>Nexus call held open</dt><dd class="held">${held}</dd></div>
    <div><dt>Lifetimes</dt><dd>${lt}</dd></div>`;
}

/** Once the run has closed, the report takes the facts' place in the strip (no extra row). */
function reportCells(run) {
  const m = run.metrics || {};
  const a = run.task?.artifact;
  const refund = run.reply?.refund_id || a?.refund_id;
  const cells = [
    m.held_open_s !== null && m.held_open_s !== undefined ? ["Nexus call held open", h`<span class="held">${dur(m.held_open_s)}</span>`] : null,
    ["Lifetimes", m.lifetimes ?? (run.compute?.lifetimes || []).length],
    ["Build IDs", (m.build_ids || []).length ? h`<span class="id">${m.build_ids.join(", ")}</span>` : "unversioned"],
    m.approval_wait_s !== null && m.approval_wait_s !== undefined ? ["Approval wait", dur(m.approval_wait_s)] : null,
    m.approval_to_reply_s !== null && m.approval_to_reply_s !== undefined ? [run.task?.state === "rejected" ? "Decision to reply" : "Approval to reply", dur(m.approval_to_reply_s)] : null,
    a ? ["Approved by", a.approved_by] : run.task?.state ? ["Finance said", run.task.state] : null,
    ["Refund", refund ? h`<span class="id">${refund}</span>` : "none"],
  ].filter(Boolean);
  return h`${cells.map(([k, v]) => h`<div><dt>${k}</dt><dd>${v}</dd></div>`)}`;
}

function report(run) {
  if (run.status === "RUNNING") return "";
  const q = (run.metrics || {}).q15;
  return h`${q ? h`<ol class="q15" aria-label="After Finance answered">
      <li class="lead">After Finance answered:</li>
      ${q15Items(q).map(([k, v]) => h`<li>${k} <b>+${dur(v)}</b></li>`)}</ol>` : ""}`;
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
  el.querySelector("#rv-head").innerHTML = String(head(run, state));
  el.querySelector("#rv-track").innerHTML = String(track(run, state));
  const big = el.querySelector("#rv-state");
  const s = run.task?.state || "none";
  if (s !== lastState) {
    big.textContent = s === "none" ? "no task yet" : s;
    big.dataset.s = s;
    if (lastState !== undefined) { big.classList.remove("enter"); void big.offsetWidth; big.classList.add("enter"); }
    lastState = s;
  }
  const subEl = el.querySelector("#rv-sub");
  subEl.textContent = sub(run);
  subEl.title = subEl.textContent;
  const closed = run.status !== "RUNNING";
  const factsEl = el.querySelector("#rv-facts");
  factsEl.innerHTML = String(closed ? reportCells(run) : facts(run, state));
  factsEl.classList.toggle("closed", closed);
  factsEl.setAttribute("aria-label", closed ? "Report" : "Facts");
  const scen = (state.scenarios || []).find((x) => x.id === run.scenario);
  const steps = mergedSteps(scen, run);
  const stepsEl = el.querySelector("#rv-steps");
  renderSteps(stepsEl, steps);
  // A finished run's report sits in the strip; the all-done stepper would only cost height.
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
}

export function unmountRunView() { mounted = null; }
