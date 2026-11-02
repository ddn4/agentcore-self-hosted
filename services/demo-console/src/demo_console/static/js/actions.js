// The presenter's verbs, each one call to the backend behind the cost gate (DESIGN 4.5, 5.3, 5.5).
import { enc, get, post } from "./api.js";
import { store } from "./store.js";
import { gated, toast } from "./ui.js";

const S = () => store.state;

export function procByName(name) {
  return (S().processes || []).find((p) => p.name === name) || null;
}
export function actionById(id) {
  return (S().actions || []).find((a) => a.id === id) || null;
}

function upsert(listKey, item, key) {
  const list = [...(S()[listKey] || [])];
  const i = list.findIndex((x) => x[key] === item[key]);
  if (i >= 0) list[i] = item; else list.unshift(item);
  store.set({ [listKey]: list });
}
export const upsertProcess = (p) => upsert("processes", p, "name");
export const upsertJob = (j) => upsert("jobs", j, "id");

const CASE_VERB = { A: "Start Case A", B: "Start Case B" };

/** Start a conversation for a scenario. opts are the scenario card's option values. */
export async function startRun(scenario, opts = {}) {
  const versioned = ["serverless-b", "wci-b", "cloud-b"].includes(scenario.id)
    || (String(scenario.id).startsWith("case-") && /^serverless|agentcore/.test(S().stack?.shape || ""));
  const queue = opts.queue || (versioned ? "versioned" : "dev");
  const profile = S().config?.profile;
  let cost = scenario.cost || "free";
  if (profile === "eks-pf" && queue === "versioned") cost = "aws";
  const body = {
    scenario: scenario.id,
    workflow_id: opts.workflow_id || null,
    queue,
    a2a_mode: opts.a2a_mode || "tool",
    nexus_send_timeout: opts.nexus_send_timeout || "25h",
    ticket: null,
  };
  const label = CASE_VERB[scenario.case] || "Start the run";
  const res = await gated({
    key: `start:${scenario.id}`, cost, title: label,
    body: `Starts ${body.workflow_id || "a new run"} on ${queue === "versioned" ? "support-tq" : "support-dev-tq"}.`,
    call: (confirm) => post("/api/runs", confirm ? { ...body, confirm: true } : body),
    ok: (r) => (r.attached ? `Opened the run that was already running: ${r.workflow_id}.` : `Started ${r.workflow_id}.`),
  });
  if (res && res.workflow_id) location.hash = `#/runs/${enc(res.workflow_id)}`;
  return res;
}

export async function decide(wf, verdict, approver, comment) {
  const word = verdict === "approve" ? "approved" : "rejected";
  return gated({
    key: `decide:${wf}`, cost: "free",
    call: () => post(`/api/runs/${enc(wf)}/${verdict}`, { approver, comment }),
    ok: (r) => (r.settled === false
      ? `Signal sent. The task is still ${r.state_after}; the stream shows the rest.`
      : `Refund ${word} by ${approver}.`),
  });
}

export async function terminate(wf) {
  return gated({
    key: `terminate:${wf}`, cost: "free",
    call: () => post(`/api/runs/${enc(wf)}/terminate`, { reason: "console cleanup", confirm: true }),
    ok: `Terminated ${wf}.`,
  });
}

export async function startProcess(name, { variant = null, wci = undefined } = {}) {
  const p = procByName(name);
  const v = variant || p?.variant || (p?.variants || [])[0] || null;
  const cost = v === "live" ? "tokens" : (p?.cost || "free");
  const body = { variant: v };
  if (wci !== undefined) body.wci = wci;
  const res = await gated({
    key: `proc:${name}`, cost, title: `Start ${p?.label || name}`,
    command: p?.make || null,
    call: (confirm) => post(`/api/processes/${enc(name)}/start`, { ...body, confirm }),
    ok: `Started ${p?.label || name}.`,
  });
  if (res && res.name) upsertProcess(res);
  return res;
}

export async function stopProcess(name, signal = "TERM") {
  const p = procByName(name);
  const res = await gated({
    key: `proc:${name}`, cost: "free",
    call: () => post(`/api/processes/${enc(name)}/stop`, { signal }),
    ok: signal === "KILL" ? `Killed ${p?.label || name} (SIGKILL).` : `Stopped ${p?.label || name}.`,
  });
  if (res && res.name) upsertProcess(res);
  return res;
}

export async function runAction(id, params = {}, { title = null, cost: forced = null, body = null, command = null } = {}) {
  const a = actionById(id);
  // The listing's cost is for the default params; a caller that changes them (a live recipe)
  // says what they cost so the gate asks first.
  const cost = forced || a?.cost || "free";
  const label = title || a?.label || id;
  const res = await gated({
    key: `action:${id}`, cost, title: label, command: command || a?.make || null,
    body: body || (a?.long ? "This takes a few minutes. The job's log shows progress in Manage stack." : null),
    call: (confirm) => post(`/api/actions/${enc(id)}`, { params, confirm }),
    ok: (r) => {
      if (r && r.result && r.result.message) return `${label}: ${r.result.message}${r.result.ping ? ` (${r.result.ping})` : ""}.`;
      return r && r.status === "running" ? `${label}: started. Follow it in Manage stack.` : `${label}: done.`;
    },
  });
  if (res && res.id && res.action) upsertJob(res);
  return res;
}

/** A precondition's fix: an action, a process start, or a command to copy (shown, not run). */
export async function runFix(fix) {
  if (!fix) return null;
  if (fix.kind === "action") return runAction(fix.action, fix.params || {}, { title: fix.label });
  if (fix.kind === "process") return startProcess(fix.process, { variant: fix.variant, wci: fix.wci });
  toast("Run this in a terminal.", { hint: fix.command || fix.make || "" });
  return null;
}

/**
 * A guided step's action. steps[].action is one of: start-run, approve, process.start,
 * process.stop (params {name, signal}), or an action id from /api/actions (params as given).
 */
export async function runStep(step, scenario, run) {
  const a = step.action;
  if (!a) return null;
  if (a === "start-run") return startRun(scenario, {});
  if (a === "approve" || a === "reject") {
    document.querySelector("#approval input[name=approver]")?.focus();
    return null;
  }
  if (a === "process.start") return startProcess(step.params?.name, step.params || {});
  if (a === "process.stop") return stopProcess(step.params?.name, step.params?.signal || "TERM");
  const params = { ...(step.params || {}) };
  if (run && actionById(a)?.params?.some((p) => p.id === "wf")) params.wf = run.workflow_id;
  return runAction(a, params);
}

export async function refreshLists() {
  const [sc, ac, pr, jb] = await Promise.allSettled([
    get("/api/scenarios"), get("/api/actions"), get("/api/processes"), get("/api/jobs?limit=20"),
  ]);
  const patch = {};
  if (sc.status === "fulfilled") patch.scenarios = sc.value.scenarios || [];
  if (ac.status === "fulfilled") patch.actions = ac.value.actions || [];
  if (pr.status === "fulfilled") patch.processes = pr.value.processes || [];
  if (jb.status === "fulfilled") patch.jobs = jb.value.jobs || [];
  store.set(patch);
}
