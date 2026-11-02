// Pure helpers: HTML escaping and templating, icons, time and money formatting.

class Raw {
  constructor(s) { this.s = s; }
  toString() { return this.s; }
}

/** Mark a string as already-safe HTML. */
export const raw = (s) => new Raw(String(s ?? ""));

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function part(v) {
  if (v === null || v === undefined || v === false) return "";
  if (v instanceof Raw) return v.s;
  if (Array.isArray(v)) return v.map(part).join("");
  return esc(v);
}

/** Tagged template: interpolations are escaped unless wrapped in raw() or produced by h``. */
export function h(strings, ...vals) {
  let out = strings[0];
  for (let i = 0; i < vals.length; i += 1) out += part(vals[i]) + strings[i + 1];
  return new Raw(out);
}

export const icon = (id, cls = "") => raw(`<svg class="i ${cls}" aria-hidden="true"><use href="#i-${id}"/></svg>`);

export const ms = (iso) => (iso ? Date.parse(iso) : NaN);

/** 0:07, 2:14, 1:02:03 */
export function clock(s) {
  if (!Number.isFinite(s) || s < 0) s = 0;
  s = Math.floor(s);
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  const p = (n) => String(n).padStart(2, "0");
  return hh ? `${hh}:${p(mm)}:${p(ss)}` : `${mm}:${p(ss)}`;
}

/** The live clock's text right now, so a re-render never flashes 0:00 (app.js ticks it). */
export const sinceText = (iso) => clock((Date.now() - ms(iso)) / 1000);

/** Durations in plain words: 0.42 s, 9.1 s, 46 s, 2:14, 3 h 12 min. */
export function dur(s) {
  if (s === null || s === undefined || !Number.isFinite(s)) return "–";
  if (s < 10) return `${s.toFixed(s < 1 ? 2 : 1)} s`;
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return clock(s);
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60);
  return mm ? `${hh} h ${mm} min` : `${hh} h`;
}

/** Offset since t0 for the time column: +0.41 s, +23.1 s, +1:42, +1:02:03. */
export function offset(t) {
  if (!Number.isFinite(t)) return "";
  if (t < 10) return `+${t.toFixed(2)} s`;
  if (t < 60) return `+${t.toFixed(1)} s`;
  return `+${clock(t)}`;
}

export function hhmm(iso) {
  const t = ms(iso);
  if (!Number.isFinite(t)) return "";
  const d = new Date(t);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export function ago(iso, now = Date.now()) {
  const s = (now - ms(iso)) / 1000;
  if (!Number.isFinite(s)) return "";
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return hhmm(iso);
}

export const money = (n) => (n === null || n === undefined ? "–"
  : `$${Number(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);

export const moneyShort = (n) => (n === null || n === undefined ? "" : `$${Math.round(Number(n))}`);

/** Parse Temporal duration text like "25h", "90m", "1h30m", "45s" into seconds. */
export function parseDuration(text) {
  const m = String(text || "").trim().match(/^(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?$/);
  if (!m || !(m[1] || m[2] || m[3])) return null;
  return (Number(m[1] || 0) * 3600) + (Number(m[2] || 0) * 60) + Number(m[3] || 0);
}

export const COST_LABEL = { free: "Free", tokens: "Uses Bedrock tokens", aws: "Calls AWS" };
export function costMark(cost, short = false) {
  if (!cost) return "";
  const ic = cost === "aws" ? "cloud" : cost === "tokens" ? "coin" : null;
  if (!ic && short) return "";
  return h`<span class="cost" data-c="${cost}">${ic ? icon(ic) : ""}${short && cost === "aws" ? "AWS" : short && cost === "tokens" ? "Tokens" : COST_LABEL[cost] || cost}</span>`;
}

/** A2A states -> glyph id (DESIGN 3.2: colour + shape + word). */
export const STATE_GLYPH = {
  submitted: "ring", working: "dot", "input-required": "hold", completed: "check",
  rejected: "x", failed: "broken", canceled: "ring", unreachable: "broken",
};

export const TERMINAL = new Set(["completed", "rejected", "failed", "canceled"]);

export function stateGlyph(state, label = state) {
  const s = state || "none";
  return h`<span class="sg" data-s="${s}">${icon(STATE_GLYPH[s] || "ring")}${label}</span>`;
}

export const SHAPE_LABEL = {
  plain: "plain worker",
  "serverless-invoke": "serverless, invoke-local",
  "serverless-wci": "serverless, WCI emulator",
  "agentcore-aws": "AgentCore in AWS",
  unknown: "not detected",
};

export const PROFILE_LABEL = { local: "local", kind: "kind", "kind-plaintext": "kind plain", "eks-pf": "eks-pf" };

/** Short base-36 suffix for fresh workflow IDs (never 12 digits: scrub-check). */
export function suffix() {
  return Math.floor(Math.random() * 36 ** 4).toString(36).padStart(4, "0");
}
