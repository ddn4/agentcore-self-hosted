// Presenter notes: what to say for the current step, what to point at, the step's button,
// and the equivalent make command (DESIGN 2.1). Also rendered alone at #/notes.
import { actionById, procByName, runStep } from "./actions.js";
import { formWorkflowId, nextButton } from "./launcher.js";
import { costMark, h, icon } from "./format.js";
import { currentStep, mergedSteps, renderSteps } from "./steps.js";
import { store } from "./store.js";
import { copyBox, patch } from "./ui.js";

/** What to point at: the catalog's point, or point_versioned on a versioned run. */
function pointAt(step, run) {
  return (run?.versioning?.build_id && step.point_versioned) || step.point || "";
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
  let steps = null;
  if (run) {
    steps = mergedSteps(scen, run);
    const cur = currentStep(steps) || (run.status !== "RUNNING" ? steps[steps.length - 1] : null);
    const say = cur?.talk || scen?.notes || "Talk through the lanes from top to bottom.";
    // The stepper is the presenter's map, so it sits here (the run view shows it only when
    // the notes are off).
    body = h`${steps.length ? h`<ol class="steps" id="notes-steps" aria-label="Guided steps"></ol>` : ""}
      <h2>Say</h2>
      ${cur ? h`<p class="stepname">Step ${cur.n || steps.indexOf(cur) + 1} of ${steps.length}: ${cur.title}</p>` : ""}
      <p class="say">${say}</p>
      ${cur && pointAt(cur, run) ? h`<h2>Point at</h2><p class="point">${pointAt(cur, run)}</p>` : ""}
      ${cur && run.status === "RUNNING" && stepButton(cur) ? h`<div class="do">${stepButton(cur)}</div>` : ""}
      ${run.status !== "RUNNING" && nextButton(scen) ? h`<div class="do">${nextButton(scen)}</div>` : ""}
      ${cur?.make ? h`<h2>Same thing from a terminal</h2>${copyBox(cur.make)}` : ""}
      ${scen?.notes ? h`<div class="story"><p>${scen.notes}</p>${storyDoc(scen)}</div>` : ""}`;
  } else if (scen) {
    // The catalog's make text carries the default ID; quote the one the form will start.
    const def = (scen.options || []).find((o) => o.id === "workflow_id")?.default;
    const wf = def ? formWorkflowId(scen) : null;
    const make = scen.steps?.[0]?.make && def && wf ? scen.steps[0].make.split(def).join(wf) : scen.steps?.[0]?.make;
    body = h`<h2>Say</h2><p class="say">${scen.notes || scen.summary}</p>
      ${make ? h`<h2>Same thing from a terminal</h2>${copyBox(make)}` : ""}
      ${scen.story ? h`<div class="story">${storyDoc(scen)}</div>` : ""}`;
  } else {
    body = h`<h2>Say</h2><p class="say">Support asks Finance for a refund. Two teams, two namespaces, one published contract over Nexus. Pick a scenario to start.</p>`;
  }
  const wasOpen = !!el.querySelector("details.maint[open]");
  if (patch(el, body) && wasOpen) el.querySelector("details.maint")?.setAttribute("open", "");
  const stepsEl = el.querySelector("#notes-steps");
  if (stepsEl && steps) renderSteps(stepsEl, steps);
}

/** Where the story is written down: for whoever maintains the demo, folded away. */
function storyDoc(scen) {
  return scen?.story ? h`<details class="maint"><summary>Story doc</summary><p class="id">${scen.story.doc}</p></details>` : "";
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
