// Scenario launcher (left column) and the scenario card (main column): preconditions with
// fix buttons, options, cost marks and Start (DESIGN 2.1, 2.2, 5.2).
import { enc } from "./api.js";
import { runAction, runFix, startRun, terminate, decide } from "./actions.js";
import { PROFILE_LABEL, costMark, h, icon, moneyShort, suffix, stateGlyph } from "./format.js";
import { store } from "./store.js";
import { copyBox } from "./ui.js";

const SHORT_TITLE = {
  "case-a": "Case A", "case-b": "Case B", crash: "Crash demo", "serverless-b": "AgentCore, invoke-local",
  "wci-b": "AgentCore, server's WCI", "cloud-b": "AgentCore in AWS", failure: "Failure demo",
  smoke: "The hop with no AI", load: "Load", cleanup: "Leftovers",
};
const SUB = {
  "case-a": "$40 headphones, auto-approved", "case-b": "$450 laptop, a human approves",
  crash: "Kill the worker mid-thought", "serverless-b": "You start each AgentCore lifetime",
  "wci-b": "Temporal starts the worker when work arrives", "cloud-b": "Two AgentCore sessions around a human",
  failure: "Finance disappears mid-call", smoke: "Smoke caller and Agent Card",
  load: "Up to 20 conversations at once", cleanup: "Runs still waiting for a human",
};

function fitLabel(s, profile) {
  if (s.fit === "ready") return "ready";
  if (s.fit === "wrong_profile") {
    const ps = (s.profiles || []).map((p) => PROFILE_LABEL[p] || p);
    return ps.length === 1 ? `${ps[0]} only` : `not on ${PROFILE_LABEL[profile] || profile}`;
  }
  const n = (s.preconditions || []).filter((p) => !p.ok).length;
  return n === 0 ? "set up" : `${n} to fix`;
}

export function renderScenarioList(state) {
  const el = document.getElementById("scenarios");
  const cur = state.route.name === "scenario" ? state.route.scenario : null;
  const list = state.scenarios || [];
  if (!list.length) {
    el.innerHTML = String(h`<li><p class="empty-side">Loading scenarios…</p></li>`);
    return;
  }
  const profile = state.config?.profile;
  el.innerHTML = list.map((s) => String(h`<li><a href="#/scenarios/${enc(s.id)}" ${cur === s.id ? h`aria-current="page"` : ""}>
    <span class="t">${SHORT_TITLE[s.id] || s.title}</span><span class="fit" data-f="${s.fit}">${fitLabel(s, profile)}</span>
    <span class="s">${s.cost !== "free" ? h`${costMark(s.cost, true)} ` : ""}${SUB[s.id] || s.summary}</span></a></li>`)).join("");
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
    return h`<div class="field"><span>${o.label || o.id}</span><div class="seg" role="radiogroup" aria-label="${o.label || o.id}">
      ${o.choices.map((c) => h`<button type="button" role="radio" aria-checked="${String(c === val)}" aria-pressed="${String(c === val)}" data-opt="${o.id}" data-val="${c}">${c}</button>`)}
    </div></div>`;
  }
  const type = o.type === "number" ? "number" : "text";
  return h`<label class="field">${o.label || o.id}<input type="${type}" name="${o.id}" value="${val ?? ""}" ${o.min !== undefined ? h`min="${o.min}"` : ""} ${o.max !== undefined ? h`max="${o.max}"` : ""} autocomplete="off" spellcheck="false" ${o.type === "duration" ? h`pattern="^[0-9hms.]+$" title="A duration like 25h, 90m or 45s"` : ""}></label>`;
}

function preList(s, busy) {
  if (!(s.preconditions || []).length) return "";
  return h`<ul class="pre">${s.preconditions.map((p) => h`<li class="${p.ok ? "ok" : "bad"}">${icon(p.ok ? "check" : "x")}<span>${p.label}<span class="sr">${p.ok ? ": ready" : ": missing"}</span></span>
    ${!p.ok && p.fix ? h`<div class="fixer">${p.fix.kind === "command" || p.fix.kind === "make"
      ? copyBox(p.fix.command || p.fix.make)
      : h`<button class="btn small ghost ${busy[fixKey(p.fix)] ? "busy" : ""}" type="button" data-fix="${p.id}">${icon("wrench")}${p.fix.label || "Fix"}</button>`}</div>` : ""}</li>`)}</ul>`;
}
const fixKey = (f) => (f.kind === "process" ? `proc:${f.process}` : `action:${f.action}`);

function startLabel(s, v) {
  if (s.case === "A") return "Start Case A";
  if (s.case === "B") return "Start Case B";
  if (s.id === "load") return `Start ${v.n || 10} conversations`;
  if (s.id === "smoke") return "Run the smoke caller";
  return "Start";
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
    el.innerHTML = String(h`<div class="welcome"><h1>No scenario called ${state.route.scenario}</h1><p>Pick one from the list.</p></div>`);
    return;
  }
  if (!formValues.has(s.id)) formValues.set(s.id, defaults(s));
  const v = formValues.get(s.id);
  const busy = state.ui.busy || {};
  const profile = state.config?.profile;
  const wrong = s.fit === "wrong_profile";
  const missing = (s.preconditions || []).filter((p) => !p.ok).length;
  const awsOff = s.cost === "aws" && !state.config?.allow_aws;
  const why = wrong ? `Switch to ${(s.profiles || []).join(" or ")} to run this. The console is on ${profile}.`
    : awsOff ? "AWS actions are off. Restart the console with CONSOLE_ALLOW_AWS=1."
      : missing ? `${missing === 1 ? "One precondition is" : `${missing} preconditions are`} missing. You can start anyway; the run waits for them.` : "";
  const startKey = s.case ? `start:${s.id}` : `action:${s.id === "load" ? "load" : "smoke"}`;
  const isCleanup = s.id === "cleanup";
  el.innerHTML = String(h`<article class="card-view" aria-labelledby="sc-title">
    <h1 id="sc-title">${s.title}</h1>
    <p class="lede">${s.summary}</p>
    <div class="facts-row">
      ${costMark(s.cost)}${s.cost_note ? h`<span>${s.cost_note}</span>` : ""}
      ${s.amount_usd ? h`<span>Refund <b>${moneyShort(s.amount_usd)}</b> for order <b>${s.order_id}</b></span>` : ""}
      <span>Profiles <b>${(s.profiles || []).map((p) => PROFILE_LABEL[p] || p).join(", ")}</b></span>
      ${s.story ? h`<span>Story <b class="id">${s.story.tag}</b></span>` : ""}
    </div>
    ${isCleanup ? h`<section class="panel" style="margin-top:1.4rem"><h2>Waiting runs</h2>${leftovers(state)}</section>` : h`
    <div class="card-grid">
      <section class="panel" aria-labelledby="pre-h"><h2 id="pre-h">Before you start</h2>${preList(s, busy) || h`<p class="muted">Nothing to check.</p>`}</section>
      <section class="panel" aria-labelledby="opt-h"><h2 id="opt-h">Options</h2>
        <form class="opts" id="scen-form" data-scenario="${s.id}">${(s.options || []).map((o) => optionField(o, v[o.id]))}
          ${(s.options || []).length ? "" : h`<p class="muted">No options.</p>`}</form></section>
    </div>
    <div class="startbar">
      <button class="btn primary start ${busy[startKey] ? "busy" : ""}" type="button" data-start="${s.id}" ${wrong || awsOff ? "disabled" : ""}>${icon("play")}${startLabel(s, v)}</button>
      ${s.id === "smoke" ? h`<button class="btn ghost ${busy["action:card"] ? "busy" : ""}" type="button" data-card>${icon("ledger")}Fetch the Agent Card</button>` : ""}
      ${why ? h`<span class="why">${why}</span>` : ""}
    </div>
    ${(s.steps || []).length ? h`<section class="panel" style="margin-top:1.4rem" aria-labelledby="plan-h"><h2 id="plan-h">What happens</h2>
      <ol class="plan">${s.steps.map((st) => h`<li><span>${st.title}<span class="k">${{ action: "you press a button", human: "you decide", manual: "in a terminal", watch: "Temporal shows it" }[st.kind] || ""}</span></span><span class="talk">${st.talk}</span></li>`)}</ol></section>` : ""}
    `}
  </article>`);
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
    const fx = t.closest("[data-fix]");
    if (fx) {
      const s = scen(); const p = s?.preconditions.find((x) => x.id === fx.dataset.fix);
      if (p) await runFix(p.fix);
      return;
    }
    const sb = t.closest("[data-start]");
    if (sb) {
      const s = scen(); if (!s) return;
      const v = { ...formValues.get(s.id) };
      if (s.case) {
        await startRun(s, v);
        formValues.delete(s.id); // a fresh workflow ID next time
      } else if (s.id === "load") {
        await runAction("load", { n: Number(v.n) || 10, kind: v.kind || "ping" });
      } else if (s.id === "smoke") {
        await runAction("smoke", {});
      }
      return;
    }
    if (t.closest("[data-card]")) { await runAction("card", {}); return; }
    const rj = t.closest("[data-cleanup-reject]");
    if (rj) { await decide(rj.dataset.cleanupReject, "reject", st.config?.default_approver || "dana.finance", "console cleanup"); return; }
    const tm = t.closest("[data-cleanup-term]");
    if (tm) await terminate(tm.dataset.cleanupTerm);
  });
  document.addEventListener("input", (e) => {
    const f = e.target.closest("#scen-form");
    if (!f || !e.target.name) return;
    const vals = formValues.get(f.dataset.scenario);
    if (vals) vals[e.target.name] = e.target.type === "number" ? Number(e.target.value) : e.target.value;
  });
  document.addEventListener("change", (e) => {
    // The notes quote the make command with this ID; refresh them once the field is left.
    if (e.target.closest("#scen-form") && e.target.name === "workflow_id") store.touch("ui");
  });
}
