// Recent conversations (visibility list) and load batches, in the left column (DESIGN 2.1, 5.3).
import { enc } from "./api.js";
import { h, hhmm, icon, stateGlyph } from "./format.js";

const SCEN_WORD = {
  "case-a": "Case A", "case-b": "Case B", crash: "Crash demo", "serverless-b": "Case B, invoke-local",
  "wci-b": "Case B, WCI", "cloud-b": "Case B, AgentCore", failure: "Failure demo", load: "Load",
};

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
    el.innerHTML = String(h`<li><p class="empty-side">${down ? "Runs appear here once Temporal is reachable." : "Loading runs…"}</p></li>`);
    return;
  }
  const runs = (body.runs || []).filter((r) => !r.group);
  const groups = body.groups || [];
  if (!runs.length && !groups.length) {
    el.innerHTML = String(h`<li><p class="empty-side">No runs yet. Start Case A to see the hop.</p></li>`);
    return;
  }
  const items = runs.map((r) => h`<li><a href="#/runs/${enc(r.workflow_id)}" ${focus === r.workflow_id ? h`aria-current="page"` : ""}>
      <span class="id" title="${r.workflow_id}">${r.workflow_id}</span><span class="st">${statusOf(r)}</span>
      <span class="when">${SCEN_WORD[r.scenario] || (r.case ? `Case ${r.case}` : "Conversation")}, ${r.status === "RUNNING" ? "started " : ""}${hhmm(r.started_at)}</span></a></li>`);
  const gs = groups.map((g) => {
    const done = g.running === 0;
    const ok = done && g.failed === 0;
    return h`<li><div class="grp" title="${g.n} runs: ${g.completed} completed, ${g.failed} failed, ${g.running} running">
      <span class="id">${g.id}</span><span class="st">${h`<span class="sg" data-s="${ok ? "completed" : done ? "failed" : "running"}">${icon(ok ? "check" : done ? "x" : "dot")}${g.completed} of ${g.n}</span>`}</span>
      <span class="when">Load, ${g.kind}${g.failed ? `, ${g.failed} failed` : ""}</span></div></li>`;
  });
  el.innerHTML = [...items, ...gs].map(String).join("");
}
