// Scenario launcher (left column) and the scenario card (main column): Start with the setup
// state, one "Set up for <short>" button running the scenario's recipe, the checklist with
// compact fixes, the step plan and options behind a disclosure (DESIGN 2.1, 2.2, 5.2).
import { enc } from "./api.js";
import { actionById, recipeById, recipeJob, runAction, runFix, setUp, startRun, terminate, decide } from "./actions.js";
import { PROFILE_LABEL, costMark, h, icon, moneyShort, suffix, stateGlyph } from "./format.js";
import { store } from "./store.js";
import { copyBox, openAttr, patch } from "./ui.js";

const temporalDown = (state) => (state.stack?.components || []).some((c) => c.id === "temporal" && c.status === "down");
const missingOf = (s) => (s.preconditions || []).filter((p) => !p.ok);
/** "2 missing", "1 not checked" (its read failed), or both: a failed read is not a miss. */
function gapText(s) {
  const gaps = missingOf(s);
  const unread = gaps.filter((p) => p.unknown).length;
  const missing = gaps.length - unread;
  return [missing && `${missing} missing`, unread && `${unread} not checked`].filter(Boolean).join(", ");
}

function fitLabel(s, down) {
  if (s.fit === "wrong_profile") {
    const ps = (s.profiles || []).map((p) => PROFILE_LABEL[p] || p);
    return ps.length === 1 ? `${ps[0]} only` : `not on this profile`;
  }
  // With Temporal down every scenario misses things for the same reason: one line says so.
  if (down) return "";
  if (s.fit === "ready") return "ready";
  return gapText(s);
}

export function renderScenarioList(state) {
  const el = document.getElementById("scenarios");
  const cur = state.route.name === "scenario" ? state.route.scenario : null;
  const list = state.scenarios || [];
  if (!list.length) {
    patch(el, h`<li><p class="empty-side">Loading scenarios…</p></li>`);
    return;
  }
  const down = temporalDown(state);
  const head = down ? h`<li class="side-state"><p>Stack not ready: Temporal is down. Open a scenario to set it up.</p></li>` : "";
  patch(el, String(head) + list.map((s) => {
    const fit = fitLabel(s, down);
    return String(h`<li><a href="#/scenarios/${enc(s.id)}" ${cur === s.id ? h`aria-current="page"` : ""}>
    <span class="t">${s.short || s.title}</span>${fit ? h`<span class="fit" data-f="${s.fit}">${fit}</span>` : ""}
    <span class="s">${s.cost !== "free" ? h`${costMark(s.cost, true)} ` : ""}${s.sub || s.summary}</span></a></li>`);
  }).join(""));
}

// ---------------------------------------------------------------- scenario card

const formValues = new Map(); // scenario id -> {option id: value}

/** The workflow ID the scenario form will start (the presenter notes quote it). */
export function formWorkflowId(s) {
  if (!formValues.has(s.id)) formValues.set(s.id, defaults(s));
  return formValues.get(s.id).workflow_id || null;
}

function defaults(s) {
  const v = {};
  for (const o of s.options || []) {
    v[o.id] = o.id === "workflow_id" && o.default ? freshId(o.default) : o.default;
  }
  return v;
}
function freshId(def) {
  // Keep the make default's shape and swap the trailing suffix for a fresh one.
  const base = String(def).replace(/-[a-z0-9]{4}$/, "");
  return `${base}-${suffix()}`;
}

function optionField(o, val) {
  if (o.type === "choice") {
    // A radio group: one tab stop (the checked choice), arrow keys move between choices.
    return h`<div class="field"><span>${o.label || o.id}</span><div class="seg" role="radiogroup" aria-label="${o.label || o.id}">
      ${o.choices.map((c) => h`<button type="button" role="radio" aria-checked="${String(c === val)}" tabindex="${c === val ? "0" : "-1"}" data-opt="${o.id}" data-val="${c}">${c}</button>`)}
    </div></div>`;
  }
  const type = o.type === "number" ? "number" : "text";
  return h`<label class="field">${o.label || o.id}<input type="${type}" name="${o.id}" value="${val ?? ""}" ${o.min !== undefined ? h`min="${o.min}"` : ""} ${o.max !== undefined ? h`max="${o.max}"` : ""} autocomplete="off" spellcheck="false" ${o.type === "duration" ? h`pattern="^[0-9hms.]+$" title="A duration like 25h, 90m or 45s"` : ""}></label>`;
}

/** The checklist, shown while something is missing. Each line keeps its own fix (a single
 *  missing piece mid-demo, a killed worker, is fastest to fix by itself), on the same line.
 *  `primary` is a fix the start bar already offers, so no line repeats it. */
function preList(s, busy, primary = null) {
  // One button per distinct fix: on kind a stopped Docker VM is the fix for several lines.
  const seen = new Set(primary ? [JSON.stringify(primary)] : []);
  const once = (p) => {
    const k = p.fix && JSON.stringify(p.fix);
    if (!k || seen.has(k)) return false;
    seen.add(k);
    return true;
  };
  return h`<ul class="pre">${s.preconditions.map((p) => h`<li class="${p.ok ? "ok" : "bad"}">${icon(p.ok ? "check" : "ring")}<div class="pc"><span class="lab">${p.label}<span class="sr">${p.ok ? ": ready" : p.unknown ? ": could not check" : ": missing"}</span></span>
    ${!p.ok && once(p) ? (p.fix.kind === "command" || p.fix.kind === "make"
      ? copyBox(p.fix.command || p.fix.make)
      : h`<button class="btn small ghost ${busy[fixKey(p.fix)] ? "busy" : ""}" type="button" data-fix="${p.id}">${p.fix.label || "Fix"}</button>`) : ""}
    ${!p.ok && p.note ? h`<span class="pnote">${p.note}</span>` : ""}</div></li>`)}</ul>`;
}
const fixKey = (f) => (f.kind === "process" ? `proc:${f.process}` : `action:${f.action}`);

/** Where a running (or just failed) setup recipe is: the step it is on, or where it stopped. */
function setupProgress(job) {
  if (!job) return "";
  const steps = job.steps || [];
  if (job.status === "failed") {
    const r = job.result || {};
    return h`<p class="setup-state" data-s="failed">Set up stopped${r.step_label ? h` at <b class="id">${r.step_label}</b>` : ""}: ${r.error || "see its log"}. <button class="linkbtn" type="button" data-open-sheet>Open the log</button></p>`;
  }
  const i = steps.findIndex((x) => x.status === "running");
  const done = steps.filter((x) => x.status === "done" || x.status === "skipped").length;
  return h`<p class="setup-state" aria-live="polite">Setting up: step ${i >= 0 ? i + 1 : done} of ${steps.length}${i >= 0 ? h`, <span class="id">${steps[i].label}</span>` : ""}.</p>`;
}

/** The options' current values on one line: what the disclosure hides. */
function optionSummary(s, raw) {
  const v = filled(s, raw);
  return (s.options || [])
    .filter((o) => !["queue", "approver"].includes(o.id) || v[o.id] !== o.default)
    .map((o) => (o.type === "number" ? `${v[o.id]} ${String(o.label || o.id).toLowerCase()}` : String(v[o.id] ?? "")))
    .filter(Boolean).join(" · ");
}

const KIND_WORD = { action: "you press a button", human: "you decide", manual: "in a terminal", watch: "Temporal shows it" };

function plan(s) {
  return h`<ol class="plan">${s.steps.map((st) => h`<li>${st.talk
    ? h`<details class="disc" data-keep="plan:${s.id}:${st.id}" ${openAttr(`plan:${s.id}:${st.id}`)}><summary>${st.title}<span class="k">${KIND_WORD[st.kind] || ""}</span></summary><p class="talk">${st.talk}</p></details>`
    : h`<span>${st.title}<span class="k">${KIND_WORD[st.kind] || ""}</span></span>`}</li>`)}</ol>`;
}

/** The form's values with an empty or out-of-range number back at its default: a cleared
 *  field means "the default", never 0. */
function filled(s, v) {
  const out = { ...v };
  for (const o of s.options || []) {
    if (o.type !== "number") continue;
    const n = out[o.id];
    const bad = typeof n !== "number" || !Number.isFinite(n) || (o.min !== undefined && n < o.min) || (o.max !== undefined && n > o.max);
    if (bad) out[o.id] = o.default;
  }
  return out;
}

/** The catalog's start_label with {option} filled from the form ("Start {n} conversations"). */
const startLabel = (s, v) => String(s.start_label || "Start").replace(/\{(\w+)\}/g, (_, k) => String(filled(s, v)[k] ?? ""));
/** The busy key of what Start runs: the scenario's action, or a Support run. */
const startKey = (s) => (s.start_action ? `action:${s.start_action}` : `start:${s.id}`);

/** What Start sends: a Support run with the form's options, or the action with its params. */
async function start(s, v) {
  if (!s.start_action) {
    await startRun(s, v);
    formValues.delete(s.id); // a fresh workflow ID next time
    return;
  }
  const params = {};
  const f = filled(s, v);
  for (const p of actionById(s.start_action)?.params || []) if (f[p.id] !== undefined) params[p.id] = f[p.id];
  await runAction(s.start_action, params);
}

function leftovers(state) {
  const waiting = (state.runs?.runs || []).filter((r) => r.status === "RUNNING");
  if (!waiting.length) return h`<p class="muted">Nothing is waiting. Every run has finished.</p>`;
  return h`<ul class="leftovers">${waiting.map((r) => h`<li>
    <span><a class="id" href="#/runs/${enc(r.workflow_id)}">${r.workflow_id}</a> ${stateGlyph(r.task_state, r.task_state || "no task")}</span>
    <span class="acts">${r.task_state === "input-required" ? h`<button class="btn small no" type="button" data-cleanup-reject="${r.workflow_id}">${icon("x")}Reject refund</button>` : ""}
      <button class="btn small ghost" type="button" data-cleanup-term="${r.workflow_id}">${icon("stop")}Terminate</button></span></li>`)}</ul>`;
}

export function renderScenarioCard(state, el) {
  const s = (state.scenarios || []).find((x) => x.id === state.route.scenario);
  if (!s) {
    patch(el, h`<div class="welcome"><h1>No scenario called ${state.route.scenario}</h1><p>Pick one from the list.</p></div>`);
    return;
  }
  if (!formValues.has(s.id)) formValues.set(s.id, defaults(s));
  const v = formValues.get(s.id);
  const busy = state.ui.busy || {};
  const profile = state.config?.profile;
  const wrong = s.fit === "wrong_profile";
  const missing = missingOf(s);
  const recipe = s.setup_recipe ? recipeById(s.setup_recipe) : null;
  const job = s.setup_recipe ? recipeJob(s.setup_recipe) : null;
  const running = job?.status === "running";
  // One button sets the stack up when the recipe fixes something that is missing.
  const canSetUp = !wrong && s.setup_recipe && missing.some((p) => p.by_setup);
  // kind with the Docker VM stopped: Bring up kind would stop at its first step, so starting
  // the VM (it asks first) is the primary button and Set up waits, saying why.
  const blocked = canSetUp ? s.setup_blocked : null;
  const setupWhy = blocked ? blocked.why : canSetUp && recipe?.available === false ? recipe.why_not : null;
  const chip = wrong ? h`<span class="chip">${(s.profiles || []).map((p) => PROFILE_LABEL[p] || p).join(" or ")} only</span>`
    : missing.length ? h`<span class="chip">${gapText(s)}</span>`
    : (s.preconditions || []).length ? h`<span class="chip" data-s="ready">${icon("check")}Ready</span>` : "";
  const blockBtn = blocked ? h`<button class="btn primary start ${busy[fixKey(blocked.fix)] ? "busy" : ""}" type="button" data-block-fix>${icon("play")}${blocked.fix.label}</button>` : "";
  const setupBtn = canSetUp ? h`${blockBtn}<button class="btn ${blocked ? "ghost" : "primary"} start ${busy["action:start-recipe"] || running ? "busy" : ""}" type="button" data-setup="${s.setup_recipe}" ${running || setupWhy ? "disabled" : ""}>${icon("play")}Set up for ${s.short}</button>` : "";
  const facts = h`<div class="facts-row">
      ${costMark(s.cost)}${s.cost_note ? h`<span>${s.cost_note}</span>` : ""}
      ${s.amount_usd ? h`<span>Refund <b>${moneyShort(s.amount_usd)}</b> for order <b>${s.order_id}</b></span>` : ""}
    </div>`;
  const why = wrong ? h`<p class="why">Switch to ${(s.profiles || []).map((p) => PROFILE_LABEL[p] || p).join(" or ")} to run this. The console is on ${PROFILE_LABEL[profile] || profile}.</p>`
    : setupWhy ? h`<p class="why">${setupWhy}</p>`
    : missing.length && !canSetUp ? h`<p class="why">You can start anyway; the run waits for what is missing.</p>` : "";
  const checklist = missing.length && !wrong ? h`<section class="block" aria-labelledby="pre-h"><h2 id="pre-h">Before you start</h2>${setupProgress(job)}${preList(s, busy, blocked?.fix)}</section>` : "";
  if (s.view === "cleanup") {
    patch(el, h`<article class="card-view" aria-labelledby="sc-title">
      <h1 id="sc-title">${s.title}</h1><p class="lede">${s.summary}</p>${facts}
      ${checklist}
      <section class="block" aria-labelledby="left-h"><h2 id="left-h">Waiting runs</h2>${leftovers(state)}</section>
    </article>`);
    return;
  }
  const opts = (s.options || []).length ? h`<details class="disc opts-d" data-keep="opts:${s.id}" ${openAttr(`opts:${s.id}`)}><summary>Options <span class="muted">${optionSummary(s, v)}</span></summary>
      <form class="opts" id="scen-form" data-scenario="${s.id}">${s.options.map((o) => optionField(o, v[o.id]))}</form></details>` : "";
  const extra = s.extra_action;
  patch(el, h`<article class="card-view" aria-labelledby="sc-title">
    <h1 id="sc-title">${s.title}</h1>
    <p class="lede">${s.summary}</p>
    ${facts}
    <div class="startbar">
      ${setupBtn}
      <button class="btn ${canSetUp ? "ghost" : "primary"} start ${busy[startKey(s)] ? "busy" : ""}" type="button" data-start="${s.id}" ${wrong ? "disabled" : ""}>${icon("play")}${startLabel(s, v)}</button>
      ${extra ? h`<button class="btn ghost ${busy[`action:${extra.action}`] ? "busy" : ""}" type="button" data-extra="${extra.action}" ${wrong ? "disabled" : ""}>${icon("ledger")}${extra.label}</button>` : ""}
      ${chip}
    </div>
    ${why}
    ${opts}
    ${checklist}
    ${(s.steps || []).length ? h`<section class="block" aria-labelledby="plan-h"><h2 id="plan-h">What happens</h2>${plan(s)}</section>` : ""}
  </article>`);
}

/** "Next: <short>" once a run has finished; "Set up and go to <short>" when the next scenario
 *  needs the stack in another shape (the backend says which recipe). */
export function nextButton(scen) {
  const n = scen?.next;
  if (!n) return "";
  if (n.setup_recipe) {
    const busy = store.state.ui.busy?.["action:start-recipe"];
    return h`<button class="btn small ghost next ${busy ? "busy" : ""}" type="button" data-next="${n.id}" data-next-setup="${n.setup_recipe}" data-next-short="${n.short}">Set up and go to ${n.short}</button>`;
  }
  return h`<a class="btn small ghost next" href="#/scenarios/${enc(n.id)}">Next: ${n.short}</a>`;
}

export function wireLauncher() {
  document.addEventListener("click", async (e) => {
    const t = e.target;
    const st = store.state;
    const scen = () => (st.scenarios || []).find((x) => x.id === st.route.scenario);
    const opt = t.closest("[data-opt]");
    if (opt) {
      const s = scen(); if (!s) return;
      formValues.get(s.id)[opt.dataset.opt] = opt.dataset.val;
      store.touch("scenarios");
      return;
    }
    const su = t.closest("[data-setup]");
    if (su) {
      const s = scen(); if (!s) return;
      await setUp(su.dataset.setup, s.short || s.title);
      return;
    }
    const nx = t.closest("[data-next]");
    if (nx) {
      location.hash = `#/scenarios/${enc(nx.dataset.next)}`;
      await setUp(nx.dataset.nextSetup, nx.dataset.nextShort);
      return;
    }
    if (t.closest("[data-block-fix]")) {
      const s = scen();
      if (s?.setup_blocked) await runFix(s.setup_blocked.fix);
      return;
    }
    const fx = t.closest("[data-fix]");
    if (fx) {
      const s = scen(); const p = s?.preconditions.find((x) => x.id === fx.dataset.fix);
      if (p) await runFix(p.fix);
      return;
    }
    const sb = t.closest("[data-start]");
    if (sb) {
      const s = scen(); if (!s) return;
      await start(s, { ...formValues.get(s.id) });
      return;
    }
    const ex = t.closest("[data-extra]");
    if (ex) { await runAction(ex.dataset.extra, {}); return; }
    const rj = t.closest("[data-cleanup-reject]");
    if (rj) { await decide(rj.dataset.cleanupReject, "reject", st.config?.default_approver, "console cleanup"); return; }
    const tm = t.closest("[data-cleanup-term]");
    if (tm) await terminate(tm.dataset.cleanupTerm);
  });
  // Arrow keys move the checked choice inside an option's radio group.
  document.addEventListener("keydown", (e) => {
    const opt = e.target.closest?.("[data-opt]");
    if (!opt || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) return;
    const all = [...opt.parentElement.querySelectorAll("[data-opt]")];
    const step = e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 1;
    const next = all[(all.indexOf(opt) + step + all.length) % all.length];
    e.preventDefault();
    next.focus();
    next.click();
  });
  document.addEventListener("input", (e) => {
    const f = e.target.closest("#scen-form");
    if (!f || !e.target.name) return;
    const vals = formValues.get(f.dataset.scenario);
    // An empty number field stays empty (null), so it falls back to its default, not 0.
    if (vals) vals[e.target.name] = e.target.type === "number" ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value;
  });
  document.addEventListener("change", (e) => {
    // The notes quote the make command with this ID; refresh them once the field is left.
    if (e.target.closest("#scen-form") && e.target.name === "workflow_id") store.touch("ui");
  });
}
