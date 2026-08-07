const POLICY_ENGINE_URL = window.POLICY_ENGINE_URL || "http://127.0.0.1:4022";

const agentsEl = document.getElementById("agents");
const feedBodyEl = document.getElementById("feedBody");
const feedCountEl = document.getElementById("feedCount");
const autoplayToggle = document.getElementById("autoplayToggle");
const resetBtn = document.getElementById("resetBtn");

let policyActions = {};
let lastSeenRequestId = 0;
let autoplayTimer = null;
let autoplayIndex = 0;

// Rotating scenario list mirroring agents/simulate.py, so the "Autoplay"
// checkbox in the dashboard gives the same curated approve/deny narrative.
const AUTOPLAY_SCENARIOS = [
  ["agent_weather", "weather"],
  ["agent_enrichment", "enrich"],
  ["agent_weather", "enrich"],
  ["agent_rogue", "weather"],
  ["agent_rogue", "weather"],
  ["agent_rogue", "weather"],
  ["agent_rogue", "enrich"],
  ["agent_ghost", "weather"],
];

const ICON_CHECK = `<svg width="11" height="11" viewBox="0 0 16 16" fill="none"><path d="M3 8.5L6.2 12L13 4" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const ICON_CROSS = `<svg width="11" height="11" viewBox="0 0 16 16" fill="none"><path d="M4 4L12 12M12 4L4 12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`;
const ICON_PENDING = `<svg width="8" height="8" viewBox="0 0 8 8"><circle cx="4" cy="4" r="4" fill="currentColor"/></svg>`;
// A reservation briefly sits as 'pending' between the atomic cap check and
// the payment settling (see storage.try_reserve) -- the feed can catch it
// mid-flight, so it needs its own icon/style rather than falling back to
// "denied" styling.
const DECISION_ICON = { approved: ICON_CHECK, denied: ICON_CROSS, pending: ICON_PENDING };

// /spend accepts arbitrary agent_id/action strings (that's how the "unknown
// agent" denial path works), and denial reasons echo them back. Both end up
// in the audit log and get rendered here via innerHTML, so anything
// user-influenced MUST be escaped before interpolation -- otherwise a
// crafted agent_id is a stored XSS reachable cross-origin by anyone who can
// reach the policy engine's port. Uses the browser's own serializer rather
// than a hand-rolled regex, so it's correct by construction.
function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value === null || value === undefined ? "" : String(value);
  return div.innerHTML;
}

async function fetchJSON(path, options) {
  const res = await fetch(`${POLICY_ENGINE_URL}${path}`, options);
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

function fmtUsd(n) {
  return `$${Number(n).toFixed(2)}`;
}

function firstDisallowedAction(agent) {
  return Object.keys(policyActions).find(
    (a) => !agent.allowed_actions.includes(a)
  );
}

function meterClass(pct) {
  if (pct >= 100) return "full";
  if (pct >= 70) return "warn";
  return "";
}

// Ease-out count-up for a stat value. Cheap no-op when the value hasn't
// actually changed (the common case on a 2s poll), and reads the previous
// value off the element itself so it composes with the "patch in place,
// never rebuild" rendering approach -- no separate state map to keep in
// sync with the DOM.
function animateValue(el, toValue, formatter, duration = 500) {
  const fromValue = el.dataset.rawValue !== undefined ? parseFloat(el.dataset.rawValue) : toValue;
  el.dataset.rawValue = toValue;
  if (fromValue === toValue) {
    el.textContent = formatter(toValue);
    return;
  }
  const start = performance.now();
  function tick(now) {
    const t = Math.min(1, (now - start) / duration);
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = formatter(fromValue + (toValue - fromValue) * eased);
    if (t < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

// refresh() polls every 2s. Rebuilding agent cards from scratch on every
// poll (the old approach) destroys and recreates the amount-input/button
// DOM nodes each time -- which wipes whatever you were typing and steals
// focus mid-edit, even if your mouse never left the field. Interactive
// elements are built exactly once per agent (allowed_actions, limits, and
// address are static for the life of the page); only the numbers that
// actually change on a poll (spend, counts, meter) get patched in place.
const agentCards = {}; // agent_id -> { totalSpend, approvedCount, deniedCount, meterSpend, meterPct, meterFill }
let builtAgentIds = null;

function buildAgentCard(agent) {
  const violateAction = firstDisallowedAction(agent);
  const initial = agent.display_name.trim().charAt(0).toUpperCase();

  const card = document.createElement("div");
  card.className = "agent-card";
  card.innerHTML = `
    <div class="agent-card-head">
      <div>
        <h3>${escapeHtml(agent.display_name)}</h3>
      </div>
      <div class="agent-avatar">${escapeHtml(initial)}</div>
    </div>
    <div class="agent-address">${escapeHtml(agent.address || "unfunded")}</div>
    <div class="agent-stats">
      <div>
        <div class="stat-label">Total spend</div>
        <div class="stat-value" data-el="totalSpend"></div>
      </div>
      <div>
        <div class="stat-label">Per-call limit</div>
        <div class="stat-value">${fmtUsd(agent.per_request_limit_usd)}</div>
      </div>
      <div>
        <div class="stat-label">Approved</div>
        <div class="stat-value approved" data-el="approvedCount"></div>
      </div>
      <div>
        <div class="stat-label">Denied</div>
        <div class="stat-value denied" data-el="deniedCount"></div>
      </div>
    </div>
    <div class="meter-row">
      <span class="meter-caption"><b data-el="meterSpend"></b> / ${fmtUsd(agent.daily_cap_usd)} daily cap</span>
      <span class="meter-pct" data-el="meterPct"></span>
    </div>
    <div class="meter-track">
      <div class="meter-fill" data-el="meterFill"></div>
    </div>
    <div class="agent-actions"></div>
  `;

  const actionsEl = card.querySelector(".agent-actions");
  for (const action of agent.allowed_actions) {
    const defaultPrice = policyActions[action]?.price_usd ?? 0;

    const row = document.createElement("div");
    row.className = "action-row";

    const field = document.createElement("label");
    field.className = "amount-field";
    field.innerHTML = `
      <span class="currency">$</span>
      <input type="number" class="amount-input" step="0.01" min="0" max="1000" value="${defaultPrice.toFixed(2)}" aria-label="Amount to request for ${escapeHtml(action)}" />
    `;
    const input = field.querySelector("input");

    const btn = document.createElement("button");
    btn.className = "btn btn-fire";
    btn.textContent = `Fire: ${action}`;
    btn.onclick = () => {
      const typed = parseFloat(input.value);
      const amount = Number.isFinite(typed) && typed >= 0 ? typed : defaultPrice;
      fireSpend(agent.agent_id, action, btn, amount);
    };

    row.appendChild(field);
    row.appendChild(btn);
    actionsEl.appendChild(row);
  }
  if (violateAction) {
    const btn = document.createElement("button");
    btn.className = "btn btn-fire violate";
    btn.textContent = `Try: ${violateAction} (should deny)`;
    btn.onclick = () => fireSpend(agent.agent_id, violateAction, btn, policyActions[violateAction]?.price_usd ?? 0);
    actionsEl.appendChild(btn);
  }

  const els = {};
  card.querySelectorAll("[data-el]").forEach((node) => {
    els[node.dataset.el] = node;
  });

  return { root: card, els };
}

function patchAgentCard(agent, els) {
  const pct = agent.daily_cap_usd > 0
    ? Math.min(100, (agent.daily_spend_usd / agent.daily_cap_usd) * 100)
    : 0;

  animateValue(els.totalSpend, agent.total_spend_usd, fmtUsd);
  animateValue(els.approvedCount, agent.approved_count, (v) => Math.round(v).toString());
  animateValue(els.deniedCount, agent.denied_count, (v) => Math.round(v).toString());
  animateValue(els.meterSpend, agent.daily_spend_usd, fmtUsd);
  els.meterPct.textContent = `${pct.toFixed(0)}%`;
  els.meterFill.style.width = `${pct}%`;
  els.meterFill.className = `meter-fill ${meterClass(pct)}`;
}

function renderAgents(agents) {
  const ids = agents.map((a) => a.agent_id).sort().join(",");
  if (ids !== builtAgentIds) {
    // First load, or the set of agents actually changed -- (re)build the
    // interactive elements. Any in-progress edits are necessarily gone
    // here regardless of approach, but this only fires once in practice.
    agentsEl.innerHTML = "";
    for (const key of Object.keys(agentCards)) delete agentCards[key];
    for (const agent of agents) {
      const { root, els } = buildAgentCard(agent);
      agentsEl.appendChild(root);
      agentCards[agent.agent_id] = els;
    }
    builtAgentIds = ids;
  }
  for (const agent of agents) {
    const els = agentCards[agent.agent_id];
    if (els) patchAgentCard(agent, els);
  }
}

function renderFeed(requests) {
  feedCountEl.textContent = requests.length
    ? `${requests.length} request${requests.length === 1 ? "" : "s"}`
    : "";

  if (requests.length === 0) {
    feedBodyEl.innerHTML = `<tr><td colspan="7" class="empty-feed">No requests yet. Fire one above.</td></tr>`;
    return;
  }
  feedBodyEl.innerHTML = "";
  for (const r of requests) {
    const tr = document.createElement("tr");
    if (r.id > lastSeenRequestId) tr.classList.add("row-new");

    const time = new Date(r.timestamp).toLocaleTimeString();
    // decision itself only ever comes from our own server code (a fixed
    // enum, never request input), so it's safe unescaped as a class name --
    // but everything below can originate from an attacker's /spend body,
    // so every one of these goes through escapeHtml before interpolation.
    const icon = DECISION_ICON[r.decision] ?? "";
    const decisionPill = `<span class="decision-pill ${r.decision}">${icon}${escapeHtml(r.decision)}</span>`;
    const txCell = r.explorer_url
      ? `<a class="tx-link" href="${escapeHtml(r.explorer_url)}" target="_blank" rel="noopener">${escapeHtml(r.tx_id.slice(0, 10))}&hellip;</a>`
      : `<span class="tx-empty">&mdash;</span>`;

    tr.innerHTML = `
      <td class="cell-time">${escapeHtml(time)}</td>
      <td class="cell-agent">${escapeHtml(r.agent_id)}</td>
      <td>${escapeHtml(r.action)}</td>
      <td class="cell-amount">${escapeHtml(fmtUsd(r.amount_usd))}</td>
      <td>${decisionPill}</td>
      <td class="reason-cell">${escapeHtml(r.reason)}</td>
      <td>${txCell}</td>
    `;
    feedBodyEl.appendChild(tr);
  }
  lastSeenRequestId = Math.max(...requests.map((r) => r.id));
}

async function refresh() {
  try {
    const [agents, requests] = await Promise.all([
      fetchJSON("/agents"),
      fetchJSON("/requests?limit=100"),
    ]);
    renderAgents(agents);
    renderFeed(requests);
  } catch (e) {
    console.error("refresh failed", e);
  }
}

async function fireSpend(agentId, action, btn, amountUsd) {
  if (btn) btn.disabled = true;
  const amount = Number.isFinite(amountUsd) ? amountUsd : 0;
  try {
    // /spend now requires a signature proving the caller controls
    // agent_id's Algorand key (see common/identity.py). The dashboard is a
    // human operator, not a separate cryptographic identity, so it asks
    // the policy engine's admin/sign convenience endpoint to sign on the
    // demo agent's behalf -- the same custodial key the engine already
    // holds for payment signing -- rather than ever handling private key
    // material in the browser. A real autonomous agent would sign locally
    // instead, exactly like agents/simulate.py does.
    let signed;
    try {
      signed = await fetchJSON("/admin/sign", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_id: agentId, action, amount_usd: amount }),
      });
    } catch (signErr) {
      // agent_ghost (the "unknown agent" demo scenario) has no key on
      // file, so signing 404s. Fall through with placeholder fields --
      // /spend rejects unknown agents before it ever looks at the
      // signature, so the denial still shows up correctly in the feed
      // instead of the request silently never happening.
      signed = { timestamp: Date.now() / 1000, nonce: "n/a", signature: "n/a" };
    }
    await fetchJSON("/spend", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent_id: agentId, action, amount_usd: amount, ...signed }),
    });
  } catch (e) {
    console.error("spend failed", e);
  } finally {
    if (btn) btn.disabled = false;
    refresh();
  }
}

function toggleAutoplay() {
  if (autoplayToggle.checked) {
    autoplayTimer = setInterval(() => {
      const [agentId, action] = AUTOPLAY_SCENARIOS[autoplayIndex % AUTOPLAY_SCENARIOS.length];
      autoplayIndex++;
      fireSpend(agentId, action, null, policyActions[action]?.price_usd ?? 0);
    }, 2500);
  } else {
    clearInterval(autoplayTimer);
    autoplayTimer = null;
  }
}

resetBtn.onclick = async () => {
  await fetchJSON("/admin/reset", { method: "POST" });
  lastSeenRequestId = 0;
  autoplayIndex = 0;
  refresh();
};

autoplayToggle.onchange = toggleAutoplay;

(async function init() {
  const policy = await fetchJSON("/policy");
  policyActions = policy.actions;
  await refresh();
  setInterval(refresh, 2000);
})();
