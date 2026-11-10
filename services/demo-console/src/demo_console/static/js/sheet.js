// "Manage stack": a failed job first, then stack recipes, laptop processes, one-shot actions,
// cluster commands and jobs (DESIGN 2.3, 4.5, 5.5). Each line shows the make target the console
// runs. Recipes and their checklists come from the backend (the start-recipe action's
// `recipes`), so the card lists exactly the steps the recipe walks.
import { enc, get } from "./api.js";
import { recipeById, runAction, startProcess, stopProcess } from "./actions.js";
import { PROFILE_LABEL, ago, costMark, h, icon, ms } from "./format.js";
import { store } from "./store.js";
import { toastError } from "./ui.js";

const dlg = () => document.getElementById("sheet");
const openLogs = new Map(); // "proc:<name>" | "job:<id>" -> lines[]
const choice = new Map();   // "<name>:variant" etc -> value
let logTimer = null;

const STEP_MARK = { done: "✓", skipped: "–", failed: "✗", running: "…" };
const STEP_WORD = { done: "done", skipped: "skipped", failed: "failed", running: "running" };
const RECENT_MS = 30 * 60 * 1000;
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

/** A recipe's numbered checklist: the plan, or the latest run's step states. */
function checklist(steps) {
  return h`<ol class="checklist">${steps.map((s) => {
    const st = s.status || "todo";
    const note = st === "skipped" ? s.note : st === "todo" || !s.elapsed_s ? "" : `${s.elapsed_s}s`;
    return h`<li data-s="${st}">${s.label}${STEP_MARK[st] ? h` <span class="mark" title="${STEP_WORD[st]}">${STEP_MARK[st]}</span><span class="sr"> ${STEP_WORD[st]}</span>` : ""}${note ? h` <span class="note">${note}</span>` : ""}</li>`;
  })}</ol>`;
}

function latestRun(jobs, id) {
  const j = jobs.find((x) => x.action === "start-recipe" && x.params?.recipe === id && x.steps);
  if (!j) return null;
  return j.status === "running" || Date.now() - ms(j.ended_at || j.started_at) < RECENT_MS ? j : null;
}

function recipeCard(r, jobs, busy) {
  const variant = r.variants.length ? choice.get(`recipe:${r.id}:variant`) || "scripted" : "scripted";
  const live = variant === "live";
  const run = latestRun(jobs, r.id);
  const steps = run && (run.status === "running" || run.params?.variant === variant) ? run.steps : r.steps[variant] || r.steps.scripted;
  const running = run?.status === "running";
  return h`<div class="recipe" data-recipe-card="${r.id}"><b>${r.title}</b>
    ${r.variants.length > 1 ? h`<div class="seg vseg" role="group" aria-label="Model for ${r.title}">${r.variants.map((v) => h`<button type="button" data-recipe-variant="${r.id}:${v}" aria-pressed="${String(v === variant)}">${v === "live" ? "live (Opus 5.5)" : "scripted"}</button>`)}</div>` : ""}
    ${checklist(steps)}
    ${r.why_not ? h`<span class="why">${r.why_not}</span>` : ""}
    <span class="rgo"><button class="btn small ${r.danger ? "no" : "ghost"} ${busy["action:start-recipe"] || running ? "busy" : ""}" type="button" data-recipe="${r.id}" ${r.available === false || running ? "disabled" : ""}>${icon(r.danger ? "stop" : "play")}${r.danger ? "Tear down" : r.long ? r.title : "Bring up"}</button>${live ? costMark("tokens", true) : ""}${r.long && !running ? h`<span class="muted">several minutes</span>` : ""}${running ? h`<span class="muted">running, started ${ago(run.started_at)}</span>` : ""}</span></div>`;
}

/** The newest job, when it failed: where, why, the next move and its last lines. */
function failedJob(j) {
  const r = j.result || {};
  const lines = (r.tail && r.tail.length ? r.tail : (j.log_tail || []).slice(-12));
  return h`<section class="failed" aria-labelledby="failed-title"><h3 id="failed-title"><span class="pstate" data-s="failed">failed</span> ${j.title || j.action}</h3>
    <p>${r.step_label ? h`At <b class="id">${r.step_label}</b>: ` : ""}${r.error || "See the log."}</p>
    ${r.hint ? h`<p class="sub">${r.hint}</p>` : ""}
    ${lines.length ? h`<pre class="log">${lines.join("\n")}</pre>` : ""}
    <span class="muted">${ago(j.ended_at || j.started_at)} · ${j.command}</span></section>`;
}

function jobItem(j) {
  return h`<li>
    <span class="nm">${j.title || j.action} <span class="pstate" data-s="${j.status}">${j.status}</span>${j.exit_code ? h` <span class="muted">exit ${j.exit_code}</span>` : ""}</span>
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
  const jobs = state.jobs || [];
  const recipes = (actions.find((a) => a.id === "start-recipe")?.recipes || []).filter((r) => r.profiles.includes(profile));
  const oneShots = actions.filter((a) => (a.group || "oneshot") === "oneshot");
  const cluster = actions.filter((a) => a.group === "cluster");
  const failed = jobs[0]?.status === "failed" ? jobs[0] : null;
  const kind = profile !== "local";
  const scroll = d.querySelector(".body")?.scrollTop || 0;
  d.innerHTML = String(h`<header><h2 id="sheet-title">Manage stack</h2><span class="muted">profile <b class="id">${profile}</b></span>
      <button class="tbtn" type="button" data-close-sheet aria-label="Close">${icon("close")}</button></header>
    <div class="body">
      ${failed ? failedJob(failed) : ""}
      ${recipes.length ? h`<section><h3>Stack recipes</h3><p class="sub">${kind
        ? "Bring up kind starts each missing layer in order and skips the ones that are up. The laptop side alone needs a running cluster."
        : "One button brings the stack to a known shape. The job's log lists each make target as it runs."}</p>
        <div class="recipes">${recipes.map((r) => recipeCard(r, jobs, busy))}</div></section>` : ""}
      <section><h3>Processes on this laptop</h3>
        <p class="sub">Each runs as a make target in its own process group and survives a console restart. Live variants call Bedrock.</p>
        ${procs.length ? h`<ul class="plist">${procs.map((p) => procItem(p, profile, busy))}</ul>` : h`<p class="muted">The backend lists no processes.</p>`}</section>
      <section><h3>One-shot actions</h3>
        <ul class="plist">${oneShots.map((a) => actionItem(a, profile, busy))}</ul></section>
      <section><h3>Cluster</h3>
        ${kind ? h`<p class="sub">One layer at a time, each a few minutes. Bring up kind runs them in order. Delete removes the cluster.</p><ul class="plist">${cluster.map((a) => actionItem(a, profile, busy))}</ul>`
          : h`<p class="sub">On kind, the cluster commands live here. Switch the profile in the stack bar.</p>`}</section>
      <section><h3>Jobs</h3>
        ${jobs.length ? h`<ul class="plist">${jobs.slice(0, 20).map(jobItem)}</ul>` : h`<p class="muted">No jobs yet. Recipes and one-shot actions show up here with their logs.</p>`}</section>
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
      const r = recipeById(rc.dataset.recipe);
      if (!r) return;
      if (r.danger) {
        await runAction("start-recipe", { recipe: r.id }, { danger: true, confirmLabel: "Tear down", body: "Stops every process the console started, then the dev server. Workflow history stays in .temporal/dev.db." });
        return;
      }
      const variant = choice.get(`recipe:${r.id}:variant`) || "scripted";
      const live = variant === "live";
      // Long or live: the gate asks first; the backend refuses without confirm too.
      await runAction("start-recipe", { recipe: r.id, variant }, {
        title: `${r.title}${live ? ", live" : ""}`,
        body: live ? `Starts (or restarts) the model workers on their live targets: Opus 5.5 on Bedrock. Needs aws sso login.${r.long ? " Takes several minutes." : ""}` : null,
      });
      return;
    }
    const ac = t.closest("[data-action]");
    if (ac) {
      const a = (store.state.actions || []).find((x) => x.id === ac.dataset.action);
      if (!a) return;
      const params = {};
      for (const p of a.params || []) {
        const v = choice.get(`${a.id}:${p.id}`) ?? p.default;
        params[p.id] = p.type === "number" ? Number(v) : v;
      }
      const del = a.id === "kind-down";
      try {
        await runAction(a.id, params, del ? { danger: true, confirmLabel: "Delete cluster", body: "Deletes the kind cluster and everything in it." } : {});
      } catch (err) { toastError(err); }
    }
  });
  d.addEventListener("change", (e) => {
    const c = e.target.closest("[data-choice]");
    if (c) choice.set(c.dataset.choice, c.value);
  });
}
