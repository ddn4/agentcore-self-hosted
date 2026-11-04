// The lanes: time | compute rail | Support | Nexus seam | Finance (DESIGN 2.1 item 4, 5.3 rows).
// Rows pair into wires across the seam; gaps over 4 s become bands; the last row is a live
// "now" row that carries the approval card while the task is input-required.
import { dur, h, icon, ms, offset, raw, sinceText } from "./format.js";
import { approvalNode } from "./approval.js";

const GAP_S = 4;
const PAIR_S = 0.2;

const LANE_CLS = { support: "sup", compute: "cmp", wci: "wci", finance: "fin", human: "hum", nexus: "nx" };
const LANE_WORD = { sup: "Support", cmp: "Support compute", wci: "Temporal server", fin: "Finance", hum: "Finance approver", nx: "Support" };

const GLYPH = {
  "run.started": "start", "run.completed": "check", "run.failed": "x", "run.terminated": "x",
  "model.turn": "think", activity: "tool", wft: "power",
  "nexus.scheduled": "seam", "nexus.started": "seam", "nexus.attempt_failed": "warn",
  "nexus.completed": "seam", "nexus.failed": "x",
  "task.started": "start", "timer.started": "clock", "timer.fired": "clock",
  "signal.approve": "person", "signal.reject": "person", "refund.issued": "receipt",
  "lifetime.start": "power", "lifetime.connected": "power", "lifetime.idle": "power",
  "lifetime.drained": "power", "lifetime.released": "power", "lifetime.killed": "bolt",
  "wci.signal": "signal", "wci.invoke": "signal", "wci.failed": "warn",
};
const STATE_ICON = { submitted: "ring", working: "dot", "input-required": "hold", completed: "check", rejected: "x", failed: "broken", canceled: "ring" };

const isDisco = (r) => /get_agent_card/.test(`${r.detail || ""} ${r.title || ""}`);
const opName = (r) => ((`${r.detail || ""} ${r.title || ""}`).match(/(send_message|get_agent_card)/) || [])[1] || "Nexus";

/** Turn API rows into display items: crossings (out/back), discovery, stubs and singles. */
export function pairRows(rows) {
  const used = new Set();
  const items = [];
  for (let i = 0; i < rows.length; i += 1) {
    const r = rows[i];
    if (used.has(r.seq)) continue;
    if (r.pair) {
      let partner = null;
      for (let j = i + 1; j < rows.length; j += 1) {
        const o = rows[j];
        if (o.pair === r.pair && !used.has(o.seq)) { partner = o; break; }
        if (o.t - r.t > PAIR_S) break;
      }
      if (partner && Math.abs(partner.t - r.t) <= PAIR_S) {
        used.add(r.seq); used.add(partner.seq);
        const nx = r.lane === "nexus" ? r : partner;
        const other = nx === r ? partner : r;
        if (other.lane === "nexus") items.push({ type: "disco", t: r.t, a: r, b: partner });
        else if (nx.kind === "nexus.scheduled" || nx.kind === "nexus.started") items.push({ type: "out", t: Math.min(r.t, partner.t), nx, fin: other });
        else items.push({ type: "back", t: Math.min(r.t, partner.t), nx, fin: other });
        continue;
      }
    }
    used.add(r.seq);
    items.push({ type: r.lane === "nexus" ? "stub" : "single", t: r.t, r });
  }
  return items;
}

function lifetimesOf(run, t0) {
  const c = run?.compute;
  if (!c) return [];
  return (c.lifetimes || []).map((l, i, arr) => {
    const s = (ms(l.start) - t0) / 1000;
    let e = l.end ? (ms(l.end) - t0) / 1000 : Infinity;
    if (!l.end && i < arr.length - 1) e = (ms(arr[i + 1].start) - t0) / 1000;
    return { n: l.n, label: l.label || `lifetime ${l.n}`, s, e, reason: l.end_reason };
  });
}

function railAt(t, lts) {
  if (!lts.length) return "none";
  if (t < lts[0].s - 0.001) return "none";
  return lts.some((l) => t >= l.s - 0.001 && t <= l.e + 0.001) ? "lit" : "off";
}

/** Intervals during which a send_message is held open by the server. */
function heldIntervals(items, run) {
  const out = [];
  let open = null;
  for (const it of items) {
    const r = it.type === "stub" ? it.r : it.type === "out" || it.type === "back" ? it.nx : null;
    if (!r || it.type === "disco" || isDisco(r)) continue;
    if (open === null && (it.type === "out" || r.kind === "nexus.scheduled")) open = { a: it.t, aItem: it };
    else if (open !== null && (it.type === "back" || r.kind === "nexus.completed" || r.kind === "nexus.failed")) {
      out.push({ ...open, b: it.t, bItem: it });
      open = null;
    }
  }
  const pending = (run?.support?.pending_nexus || []).some((p) => p.operation !== "get_agent_card");
  if (open !== null && (pending || run?.status === "RUNNING")) out.push({ ...open, b: Infinity, bItem: null });
  return out;
}

function seamCls(it, t, held) {
  for (const iv of held) {
    if (t >= iv.a - 0.0001 && t <= iv.b + 0.0001) {
      let c = "held";
      if (it && iv.aItem === it) c += " held-top";
      if (it && iv.bItem === it) c += " held-end";
      return c;
    }
  }
  return "";
}

function title(r) {
  if (r.agent && r.title && r.title.startsWith(r.agent)) {
    return h`<span class="ag">${r.agent}</span>${r.title.slice(r.agent.length)}`;
  }
  return r.agent ? h`<span class="ag">${r.agent}</span> ${r.title}` : h`${r.title}`;
}

function deep(href) {
  return href ? h`<a class="deep" href="${href}" target="_blank" rel="noopener" aria-label="Open in the Temporal UI">${icon("ext")}</a>` : "";
}

function ev(r, cls, ctx, extra = {}) {
  const glyph = extra.glyph || (r.kind === "task.state" && STATE_ICON[r.state]) || GLYPH[r.kind] || "dot";
  const k = [`ev ${cls}-ev`];
  if (/lifetime\.(idle|drained|released)/.test(r.kind)) k.push("drain");
  if (r.kind === "lifetime.killed") k.push("killed");
  if (r.kind === "signal.reject") k.push("reject");
  if (/failed/.test(r.kind)) k.push("bad");
  const link = r.link || (cls === "fin" || cls === "hum" ? ctx.links.finance : ctx.links.support);
  const side = cls === "fin" || cls === "hum" ? "fin" : "sup";
  let ident = "";
  if (r.identity && (cls === "cmp" || r.identity !== ctx.lastIdent[side])) {
    ident = h`<span class="dd ident id">${r.identity}</span>`;
    ctx.lastIdent[side] = r.identity;
  }
  return h`<div class="${k.join(" ")}" ${r.state ? h`data-state="${r.state}"` : ""}><span class="g">${icon(glyph)}</span><span class="tt"><span class="lane">${extra.lane || LANE_WORD[cls]}</span>${title(r)}${deep(link)}</span>${r.detail ? h`<span class="dd">${r.detail}</span>` : ""}${ident}${extra.after || ""}</div>`;
}

function railCell(t, lts, it, ctx) {
  let cls = railAt(t, lts);
  const r = it && it.type === "single" ? it.r : null;
  // A draining lifetime: the rail fades on the idle row and is gone from the drained row on,
  // whatever the timestamps' rounding says (Support workers running is already 0 there).
  if (r && (r.kind === "lifetime.drained" || r.kind === "lifetime.released")) cls = "off";
  else if (r && r.kind === "lifetime.idle" && cls === "lit") cls = "lit fading";
  if (r && r.kind === "lifetime.killed") cls += " cut";
  if (r && r.kind === "lifetime.start" && ctx.fresh.has(r.seq)) cls += " ignite";
  return h`<div class="c-rail ${cls}" aria-hidden="true"></div>`;
}

function itemHTML(it, ctx) {
  const { lts, held } = ctx;
  const t = it.t;
  const isNew = ctx.fresh.size && [it.r, it.a, it.b, it.nx, it.fin].some((r) => r && ctx.fresh.has(r.seq));
  const cls = `row${isNew ? " new" : ""}`;
  const time = h`<div class="c-time">${offset(t)}</div>`;
  const seam = seamCls(it, t, held);
  if (it.type === "out" || it.type === "back") {
    const out = it.type === "out";
    const op = opName(it.nx);
    const hop = out ? `Nexus ${op} to Finance, held open by the server` : `Nexus ${op} completes back to Support`;
    const sup = ev(it.nx, "nx", ctx, { glyph: "seam" });
    const fin = ev(it.fin, "fin", ctx);
    return h`<div class="${cls} cross ${out ? "out" : "back"}" role="listitem">${time}${railCell(t, lts, it, ctx)}
      <div class="c-sup" data-hop="${hop}">${sup}<span class="wire ${out ? "" : "arrow-l"}"></span></div>
      <div class="c-seam ${seam}"><span class="wire"></span><span class="lab"><span class="id">${op}</span><br>${out ? "to Finance" : "answer"}</span></div>
      <div class="c-fin">${fin}<span class="wire ${out ? "arrow-r" : ""}"></span></div></div>`;
  }
  if (it.type === "disco") {
    const r = { ...it.a, detail: "sync call, answered by Finance's handler" };
    return h`<div class="${cls} cross disco" role="listitem">${time}${railCell(t, lts, it, ctx)}
      <div class="c-sup" data-hop="Nexus get_agent_card (discovery)">${ev(r, "nx", ctx, { glyph: "seam" })}<span class="wire"></span></div>
      <div class="c-seam ${seam}"><span class="lab"><span class="id">get_agent_card</span></span></div><div class="c-fin"></div></div>`;
  }
  if (it.type === "stub") {
    const r = it.r;
    const bad = /failed/.test(r.kind);
    const lab = r.kind === "nexus.attempt_failed" ? `attempt ${r.attempt || ""} failed`
      : r.kind === "nexus.failed" ? "failed"
        : r.kind === "nexus.started" ? "started, held"
          : r.kind === "nexus.completed" ? "answer" : opName(r);
    const fromSeam = r.kind === "nexus.completed" || bad;
    return h`<div class="${cls} stub" role="listitem">${time}${railCell(t, lts, it, ctx)}
      <div class="c-sup" data-hop="Nexus ${lab}">${ev(r, "nx", ctx, { glyph: GLYPH[r.kind] || "seam" })}<span class="wire ${bad ? "bad" : ""} ${fromSeam ? "arrow-l" : ""}"></span></div>
      <div class="c-seam ${seam}"><span class="wire ${bad ? "bad" : ""}"></span><span class="lab ${bad ? "bad" : ""}">${lab}</span></div><div class="c-fin"></div></div>`;
  }
  const r = it.r;
  const lc = LANE_CLS[r.lane] || "sup";
  let after = "";
  if (r.kind === "run.completed" && ctx.run?.reply?.text) {
    after = h`<div class="reply"><div class="who">Reply to the customer</div><p>${ctx.run.reply.text}</p></div>`;
  }
  const body = ev(r, lc, ctx, { after });
  const onFin = lc === "fin" || lc === "hum";
  const end = /^run\.(completed|failed|terminated)$/.test(r.kind) ? " end" : "";
  return h`<div class="${cls}${end}" role="listitem">${time}${railCell(t, lts, it, ctx)}<div class="c-sup">${onFin ? "" : body}</div><div class="c-seam ${seam}"></div><div class="c-fin">${onFin ? body : ""}</div></div>`;
}

function gapHTML(prev, next, ctx) {
  const a = prev.t, b = next.t, d = b - a, mid = a + d / 2;
  const rail = railAt(mid, ctx.lts);
  const seam = seamCls(null, mid, ctx.held);
  const pr = prev.r || prev.fin || prev.nx || prev.a;
  const nx = next.r || next.nx || next.a;
  let note, glyph = "clock", onFin = false;
  if (rail === "off") {
    note = h`<b>${dur(d)}</b> with no Support compute. The conversation is in history; nothing is billed.`;
    glyph = "power";
  } else if (pr && pr.kind === "model.turn") {
    note = h`<b>${dur(d)}</b> while ${pr.agent || "the agent"} thinks`;
    glyph = "think";
    onFin = pr.lane === "finance";
  } else if (nx && (nx.kind === "lifetime.idle" || nx.kind === "lifetime.drained")) {
    note = h`<b>${dur(d)}</b>: ${ctx.lts.find((l) => mid >= l.s && mid <= l.e)?.label || "the worker"} stays up for the debounce, then drains`;
    glyph = "hold";
  } else if (seam && [pr?.kind, nx?.kind].includes("nexus.attempt_failed")) {
    // Failure demo: Finance has no task yet, so nothing "works"; the server retries the call.
    note = h`<b>${dur(d)}</b> while the server tries to deliver Support's call; no Finance worker answers`;
    glyph = "warn";
  } else if (seam) {
    note = h`<b>${dur(d)}</b> while Finance works; the server holds Support's call open`;
    onFin = false;
  } else {
    note = h`<b>${dur(d)}</b> with no new events`;
  }
  const cell = h`<div class="gapnote">${icon(glyph)}<span>${note}</span></div>`;
  return h`<div class="row gap" role="listitem"><div class="c-time"></div><div class="c-rail ${rail}" aria-hidden="true"></div>
    <div class="c-sup">${onFin ? "" : cell}</div><div class="c-seam ${seam}"></div><div class="c-fin">${onFin ? cell : ""}</div></div>`;
}

function nowHTML(last, ctx) {
  const run = ctx.run;
  const c = run.compute || {};
  const running = !!c.running;
  const rail = running ? "lit" : ctx.lts.length ? "off" : "none";
  const openHeld = ctx.held.some((iv) => iv.b === Infinity);
  const pend = (run.support?.pending_nexus || []).find((p) => p.operation !== "get_agent_card");
  const lastAt = last ? new Date(ctx.t0 + last.t * 1000).toISOString() : run.started_at;
  let note;
  if (!running && ctx.lts.length) {
    const lastEnd = [...(c.lifetimes || [])].reverse().find((l) => l.end)?.end;
    const since = c.drain?.at || lastEnd || lastAt;
    note = h`${icon("power")}<span><b data-since="${since}">${sinceText(since)}</b> with no Support compute${c.drain?.estimated ? " (drain estimated from the debounce)" : ""}. The conversation is in history; nothing is billed.</span>`;
  } else if (running) {
    const l = ctx.lts[ctx.lts.length - 1];
    note = h`<span class="livedot" aria-hidden="true"></span><span>${l ? l.label.replace(/^./, (x) => x.toUpperCase()) : "A Support worker"} is up. Last event <b data-since="${lastAt}">${sinceText(lastAt)}</b> ago.</span>`;
  } else {
    note = h`${icon("clock")}<span>No Support worker has picked this up yet. <b data-since="${lastAt}">${sinceText(lastAt)}</b> since the last event.</span>`;
  }
  let seamLab = "";
  if (pend && pend.attempt > 1) {
    seamLab = h`<span class="lab bad" style="top:.45rem">attempt ${pend.attempt}</span>`;
  }
  const task = run.task;
  let fin = "";
  if (task && task.state === "input-required") fin = raw('<div data-slot="approval"></div>');
  else if (pend && pend.attempt > 1) fin = h`<div class="gapnote">${icon("warn")}<span>No Finance worker answers, so the server keeps retrying Support's call.</span></div>`;
  else if (task && (task.state === "working" || task.state === "submitted")) fin = h`<div class="gapnote">${icon("dot")}<span>Finance's agent is working</span></div>`;
  return h`<div class="row gap now" role="listitem" aria-label="Now"><div class="c-time"><span class="livedot" aria-hidden="true"></span>now</div>
    <div class="c-rail ${rail}" aria-hidden="true"></div>
    <div class="c-sup"><div class="gapnote">${note}</div></div>
    <div class="c-seam ${openHeld ? "held live-edge" : ""}">${seamLab}</div>
    <div class="c-fin">${fin}</div></div>`;
}

/** A key over everything the lanes draw, without the ticking numbers. */
export function lanesKey(state) {
  const run = state.run || {};
  const c = run.compute || {};
  return JSON.stringify([
    state.rows?.workflow_id, state.rows?.rows_version, (state.rows?.rows || []).length,
    run.status, run.task?.state, c.running, c.drain?.state, c.drain?.at,
    (c.lifetimes || []).map((l) => [l.n, l.start, l.end, l.label]),
    (run.support?.pending_nexus || []).map((p) => [p.operation, p.attempt, p.state]),
    !!run.reply, run.links?.support,
  ]);
}

const seen = new Map(); // wf -> Set(seq) for the new-row flash

export function renderLanes(state, rowsEl) {
  const run = state.run;
  const rowsBody = state.rows || {};
  const rows = (rowsBody.workflow_id === run?.workflow_id ? rowsBody.rows : []) || [];
  if (!run) { rowsEl.innerHTML = ""; return; }
  const t0 = ms(rowsBody.t0 || run.started_at);
  const sorted = [...rows].sort((a, b) => (a.t - b.t) || (a.seq - b.seq)).map((r) => (
    Number.isFinite(r.t) ? r : { ...r, t: (ms(r.at) - t0) / 1000 }));
  const wf = run.workflow_id;
  const prevSeen = seen.get(wf);
  const fresh = new Set(prevSeen ? sorted.filter((r) => !prevSeen.has(r.seq)).map((r) => r.seq) : []);
  seen.set(wf, new Set(sorted.map((r) => r.seq)));
  const items = pairRows(sorted);
  const lts = lifetimesOf(run, t0);
  const held = heldIntervals(items, run);
  const ctx = { run, t0, lts, held, fresh, lastIdent: {}, links: run.links || {} };
  const parts = [];
  if (!items.length) {
    parts.push(h`<div class="lempty">Waiting for the first event in history…</div>`);
  }
  items.forEach((it, i) => {
    if (i > 0 && it.t - items[i - 1].t > GAP_S) parts.push(gapHTML(items[i - 1], it, ctx));
    parts.push(itemHTML(it, ctx));
  });
  if (run.status === "RUNNING") parts.push(nowHTML(items[items.length - 1], ctx));

  // Keep the approval card's DOM node (and what the presenter typed) across re-renders.
  const card = approvalNode();
  const active = document.activeElement;
  const focusName = card.contains(active) ? active.name || active.id : null;
  const sel = focusName && active.selectionStart !== undefined ? [active.selectionStart, active.selectionEnd] : null;
  card.remove();
  rowsEl.innerHTML = parts.map(String).join("");
  const slot = rowsEl.querySelector('[data-slot="approval"]');
  if (slot) {
    slot.replaceWith(card);
    if (focusName) {
      const f = card.querySelector(`[name="${focusName}"]`) || card.querySelector(`#${focusName}`);
      if (f) { f.focus({ preventScroll: true }); if (sel && f.setSelectionRange) f.setSelectionRange(sel[0], sel[1]); }
    }
  }
}
