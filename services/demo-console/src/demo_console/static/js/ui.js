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

/** Ask before anything that costs tokens or calls AWS. Resolves true when confirmed. */
export function confirmDialog({ title, body, cost = null, confirmLabel = "Continue", danger = false, command = null }) {
  const dlg = $("#confirm");
  const costLine = cost === "aws"
    ? h`<p>${icon("cloud")} This calls AWS. On a paused stack it fails; on a running one it can cost money.</p>`
    : cost === "tokens"
      ? h`<p>${icon("coin")} This uses the live model on Bedrock and costs tokens.</p>`
      : "";
  dlg.innerHTML = String(h`<form method="dialog">
    <h2 id="confirm-title">${icon(cost === "aws" ? "cloud" : cost === "tokens" ? "coin" : "warn")}${title}</h2>
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
 * Handles the server's own 409 confirm_required by asking and retrying once.
 */
export async function gated({ key, cost = "free", title, body = null, command = null, ok = null, call }) {
  const cfg = store.state.config || {};
  if (cost === "aws" && !cfg.allow_aws) {
    toast("AWS actions are off in this console.", { level: "warn", hint: "Restart it with CONSOLE_ALLOW_AWS=1 make console to allow them." });
    return null;
  }
  let confirm = false;
  if (cost === "tokens" || cost === "aws") {
    confirm = await confirmDialog({ title, body, cost, command, confirmLabel: title });
    if (!confirm) return null;
  }
  store.busy(key, true);
  try {
    const res = await call(confirm);
    if (ok) toast(typeof ok === "function" ? ok(res) : ok, { level: "ok" });
    return res;
  } catch (err) {
    if (err instanceof ApiError && err.code === "confirm_required" && !confirm) {
      const yes = await confirmDialog({ title, body: err.message, cost: cost === "free" ? "tokens" : cost, command, confirmLabel: title });
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
