// Presenter notes: what to say for the current step, what to point at, the step's button,
// and the equivalent make command (DESIGN 2.1). Also rendered alone at #/notes.
import { actionById, procByName, runStep } from "./actions.js";
import { formWorkflowId } from "./launcher.js";
import { costMark, h, icon } from "./format.js";
import { currentStep, mergedSteps } from "./steps.js";
import { store } from "./store.js";
import { copyBox } from "./ui.js";

const POINT_AT = {
  start: "The ticket at the top, and the first rows in Support's lane.",
  invoke1: "The compute rail lighting up: lifetime 1. Nothing polled support-tq until the AgentCore app was invoked.",
  card: "The short wire to the seam: get_agent_card, the one thing Finance publishes.",
  ask: "The wire crossing the seam into Finance's lane: one Nexus call between two namespaces.",
  auto: "Finance's lane: approved_by auto. No human was needed under $100.",
  "input-required": "The big state, input-required, and the approval card waiting in Finance's lane.",
  "stop-support": "Support workers running drops to 0. Finance keeps waiting; nothing is lost.",
  drained: "The rail on the left is an empty dashed lane: no Support worker exists. The glowing line in the Nexus seam: the server is holding Support's call to Finance open.",
  approve: "The empty Support rail and the glowing held-open line in the seam, side by side. Then the approval card in Finance's lane.",
  restart: "A new worker identity in Support's lane, on the same run.",
  invoke2: "The rail lights up again as lifetime 2 (with the server's WCI, its InvokeWorker row sits on the rail's edge).",
  // Functions: what to point at depends on the run (versioned or plain).
  reply: (run) => (run?.versioning?.build_id
    ? "The reply quoting the refund ID, and the pinned Build ID in the header."
    : "The reply quoting the refund ID. Same run ID from start to finish."),
  report: (run) => (run?.versioning?.build_id
    ? "The report: lifetimes, one Build ID, and seconds from approval to reply."
    : "The report: how long the approval took, and seconds from approval to reply. This run is unversioned: plain workers on support-dev-tq."),
  kill: "The rail: the lifetime ends abruptly with a red cut.",
  "stop-finance": "Finance's pip in the stack bar goes down.",
  retry: "The seam: send_message on attempt 2, then 3, with the last failure. Support's code has no retry loop.",
  "start-finance": "The same call completing as soon as Finance is back.",
};
// The catalog's ids (data/scenarios.toml) for the same moments.
POINT_AT.approved = POINT_AT.auto;
POINT_AT["restart-support"] = POINT_AT.restart;
POINT_AT.retrying = POINT_AT.retry;

function pointAt(id, run) {
  const p = POINT_AT[id];
  return typeof p === "function" ? p(run) : p;
}

function stepButton(step) {
  const a = step.action;
  if (!a || a === "start-run") return "";
  const busy = store.state.ui.busy || {};
  if (a === "approve" || a === "reject" || step.kind === "human") {
    return h`<button class="btn small ghost" type="button" data-step-act="${step.id}">${icon("hold")}Go to the approval card</button>`;
  }
  if (a === "process.stop" || a === "process.start") {
    const p = procByName(step.params?.name);
    const label = p?.label || step.params?.name;
    const kill = a === "process.stop" && step.params?.signal === "KILL";
    const verb = kill ? `Kill ${label}` : a === "process.stop" ? `Stop ${label}` : `Start ${label}`;
    return h`<button class="btn small ${kill ? "no" : "primary"} ${busy[`proc:${step.params?.name}`] ? "busy" : ""}" type="button" data-step-act="${step.id}">${icon(a === "process.stop" ? (kill ? "bolt" : "stop") : "play")}${verb}</button>`;
  }
  const act = actionById(a);
  return h`<button class="btn small primary ${busy[`action:${a}`] ? "busy" : ""}" type="button" data-step-act="${step.id}" ${act && !act.available ? h`disabled title="${act.why_not || ""}"` : ""}>${icon("play")}${act?.label || a}${act ? h` ${costMark(act.cost, true)}` : ""}</button>`;
}

function ctx(state) {
  const r = state.route;
  const run = (r.name === "run" || r.name === "notes") && r.wf && state.run?.workflow_id === r.wf ? state.run : null;
  const scen = (state.scenarios || []).find((s) => s.id === (run ? run.scenario : r.scenario)) || null;
  return { run, scen };
}

export function renderNotes(state) {
  const el = document.getElementById("notes");
  const { run, scen } = ctx(state);
  let body;
  if (run) {
    const steps = mergedSteps(scen, run);
    const cur = currentStep(steps) || (run.status !== "RUNNING" ? steps[steps.length - 1] : null);
    const say = cur?.talk || scen?.notes || "Talk through the lanes from top to bottom.";
    body = h`<h2>Say</h2>
      ${cur ? h`<p class="stepname">Step ${cur.n || steps.indexOf(cur) + 1} of ${steps.length}: ${cur.title}</p>` : ""}
      <p class="say">${say}</p>
      ${cur && POINT_AT[cur.id] ? h`<h2>Point at</h2><p class="point">${pointAt(cur.id, run)}</p>` : ""}
      ${cur && run.status === "RUNNING" && stepButton(cur) ? h`<div class="do">${stepButton(cur)}</div>` : ""}
      ${cur?.make ? h`<h2>Same thing from a terminal</h2>${copyBox(cur.make)}` : ""}
      <div class="story">${scen?.notes ? h`<p>${scen.notes}</p>` : ""}
        ${scen?.story ? h`<p>Story: <span class="id">${scen.story.doc}</span></p>` : ""}
        <p>The console never queries Support (ledger 72); it reads history and describe.</p></div>`;
  } else if (scen) {
    // The catalog's make text carries the default ID; quote the one the form will start.
    const def = (scen.options || []).find((o) => o.id === "workflow_id")?.default;
    const wf = scen.case ? formWorkflowId(scen) : null;
    const make = scen.steps?.[0]?.make && def && wf ? scen.steps[0].make.split(def).join(wf) : scen.steps?.[0]?.make;
    body = h`<h2>Say</h2><p class="say">${scen.notes || scen.summary}</p>
      ${make ? h`<h2>Same thing from a terminal</h2>${copyBox(make)}` : ""}
      ${scen.story ? h`<div class="story"><p>Story: <span class="id">${scen.story.doc}</span></p></div>` : ""}`;
  } else {
    body = h`<h2>Say</h2><p class="say">Support asks Finance for a refund. Two teams, two namespaces, one published contract over Nexus. Pick a scenario to start.</p>`;
  }
  el.innerHTML = String(body);
}

export function wireNotes() {
  document.addEventListener("click", (e) => {
    const b = e.target.closest("[data-step-act]");
    if (!b) return;
    const st = store.state;
    const { run, scen } = ctx(st);
    const step = mergedSteps(scen, run).find((s) => s.id === b.dataset.stepAct);
    if (!step) return;
    if (step.kind === "human" || step.action === "approve") {
      const card = document.getElementById("approval");
      if (card) {
        card.scrollIntoView({ block: "nearest", behavior: "smooth" });
        card.querySelector("[name=approver]")?.focus({ preventScroll: true });
      }
      return;
    }
    runStep(step, scen, run);
  });
}
