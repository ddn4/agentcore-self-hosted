// Demo console entry: router (#/runs/<wf>, #/scenarios/<id>, #/notes), the SSE stream, the
// store and fixture mode (?fixture=<name> or ?demo=1). No build step; ES modules only.
import { enc, get, isFixture, openStream, useBackend } from "./js/api.js";
import { refreshLists, upsertJob, upsertProcess } from "./js/actions.js";
import { updateApproval } from "./js/approval.js";
import { loadFixture } from "./js/fixture.js";
import { clock, h, icon, ms, stateGlyph } from "./js/format.js";
import { renderScenarioCard, renderScenarioList, wireLauncher } from "./js/launcher.js";
import { renderNotes, wireNotes } from "./js/notes.js";
import { renderRuns } from "./js/runs.js";
import { renderRunView, unmountRunView } from "./js/runview.js";
import { openSheet, renderSheet, wireSheet } from "./js/sheet.js";
import { renderBanners, renderStackBar, toggleNotes, toggleProjector, wireStackBar } from "./js/stackbar.js";
import { toggleTick } from "./js/steps.js";
import { store } from "./js/store.js";
import { toast } from "./js/ui.js";

const root = document.documentElement;
const q = new URLSearchParams(location.search);
const channel = typeof BroadcastChannel === "function" ? new BroadcastChannel("demo-console") : null;

// ------------------------------------------------------------------ routing

function parseRoute() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  if (parts[0] === "runs" && parts[1]) return { name: "run", wf: parts[1], scenario: null };
  if (parts[0] === "scenarios" && parts[1]) return { name: "scenario", wf: null, scenario: parts[1] };
  if (parts[0] === "notes") return { name: "notes", wf: parts[1] || store.state.route.wf || null, scenario: null };
  return { name: "home", wf: null, scenario: null };
}

let streamWf;
let closeStream = null;

function focus(wf) {
  if (wf === streamWf && closeStream) return;
  streamWf = wf;
  if (closeStream) closeStream();
  store.set({ run: null, rows: { workflow_id: wf, t0: null, rows_version: 0, rows: [] }, runMissing: null });
  closeStream = openStream(wf, { event: onEvent, up: () => store.set({ connection: "up" }), down: () => store.set({ connection: "down" }) });
  if (wf) loadRun(wf);
}

async function loadRun(wf) {
  try {
    const [run, rows] = await Promise.all([get(`/api/runs/${enc(wf)}`), get(`/api/runs/${enc(wf)}/rows`)]);
    if (streamWf !== wf) return;
    store.set({ run, rows: { ...rows, workflow_id: wf } });
  } catch (err) {
    if (err.status === 404 && streamWf === wf) store.set({ runMissing: wf });
  }
}

function route() {
  const r = parseRoute();
  root.dataset.route = r.name;
  root.dataset.drawer = "closed";
  store.set({ route: r });
  if (r.name !== "run") unmountRunView();
  focus(r.wf || null);
  if (r.name === "run" && channel) channel.postMessage({ wf: r.wf });
}

// ------------------------------------------------------------------ stream

let listTimer = null;
let lastListRefresh = 0;
let rowsTimer = null;
let alertId = 0;

function scheduleListRefresh() {
  if (listTimer) return;
  const wait = Math.max(500, 5000 - (Date.now() - lastListRefresh));
  listTimer = setTimeout(async () => {
    listTimer = null;
    lastListRefresh = Date.now();
    await refreshLists();
  }, wait);
}

function onEvent(name, data) {
  const st = store.state;
  switch (name) {
    case "hello": store.set({ config: data }); break;
    case "stack": store.set({ stack: data }); scheduleListRefresh(); break;
    case "runs": store.set({ runs: data }); break;
    case "run":
      if (data.workflow_id !== streamWf) break;
      store.set({ run: data, runMissing: null });
      if (data.rows_version !== st.rows.rows_version) {
        clearTimeout(rowsTimer);
        rowsTimer = setTimeout(async () => {
          if (store.state.run?.rows_version === store.state.rows.rows_version || streamWf !== data.workflow_id) return;
          try {
            const rows = await get(`/api/runs/${enc(data.workflow_id)}/rows`);
            if (streamWf === data.workflow_id) store.set({ rows: { ...rows, workflow_id: data.workflow_id } });
          } catch { /* the stream will catch up */ }
        }, 1500);
      }
      break;
    case "rows": {
      if (data.workflow_id !== streamWf) break;
      const cur = st.rows.workflow_id === data.workflow_id && !data.reset ? st.rows.rows : [];
      const have = new Set(cur.map((r) => r.seq));
      const rows = cur.concat((data.rows || []).filter((r) => !have.has(r.seq)));
      store.set({ rows: { workflow_id: data.workflow_id, t0: data.t0 || st.rows.t0, rows_version: data.rows_version, rows } });
      break;
    }
    case "job": upsertJob(data); break;
    case "process": upsertProcess(data); break;
    case "alert":
      if (data.level === "info") { toast(data.message, { hint: data.hint }); break; }
      if (!st.alerts.some((a) => a.message === data.message)) {
        alertId += 1;
        store.set({ alerts: [...st.alerts, { ...data, id: alertId }].slice(-3) });
      }
      break;
    default: break;
  }
}

// ------------------------------------------------------------------ main column

function renderHome(state, el) {
  const waiting = (state.runs?.runs || []).filter((r) => r.status === "RUNNING");
  el.innerHTML = String(h`<div class="welcome">
    <h1>Support asks Finance for a refund</h1>
    <p>Two teams, two namespaces, one published contract over Nexus. Pick a scenario from the list to check the stack and start a run. Every run you start opens here, with both workflows side by side.</p>
    ${waiting.length ? h`<p>Still running:</p><ul class="leftovers">${waiting.slice(0, 5).map((r) => h`<li><a class="id" href="#/runs/${enc(r.workflow_id)}">${r.workflow_id}</a>${stateGlyph(r.task_state, r.task_state || "running")}</li>`)}</ul>` : ""}
    <div class="acts"><a class="btn primary" href="#/scenarios/case-a">${icon("play")}Case A</a><a class="btn ghost" href="#/scenarios/case-b">${icon("play")}Case B</a>
    <button class="btn ghost" type="button" data-open-sheet>${icon("wrench")}Manage stack</button></div></div>`);
}

function renderMain(state) {
  const el = document.getElementById("main");
  const r = state.route;
  if (r.name === "run") { renderRunView(state, el); return; }
  if (r.name === "notes") return;
  // Keep focus and caret in the scenario form across re-renders.
  const a = document.activeElement;
  const keep = a && el.contains(a) && a.name ? { name: a.name, s: a.selectionStart, e: a.selectionEnd } : null;
  if (r.name === "scenario") renderScenarioCard(state, el); else renderHome(state, el);
  if (keep) {
    const f = el.querySelector(`[name="${keep.name}"]`);
    if (f) { f.focus(); try { f.setSelectionRange(keep.s, keep.e); } catch { /* number inputs */ } }
  }
}

// ------------------------------------------------------------------ boot

function wire() {
  store.subscribe("stackbar", ["config", "stack", "fixture"], renderStackBar);
  store.subscribe("banners", ["connection", "stack", "alerts", "config", "fixture"], renderBanners);
  store.subscribe("scenarios", ["scenarios", "route", "config"], renderScenarioList);
  store.subscribe("runs", ["runs", "route", "stack"], renderRuns);
  store.subscribe("main", ["route", "run", "rows", "scenarios", "stack", "ui", "config", "runs", "runMissing"], renderMain);
  store.subscribe("notes", ["route", "run", "scenarios", "processes", "actions", "ui", "config"], renderNotes);
  store.subscribe("sheet", ["processes", "jobs", "actions", "config", "ui"], renderSheet);
  wireStackBar(openSheet);
  wireLauncher();
  wireNotes();
  wireSheet();
  document.getElementById("side").insertAdjacentHTML("afterbegin",
    String(h`<button class="tbtn drawer-close" type="button" data-close-drawer>${icon("close")}Close</button>`));

  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-close-drawer]")) root.dataset.drawer = "closed";
    const tick = e.target.closest("[data-tick]");
    if (tick && store.state.run) { toggleTick(store.state.run.workflow_id, tick.dataset.tick); store.touch("run"); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, textarea, select, dialog") || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "n" || e.key === "N") toggleNotes();
    else if (e.key === "p" || e.key === "P") toggleProjector();
    else if (e.key === "Escape") root.dataset.drawer = "closed";
  });
  window.addEventListener("hashchange", route);
  if (channel) {
    channel.onmessage = (m) => {
      if (store.state.route.name === "notes" && m.data && m.data.wf) {
        store.set({ route: { name: "notes", wf: m.data.wf, scenario: null } });
        focus(m.data.wf);
      }
    };
  }
  // Live clocks: anything with data-since counts up from that instant.
  setInterval(() => {
    const now = Date.now();
    document.querySelectorAll("[data-since]").forEach((n) => {
      const t = clock((now - ms(n.dataset.since)) / 1000);
      if (n.textContent !== t) n.textContent = t;
    });
    if (store.state.run?.task?.state === "input-required") updateApproval(store.state);
  }, 1000);
}

async function boot() {
  wire();
  const fixtureName = q.get("fixture") || (q.get("demo") ? "case-b-replay" : null);
  if (fixtureName) {
    try {
      const fb = await loadFixture(fixtureName, { at: q.get("at"), verdict: q.get("verdict") });
      useBackend(fb);
      store.set({ fixture: { name: fb.name, description: fb.description, replay: !!q.get("demo") } });
      if (!location.hash || location.hash === "#" || location.hash === "#/") history.replaceState(null, "", `${location.search}${fb.route}`);
    } catch (err) {
      toast(String(err.message || err), { level: "error", hint: "Fixtures live in static/fixtures/." });
    }
  }
  try {
    store.set({ config: await get("/api/config") });
  } catch {
    store.set({ connection: "down" });
  }
  route();
  await refreshLists();
  lastListRefresh = Date.now();
  if (isFixture()) store.set({ connection: "up" });
}

boot();
