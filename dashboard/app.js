const POLICY_ENGINE_URL = window.POLICY_ENGINE_URL || "http://127.0.0.1:4022";

const agentsEl = document.getElementById("agents");
const feedBodyEl = document.getElementById("feedBody");
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

async function fetchJSON(path, options) {
  const res = await fetch(`${POLICY_ENGINE_URL}${path}`, options);
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

function fmtUsd(n) {
  return `$${Number(n).toFixed(2)}`;
}

function shortAddr(addr) {
  if (!addr) return "";
  return `${addr.slice(0, 6)}...${addr.slice(-6)}`;
}

function firstDisallowedAction(agent) {
  return Object.keys(policyActions).find(
    (a) => !agent.allowed_actions.includes(a)
  );
}

function renderAgents(agents) {
  agentsEl.innerHTML = "";
  for (const agent of agents) {
    const pct = agent.daily_cap_usd > 0
      ? Math.min(100, (agent.daily_spend_usd / agent.daily_cap_usd) * 100)
      : 0;
    const isFull = agent.daily_spend_usd >= agent.daily_cap_usd;

    const card = document.createElement("div");
    card.className = "agent-card";

    const violateAction = firstDisallowedAction(agent);

    card.innerHTML = `
      <h3>${agent.display_name}</h3>
      <div class="agent-address">${agent.address || "unfunded"}</div>
      <div class="agent-stats">
        <div>
          <div class="stat-label">Total spend</div>
          <div class="stat-value">${fmtUsd(agent.total_spend_usd)}</div>
        </div>
        <div>
          <div class="stat-label">Per-request limit</div>
          <div class="stat-value">${fmtUsd(agent.per_request_limit_usd)}</div>
        </div>
        <div>
          <div class="stat-label">Approved</div>
          <div class="stat-value approved">${agent.approved_count}</div>
        </div>
        <div>
          <div class="stat-label">Denied</div>
          <div class="stat-value denied">${agent.denied_count}</div>
        </div>
      </div>
      <div class="cap-bar-track">
        <div class="cap-bar-fill ${isFull ? "full" : ""}" style="width: ${pct}%"></div>
      </div>
      <div class="cap-caption">${fmtUsd(agent.daily_spend_usd)} / ${fmtUsd(agent.daily_cap_usd)} daily cap</div>
      <div class="agent-actions"></div>
    `;

    const actionsEl = card.querySelector(".agent-actions");
    for (const action of agent.allowed_actions) {
      const btn = document.createElement("button");
      btn.className = "btn btn-fire";
      btn.textContent = `Fire: ${action} (${fmtUsd(policyActions[action]?.price_usd ?? 0)})`;
      btn.onclick = () => fireSpend(agent.agent_id, action, btn);
      actionsEl.appendChild(btn);
    }
    if (violateAction) {
      const btn = document.createElement("button");
      btn.className = "btn btn-fire violate";
      btn.textContent = `Try: ${violateAction} (should deny)`;
      btn.onclick = () => fireSpend(agent.agent_id, violateAction, btn);
      actionsEl.appendChild(btn);
    }

    agentsEl.appendChild(card);
  }
}

function renderFeed(requests) {
  if (requests.length === 0) {
    feedBodyEl.innerHTML = `<tr><td colspan="7" class="empty-feed">No requests yet. Fire one above.</td></tr>`;
    return;
  }
  feedBodyEl.innerHTML = "";
  for (const r of requests) {
    const tr = document.createElement("tr");
    if (r.id > lastSeenRequestId) tr.classList.add("row-new");

    const time = new Date(r.timestamp).toLocaleTimeString();
    const decisionPill = `<span class="decision-pill ${r.decision}">${r.decision}</span>`;
    const txCell = r.explorer_url
      ? `<a class="tx-link" href="${r.explorer_url}" target="_blank" rel="noopener">${r.tx_id.slice(0, 10)}...</a>`
      : "&mdash;";

    tr.innerHTML = `
      <td>${time}</td>
      <td>${r.agent_id}</td>
      <td>${r.action}</td>
      <td>${fmtUsd(r.amount_usd)}</td>
      <td>${decisionPill}</td>
      <td class="reason-cell">${r.reason}</td>
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

async function fireSpend(agentId, action, btn) {
  if (btn) btn.disabled = true;
  try {
    await fetchJSON("/spend", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent_id: agentId, action }),
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
      fireSpend(agentId, action);
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
