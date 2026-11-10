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
export function upsertJob(j) {
  // The POST answer (running) can arrive after the stream's final event for a quick job.
  const prev = (S().jobs || []).find((x) => x.id === j.id);
  if (prev && prev.status !== "running" && j.status === "running") return;
  upsert("jobs", j, "id");
}
/** Recipe metadata as the backend serves it on the start-recipe action (DESIGN 2.3, 5.5). */
export const recipes = () => actionById("start-recipe")?.recipes || [];
export const recipeById = (id) => recipes().find((r) => r.id === id) || null;

/** Start a conversation for a scenario. opts are the scenario card's option values; the queue
 *  defaults to the one the backend picked for this scenario and the stack's shape. */
export async function startRun(scenario, opts = {}) {
  const queueDefault = (scenario.options || []).find((o) => o.id === "queue")?.default;
  const queue = opts.queue || queueDefault || "dev";
  const cost = scenario.cost || "free";
  const body = {
    scenario: scenario.id,
    workflow_id: opts.workflow_id || null,
    queue,
    a2a_mode: opts.a2a_mode || "tool",
    nexus_send_timeout: opts.nexus_send_timeout || "25h",
    ticket: null,
  };
  const label = scenario.start_label || "Start the run";
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

export async function runAction(id, params = {}, { title = null, cost: forced = null, body = null, command = null, danger = false, confirmLabel = null } = {}) {
  const a = actionById(id);
  const r = id === "start-recipe" ? recipeById(params.recipe || a?.params?.find((p) => p.id === "recipe")?.default) : null;
  // The listing's cost is for the default params; a live recipe costs tokens.
  const cost = forced || (r && params.variant === "live" ? "tokens" : null) || a?.cost || "free";
  const label = title || r?.title || a?.label || id;
  const long = !!(a?.long || r?.long);
  const steps = r ? r.steps[params.variant || "scripted"] || r.steps.scripted || [] : null;
  const res = await gated({
    key: `action:${id}`, cost, ask: long || danger, danger, title: label, confirmLabel,
    command: command || (steps ? steps.map((s, i) => `${i + 1}. ${s.label}`).join("\n") : a?.make) || null,
    body: body || (long ? "Takes several minutes. The checklist and log are under Manage stack." : null),
    call: (confirm) => post(`/api/actions/${enc(id)}`, { params, confirm }),
    ok: (res) => {
      if (res && res.result && res.result.message) return `${label}: ${res.result.message}${res.result.ping ? ` (${res.result.ping})` : ""}.`;
      return res && res.status === "running" ? `${label}: started.` : `${label}: done.`;
    },
  });
  if (res && res.id && res.action) upsertJob(res);
  return res;
}

/**
 * A scenario's one-click setup (DESIGN 2.2): its stack recipe, the same job as the recipe card
 * in Manage stack. The button and the toast both say "Set up for <short>". The model follows
 * the stack's (a recipe for the other model would restart the workers on it); long recipes and
 * live ones ask first.
 */
export async function setUp(recipe, short) {
  const variant = S().stack?.model?.mode === "live" ? "live" : "scripted";
  return runAction("start-recipe", { recipe, variant }, { title: `Set up for ${short}` });
}

/** The latest job of a recipe, while it runs or when it failed in the last few minutes. */
export function recipeJob(recipe, withinMs = 10 * 60 * 1000) {
  const j = (S().jobs || []).find((x) => x.action === "start-recipe" && x.params?.recipe === recipe);
  if (!j) return null;
  if (j.status === "running") return j;
  return j.status === "failed" && Date.now() - Date.parse(j.ended_at || j.started_at) < withinMs ? j : null;
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
