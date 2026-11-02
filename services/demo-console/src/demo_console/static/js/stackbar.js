// The stack bar (profile, Support shape, model, component pips, Build ID, toggles) and the
// banners under it (Temporal unreachable, profile mismatch, backend lost, stream alerts).
import { post } from "./api.js";
import { startProcess } from "./actions.js";
import { PROFILE_LABEL, SHAPE_LABEL, h, icon } from "./format.js";
import { prefs, store } from "./store.js";
import { copyBox, toast, toastError } from "./ui.js";

const KEY_PIPS = new Set(["temporal", "finance", "support"]);
const root = document.documentElement;

// Parts the current Support shape does not use: down is not a fault for them (shown as off).
// The AgentCore app only matters in a serverless shape, the WCI emulator only with the WCI.
function notNeeded(c, shape) {
  if (c.status !== "down") return false;
  if (c.id === "agentcore") return shape === "plain" || shape === "unknown";
  if (c.id === "emulator") return shape !== "serverless-wci";
  return false;
}

function pip(c, shape) {
  if (notNeeded(c, shape)) {
    c = { ...c, status: "off", detail: `${c.detail || "not running"}; not used by this Support shape` };
  }
  const n = c.count !== undefined && c.count !== null ? h` <span class="n">${c.count}</span>` : "";
  const word = { up: "up", down: "down", zero: "running zero by design", degraded: "degraded", unknown: "not checked", external: "running outside the console", off: "off, not needed now" }[c.status] || c.status;
  return h`<li class="pip" data-s="${c.status}" ${KEY_PIPS.has(c.id) ? h`data-key` : ""}>
    <button type="button" data-open-sheet title="${c.label}: ${c.detail || word}"><span class="dot"></span>${c.label.replace(/ workers$/, "")}${n}<span class="sr"> ${word}</span></button></li>`;
}

function buildBadge(stack) {
  const b = stack?.build_id;
  if ((!b || !b.value) && stack?.shape === "plain") {
    // Plain workers on support-dev-tq are unversioned: nothing is missing.
    return h`<span class="build" title="Plain workers poll support-dev-tq without Worker Versioning. The serverless scenarios need a Build ID: commit uv.lock, or start the console with TEMPORAL_BUILD_ID=<id>."><span class="muted">Build</span> unversioned <span class="muted">(plain)</span></span>`;
  }
  if (!b || !b.value) {
    return h`<span class="build warn" title="Commit uv.lock, or start the console with TEMPORAL_BUILD_ID=<id>.">${icon("warn")}No Build ID</span>`;
  }
  const dep = stack.deployment || {};
  const cur = dep.current_build === b.value;
  const words = [cur ? "current" : dep.current_build ? `current is ${dep.current_build}` : null,
    dep.behavior === "PINNED" ? "pinned" : null].filter(Boolean).join(", ");
  return h`<span class="build" title="Build ID from ${b.source}"><span class="muted">Build</span> <span class="id">${b.value}</span> <span class="muted">${words}</span></span>`;
}

export function renderStackBar(state) {
  const el = document.getElementById("stackbar");
  const cfg = state.config || {};
  const stack = state.stack;
  const profiles = cfg.profiles || ["local", "kind", "kind-plaintext", "eks-pf"];
  const comps = stack?.components || [];
  const counted = comps.filter((c) => !notNeeded(c, stack?.shape));
  const upCount = counted.filter((c) => c.status === "up" || c.status === "zero" || c.status === "external").length;
  const fx = state.fixture;
  el.innerHTML = String(h`
    <button class="tbtn navbtn" type="button" id="t-drawer" aria-label="Scenarios and runs">${icon("menu")}</button>
    <span class="brand"><span class="mark" aria-hidden="true"><i></i><i></i><i></i></span><span><span class="full">AgentCore demo </span>console</span></span>
    ${fx ? h`<span class="recorded" title="${fx.description || ""}">${icon("replay")}${fx.replay ? "Recorded replay" : "Recorded data"}</span>` : ""}
    <div class="seg" role="group" aria-label="Temporal profile">
      ${profiles.map((p) => h`<button type="button" data-profile="${p}" aria-pressed="${String(p === cfg.profile)}">${PROFILE_LABEL[p] || p}</button>`)}
    </div>
    <div class="shape"><span>Support <b>${SHAPE_LABEL[stack?.shape || "unknown"] || stack?.shape}</b></span><span>Model <b>${stack?.model?.mode || "unknown"}</b></span></div>
    <ul class="pips" aria-label="Components">
      ${comps.map((c) => pip(c, stack?.shape))}
      <li class="pip summary" data-s="${upCount === counted.length && counted.length ? "up" : "degraded"}"><button type="button" data-open-sheet><span class="dot"></span>${upCount} of ${counted.length} up</button></li>
    </ul>
    ${buildBadge(stack)}
    <div class="tools">
      <button class="tbtn" type="button" data-open-sheet>${icon("wrench")}<span class="wide">Manage stack</span><span class="sr">Manage stack</span></button>
      <button class="tbtn" type="button" id="t-notes" aria-pressed="${String(root.dataset.notes !== "off")}" aria-label="Presenter notes" title="Presenter notes (N)">${icon("notes")}<kbd class="wide">N</kbd></button>
      <button class="tbtn wide" type="button" id="t-proj" aria-pressed="${String(root.dataset.projector === "on")}" aria-label="Projector mode" title="Projector mode (P)">${icon("screen")}<kbd>P</kbd></button>
      <button class="tbtn" type="button" id="t-theme" aria-label="Theme: ${root.dataset.theme || "dark"}" title="Theme: ${root.dataset.theme || "dark"} (switch to ${root.dataset.theme === "light" ? "dark" : "light"})">${icon("contrast")}</button>
    </div>`);
}

export function renderBanners(state) {
  const el = document.getElementById("banners");
  const out = [];
  const cfg = state.config || {};
  if (state.connection === "down" && !state.fixture) {
    out.push(h`<div class="banner" data-level="error">${icon("warn")}<p>The console backend stopped answering. The page reconnects by itself when it is back.</p>${copyBox("make console")}</div>`);
  }
  const t = (state.stack?.components || []).find((c) => c.id === "temporal");
  if (t && t.status === "down") {
    const local = cfg.profile === "local";
    out.push(h`<div class="banner" data-level="error">${icon("warn")}<p>${(t.detail || "Temporal is not reachable at 127.0.0.1:7233").replace(/\.?$/, ".")} ${local ? "Start the dev server." : `Run the port-forward for ${cfg.profile}.`}</p>
      ${local ? h`<button class="btn small primary" type="button" data-start-dev>${icon("play")}Start dev server</button>`
        : copyBox(`make pf TEMPORAL_PROFILE=${cfg.profile}`)}</div>`);
  }
  const mm = state.stack?.profile?.mismatch;
  if (mm) {
    const det = state.stack.profile.detected;
    out.push(h`<div class="banner" data-level="warn">${icon("warn")}<p>${mm}</p>${det ? h`<button class="btn small ghost" type="button" data-profile="${det}">Switch to ${det}</button>` : ""}</div>`);
  }
  // The stack banner above already says Temporal is down, with the fix; the stream's alert
  // carries the raw connection error and would only repeat it.
  const stackKnown = !!t;
  for (const a of state.alerts || []) {
    if (stackKnown && /^Temporal is not reachable/.test(a.message || "")) continue;
    out.push(h`<div class="banner" data-level="${a.level}">${icon("warn")}<p>${a.message}${a.hint ? h` <span class="muted">${a.hint}</span>` : ""}</p><button class="tbtn" type="button" data-dismiss="${a.id}" aria-label="Dismiss">${icon("close")}</button></div>`);
  }
  el.innerHTML = out.map(String).join("");
}

async function switchProfile(p) {
  const cfg = store.state.config || {};
  if (p === cfg.profile) return;
  try {
    const res = await post("/api/profile", { profile: p });
    store.set({ config: res });
    toast(`Switched to profile ${p}.`, { level: "ok" });
  } catch (err) {
    const procs = err.extra?.processes;
    if (procs && procs.length) {
      toast(`Stop these first: ${procs.join(", ")}.`, { level: "warn", hint: "They were started under another profile." });
    } else toastError(err);
  }
}

export function setTheme(t) {
  // Dark is the brand default (docs/console/BRAND.md); light is the one alternative.
  root.dataset.theme = t === "light" ? "light" : "dark";
  prefs.write({ theme: root.dataset.theme });
  store.touch("config");
}
export function toggleNotes() {
  root.dataset.notes = root.dataset.notes === "off" ? "on" : "off";
  // Remembered per mode: on the projector the notes are off unless turned on there.
  prefs.write(root.dataset.projector === "on" ? { projectorNotes: root.dataset.notes } : { notes: root.dataset.notes });
  store.touch("config", "run");
}
export function toggleProjector() {
  root.dataset.projector = root.dataset.projector === "on" ? "off" : "on";
  const p = prefs.read();
  // The projector shows the lanes, not the presenter's notes (those go to #/notes on the laptop).
  root.dataset.notes = root.dataset.projector === "on" ? (p.projectorNotes || "off") : (p.notes || "on");
  prefs.write({ projector: root.dataset.projector });
  store.touch("config", "run");
}

export function wireStackBar(openSheet) {
  document.addEventListener("click", (e) => {
    const t = e.target;
    if (t.closest("[data-open-sheet]")) { openSheet(); return; }
    const pb = t.closest("[data-profile]");
    if (pb) { switchProfile(pb.dataset.profile); return; }
    if (t.closest("#t-notes")) { toggleNotes(); return; }
    if (t.closest("#t-proj")) { toggleProjector(); return; }
    if (t.closest("#t-theme")) {
      setTheme(root.dataset.theme === "light" ? "dark" : "light");
      return;
    }
    if (t.closest("#t-drawer")) { root.dataset.drawer = root.dataset.drawer === "open" ? "closed" : "open"; return; }
    if (t.closest("[data-start-dev]")) { startProcess("dev-server", { wci: 0 }); return; }
    const d = t.closest("[data-dismiss]");
    if (d) store.set({ alerts: store.state.alerts.filter((a) => String(a.id) !== d.dataset.dismiss) });
  });
}
