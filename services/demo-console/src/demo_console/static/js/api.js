// REST + SSE client for the console backend (DESIGN.md section 5). In fixture mode the same
// interface is served by js/fixture.js from static/fixtures/<name>.json.

export class ApiError extends Error {
  constructor(status, body) {
    const e = (body && body.error) || {};
    super(e.message || `The console backend answered HTTP ${status}.`);
    this.status = status;
    this.code = e.code || (status === 0 ? "network" : "http_error");
    this.hint = e.hint || null;
    this.extra = e;
  }
}

let backend = null;

/** Route every call to a fake backend (fixture mode). */
export function useBackend(b) { backend = b; }
export const isFixture = () => backend !== null;

async function parse(res) {
  let body = null;
  const text = await res.text();
  if (text) {
    try { body = JSON.parse(text); } catch { body = { error: { message: text.slice(0, 300) } }; }
  }
  if (!res.ok) throw new ApiError(res.status, body);
  return body;
}

export async function get(path) {
  if (backend) return backend.get(path);
  let res;
  try {
    res = await fetch(path, { headers: { Accept: "application/json" }, cache: "no-store" });
  } catch {
    throw new ApiError(0, { error: { message: "The console backend is not reachable.", hint: "Start it with make console." } });
  }
  return parse(res);
}

export async function post(path, body = {}) {
  if (backend) return backend.post(path, body);
  let res;
  try {
    res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Console": "1", Accept: "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, { error: { message: "The console backend is not reachable.", hint: "Start it with make console." } });
  }
  return parse(res);
}

export const EVENTS = ["hello", "stack", "runs", "run", "rows", "job", "process", "alert"];

/**
 * Open the SSE stream for the focused run (or none). handlers: {event(name, data), up(), down()}.
 * Returns a close function.
 */
export function openStream(wf, handlers) {
  if (backend) return backend.openStream(wf, handlers);
  const url = wf ? `/api/stream?wf=${encodeURIComponent(wf)}` : "/api/stream";
  const es = new EventSource(url);
  for (const name of EVENTS) {
    es.addEventListener(name, (e) => {
      let data = null;
      try { data = JSON.parse(e.data); } catch { return; }
      handlers.event(name, data);
    });
  }
  es.onopen = () => handlers.up();
  es.onerror = () => handlers.down();
  return () => es.close();
}

export const enc = encodeURIComponent;
