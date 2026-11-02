// The guided stepper: scenario steps (5.2) merged with the run's step statuses (5.3/5.4).
// Statuses come from the backend's predicates; only `manual` steps can be ticked here.
import { h } from "./format.js";
import { prefs } from "./store.js";

const SHORT = {
  start: "Start", card: "Agent Card", ask: "Ask Finance", auto: "Auto-approved",
  invoke1: "Lifetime 1", "input-required": "A human is asked", drained: "Compute drains",
  "stop-support": "Stop Support", approve: "Approve", restart: "Restart Support",
  invoke2: "Lifetime 2", reply: "Reply", report: "Report", kill: "Kill worker",
  "stop-finance": "Stop Finance", retry: "Retries", "start-finance": "Start Finance",
  resume: "Resume AWS", smoke: "Smoke", load: "Load",
  // The catalog's ids (data/scenarios.toml) where they differ from the short names above.
  approved: "Approved", "restart-support": "Restart Support", retrying: "Retries",
};

function ticks(wf) {
  return new Set(((prefs.read().ticks || {})[wf]) || []);
}
export function toggleTick(wf, id) {
  const all = prefs.read().ticks || {};
  const cur = new Set(all[wf] || []);
  if (cur.has(id)) cur.delete(id); else cur.add(id);
  all[wf] = [...cur];
  prefs.write({ ticks: all });
}

/** Scenario steps with statuses. Falls back to the run's own step ids when no catalog entry. */
export function mergedSteps(scenario, run) {
  const statuses = new Map((run?.steps || []).map((s) => [s.id, s]));
  const base = scenario?.steps?.length ? scenario.steps
    : (run?.steps || []).map((s, i) => ({ id: s.id, n: i + 1, title: s.title || SHORT[s.id] || s.id, kind: "watch" }));
  const manual = run ? ticks(run.workflow_id) : new Set();
  // The catalog's make text quotes the scenario's default workflow ID; quote this run's.
  const def = (scenario?.options || []).find((o) => o.id === "workflow_id")?.default;
  const make = (m) => (m && def && run ? m.split(def).join(run.workflow_id) : m);
  const out = base.map((s) => {
    const st = statuses.get(s.id) || {};
    let status = st.status || "todo";
    if (s.kind === "manual" && manual.has(s.id)) status = "done";
    return { ...s, make: make(s.make), status, why: st.why || null };
  });
  // A manual tick can move "current" forward.
  if (!out.some((s) => s.status === "current")) {
    const first = out.find((s) => s.status === "todo");
    if (first && run?.status === "RUNNING") first.status = "current";
  }
  return out;
}

export const currentStep = (steps) => steps.find((s) => s.status === "current" || s.status === "blocked") || null;

export function renderSteps(el, steps) {
  if (!steps.length) { el.innerHTML = ""; el.hidden = true; return; }
  el.hidden = false;
  const allDone = steps.every((s) => s.status === "done" || s.status === "skipped");
  // One line (DESIGN 2.1): with many steps the current pill uses the short label too; the full
  // title is in its tooltip and in the presenter notes ("Step 2 of 8: ...").
  const long = steps.length > 6;
  el.innerHTML = steps.map((s, i) => {
    const now = s.status === "current" || s.status === "blocked";
    const label = now && !long ? s.title : (SHORT[s.id] || s.title);
    const word = { done: "done", current: "now", todo: "to do", skipped: "skipped", blocked: "blocked" }[s.status] || s.status;
    const manual = s.kind === "manual";
    return String(h`<li class="${s.status}${manual ? " manual" : ""}" title="${s.title}${s.why ? `: ${s.why}` : ""}" ${s.status === "current" ? h`aria-current="step"` : ""} ${manual ? h`data-tick="${s.id}" role="button" tabindex="0"` : ""}>
      <span class="n" aria-hidden="true">${s.status === "done" ? "✓" : s.n || i + 1}</span><span class="lab">${label}</span><span class="sr">: ${word}</span></li>`);
  }).join("") + (allDone ? String(h`<li class="alldone">${steps.some((s) => s.status === "skipped") ? "Done" : `All ${steps.length} steps done`}</li>`) : "");
}
