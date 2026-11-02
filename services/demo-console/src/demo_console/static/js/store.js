// One state object; renderers subscribe to the keys they read and run once per frame.

const state = {
  config: null,        // /api/config
  stack: null,         // StackSnapshot
  runs: null,          // /api/runs body
  scenarios: [],       // Scenario[]
  actions: [],         // Action[]
  processes: [],       // Process[]
  jobs: [],            // Job[]
  run: null,           // RunView of the focused run
  rows: { workflow_id: null, t0: null, rows_version: 0, rows: [] },
  route: { name: "home", wf: null, scenario: null },
  connection: "connecting",  // connecting | up | down
  alerts: [],          // banner-level alerts from the stream
  fixture: null,       // {name, description, replay: bool} in fixture mode
  ui: { busy: {} },    // in-flight actions, keyed
};

const subs = [];
const dirty = new Set();
let scheduled = false;

function flush() {
  scheduled = false;
  const keys = new Set(dirty);
  dirty.clear();
  for (const s of subs) {
    if (s.keys.some((k) => keys.has(k))) {
      try { s.fn(state); } catch (err) { console.error("render failed", s.name, err); }
    }
  }
}

export const store = {
  get state() { return state; },
  /** Merge a patch into state and schedule the renderers that read any changed key. */
  set(patch) {
    for (const [k, v] of Object.entries(patch)) {
      state[k] = v;
      dirty.add(k);
    }
    if (!scheduled) {
      scheduled = true;
      (typeof requestAnimationFrame === "function" ? requestAnimationFrame : setTimeout)(flush);
    }
  },
  touch(...keys) { this.set(Object.fromEntries(keys.map((k) => [k, state[k]]))); },
  subscribe(name, keys, fn) { subs.push({ name, keys, fn }); },
  busy(key, on) {
    const b = { ...state.ui.busy };
    if (on) b[key] = true; else delete b[key];
    this.set({ ui: { ...state.ui, busy: b } });
  },
};

/** Per-viewer conveniences only (theme, notes, manual ticks). Storage can be unavailable. */
export const prefs = {
  read() {
    try { return JSON.parse(localStorage.getItem("console.ui") || "{}") || {}; } catch { return {}; }
  },
  write(patch) {
    try {
      const cur = this.read();
      localStorage.setItem("console.ui", JSON.stringify({ ...cur, ...patch }));
    } catch { /* storage blocked: the UI still works */ }
  },
};
