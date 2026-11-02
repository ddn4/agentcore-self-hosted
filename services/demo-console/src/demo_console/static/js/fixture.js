// Fixture mode: a fake backend that serves static/fixtures/<name>.json through the same
// get/post/openStream interface as js/api.js. No Temporal, no backend; for screenshots,
// review and offline rehearsal.
//
//   ?fixture=case-b-input-required   a still of one moment (Approve/Reject play the recording on)
//   ?demo=1                          replay a recorded Case B from the start, live
//   ?demo=1&at=gate                  jump to "Finance asks a human"
//   ?demo=1&at=end&verdict=reject    jump to the end of the approve or reject branch
//
// Every timestamp in a fixture is moved so that the recording's "now" is the page's now,
// so the live clocks (held open, no compute) tick from believable values.
import { ApiError } from "./api.js";

const ISO = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z$/;
const clone = (o) => (o === undefined ? undefined : JSON.parse(JSON.stringify(o)));

function shift(obj, deltaMs) {
  if (typeof obj === "string") return ISO.test(obj) ? new Date(Date.parse(obj) + deltaMs).toISOString() : obj;
  if (Array.isArray(obj)) return obj.map((x) => shift(x, deltaMs));
  if (obj && typeof obj === "object") {
    const out = {};
    for (const [k, v] of Object.entries(obj)) out[k] = shift(v, deltaMs);
    return out;
  }
  return obj;
}

const notFound = (what) => new ApiError(404, { error: { code: "not_found", message: `No ${what} in this recording.` } });
const recorded = () => new ApiError(409, { error: {
  code: "precondition_failed", message: "This is recorded data, so nothing was sent.",
  hint: "Run make console for the real thing, or open ?demo=1 to replay a Case B.",
} });

export async function loadFixture(name, params) {
  const url = new URL(`../fixtures/${encodeURIComponent(name)}.json`, import.meta.url);
  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`No fixture called ${name} (static/fixtures/${name}.json).`);
  const raw = await res.json();
  return new FixtureBackend(raw, params);
}

class FixtureBackend {
  constructor(raw, params) {
    this.raw = raw;
    this.params = params;
    this.listeners = new Set();
    this.timers = [];
    this.verdict = null;
    this.reset();
  }

  get name() { return this.raw.fixture; }
  get description() { return this.raw.description; }
  get isReplay() { return !!(this.raw.replay && this.raw.replay.frames && this.raw.replay.frames.length); }
  get route() { return this.raw.route || (this.raw.focus ? `#/runs/${encodeURIComponent(this.raw.focus)}` : "#/"); }

  /** Load the still state, then start (or jump through) the replay. */
  reset() {
    this.timers.forEach(clearTimeout);
    this.timers = [];
    const raw = this.raw;
    const rep = raw.replay || null;
    const frames = rep ? rep.frames || [] : [];
    const at = this.params.at;
    const now = Date.now();
    // Choose where "now" sits in the recording.
    let recNow = Date.parse(raw.now);
    let jumpTo = -1; // apply intro frames [0..jumpTo] instantly
    if (frames.length && (at === "gate" || at === "end")) {
      jumpTo = frames.length - 1;
      const gateT = frames[frames.length - 1].t;
      recNow = Date.parse(raw.now) + (gateT + 74) * 1000;
    }
    this.delta = now - recNow;
    this.introDelta = this.delta;
    const s = (o) => shift(clone(o), this.delta);
    this.state = {
      config: clone(raw.config), stack: s(raw.stack), runs: s(raw.runs),
      scenarios: s(raw.scenarios), actions: clone(raw.actions), processes: s(raw.processes),
      jobs: s(raw.jobs), run: s(raw.run), rows: s(raw.rows), report: s(raw.report),
    };
    this.extra = {};
    for (const [wf, d] of Object.entries(raw.extra_runs || {})) this.extra[wf] = s(d);
    this.gateOpen = !!(rep && (rep.start_at_gate || jumpTo >= 0));
    this.loadedAt = now;
    if (frames.length) {
      if (jumpTo >= 0) {
        for (let i = 0; i <= jumpTo; i += 1) this.apply(frames[i], this.delta, false);
      } else {
        // Live replay from the start, one frame at its recorded offset.
        frames.forEach((f, i) => {
          this.timers.push(setTimeout(() => {
            this.apply(f, this.delta, true);
            if (i === frames.length - 1) this.gateOpen = true;
          }, f.t * 1000));
        });
      }
    }
    if (at === "end" && rep && rep.branches) {
      const v = this.params.verdict === "reject" ? "reject" : "approve";
      this.decide(v, { instant: true });
    }
  }

  apply(frame, delta, emit, extraWait = 0) {
    const f = shift(clone(frame), delta);
    const st = this.state;
    if (f.stack) { st.stack = f.stack; if (emit) this.emit("stack", st.stack); }
    if (f.runs) { st.runs = f.runs; if (emit) this.emit("runs", st.runs); }
    if (f.run) {
      if (extraWait && f.run.metrics) {
        for (const k of ["approval_wait_s", "held_open_s"]) {
          if (typeof f.run.metrics[k] === "number") f.run.metrics[k] = +(f.run.metrics[k] + extraWait).toFixed(2);
        }
      }
      st.run = f.run;
    }
    if (f.rows && f.rows.length && st.run) {
      const t0 = st.rows && st.rows.t0 ? st.rows.t0 : st.run.started_at;
      const base = Date.parse(t0);
      const fixed = f.rows.map((r) => ({ ...r, t: +((Date.parse(r.at) - base) / 1000).toFixed(3) }));
      const prev = st.rows && st.rows.workflow_id === st.run.workflow_id ? st.rows.rows : [];
      st.rows = { workflow_id: st.run.workflow_id, t0, rows_version: prev.length + fixed.length, rows: prev.concat(fixed) };
      if (emit) this.emitRun({ workflow_id: st.run.workflow_id, rows_version: st.rows.rows_version, reset: false, rows: fixed });
    } else if (emit && f.run) {
      this.emitRun(null);
    }
  }

  /** Approve or reject at the gate: play the recorded branch from now. */
  decide(verdict, { instant = false } = {}) {
    const rep = this.raw.replay;
    const branch = rep && rep.branches && rep.branches[verdict];
    if (!branch || !this.gateOpen) return false;
    this.gateOpen = false;
    this.verdict = verdict;
    const now = Date.now();
    let delta = now - Date.parse(rep.gate.recorded_at);
    if (instant) {
      // Jump to the end: the last frame sits 3 s before now.
      const lastT = branch[branch.length - 1].t;
      delta -= (lastT + 3) * 1000;
    }
    const extraWait = (delta - this.introDelta) / 1000;
    if (rep.reports && rep.reports[verdict]) this.state.report = shift(clone(rep.reports[verdict]), delta);
    branch.forEach((f) => {
      if (instant) this.apply(f, delta, false, extraWait);
      else this.timers.push(setTimeout(() => this.apply(f, delta, true, extraWait), f.t * 1000));
    });
    return true;
  }

  emit(name, data) {
    for (const l of this.listeners) l.handlers.event(name, data);
  }

  emitRun(rowsEvent) {
    for (const l of this.listeners) {
      if (!this.state.run || l.wf !== this.state.run.workflow_id) continue;
      l.handlers.event("run", this.state.run);
      if (rowsEvent) l.handlers.event("rows", rowsEvent);
    }
  }

  runData(wf) {
    if (this.state.run && this.state.run.workflow_id === wf) return { run: this.state.run, rows: this.state.rows, report: this.state.report };
    return this.extra[wf] || null;
  }

  openStream(wf, handlers) {
    const l = { wf, handlers };
    this.listeners.add(l);
    setTimeout(() => {
      handlers.up();
      handlers.event("hello", this.state.config);
      handlers.event("stack", this.state.stack);
      handlers.event("runs", this.state.runs);
      const d = wf ? this.runData(wf) : null;
      if (d && d.run) {
        handlers.event("run", d.run);
        if (d.rows) handlers.event("rows", { ...d.rows, reset: true });
      }
    }, 0);
    return () => this.listeners.delete(l);
  }

  async get(path) {
    const u = new URL(path, "http://127.0.0.1");
    const p = u.pathname;
    const st = this.state;
    let m;
    if (p === "/healthz") return { status: "ok" };
    if (p === "/api/config") return st.config;
    if (p === "/api/stack") return st.stack;
    if (p === "/api/scenarios") return st.scenarios;
    if (p === "/api/actions") return st.actions;
    if (p === "/api/processes") return st.processes;
    if (p === "/api/jobs") return st.jobs;
    if ((m = p.match(/^\/api\/jobs\/([^/]+)$/))) {
      const j = (st.jobs.jobs || []).find((x) => x.id === decodeURIComponent(m[1]));
      if (!j) throw notFound("job");
      return j;
    }
    if ((m = p.match(/^\/api\/processes\/([^/]+)\/log$/))) {
      const name = decodeURIComponent(m[1]);
      return { name, lines: [`(recorded) ${name}: log lines are not part of this fixture.`] };
    }
    if (p === "/api/runs") return st.runs;
    if ((m = p.match(/^\/api\/runs\/([^/]+)(\/rows|\/report)?$/))) {
      const d = this.runData(decodeURIComponent(m[1]));
      if (!d || !d.run) throw notFound("run with that ID");
      if (m[2] === "/rows") return d.rows || { workflow_id: d.run.workflow_id, t0: d.run.started_at, rows_version: 0, rows: [] };
      if (m[2] === "/report") { if (!d.report) throw notFound("report"); return d.report; }
      return d.run;
    }
    throw notFound(`route ${p}`);
  }

  async post(path, body) {
    const p = new URL(path, "http://127.0.0.1").pathname;
    const st = this.state;
    let m;
    await new Promise((r) => setTimeout(r, 250)); // feel like a request
    if ((m = p.match(/^\/api\/runs\/([^/]+)\/(approve|reject)$/))) {
      const wf = decodeURIComponent(m[1]).replace(/^a2a-task-/, "");
      const verdict = m[2];
      const d = this.runData(wf);
      const state = d && d.run && d.run.task ? d.run.task.state : null;
      if (!body || !String(body.approver || "").trim()) {
        throw new ApiError(422, { error: { code: "invalid_param", message: "Enter who decides. Finance ignores a decision with no approver." } });
      }
      if (state !== "input-required" || !this.decide(verdict)) {
        throw new ApiError(409, { error: { code: "not_input_required", message: `The A2A task is ${state || "not started"}; approve and reject only apply to input-required.`, hint: "Open the run to see who decided." } });
      }
      return { workflow_id: wf, task_id: `a2a-task-${wf}`, verdict, state_before: "input-required", state_after: verdict === "approve" ? "completed" : "rejected", settled: true };
    }
    if (p === "/api/runs") {
      if (!this.isReplay && !(this.raw.replay && this.raw.replay.branches)) throw recorded();
      this.params = { ...this.params, at: null };
      if (!this.raw.replay.frames.length) throw recorded();
      this.reset();
      this.emit("stack", st.stack);
      this.emit("runs", this.state.runs);
      const wf = this.raw.focus;
      return { workflow_id: wf, run_id: "recorded", attached: false, links: (this.raw.replay.frames[0].run || {}).links || {} };
    }
    if ((m = p.match(/^\/api\/runs\/([^/]+)\/terminate$/))) return { terminated: true };
    if (p === "/api/profile") {
      st.config = { ...st.config, profile: body.profile };
      return st.config;
    }
    if ((m = p.match(/^\/api\/processes\/([^/]+)\/(start|stop)$/))) {
      const name = decodeURIComponent(m[1]);
      const list = st.processes.processes || [];
      const proc = list.find((x) => x.name === name);
      if (!proc) throw notFound("process");
      Object.assign(proc, m[2] === "start"
        ? { status: "running", pid: 9000 + list.indexOf(proc), started_at: new Date().toISOString(), variant: body.variant || proc.variant }
        : { status: "stopped", pid: null, started_at: null });
      this.emit("process", proc);
      return proc;
    }
    if ((m = p.match(/^\/api\/actions\/([^/]+)$/))) {
      const id = decodeURIComponent(m[1]);
      const a = (st.actions.actions || []).find((x) => x.id === id);
      if (!a) throw notFound("action");
      const t = new Date().toISOString();
      const job = { id: `j-recorded-${id}-${Date.now() % 100000}`, action: id, status: "succeeded", command: `make -s ${id}`, cost: a.cost, started_at: t, ended_at: t, exit_code: 0, log_tail: ["(recorded) nothing ran: this page is in fixture mode."], result: id === "invoke-local" ? { message: "worker starting", lifetime: 1, ping: "HealthyBusy" } : null };
      st.jobs.jobs = [job, ...(st.jobs.jobs || [])];
      this.emit("job", job);
      return job;
    }
    throw recorded();
  }
}
