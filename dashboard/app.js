/* Toll Gate admin dashboard — polling client.
 *
 * Contracts:
 * - Admin key: prompt -> localStorage -> X-Admin-Key on every call.
 *   401 clears and re-prompts; 503 shows a banner instead of nagging.
 * - One poll tick per 2s; ticks never overlap (slow API skips beats).
 * - NO TRUNCATION, EVER. Every column has wrapText + autoHeight: text
 *   that does not fit the cell width wraps to the next line INSIDE the
 *   same cell and the row grows. Multi-line values (timestamps, JSON,
 *   evidence lists, stack traces) keep their line breaks via pre-wrap.
 *   No clipping shortcuts — nothing is sliced server- or client-side.
 * - Stable updates: every grid has getRowId, so each poll's rowData
 *   assignment is an IMMUTABLE-DATA update — AG Grid diffs by id and
 *   patches rows in place (no flicker, no scroll-position reset, no
 *   full re-render while you are reading or about to click).
 * - Decisions: approve/reject buttons on pending approvals POST
 *   /v1/admin/approvals/{id}/decision, which (server-side, one
 *   transaction) flips the approval AND enqueues the resume_agent job
 *   that wakes the paused LangGraph run in the worker.
 */
"use strict";

const API = "/v1/admin/dashboard";
const POLL_MS = 2000;
const KEY_STORAGE = "tollgate_admin_key";
const OPERATOR_STORAGE = "tollgate_decided_by";

const state = {
  key: localStorage.getItem(KEY_STORAGE) || "",
  grids: {},        // panel name -> AG Grid api
  timer: null,
  inFlight: false,
  everSucceeded: false,
};

/* ---------------- admin key ---------------- */

function askForKey(reason) {
  const key = window.prompt(
    reason || "Enter the admin API key (sent as X-Admin-Key):", "");
  if (key && key.trim()) {
    state.key = key.trim();
    localStorage.setItem(KEY_STORAGE, state.key);
    hideBanner();
  }
}

document.getElementById("key-btn").addEventListener("click", () => {
  askForKey("Replace the stored admin key:");
  poll();
});

/* ---------------- small DOM helpers ---------------- */

function esc(s) {
  // Every renderer escapes: data comes from the DB (and ultimately from
  // webhooks/LLMs) — the dashboard must never execute it as HTML.
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function showBanner(text) {
  const el = document.getElementById("banner");
  el.textContent = text;
  el.hidden = false;
}
function hideBanner() { document.getElementById("banner").hidden = true; }

function setConn(cls, text) {
  document.getElementById("conn-dot").className = "dot" + (cls ? " " + cls : "");
  document.getElementById("conn-text").textContent = text;
}

function setLastUpdate() {
  document.getElementById("last-update").textContent =
    "updated " + new Date().toLocaleTimeString();
}

/* ---------------- value formatting (full fidelity) ---------------- */

function fmtTs(iso) {
  // Local, second-precision, on TWO lines (date\ntime) — pre-wrap renders
  // the break, so the full timestamp is always visible without widening.
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  const p = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
    "\n" + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}

function fmtJson(obj) {
  if (obj == null) return "";
  if (typeof obj === "string") return obj;
  return JSON.stringify(obj, null, 2);  // pretty + pre-wrap: nothing hidden
}

/* ---------------- fetch layer ---------------- */

async function getJSON(path) {
  const res = await fetch(API + path, { headers: { "X-Admin-Key": state.key } });
  if (res.status === 401) {
    localStorage.removeItem(KEY_STORAGE);
    state.key = "";
    setConn("error", "bad admin key");
    askForKey("Wrong admin key (401). Enter the correct one:");
    throw new Error("unauthorized");
  }
  if (res.status === 503) {
    showBanner("Admin API is disabled on the server (ADMIN_API_KEY not set).");
    setConn("error", "admin disabled");
    throw new Error("admin disabled");
  }
  if (!res.ok) throw new Error(path + " -> HTTP " + res.status);
  return res.json();
}

/* ---------------- metrics cards ---------------- */

function card(label, value, sub, tone) {
  return '<div class="card' + (tone ? " " + tone : "") + '">' +
    '<div class="label">' + esc(label) + "</div>" +
    '<div class="value">' + esc(value) + "</div>" +
    (sub ? '<div class="sub">' + esc(sub) + "</div>" : "") +
    "</div>";
}

function renderMetrics(m) {
  const agent = m.agent || {};
  const median = agent.median_run_seconds != null
    ? Number(agent.median_run_seconds).toFixed(1) + "s" : "—";
  const cost = agent.total_cost_usd != null
    ? "$" + Number(agent.total_cost_usd).toFixed(4) : "—";
  document.getElementById("metrics").innerHTML =
    card("Actions", m.actions.total,
         m.actions.denied + " denied · " +
         m.actions.duplicates_absorbed + " duplicates absorbed",
         m.actions.denied > 0 ? "warn" : "ok") +
    card("Pending approvals", m.approvals.pending,
         m.approvals.total + " total",
         m.approvals.pending > 0 ? "warn" : "ok") +
    card("Webhook events", m.webhooks.events_received,
         "jobs: " + fmtJson(m.webhooks.jobs_by_status)) +
    card("Dead letter", m.webhooks.dead_letter,
         m.webhooks.dead_letter > 0 ? "needs attention" : "all clear",
         m.webhooks.dead_letter > 0 ? "bad" : "ok") +
    card("Agent runs", agent.runs_total != null ? agent.runs_total : "—",
         (agent.awaiting_approval != null ? agent.awaiting_approval : "—") +
         " awaiting approval · " +
         (agent.refunds_executed != null ? agent.refunds_executed : "—") +
         " refunds",
         m.agent ? "" : "warn") +
    card("Median run time", median,
         (agent.total_tokens != null
           ? Number(agent.total_tokens).toLocaleString() : "—") +
         " tokens · " + cost);
}

/* ---------------- cell renderers ---------------- */

function preRenderer(value, mono) {
  // pre-wrap block: keeps newlines, wraps long words, never clips.
  return '<div class="pre' + (mono ? " mono" : "") + '">' + esc(value) + "</div>";
}

function evidenceRenderer(params) {
  const ev = (params.data && params.data.evidence_items) || [];
  if (!ev.length) return '<span class="muted">none cited</span>';
  return ev.map((e) =>
    '<div class="ev-item"><b>' + esc(e.source) + "</b> · " + esc(e.claim) +
    (e.value != null ? " = <b>" + esc(e.value) + "</b>" : "") +
    (e.transaction_id ? " <span class='muted'>txn " + esc(e.transaction_id) + "</span>" : "") +
    (e.observed_at ? " <span class='muted'>@" + esc(e.observed_at) + "</span>" : "") +
    "</div>").join("");
}

/* ---------------- decisions (approve / reject -> agent resume) ---------------- */

function operatorName() {
  let who = localStorage.getItem(OPERATOR_STORAGE);
  if (!who) {
    who = window.prompt("Your name (recorded on every decision you make):",
                        "operator");
    who = (who && who.trim()) || "operator";
    localStorage.setItem(OPERATOR_STORAGE, who);
  }
  return who;
}

class DecisionCell {
  init(params) {
    this.eGui = document.createElement("div");
    const d = params.data || {};
    if (d.status !== "pending") {
      this.eGui.innerHTML =
        '<div class="dec-status">' + esc(d.status) +
        (d.decided_by ? " by " + esc(d.decided_by) : "") +
        (d.decided_at ? "\n" + esc(fmtTs(d.decided_at)) : "") + "</div>";
      return;
    }
    this.eGui.className = "dec-btns";
    const ok = document.createElement("button");
    ok.type = "button"; ok.className = "dec-btn approve"; ok.textContent = "Approve";
    const no = document.createElement("button");
    no.type = "button"; no.className = "dec-btn reject"; no.textContent = "Reject";
    const wire = (btn, decision) => btn.addEventListener("click", () =>
      this.decide(d.approval_id, decision, [ok, no], params));
    wire(ok, "approved");
    wire(no, "rejected");
    this.eGui.append(ok, no);
  }
  getGui() { return this.eGui; }

  async decide(approvalId, decision, btns, params) {
    const verb = decision === "approved" ? "APPROVE" : "REJECT";
    if (!window.confirm(
        verb + " approval #" + approvalId + " — $" +
        ((params.data.action_amount || "?") + " " + (params.data.action_currency || "")) +
        "\n\n" + (decision === "approved"
          ? "The refund will EXECUTE (the agent run resumes and moves the money)."
          : "No money will move; the agent run is closed out.") +
        "\n\nDeciding as: " + operatorName())) {
      return;
    }
    btns.forEach((b) => { b.disabled = true; });
    this.eGui.innerHTML = '<div class="dec-status">submitting…</div>';
    try {
      const res = await fetch("/v1/admin/approvals/" + approvalId + "/decision", {
        method: "POST",
        headers: { "Content-Type": "application/json",
                   "X-Admin-Key": state.key },
        body: JSON.stringify({ decision, decided_by: operatorName() }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        this.eGui.innerHTML = '<div class="dec-status bad">failed: HTTP ' +
          res.status + "\n" + esc(fmtJson(body.detail || body)) + "</div>";
        return;
      }
      this.eGui.innerHTML = '<div class="dec-status ok">' + esc(decision) +
        " · resume job #" + esc(body.resume_job_id) + " queued" +
        "\nrow leaves the pending list on next poll</div>";
      pollSoon();  // reflect the change immediately, don't wait for the beat
    } catch (err) {
      this.eGui.innerHTML = '<div class="dec-status bad">network error:\n' +
        esc(String((err && err.message) || err)) + "</div>";
    }
  }
}

/* ---------------- grids ---------------- */

const TS = { valueFormatter: (p) => fmtTs(p.value) };

const GRID_DEFS = {
  approvals: {
    el: "#grid-approvals",
    countSel: "#panel-approvals .count",
    rowId: (d) => "ap-" + d.approval_id,
    row: (a) => ({
      approval_id: a.approval_id,
      status: a.status,
      agent: a.agent_name,
      action_type: a.action ? a.action.action_type : "",
      action_amount: a.action ? a.action.amount : "",
      action_currency: a.action ? a.action.currency : "",
      requested_at: a.requested_at,
      expires_at: a.expires_at,
      decided_at: a.decided_at,
      decided_by: a.decided_by || "",
      confidence: a.agent && a.agent.confidence != null
        ? Number(a.agent.confidence).toFixed(2) : "",
      proposed_action: a.agent ? a.agent.proposed_action || "" : "",
      run_status: a.agent ? a.agent.status : "",
      dispute_id: a.agent ? a.agent.dispute_id : "",
      reasoning: a.agent
        ? a.agent.reasoning || "(run on record has no reasoning text)"
        : "(no linked agent run — predates the webhook pipeline or agent DB unreachable)",
      evidence_items: a.agent ? a.agent.cited_evidence || [] : [],
    }),
    colDefs: [
      { field: "approval_id", headerName: "#", width: 74 },
      { field: "status", width: 110 },
      { field: "agent", headerName: "Agent", width: 130 },
      { field: "action_type", headerName: "Type", width: 96 },
      { headerName: "Amount", width: 118, valueGetter: (p) =>
          p.data.action_amount + " " + p.data.action_currency },
      { field: "requested_at", headerName: "Requested", width: 118, ...TS },
      { field: "expires_at", headerName: "Expires", width: 118, ...TS },
      { field: "confidence", width: 100 },
      { field: "proposed_action", headerName: "Proposed", width: 128 },
      { field: "dispute_id", headerName: "Dispute", width: 140 },
      { field: "run_status", headerName: "Run status", width: 150 },
      { field: "reasoning", headerName: "Agent reasoning", flex: 2, minWidth: 280 },
      { headerName: "Cited evidence", flex: 2, minWidth: 260,
        cellRenderer: evidenceRenderer, autoHeight: true },
      { headerName: "Decision", width: 210, cellRenderer: DecisionCell,
        autoHeight: true },
    ],
  },
  actions: {
    el: "#grid-actions",
    countSel: "#panel-actions .count",
    rowId: (d) => "act-" + d.id,
    row: (a) => a,
    colDefs: [
      { field: "id", width: 74 },
      { field: "created_at", headerName: "When", width: 118, ...TS },
      { field: "agent_name", headerName: "Agent", width: 130 },
      { field: "action_type", headerName: "Type", width: 96 },
      { headerName: "Amount", width: 118, valueGetter: (p) =>
          p.data.amount + " " + p.data.currency },
      { field: "outcome", width: 148 },
      { field: "reason_code", headerName: "Reason", width: 210 },
      { field: "reference_id", headerName: "Reference", width: 190 },
      { field: "idempotency_key", headerName: "Idempotency key",
        flex: 1, minWidth: 220 },
    ],
  },
  audit: {
    el: "#grid-audit",
    countSel: "#panel-audit .count",
    rowId: (d) => "aud-" + d.id,
    row: (r) => ({ ...r, detail: fmtJson(r.detail) }),
    colDefs: [
      { field: "id", width: 74 },
      { field: "created_at", headerName: "When", width: 118, ...TS },
      { field: "decision", width: 170 },
      { field: "reason_code", headerName: "Reason", width: 210 },
      { field: "agent_name", headerName: "Agent", width: 130 },
      { field: "action_type", headerName: "Type", width: 96 },
      { field: "amount", width: 100 },
      { field: "detail", headerName: "Detail (JSON)", flex: 1, minWidth: 260,
        cellRenderer: (p) => preRenderer(p.value, true) },
    ],
  },
  dead: {
    el: "#grid-dead",
    countSel: "#panel-dead .count",
    rowId: (d) => "job-" + d.id,
    row: (j) => ({ ...j, payload: fmtJson(j.payload) }),
    colDefs: [
      { field: "id", width: 74 },
      { field: "job_type", headerName: "Job", width: 140 },
      { field: "dedup_key", headerName: "Dedup key", width: 180 },
      { field: "attempts", width: 90 },
      { field: "failed_at", headerName: "Failed at", width: 118, ...TS },
      { field: "created_at", headerName: "Created", width: 118, ...TS },
      { field: "payload", width: 230,
        cellRenderer: (p) => preRenderer(p.value, true) },
      { field: "last_error", headerName: "Last error (full text)",
        flex: 1, minWidth: 300,
        cellRenderer: (p) => preRenderer(p.value, true) },
    ],
  },
};

function initGrids() {
  for (const [name, def] of Object.entries(GRID_DEFS)) {
    state.grids[name] = agGrid.createGrid(document.querySelector(def.el), {
      columnDefs: def.colDefs,
      defaultColDef: {
        sortable: true,
        filter: true,
        resizable: true,
        wrapText: true,      // <-- the no-truncation contract
        autoHeight: true,    // rows grow to fit wrapped content
      },
      getRowId: (params) => def.rowId(params.data),
      rowData: [],
      animateRows: false,
      suppressCellFocus: true,
      rowSelection: "single",
    });
  }
}

function renderGrid(name, rows) {
  const def = GRID_DEFS[name];
  // Immutable-data update: with getRowId set, AG Grid diffs against the
  // current rows and patches in place — your scroll position and any
  // half-typed filter survive the 2s refresh.
  state.grids[name].setGridOption("rowData", rows.map(def.row));
  document.querySelector(def.countSel).textContent = "(" + rows.length + ")";
}

/* ---------------- poll loop ---------------- */

async function poll() {
  if (!state.key) {
    setConn("stale", "no admin key");
    askForKey();
    if (!state.key) { scheduleNext(); return; }
  }
  if (state.inFlight) { scheduleNext(); return; }  // skip a beat, never pile up
  state.inFlight = true;
  try {
    const [metrics, approvals, actions, audit, dead] = await Promise.all([
      getJSON("/metrics"),
      getJSON("/approvals?limit=100"),
      getJSON("/actions?limit=100"),
      getJSON("/audit?limit=200"),
      getJSON("/dead-letter?limit=100"),
    ]);
    renderMetrics(metrics);
    renderGrid("approvals", approvals.approvals);
    renderGrid("actions", actions.actions);
    renderGrid("audit", audit.audit);
    renderGrid("dead", dead.dead_letter);
    state.everSucceeded = true;
    hideBanner();
    setConn("ok", metrics.agent ? "live" : "live (agent DB unreachable)");
    setLastUpdate();
  } catch (err) {
    if (state.everSucceeded) {
      // Keep the last good data on screen; just mark the state.
      setConn("stale", "reconnecting… " +
        (document.getElementById("last-update").textContent || ""));
    } else {
      setConn("error", String((err && err.message) || err));
    }
  } finally {
    state.inFlight = false;
    scheduleNext();
  }
}

function scheduleNext() {
  clearTimeout(state.timer);
  state.timer = setTimeout(poll, POLL_MS);
}

function pollSoon() {
  clearTimeout(state.timer);
  state.timer = setTimeout(poll, 250);
}

initGrids();
poll();
