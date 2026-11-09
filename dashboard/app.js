/* Toll Gate admin dashboard — polling client.
 *
 * Contract:
 * - The admin key lives in localStorage and is sent as X-Admin-Key on
 *   every API call. 401 clears it and re-prompts; 503 (ADMIN_DISABLED)
 *   shows a banner instead of nagging.
 * - One poll tick every 2s. A tick never overlaps the previous one — if
 *   the API is slow, beats are skipped, not queued.
 * - Static assets are local; only /v1/admin/dashboard/* is fetched.
 *
 * Increment 9 (this file): scaffold — grids show the raw fields.
 * Increment 10 upgrades the grids to the no-truncation presentation
 * (wrapText + autoHeight, custom cell renderers, stable row updates).
 */
"use strict";

const API = "/v1/admin/dashboard";
const POLL_MS = 2000;
const KEY_STORAGE = "tollgate_admin_key";

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

function showBanner(text) {
  const el = document.getElementById("banner");
  el.textContent = text;
  el.hidden = false;
}
function hideBanner() { document.getElementById("banner").hidden = true; }

function setConn(cls, text) {
  const dot = document.getElementById("conn-dot");
  dot.className = "dot" + (cls ? " " + cls : "");
  document.getElementById("conn-text").textContent = text;
}

function setLastUpdate() {
  document.getElementById("last-update").textContent =
    "updated " + new Date().toLocaleTimeString();
}

/* ---------------- fetch layer ---------------- */

async function getJSON(path) {
  const res = await fetch(API + path, {
    headers: { "X-Admin-Key": state.key },
  });
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
    '<div class="label">' + label + "</div>" +
    '<div class="value">' + value + "</div>" +
    (sub ? '<div class="sub">' + sub + "</div>" : "") +
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
         "jobs: " + JSON.stringify(m.webhooks.jobs_by_status)) +
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
         (agent.total_tokens != null ? Number(agent.total_tokens).toLocaleString() : "—") +
         " tokens · " + cost);
}

/* ---------------- grids (scaffold columns) ---------------- */

const GRID_DEFS = {
  approvals: {
    el: "#grid-approvals",
    countSel: "#panel-approvals .count",
    row: (a) => ({
      approval_id: a.approval_id,
      status: a.status,
      agent: a.agent_name,
      action_type: a.action ? a.action.action_type : "",
      amount: a.action ? a.action.amount + " " + a.action.currency : "",
      requested_at: a.requested_at,
      expires_at: a.expires_at,
      decided_by: a.decided_by || "",
      confidence: a.agent && a.agent.confidence != null ? a.agent.confidence : "",
      proposed_action: a.agent ? a.agent.proposed_action || "" : "",
      reasoning: a.agent ? a.agent.reasoning || "(run has no reasoning on record)"
                         : "(no linked agent run)",
    }),
    colDefs: [
      { field: "approval_id", headerName: "#", width: 80 },
      { field: "status", width: 110 },
      { field: "agent", headerName: "Agent", width: 140 },
      { field: "action_type", headerName: "Type", width: 100 },
      { field: "amount", width: 120 },
      { field: "requested_at", headerName: "Requested", width: 180 },
      { field: "expires_at", headerName: "Expires", width: 180 },
      { field: "decided_by", headerName: "Decided by", width: 120 },
      { field: "confidence", width: 110 },
      { field: "proposed_action", headerName: "Proposed", width: 130 },
      { field: "reasoning", flex: 1, minWidth: 220 },
    ],
  },
  actions: {
    el: "#grid-actions",
    countSel: "#panel-actions .count",
    row: (a) => a,
    colDefs: [
      { field: "id", width: 80 },
      { field: "created_at", headerName: "When", width: 180 },
      { field: "agent_name", headerName: "Agent", width: 140 },
      { field: "action_type", headerName: "Type", width: 100 },
      { field: "amount", width: 110 },
      { field: "currency", width: 90 },
      { field: "outcome", width: 140 },
      { field: "reason_code", headerName: "Reason", width: 200 },
      { field: "idempotency_key", headerName: "Idempotency key", flex: 1, minWidth: 200 },
    ],
  },
  audit: {
    el: "#grid-audit",
    countSel: "#panel-audit .count",
    row: (r) => ({ ...r, detail: JSON.stringify(r.detail ?? {}) }),
    colDefs: [
      { field: "id", width: 80 },
      { field: "created_at", headerName: "When", width: 180 },
      { field: "decision", width: 150 },
      { field: "reason_code", headerName: "Reason", width: 200 },
      { field: "agent_name", headerName: "Agent", width: 130 },
      { field: "action_type", headerName: "Type", width: 90 },
      { field: "amount", width: 100 },
      { field: "detail", flex: 1, minWidth: 220 },
    ],
  },
  dead: {
    el: "#grid-dead",
    countSel: "#panel-dead .count",
    row: (j) => ({ ...j, payload: JSON.stringify(j.payload ?? {}) }),
    colDefs: [
      { field: "id", width: 80 },
      { field: "job_type", headerName: "Job", width: 130 },
      { field: "dedup_key", headerName: "Dedup key", width: 170 },
      { field: "attempts", width: 90 },
      { field: "failed_at", headerName: "Failed at", width: 180 },
      { field: "created_at", headerName: "Created", width: 180 },
      { field: "payload", width: 220 },
      { field: "last_error", headerName: "Last error", flex: 1, minWidth: 260 },
    ],
  },
};

function initGrids() {
  for (const [name, def] of Object.entries(GRID_DEFS)) {
    state.grids[name] = agGrid.createGrid(document.querySelector(def.el), {
      columnDefs: def.colDefs,
      defaultColDef: { sortable: true, filter: true, resizable: true },
      rowData: [],
      animateRows: false,
      suppressCellFocus: true,
    });
  }
}

function renderGrid(name, rows) {
  const def = GRID_DEFS[name];
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
    setConn("error", String((err && err.message) || err));
    if (state.everSucceeded) {
      // Keep the last good data on screen; just mark the state.
      setConn("stale", "reconnecting… last data " +
        (document.getElementById("last-update").textContent || ""));
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

initGrids();
poll();
