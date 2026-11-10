// Recent conversations (visibility list) and load batches, in the left column (DESIGN 2.1, 5.3).
import { enc } from "./api.js";
import { h, hhmm, icon, stateGlyph } from "./format.js";
import { patch } from "./ui.js";

const ANSWERED = new Set(["completed", "rejected", "failed", "canceled"]);

/** The badge follows Support's run; the A2A task state only says what it is waiting on. */
export function statusOf(r) {
  if (r.status === "RUNNING") {
    if (r.task_state === "input-required") return stateGlyph("input-required", "waiting");
    // Finance answered, but Support has not replied yet (its worker may be scaled to zero).
    if (ANSWERED.has(r.task_state)) return h`<span title="Finance's task is ${r.task_state}; Support's run is still open, waiting for a Support worker to reply">${stateGlyph("running", "answered")}</span>`;
    if (r.task_state) return stateGlyph(r.task_state, r.task_state);
    return stateGlyph("running", "running");
  }
  if (r.status === "COMPLETED") {
    const s = r.task_state && r.task_state !== "working" ? r.task_state : "completed";
    return stateGlyph(s, s);
  }
  const s = String(r.status || "").toLowerCase();
  return stateGlyph(s === "terminated" || s === "timed_out" ? s : "failed", s.replace("_", " "));
}

export function renderRuns(state) {
  const el = document.getElementById("runs");
  const body = state.runs;
  const focus = state.route.name === "run" ? state.route.wf : null;
  if (!body) {
    const down = (state.stack?.components || []).some((c) => c.id === "temporal" && c.status === "down");
    patch(el, h`<li><p class="empty-side">${down ? "Runs appear here once Temporal is reachable." : "Loading runs…"}</p></li>`);
    return;
  }
  const runs = (body.runs || []).filter((r) => !r.group);
  const groups = body.groups || [];
  if (!runs.length && !groups.length) {
    const first = (state.scenarios || []).find((s) => s.fit !== "wrong_profile");
    patch(el, h`<li><p class="empty-side">No runs yet. ${first ? `Start ${first.short || first.title} to see the hop.` : "Pick a scenario to start one."}</p></li>`);
    return;
  }
  const label = (r) => (state.scenarios || []).find((s) => s.id === r.scenario)?.run_label;
  const items = runs.map((r) => h`<li><a href="#/runs/${enc(r.workflow_id)}" ${focus === r.workflow_id ? h`aria-current="page"` : ""}>
      <span class="id" title="${r.workflow_id}">${r.workflow_id}</span><span class="st">${statusOf(r)}</span>
      <span class="when">${label(r) || (r.case ? `Case ${r.case}` : "Conversation")}, ${r.status === "RUNNING" ? "started " : ""}${hhmm(r.started_at)}</span></a></li>`);
  const gs = groups.map((g) => {
    const done = g.running === 0;
    const ok = done && g.failed === 0;
    return h`<li><div class="grp" title="${g.n} runs: ${g.completed} completed, ${g.failed} failed, ${g.running} running">
      <span class="id">${g.id}</span><span class="st">${h`<span class="sg" data-s="${ok ? "completed" : done ? "failed" : "running"}">${icon(ok ? "check" : done ? "x" : "dot")}${g.completed} of ${g.n}</span>`}</span>
      <span class="when">Load, ${g.kind}${g.failed ? `, ${g.failed} failed` : ""}</span></div></li>`;
  });
  patch(el, [...items, ...gs].map(String).join(""));
}
