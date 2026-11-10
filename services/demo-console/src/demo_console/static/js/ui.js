// Toasts, the confirm dialog, copy-to-clipboard and the cost gate shared by every action.
import { ApiError } from "./api.js";
import { esc, h, icon } from "./format.js";
import { store } from "./store.js";

const $ = (s) => document.querySelector(s);

export function toast(message, { level = "info", hint = null, ms = 5200 } = {}) {
  const box = $("#toasts");
  if (!box) return;
  const el = document.createElement("div");
  el.className = "toast";
  el.dataset.level = level;
  el.innerHTML = `<p>${esc(message)}</p>${hint ? `<p class="hint">${esc(hint)}</p>` : ""}`;
  box.append(el);
  while (box.children.length > 4) box.firstElementChild.remove();
  setTimeout(() => el.remove(), level === "error" ? ms * 1.6 : ms);
}

export function toastError(err) {
  if (err instanceof ApiError) toast(err.message, { level: "error", hint: err.hint });
  else toast(String(err && err.message ? err.message : err), { level: "error" });
}

/** Ask before anything that costs tokens. Resolves true when confirmed. */
export function confirmDialog({ title, body, cost = null, confirmLabel = "Continue", danger = false, command = null }) {
  const dlg = $("#confirm");
  const costLine = cost === "tokens"
    ? h`<p>${icon("coin")} This uses the live model on Bedrock and costs tokens.</p>`
    : "";
  dlg.innerHTML = String(h`<form method="dialog">
    <h2 id="confirm-title">${icon(cost === "tokens" ? "coin" : "warn")}${title}</h2>
    ${body ? h`<p>${body}</p>` : ""}${costLine}
    ${command ? h`<div class="copy"><code>${command}</code></div>` : ""}
    <div class="acts">
      <button class="btn ghost" value="cancel" type="submit">Cancel</button>
      <button class="btn ${danger ? "no" : "primary"}" value="ok" type="submit" autofocus>${confirmLabel}</button>
    </div></form>`);
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
    dlg.showModal();
  });
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied to the clipboard.", { level: "ok", ms: 1800 });
  } catch {
    toast("Copy is blocked here. Select the text and copy it by hand.", { level: "warn" });
  }
}

export const copyBox = (text) => h`<div class="copy"><code>${text}</code><button type="button" data-copy="${text}" aria-label="Copy command" title="Copy">${icon("copy")}</button></div>`;

/**
 * Run a mutating call behind the cost gate. `call(confirm)` performs the request.
 * Asks first when it costs tokens or when `ask` is set (long or destructive jobs).
 * Handles the server's own 409 confirm_required by asking and retrying once.
 */
export async function gated({ key, cost = "free", ask = false, danger = false, title, body = null, command = null, confirmLabel = null, ok = null, call }) {
  let confirm = false;
  const label = confirmLabel || title;
  if (cost === "tokens" || ask) {
    confirm = await confirmDialog({ title, body, cost, command, confirmLabel: label, danger });
    if (!confirm) return null;
  }
  store.busy(key, true);
  try {
    const res = await call(confirm);
    if (ok) toast(typeof ok === "function" ? ok(res) : ok, { level: "ok" });
    return res;
  } catch (err) {
    if (err instanceof ApiError && err.code === "confirm_required" && !confirm) {
      const yes = await confirmDialog({ title, body: err.message, cost, command, confirmLabel: label, danger });
      if (yes) {
        try {
          const res = await call(true);
          if (ok) toast(typeof ok === "function" ? ok(res) : ok, { level: "ok" });
          return res;
        } catch (err2) { toastError(err2); }
      }
      return null;
    }
    toastError(err);
    return null;
  } finally {
    store.busy(key, false);
  }
}

// Delegated copy buttons anywhere on the page.
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-copy]");
  if (b) { e.preventDefault(); copyText(b.dataset.copy); }
});

// Disclosures that survive re-renders: a <details data-keep="key"> keeps what the presenter
// chose. Only clicks on the summary are remembered, so a default (open when something failed)
// still follows the state until someone overrides it.
const keptOpen = new Map();
document.addEventListener("click", (e) => {
  const s = e.target.closest("details[data-keep] > summary");
  if (s) keptOpen.set(s.parentElement.dataset.keep, !s.parentElement.open);
});
/** `open` for a kept disclosure: what the presenter chose, else `dflt`. */
export const openAttr = (key, dflt = false) => ((keptOpen.has(key) ? keptOpen.get(key) : dflt) ? "open" : "");

/**
 * Replace el's HTML only when it changed, so a re-render with nothing new (an unrelated stream
 * event) keeps focus, typed values, open disclosures and scroll. When it does write, focus
 * goes back to the element with the same name and data-* attributes, caret included.
 * Returns true when it wrote. Code that writes el some other way sets el.__html = null.
 */
export function patch(el, html) {
  const s = String(html);
  if (el.__html === s) return false;
  const a = document.activeElement;
  let sel = null;
  let caret = null;
  if (a && a !== el && el.contains(a)) {
    const keys = [...a.attributes].filter((x) => x.name === "name" || x.name.startsWith("data-"));
    if (keys.length) sel = a.tagName.toLowerCase() + keys.map((x) => `[${x.name}="${CSS.escape(x.value)}"]`).join("");
    try { caret = [a.selectionStart, a.selectionEnd]; } catch { /* number inputs have none */ }
  }
  el.__html = s;
  el.innerHTML = s;
  const f = sel && el.querySelector(sel);
  if (f) {
    f.focus({ preventScroll: true });
    if (caret && caret[0] !== null) { try { f.setSelectionRange(caret[0], caret[1]); } catch { /* not a text field */ } }
  }
  return true;
}
