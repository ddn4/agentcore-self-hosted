// "Manage stack": laptop processes, stack recipes, one-shot actions, cluster commands and jobs
// (DESIGN 2.3, 4.5, 5.5). Each line shows the make target the console runs.
import { enc, get } from "./api.js";
import { runAction, startProcess, stopProcess } from "./actions.js";
import { PROFILE_LABEL, ago, costMark, h, icon } from "./format.js";
import { store } from "./store.js";
import { confirmDialog, toastError } from "./ui.js";

const dlg = () => document.getElementById("sheet");
const openLogs = new Map(); // "proc:<name>" | "job:<id>" -> lines[]
const choice = new Map();   // "<name>:variant" etc -> value
let logTimer = null;

const RECIPES = {
  "local-plain": { title: "Local, plain Support", steps: ["dev-server-up WCI=0", "payments-mock", "worker-finance-scripted", "nexus-endpoint", "worker-support-scripted"], stops: "agentcore-local*, emulator" },
  "local-invoke": { title: "Local, serverless (invoke-local)", steps: ["dev-server-up WCI=0", "payments-mock", "worker-finance-scripted", "nexus-endpoint", "agentcore-local-scripted", "invoke-local", "register-version MODE=--poller"], stops: "worker-support*" },
  "local-wci": { title: "Local, serverless (WCI emulator)", steps: ["wait for /ping Healthy", "dev-server-down", "dev-server-up WCI=1", "agentcore-emulator", "agentcore-local-scripted", "register-version"], stops: "worker-support*" },
  "kind-laptop": { title: "kind, laptop side", steps: ["nexus-endpoint", "agentcore-local-scripted", "invoke-local", "register-version MODE=--poller"], stops: null },
  teardown: { title: "Tear down local", steps: ["stop every managed process", "dev-server-down"], stops: "everything", danger: true },
};
// The make targets a recipe's worker steps run for each model variant (procs.py SPECS).
const LIVE_TARGET = { "worker-finance-scripted": "worker-finance", "worker-support-scripted": "worker-support", "agentcore-local-scripted": "agentcore-local" };
const recipeSteps = (R, variant) => R.steps.map((s) => (variant === "live" ? LIVE_TARGET[s] || s : s));
const RECIPE_PROFILES = { "local-plain": ["local"], "local-invoke": ["local"], "local-wci": ["local"], "kind-laptop": ["kind", "kind-plaintext"], teardown: ["local", "kind", "kind-plaintext", "eks-pf"] };
const CLUSTER = new Set(["kind-up", "kind-temporal", "kind-workers", "certs-pull", "pf", "pf-stop", "kind-down"]);
const STATUS_WORD = { running: "running", stopped: "stopped", exited: "exited", external: "running outside the console", unavailable: "unavailable", succeeded: "succeeded", failed: "failed" };

function logBox(key) {
  const lines = openLogs.get(key);
  if (!lines) return "";
  return h`<pre class="log" aria-label="Log">${lines.length ? lines.join("\n") : "No output yet."}</pre>`;
}

function procItem(p, profile, busy) {
  const off = (p.profiles || []).length && !(p.profiles || []).includes(profile);
  const running = p.status === "running";
  const vkey = `${p.name}:variant`;
  const variant = choice.get(vkey) || p.variant || (p.variants || [])[0];
  const isDev = p.name === "dev-server";
  const wci = choice.get(`${p.name}:wci`) || "0";
  const b = busy[`proc:${p.name}`];
  return h`<li>
    <span class="nm">${p.label} <span class="pstate" data-s="${p.status}">${STATUS_WORD[p.status] || p.status}</span>${p.pid && running ? h` <span class="id muted">pid ${p.pid}</span>` : ""}${p.ports?.length ? h` <span class="id muted">:${p.ports.join(", :")}</span>` : ""}</span>
    <span class="mk id">${p.make}${p.started_at && running ? h` <span class="muted">started ${ago(p.started_at)}</span>` : ""}</span>
    <span class="ctl">
      ${!running && (p.variants || []).length > 1 ? h`<select aria-label="Variant for ${p.label}" data-choice="${vkey}">${p.variants.map((v) => h`<option value="${v}" ${v === variant ? "selected" : ""}>${v}${v === "live" ? " (tokens)" : ""}</option>`)}</select>` : ""}
      ${isDev && !running ? h`<select aria-label="Worker Controller for the dev server" data-choice="${p.name}:wci"><option value="0" ${wci === "0" ? "selected" : ""}>WCI off</option><option value="1" ${wci === "1" ? "selected" : ""}>WCI=1 (emulator)</option></select>` : ""}
      ${running && p.managed !== false
        ? h`<button class="btn small ghost ${b ? "busy" : ""}" type="button" data-proc-stop="${p.name}">${icon("stop")}Stop</button>`
        : p.status === "external" ? "" : h`<button class="btn small primary ${b ? "busy" : ""}" type="button" data-proc-start="${p.name}" ${off || p.status === "unavailable" ? "disabled" : ""}>${icon("play")}Start</button>`}
      ${variant === "live" && !running ? costMark("tokens", true) : ""}
      <button class="tbtn" type="button" data-log="proc:${p.name}" aria-pressed="${String(openLogs.has(`proc:${p.name}`))}">${icon("log")}Log</button>
    </span>
    ${p.status === "external" ? h`<span class="why">Started outside the console. Stop it in its own terminal.</span>` : ""}
    ${p.why ? h`<span class="why">${p.why}</span>` : off ? h`<span class="why">Not used on profile ${profile}.</span>` : ""}
    ${p.exit_code !== null && p.exit_code !== undefined && p.status === "exited" ? h`<span class="why">Exited with code ${p.exit_code}. Open the log to see why.</span>` : ""}
    ${logBox(`proc:${p.name}`)}
  </li>`;
}

function paramField(a, p) {
  const key = `${a.id}:${p.id}`;
  const v = choice.get(key) ?? p.default;
  if (p.type === "choice") {
    return h`<select aria-label="${p.id} for ${a.label}" data-choice="${key}">${p.choices.map((c) => h`<option value="${c}" ${String(c) === String(v) ? "selected" : ""}>${c}</option>`)}</select>`;
  }
  return h`<input type="${p.type === "number" ? "number" : "text"}" style="width:5.5rem" aria-label="${p.id} for ${a.label}" data-choice="${key}" value="${v ?? ""}" ${p.min !== undefined ? h`min="${p.min}"` : ""} ${p.max !== undefined ? h`max="${p.max}"` : ""}>`;
}

function actionItem(a, profile, busy) {
  const off = !(a.profiles || []).includes(profile);
  const disabled = off || !a.available;
  return h`<li>
    <span class="nm">${a.label} ${a.cost !== "free" ? costMark(a.cost, true) : ""}</span>
    <span class="mk id">${a.make}</span>
    <span class="ctl">${(a.params || []).map((p) => paramField(a, p))}
      <button class="btn small ${a.id === "kind-down" ? "no" : "primary"} ${busy[`action:${a.id}`] ? "busy" : ""}" type="button" data-action="${a.id}" ${disabled ? "disabled" : ""}>${icon("play")}Run</button></span>
    ${off ? h`<span class="why">Only on ${(a.profiles || []).map((p) => PROFILE_LABEL[p] || p).join(", ")}.</span>` : a.why_not ? h`<span class="why">${a.why_not}</span>` : ""}
  </li>`;
}

function jobItem(j) {
  return h`<li>
    <span class="nm">${j.action} <span class="pstate" data-s="${j.status}">${j.status}</span>${j.exit_code ? h` <span class="muted">exit ${j.exit_code}</span>` : ""}</span>
    <span class="mk id">${j.command}${j.started_at ? h` <span class="muted">${ago(j.started_at)}</span>` : ""}</span>
    <span class="ctl"><button class="tbtn" type="button" data-log="job:${j.id}" aria-pressed="${String(openLogs.has(`job:${j.id}`))}">${icon("log")}Log</button></span>
    ${openLogs.has(`job:${j.id}`) ? h`<pre class="log">${(j.log_tail || []).join("\n") || "No output."}</pre>` : ""}
  </li>`;
}

export function renderSheet(state) {
  const d = dlg();
  if (!d.open) return;
  const profile = state.config?.profile || "local";
  const busy = state.ui.busy || {};
  const procs = state.processes || [];
  const actions = state.actions || [];
  const recipe = actions.find((a) => a.id === "start-recipe");
  const recipes = (recipe?.params?.[0]?.choices || Object.keys(RECIPES)).filter((r) => (RECIPE_PROFILES[r] || []).includes(profile));
  const oneShots = actions.filter((a) => a.id !== "start-recipe" && !CLUSTER.has(a.id));
  const cluster = actions.filter((a) => CLUSTER.has(a.id));
  const scroll = d.querySelector(".body")?.scrollTop || 0;
  d.innerHTML = String(h`<header><h2 id="sheet-title">Manage stack</h2><span class="muted">profile <b class="id">${profile}</b></span>
      <button class="tbtn" type="button" data-close-sheet aria-label="Close">${icon("close")}</button></header>
    <div class="body">
      <section><h3>Processes on this laptop</h3>
        <p class="sub">Each runs as a make target in its own process group and survives a console restart. Live variants call Bedrock.</p>
        ${procs.length ? h`<ul class="plist">${procs.map((p) => procItem(p, profile, busy))}</ul>` : h`<p class="muted">The backend lists no processes.</p>`}</section>
      ${recipes.length ? h`<section><h3>Stack recipes</h3><p class="sub">One button brings the stack to a known shape. The job's log lists each make target as it runs.</p>
        <div class="recipes">${recipes.map((r) => {
          const R = RECIPES[r] || { title: r, steps: [] };
          const variant = choice.get(`recipe:${r}:variant`) || "scripted";
          const live = variant === "live";
          return h`<div class="recipe"><b>${R.title}</b>
            ${R.danger ? "" : h`<div class="seg vseg" role="group" aria-label="Model for ${R.title}">${["scripted", "live"].map((v) => h`<button type="button" data-recipe-variant="${r}:${v}" aria-pressed="${String(v === variant)}">${v === "live" ? "live (Opus 5.5)" : "scripted"}</button>`)}</div>`}
            <ol>${recipeSteps(R, variant).map((s) => h`<li>${s}</li>`)}</ol>${R.stops ? h`<span class="muted" style="font-size:.82rem">Stops ${R.stops}${live ? "; restarts scripted workers as live" : ""}</span>` : ""}
            <span class="rgo"><button class="btn small ${R.danger ? "no" : "ghost"} ${busy["action:start-recipe"] ? "busy" : ""}" type="button" data-recipe="${r}">${icon(R.danger ? "stop" : "play")}${R.danger ? "Tear down" : "Bring up"}</button>${live ? costMark("tokens", true) : ""}</span></div>`;
        })}</div></section>` : ""}
      <section><h3>One-shot actions</h3>
        <ul class="plist">${oneShots.map((a) => actionItem(a, profile, busy))}</ul></section>
      <section><h3>Cluster</h3>
        ${profile.startsWith("kind") ? h`<p class="sub">Docker only. Each takes a few minutes; Delete removes the cluster.</p><ul class="plist">${cluster.map((a) => actionItem(a, profile, busy))}</ul>`
          : h`<p class="sub">On kind, the cluster commands live here. Switch the profile in the stack bar.</p>`}</section>
      <section><h3>Jobs</h3>
        ${(state.jobs || []).length ? h`<ul class="plist">${state.jobs.slice(0, 20).map(jobItem)}</ul>` : h`<p class="muted">No jobs yet. Recipes and one-shot actions show up here with their logs.</p>`}</section>
    </div>`);
  const body = d.querySelector(".body");
  if (body) body.scrollTop = scroll;
}

async function refreshLogs() {
  const procKeys = [...openLogs.keys()].filter((k) => k.startsWith("proc:"));
  if (!procKeys.length) return;
  await Promise.all(procKeys.map(async (k) => {
    try {
      const res = await get(`/api/processes/${enc(k.slice(5))}/log?tail=200`);
      openLogs.set(k, res.lines || []);
    } catch { /* keep the last lines */ }
  }));
  store.touch("processes");
}

export function openSheet() {
  const d = dlg();
  if (!d.open) d.showModal();
  renderSheet(store.state);
  clearInterval(logTimer);
  logTimer = setInterval(() => { if (d.open) refreshLogs(); }, 3000);
}

export function wireSheet() {
  const d = dlg();
  d.addEventListener("close", () => clearInterval(logTimer));
  d.addEventListener("click", async (e) => {
    const t = e.target;
    if (t === d) { d.close(); return; } // backdrop
    if (t.closest("[data-close-sheet]")) { d.close(); return; }
    const lg = t.closest("[data-log]");
    if (lg) {
      const k = lg.dataset.log;
      if (openLogs.has(k)) openLogs.delete(k); else { openLogs.set(k, []); if (k.startsWith("proc:")) refreshLogs(); }
      renderSheet(store.state);
      return;
    }
    const st = t.closest("[data-proc-start]");
    if (st) {
      const n = st.dataset.procStart;
      const opts = { variant: choice.get(`${n}:variant`) || undefined };
      if (n === "dev-server") opts.wci = Number(choice.get("dev-server:wci") || 0);
      await startProcess(n, opts);
      return;
    }
    const sp = t.closest("[data-proc-stop]");
    if (sp) { await stopProcess(sp.dataset.procStop, "TERM"); return; }
    const rv = t.closest("[data-recipe-variant]");
    if (rv) {
      const [r, v] = rv.dataset.recipeVariant.split(":");
      choice.set(`recipe:${r}:variant`, v);
      renderSheet(store.state);
      return;
    }
    const rc = t.closest("[data-recipe]");
    if (rc) {
      const r = rc.dataset.recipe;
      if (RECIPES[r]?.danger) {
        const ok = await confirmDialog({ title: "Tear down local", body: "Stops every process the console started, then the dev server. Workflow history stays in .temporal/dev.db.", confirmLabel: "Tear down", danger: true });
        if (!ok) return;
        await runAction("start-recipe", { recipe: r }, { title: RECIPES[r]?.title || r });
        return;
      }
      const variant = choice.get(`recipe:${r}:variant`) || "scripted";
      const title = `${RECIPES[r]?.title || r}${variant === "live" ? ", live" : ""}`;
      // live: the gate asks first (Bedrock tokens); the backend refuses without confirm too.
      await runAction("start-recipe", { recipe: r, variant }, variant === "live"
        ? { title, cost: "tokens", body: "Starts (or restarts) the model workers on their live targets: Opus 5.5 on Bedrock. Needs aws sso login.", command: recipeSteps(RECIPES[r] || { steps: [] }, "live").join(" -> ") }
        : { title });
      return;
    }
    const ac = t.closest("[data-action]");
    if (ac) {
      const a = (store.state.actions || []).find((x) => x.id === ac.dataset.action);
      if (!a) return;
      if (a.long || a.id === "kind-down") {
        const ok = await confirmDialog({ title: a.label, body: a.id === "kind-down" ? "Deletes the kind cluster and everything in it." : "This takes several minutes. Follow it under Jobs.", command: a.make, confirmLabel: a.id === "kind-down" ? "Delete cluster" : "Run", danger: a.id === "kind-down" });
        if (!ok) return;
      }
      const params = {};
      for (const p of a.params || []) {
        const v = choice.get(`${a.id}:${p.id}`) ?? p.default;
        params[p.id] = p.type === "number" ? Number(v) : v;
      }
      try { await runAction(a.id, params); } catch (err) { toastError(err); }
    }
  });
  d.addEventListener("change", (e) => {
    const c = e.target.closest("[data-choice]");
    if (c) choice.set(c.dataset.choice, c.value);
  });
}
