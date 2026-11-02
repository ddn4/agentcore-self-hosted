// The approval card: Finance's human decision, shown in Finance's lane on the live "now" row.
// One DOM node for the page, moved between renders, so typed values and focus survive updates.
import { decide } from "./actions.js";
import { dur, money, ms } from "./format.js";
import { store } from "./store.js";

let node = null;
let forWf = null;
const DEFAULT_COMMENT = "photos checked";

export function approvalNode() {
  if (node) return node;
  node = document.createElement("form");
  node.className = "approval";
  node.id = "approval";
  node.setAttribute("aria-label", "Approve or reject the refund");
  node.noValidate = true;
  node.innerHTML = `<div class="in">
      <h3><svg class="i" aria-hidden="true"><use href="#i-hold"/></svg>Finance asks a human</h3>
      <dl class="akv">
        <div><dt>Amount</dt><dd data-f="amount"></dd></div>
        <div><dt>Auto-approve limit</dt><dd data-f="limit"></dd></div>
        <div><dt>Risk</dt><dd data-f="risk"></dd></div>
        <div><dt>Finance gives up</dt><dd data-f="deadline"></dd></div>
      </dl>
      <div class="form">
        <label>Approver<input name="approver" autocomplete="off" spellcheck="false" required></label>
        <label>Comment<input name="comment" autocomplete="off" value="${DEFAULT_COMMENT}"></label>
        <div class="acts">
          <button class="btn ok" type="submit" data-verdict="approve"><svg class="i" aria-hidden="true"><use href="#i-check"/></svg>Approve refund</button>
          <button class="btn no" type="button" data-verdict="reject"><svg class="i" aria-hidden="true"><use href="#i-x"/></svg>Reject refund</button>
        </div>
      </div>
      <p class="err" data-f="err" hidden></p>
      <div class="drainok"><span class="lamp" data-f="lamp"></span><span data-f="drain"></span></div>
    </div>`;
  node.addEventListener("submit", (e) => { e.preventDefault(); submit("approve"); });
  node.querySelector('[data-verdict="reject"]').addEventListener("click", () => submit("reject"));
  return node;
}

async function submit(verdict) {
  const st = store.state;
  const run = st.run;
  if (!run) return;
  const approver = node.querySelector("[name=approver]").value.trim();
  let comment = node.querySelector("[name=comment]").value.trim();
  // The prefilled comment is an approver's note; it would read as the reason for a rejection.
  if (verdict === "reject" && comment === DEFAULT_COMMENT) comment = "";
  const err = node.querySelector('[data-f="err"]');
  if (!approver) {
    err.hidden = false;
    err.textContent = "Enter who decides. Finance ignores a decision with no approver.";
    node.querySelector("[name=approver]").focus();
    return;
  }
  err.hidden = true;
  const btns = node.querySelectorAll("button");
  btns.forEach((b) => { b.disabled = true; });
  node.querySelector(`[data-verdict="${verdict}"]`).classList.add("busy");
  const res = await decide(run.workflow_id, verdict, approver, comment);
  btns.forEach((b) => { b.disabled = false; b.classList.remove("busy"); });
  if (res) node.classList.add("done");
}

const riskOf = (task) => task?.artifact?.risk || ((task?.message || "").match(/\brisk\s+(LOW|MEDIUM|HIGH)\b/i) || [])[1]?.toUpperCase() || "–";

/** Refresh the card's read-only facts (never the inputs). */
export function updateApproval(state) {
  const n = approvalNode();
  const run = state.run;
  if (!run) return;
  if (forWf !== run.workflow_id) {
    forWf = run.workflow_id;
    n.classList.remove("done");
    n.querySelector("[name=approver]").value = state.config?.default_approver || "dana.finance";
  }
  if (run.task?.state === "input-required") n.classList.remove("done");
  const task = run.task || {};
  const set = (f, v) => { const el = n.querySelector(`[data-f="${f}"]`); if (el && el.textContent !== v) el.textContent = v; };
  set("amount", money(run.amount_usd));
  set("limit", money(state.config?.auto_approve_limit_usd ?? 100));
  set("risk", riskOf(task));
  const dl = task.approval?.deadline;
  const left = dl ? (ms(dl) - Date.now()) / 1000 : null;
  set("deadline", left !== null ? `in ${dur(Math.max(0, left))}` : task.approval?.timeout_text ? `after ${task.approval.timeout_text}` : "–");
  const c = run.compute || {};
  const lamp = n.querySelector('[data-f="lamp"]');
  lamp.classList.toggle("lit", !!c.running);
  const drained = c.drain?.state === "drained";
  const serverless = run.task_queue === "support-tq";
  let line;
  if (!c.running && (c.lifetimes || []).length) {
    line = `Support has no compute right now${c.drain?.estimated ? " (estimated)" : ""}. Approving signals the A2A task in agent-services, and the server brings Support back.`;
  } else if (c.running && serverless && !drained) {
    line = "Support's worker is still up. Wait for it to drain to show scale to zero, or approve now.";
  } else {
    line = "Approving signals the A2A task in agent-services. Support picks up Finance's answer from history.";
  }
  set("drain", line);
  n.querySelector('[data-verdict="approve"]').title = `Signal approve to a2a-task-${run.workflow_id}`;
}
