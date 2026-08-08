const POLICY_ENGINE_URL = window.POLICY_ENGINE_URL || "http://127.0.0.1:4022";

const agentsEl = document.getElementById("agents");
const feedBodyEl = document.getElementById("feedBody");
const feedCountEl = document.getElementById("feedCount");
const feedFiltersEl = document.getElementById("feedFilters");
const pulseStripEl = document.getElementById("pulseStrip");
const toastStackEl = document.getElementById("toastStack");
const autoplayToggle = document.getElementById("autoplayToggle");
const resetBtn = document.getElementById("resetBtn");
const sfxBtn = document.getElementById("sfxBtn");
const statusChipEl = document.getElementById("statusChip");
const statusLabelEl = document.getElementById("statusLabel");
const approvalsEl = document.getElementById("approvals");
const approvalsHeadEl = document.getElementById("approvalsHead");
const anchorBtn = document.getElementById("anchorBtn");
const tamperBtn = document.getElementById("tamperBtn");
const restoreBtn = document.getElementById("restoreBtn");

const integrityEls = {
  root: document.getElementById("integrity"),
  shield: document.getElementById("integrityShield"),
  state: document.getElementById("integrityState"),
  detail: document.getElementById("integrityDetail"),
  events: document.getElementById("integrityEvents"),
  head: document.getElementById("integrityHead"),
  anchor: document.getElementById("integrityAnchor"),
};

const statEls = {
  spend: document.getElementById("statSpend"),
  approved: document.getElementById("statApproved"),
  denied: document.getElementById("statDenied"),
  tx: document.getElementById("statTx"),
  rate: document.getElementById("statRate"),
  rateFill: document.getElementById("statRateFill"),
};

let policyActions = {};
let lastSeenRequestId = 0;
let autoplayTimer = null;
let autoplayIndex = 0;
// Cached so the decision filter can re-render the feed instantly on click
// instead of waiting up to 2s for the next poll.
let lastRequests = [];
let feedFilter = "all";
// null until the first poll resolves, so the very first paint can say
// "Connecting…" rather than claiming either state.
let engineOnline = null;

// Rotating scenario list mirroring agents/simulate.py, so the "Autopilot"
// switch in the dashboard gives the same curated approve/deny narrative.
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

// Each agent card derives its entire colour identity from a single --hue
// custom property (see the theming note at the top of style.css). The demo
// agents get hand-picked hues so the fleet reads deliberately; anything
// else -- including the unknown agents that only ever appear in the feed --
// falls back to a stable hash so its colour never changes between polls.
const AGENT_HUES = {
  agent_weather: 190,
  agent_enrichment: 268,
  agent_rogue: 350,
  agent_ghost: 45,
};

function hueFor(agentId) {
  if (AGENT_HUES[agentId] !== undefined) return AGENT_HUES[agentId];
  let hash = 0;
  for (let i = 0; i < agentId.length; i++) {
    hash = (hash * 31 + agentId.charCodeAt(i)) >>> 0;
  }
  return hash % 360;
}

const RING_RADIUS = 31;
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;

const ICON_CHECK = `<svg width="11" height="11" viewBox="0 0 16 16" fill="none"><path d="M3 8.5L6.2 12L13 4" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
const ICON_CROSS = `<svg width="11" height="11" viewBox="0 0 16 16" fill="none"><path d="M4 4L12 12M12 4L4 12" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>`;
const ICON_PENDING = `<svg width="8" height="8" viewBox="0 0 8 8"><circle cx="4" cy="4" r="4" fill="currentColor"/></svg>`;
const ICON_LINK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6"/><path d="M20 4 10 14"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>`;
const ICON_HOLD = `<svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="8" cy="8" r="6"/><path d="M8 4.8V8l2.2 2.2"/></svg>`;
// A reservation briefly sits as 'pending' between the atomic cap check and
// the payment settling (see storage.try_reserve) -- the feed can catch it
// mid-flight, so it needs its own icon/style rather than falling back to
// "denied" styling. 'awaiting_approval' is the same idea but deliberate and
// open-ended: it sits there until a human decides, so it must never read as
// either a success or a failure.
const DECISION_ICON = {
  approved: ICON_CHECK,
  denied: ICON_CROSS,
  pending: ICON_PENDING,
  awaiting_approval: ICON_HOLD,
};
const DECISION_LABEL = { awaiting_approval: "held" };

function decisionLabel(decision) {
  return DECISION_LABEL[decision] ?? decision;
}

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

function capMood(pct) {
  if (pct >= 100) return { cls: "mood-blocked", label: "Daily cap reached" };
  if (pct >= 70) return { cls: "mood-warn", label: "Approaching cap" };
  return { cls: "mood-ok", label: "Within policy" };
}

// Ease-out count-up for a stat value. Cheap no-op when the value hasn't
// actually changed (the common case on a 2s poll), and reads the previous
// value off the element itself so it composes with the "patch in place,
// never rebuild" rendering approach -- no separate state map to keep in
// sync with the DOM.
function animateValue(el, toValue, formatter, duration = 550) {
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

const intFmt = (v) => Math.round(v).toString();

/* -------------------------------------------------------------------------
   Sound effects -- off by default, remembered across reloads. The AudioContext
   is created lazily inside the click handler so it starts in a "running"
   state: browsers suspend any context constructed outside a user gesture.
   ---------------------------------------------------------------------- */

let sfxOn = localStorage.getItem("aspe.sfx") === "on";
let audioCtx = null;

function updateSfxButton() {
  sfxBtn.setAttribute("aria-pressed", sfxOn ? "true" : "false");
  sfxBtn.title = sfxOn ? "Verdict sounds on" : "Verdict sounds off";
}

function blip(freqs, { type = "sine", gain = 0.05, step = 0.09, hold = 0.16 } = {}) {
  if (!sfxOn || !audioCtx) return;
  freqs.forEach((freq, i) => {
    const at = audioCtx.currentTime + i * step;
    const osc = audioCtx.createOscillator();
    const amp = audioCtx.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, at);
    amp.gain.setValueAtTime(0.0001, at);
    amp.gain.exponentialRampToValueAtTime(gain, at + 0.012);
    amp.gain.exponentialRampToValueAtTime(0.0001, at + hold);
    osc.connect(amp).connect(audioCtx.destination);
    osc.start(at);
    osc.stop(at + hold + 0.02);
  });
}

function playVerdictSound(decision) {
  if (decision === "approved") blip([660, 880, 1320], { type: "triangle", gain: 0.05 });
  else if (decision === "denied") blip([200, 140], { type: "sawtooth", gain: 0.035, step: 0.11, hold: 0.2 });
}

sfxBtn.onclick = () => {
  sfxOn = !sfxOn;
  localStorage.setItem("aspe.sfx", sfxOn ? "on" : "off");
  updateSfxButton();
  if (sfxOn) {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
    blip([520, 780], { type: "triangle", gain: 0.04 });
  }
};
updateSfxButton();

/* -------------------------------------------------------------------------
   Toasts
   ---------------------------------------------------------------------- */

function toast({ decision, title, message, url }) {
  const el = document.createElement("div");
  el.className = `toast ${decision === "approved" || decision === "denied" ? decision : ""}`;

  const icon = document.createElement("div");
  icon.className = "toast-icon";
  icon.innerHTML = DECISION_ICON[decision] ?? ICON_PENDING;

  const body = document.createElement("div");
  body.className = "toast-body";

  const titleEl = document.createElement("div");
  titleEl.className = "toast-title";
  titleEl.textContent = title;

  const msgEl = document.createElement("div");
  msgEl.className = "toast-msg";
  // Reasons echo back caller-supplied agent_id/action values, so this is
  // set as text, never parsed as markup.
  msgEl.textContent = message;

  body.append(titleEl, msgEl);

  if (url) {
    const link = document.createElement("a");
    link.className = "toast-link";
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "View on Algorand explorer →";
    body.appendChild(link);
  }

  const timer = document.createElement("span");
  timer.className = "toast-timer";
  const life = 6000;
  timer.style.setProperty("--life", `${life}ms`);

  el.append(icon, body, timer);
  toastStackEl.appendChild(el);

  // Keep the stack from growing without bound during autopilot runs.
  while (toastStackEl.children.length > 4) toastStackEl.firstElementChild.remove();

  const dismiss = () => {
    if (!el.isConnected || el.classList.contains("is-leaving")) return;
    el.classList.add("is-leaving");
    setTimeout(() => el.remove(), 340);
  };
  el.addEventListener("click", dismiss);
  setTimeout(dismiss, life);
}

/* -------------------------------------------------------------------------
   Agent cards

   refresh() polls every 2s. Rebuilding agent cards from scratch on every
   poll (the old approach) destroys and recreates the amount-input/button
   DOM nodes each time -- which wipes whatever you were typing and steals
   focus mid-edit, even if your mouse never left the field. Interactive
   elements are built exactly once per agent (allowed_actions, limits, and
   address are static for the life of the page); only the numbers that
   actually change on a poll (spend, counts, ring) get patched in place.
   ---------------------------------------------------------------------- */

const agentCards = {}; // agent_id -> { root, els }
let builtAgentIds = null;

const BOT_SVG = `
  <svg class="bot" viewBox="0 0 56 56" fill="none" aria-hidden="true">
    <line class="bot-antenna" x1="28" y1="6" x2="28" y2="14" />
    <circle class="bot-bulb" cx="28" cy="4.5" r="3" />
    <rect class="bot-head" x="8" y="14" width="40" height="33" rx="12" />
    <g class="bot-eyes">
      <circle class="bot-eye" cx="20" cy="28" r="3.4" />
      <circle class="bot-eye" cx="36" cy="28" r="3.4" />
    </g>
    <g class="bot-crosses">
      <path class="bot-cross" d="M17 25l6 6M23 25l-6 6" />
      <path class="bot-cross" d="M33 25l6 6M39 25l-6 6" />
    </g>
    <path class="bot-mouth" d="M21 37q7 5 14 0" />
    <path class="bot-mouth-sad" d="M21 40q7 -5 14 0" />
  </svg>`;

function buildAgentCard(agent, index) {
  const violateAction = firstDisallowedAction(agent);

  const card = document.createElement("div");
  card.className = "agent-card mood-ok";
  card.style.setProperty("--hue", hueFor(agent.agent_id));
  card.style.animationDelay = `${index * 70}ms`;
  card.dataset.agentId = agent.agent_id;

  card.innerHTML = `
    <div class="agent-card-head">
      <div class="agent-id-block">
        <h3>${escapeHtml(agent.display_name)}</h3>
        <div class="agent-status"><i></i><span data-el="statusLabel">Within policy</span></div>
        <div class="limit-chip">≤ ${fmtUsd(agent.per_request_limit_usd)} per call</div>
      </div>
      <span class="agent-avatar">${BOT_SVG}</span>
    </div>

    <div class="govern-row">
      <span class="govern-chip" data-el="velocityChip" title="Requests in the last 60s vs this agent's ceiling">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a9 9 0 1 0 9 9"/><path d="M12 12 17 7"/></svg>
        <b data-el="velocityText">&mdash;</b>
      </span>
      <span class="govern-chip" data-el="approvalChip" title="Spends above this need a human to release them">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 20v-1.5a3.5 3.5 0 0 0-7 0V20"/><circle cx="12.5" cy="9" r="3"/><path d="M4 20v-1a4 4 0 0 1 3-3.9"/></svg>
        <b data-el="approvalText">&mdash;</b>
      </span>
    </div>

    <div class="cap-block">
      <div class="ring-wrap">
        <svg class="ring" viewBox="0 0 76 76">
          <circle class="ring-track" cx="38" cy="38" r="${RING_RADIUS}" />
          <circle class="ring-fill" data-el="ringFill" cx="38" cy="38" r="${RING_RADIUS}" />
        </svg>
        <span class="ring-pct" data-el="ringPct">0%</span>
      </div>
      <div class="cap-meta">
        <div class="cap-title">Daily budget</div>
        <div class="cap-value">
          <b data-el="capSpend">$0.00</b> <span>/ ${fmtUsd(agent.daily_cap_usd)}</span>
        </div>
        <div class="cap-note" data-el="capNote"></div>
      </div>
    </div>

    <div class="agent-stats">
      <div class="stat">
        <div class="stat-label">Total spend</div>
        <div class="stat-value" data-el="totalSpend">$0.00</div>
      </div>
      <div class="stat">
        <div class="stat-label">Approved</div>
        <div class="stat-value approved" data-el="approvedCount">0</div>
      </div>
      <div class="stat">
        <div class="stat-label">Denied</div>
        <div class="stat-value denied" data-el="deniedCount">0</div>
      </div>
    </div>

    <div class="agent-address" title="${escapeHtml(agent.address || "unfunded")}">${escapeHtml(agent.address || "unfunded")}</div>

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
    btn.type = "button";
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
    btn.type = "button";
    btn.textContent = `Try: ${violateAction} (should deny)`;
    btn.onclick = () => fireSpend(agent.agent_id, violateAction, btn, policyActions[violateAction]?.price_usd ?? 0);
    actionsEl.appendChild(btn);
  }

  // The kill switch. Every other control on this card is a budget -- it
  // bounds how much a misbehaving agent spends before someone notices.
  // This is the one for after someone notices, so it gets its own full-width
  // row rather than sitting among the "fire a request" buttons.
  const freezeBtn = document.createElement("button");
  freezeBtn.className = "btn btn-freeze";
  freezeBtn.type = "button";
  freezeBtn.dataset.el = "freezeBtn";
  freezeBtn.onclick = () => toggleFreeze(agent.agent_id, freezeBtn);
  actionsEl.appendChild(freezeBtn);

  const els = {};
  card.querySelectorAll("[data-el]").forEach((node) => {
    els[node.dataset.el] = node;
  });

  // stroke-dasharray must match the rendered circumference exactly or the
  // ring under/over-shoots; set it here from the same radius the markup uses.
  els.ringFill.style.strokeDasharray = RING_CIRCUMFERENCE.toFixed(2);
  els.ringFill.style.strokeDashoffset = RING_CIRCUMFERENCE.toFixed(2);

  return { root: card, els };
}

function patchAgentCard(agent, card, els) {
  const pct = agent.daily_cap_usd > 0
    ? Math.min(100, (agent.daily_spend_usd / agent.daily_cap_usd) * 100)
    : 0;

  animateValue(els.totalSpend, agent.total_spend_usd, fmtUsd);
  animateValue(els.approvedCount, agent.approved_count, intFmt);
  animateValue(els.deniedCount, agent.denied_count, intFmt);
  animateValue(els.capSpend, agent.daily_spend_usd, fmtUsd);

  els.ringPct.textContent = `${pct.toFixed(0)}%`;
  els.ringFill.style.strokeDashoffset = (RING_CIRCUMFERENCE * (1 - pct / 100)).toFixed(2);
  els.ringFill.classList.remove("warn", "full");
  const mc = meterClass(pct);
  if (mc) els.ringFill.classList.add(mc);

  const remaining = Math.max(0, agent.daily_cap_usd - agent.daily_spend_usd);
  els.capNote.textContent = remaining > 0
    ? `${fmtUsd(remaining)} left before the engine stops it`
    : "further spends will be denied today";

  const rpm = agent.max_requests_per_minute;
  els.velocityText.textContent =
    rpm === null || rpm === undefined
      ? `${agent.recent_requests}/min · no limit`
      : `${agent.recent_requests}/${rpm} per min`;
  els.velocityChip.classList.toggle(
    "is-hot",
    rpm !== null && rpm !== undefined && agent.recent_requests >= rpm * 0.7
  );

  const threshold = agent.require_approval_above_usd;
  els.approvalText.textContent =
    threshold === null || threshold === undefined
      ? "no review needed"
      : `review > ${fmtUsd(threshold)}`;

  els.freezeBtn.textContent = agent.frozen ? "Unfreeze agent" : "Freeze agent";
  els.freezeBtn.classList.toggle("is-frozen", !!agent.frozen);

  // A frozen agent is stopped regardless of how much budget it has left, so
  // its cap-derived mood would actively mislead here ("Within policy" on an
  // agent that cannot spend at all). Frozen wins.
  card.classList.remove("mood-ok", "mood-warn", "mood-blocked", "mood-frozen");
  if (agent.frozen) {
    els.statusLabel.textContent = "Frozen — kill switch engaged";
    card.classList.add("mood-frozen");
  } else {
    const mood = capMood(pct);
    els.statusLabel.textContent = mood.label;
    card.classList.add(mood.cls);
  }
}

async function toggleFreeze(agentId, btn) {
  const freezing = !btn.classList.contains("is-frozen");
  btn.disabled = true;
  try {
    await fetchJSON(`/admin/agents/${encodeURIComponent(agentId)}/freeze`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ frozen: freezing }),
    });
    toast({
      decision: freezing ? "denied" : "approved",
      title: freezing ? "Agent frozen" : "Agent unfrozen",
      message: freezing
        ? `${agentId} is stopped immediately — every spend is denied until you release it.`
        : `${agentId} can spend within its limits again.`,
    });
  } catch (e) {
    console.error("freeze failed", e);
    toast({ decision: "denied", title: "Could not change the kill switch", message: e.message });
  } finally {
    btn.disabled = false;
    refresh();
  }
}

function renderAgents(agents) {
  const ids = agents.map((a) => a.agent_id).sort().join(",");
  if (ids !== builtAgentIds) {
    // First load, or the set of agents actually changed -- (re)build the
    // interactive elements. Any in-progress edits are necessarily gone
    // here regardless of approach, but this only fires once in practice.
    agentsEl.innerHTML = "";
    for (const key of Object.keys(agentCards)) delete agentCards[key];
    agents.forEach((agent, i) => {
      const built = buildAgentCard(agent, i);
      agentsEl.appendChild(built.root);
      agentCards[agent.agent_id] = built;
    });
    builtAgentIds = ids;
  }
  for (const agent of agents) {
    const built = agentCards[agent.agent_id];
    if (built) patchAgentCard(agent, built.root, built.els);
  }
}

// Transient celebration/rejection on the card that fired. The mood-* classes
// are recomputed from cap usage on every poll, so this uses its own
// short-lived class that can't be clobbered by a refresh landing mid-animation.
function flashCard(agentId, decision) {
  const built = agentCards[agentId];
  if (!built || (decision !== "approved" && decision !== "denied")) return;
  const card = built.root;
  const cls = decision === "approved" ? "is-approving" : "is-denying";

  card.classList.remove("is-approving", "is-denying");
  void card.offsetWidth; // restart the animation if the same class re-applies
  card.classList.add(cls);
  setTimeout(() => card.classList.remove(cls), decision === "approved" ? 1100 : 1400);

  const stamp = document.createElement("div");
  stamp.className = `verdict-stamp ${decision}`;
  stamp.textContent = decision === "approved" ? "Approved" : "Denied";
  card.appendChild(stamp);
  setTimeout(() => stamp.remove(), 1450);
}

/* -------------------------------------------------------------------------
   Fleet totals + decision pulse
   ---------------------------------------------------------------------- */

function renderHero(agents, requests) {
  const totals = agents.reduce(
    (acc, a) => {
      acc.spend += a.total_spend_usd;
      acc.approved += a.approved_count;
      acc.denied += a.denied_count;
      return acc;
    },
    { spend: 0, approved: 0, denied: 0 }
  );

  // Unknown agents never appear in /agents (they have no policy entry), so
  // their denials are only visible in the feed -- count those in too, or the
  // "Blocked" tile silently undercounts the exact scenario it exists to show.
  const knownIds = new Set(agents.map((a) => a.agent_id));
  for (const r of requests) {
    if (!knownIds.has(r.agent_id)) {
      if (r.decision === "approved") totals.approved++;
      else if (r.decision === "denied") totals.denied++;
    }
  }

  const settled = requests.filter((r) => r.tx_id).length;
  const decided = totals.approved + totals.denied;
  const rate = decided > 0 ? (totals.approved / decided) * 100 : 0;

  animateValue(statEls.spend, totals.spend, fmtUsd);
  animateValue(statEls.approved, totals.approved, intFmt);
  animateValue(statEls.denied, totals.denied, intFmt);
  animateValue(statEls.tx, settled, intFmt);
  statEls.rate.textContent = decided > 0 ? `${rate.toFixed(0)}%` : "—";
  statEls.rateFill.style.width = `${decided > 0 ? rate : 0}%`;
}

const PULSE_MAX = 72;

function renderPulse(requests, newSince) {
  if (requests.length === 0) {
    const msg = engineOnline === false
      ? "Policy engine unreachable — retrying…"
      : "Awaiting the first request…";
    pulseStripEl.innerHTML = `<span class="pulse-empty">${escapeHtml(msg)}</span>`;
    return;
  }
  // /requests comes back newest-first; the strip reads left-to-right in time
  // order, so the newest tick lands on the right edge.
  const recent = requests.slice(0, PULSE_MAX).reverse();
  const maxAmount = Math.max(...recent.map((r) => r.amount_usd), 0.01);

  pulseStripEl.innerHTML = "";
  for (const r of recent) {
    const tick = document.createElement("i");
    const decision = DECISION_ICON[r.decision] ? r.decision : "pending";
    tick.className = `tick ${decision}${r.id > newSince ? " is-new" : ""}`;
    // Floor at 22% so a $0.00 denial still registers as a visible mark.
    tick.style.height = `${22 + (r.amount_usd / maxAmount) * 78}%`;
    tick.title = `${r.agent_id} · ${r.action} · ${fmtUsd(r.amount_usd)} · ${r.decision}`;
    pulseStripEl.appendChild(tick);
  }
}

/* -------------------------------------------------------------------------
   Feed
   ---------------------------------------------------------------------- */

const EMPTY_LABEL = {
  all: ["No requests yet", "Fire one from an agent card above, or flip on Autopilot."],
  approved: ["No approvals yet", "Approved spends will appear here with their Algorand tx."],
  denied: ["Nothing blocked yet", "Try a disallowed action or overshoot a per-call limit."],
};

// An unreachable engine and an unused demo both bottom out at "zero of
// everything", so the empty states have to say which one you're looking at.
const OFFLINE_LABEL = [
  "Can't reach the policy engine",
  `Nothing is being polled. Is it running on ${POLICY_ENGINE_URL}?`,
];

function renderFeed(requests, newSince) {
  const rows = feedFilter === "all" ? requests : requests.filter((r) => r.decision === feedFilter);

  feedCountEl.textContent = feedFilter === "all"
    ? `${requests.length} request${requests.length === 1 ? "" : "s"}`
    : `${rows.length} of ${requests.length}`;

  if (rows.length === 0) {
    const [title, sub] = engineOnline === false && requests.length === 0
      ? OFFLINE_LABEL
      : EMPTY_LABEL[feedFilter] ?? EMPTY_LABEL.all;
    feedBodyEl.innerHTML = `
      <tr><td colspan="7" class="empty-feed">
        <svg class="empty-art" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
          <path d="M3 12h4l2.5-6 4 12L16 12h5"/>
        </svg>
        <div class="empty-title">${escapeHtml(title)}</div>
        <div class="empty-sub">${escapeHtml(sub)}</div>
      </td></tr>`;
    return;
  }

  feedBodyEl.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.className = `d-${DECISION_ICON[r.decision] ? r.decision : "pending"}`;
    if (r.id > newSince) tr.classList.add("row-new");
    tr.style.setProperty("--hue", hueFor(r.agent_id));

    const time = new Date(r.timestamp).toLocaleTimeString();
    // decision itself only ever comes from our own server code (a fixed
    // enum, never request input), so it's safe unescaped as a class name --
    // but everything below can originate from an attacker's /spend body,
    // so every one of these goes through escapeHtml before interpolation.
    const icon = DECISION_ICON[r.decision] ?? "";
    const decisionPill = `<span class="decision-pill ${r.decision}">${icon}${escapeHtml(decisionLabel(r.decision))}</span>`;
    const txCell = r.explorer_url
      ? `<a class="tx-link" href="${escapeHtml(r.explorer_url)}" target="_blank" rel="noopener">${escapeHtml(r.tx_id.slice(0, 8))}&hellip;${ICON_LINK}</a>`
      : `<span class="tx-empty">&mdash;</span>`;

    tr.innerHTML = `
      <td class="cell-time">${escapeHtml(time)}</td>
      <td class="cell-agent"><span class="dot"></span>${escapeHtml(r.agent_id)}</td>
      <td class="cell-action"><code>${escapeHtml(r.action)}</code></td>
      <td class="cell-amount">${escapeHtml(fmtUsd(r.amount_usd))}</td>
      <td>${decisionPill}</td>
      <td class="reason-cell">${escapeHtml(r.reason)}</td>
      <td>${txCell}</td>
    `;
    feedBodyEl.appendChild(tr);
  }
}

feedFiltersEl.addEventListener("click", (e) => {
  const btn = e.target.closest(".filter-btn");
  if (!btn) return;
  feedFilter = btn.dataset.filter;
  feedFiltersEl.querySelectorAll(".filter-btn").forEach((b) => {
    b.classList.toggle("is-active", b === btn);
  });
  // Re-render from cache with newSince = lastSeenRequestId so switching
  // filters doesn't re-play the "new row" animation on old rows.
  renderFeed(lastRequests, lastSeenRequestId);
});

/* -------------------------------------------------------------------------
   Approval queue

   Spends above an agent's threshold park here holding their budget until a
   person decides. Rebuilt wholesale on each poll rather than patched in
   place (unlike agent cards): these rows carry no editable state to
   clobber, and the set changes on every decision, so a diff would be more
   machinery than the thing it optimises.
   ---------------------------------------------------------------------- */

function renderApprovals(holds) {
  const has = holds.length > 0;
  approvalsHeadEl.hidden = !has;
  approvalsEl.hidden = !has;
  if (!has) {
    approvalsEl.innerHTML = "";
    return;
  }

  approvalsEl.innerHTML = "";
  for (const hold of holds) {
    const card = document.createElement("article");
    card.className = "approval-card";
    card.style.setProperty("--hue", hueFor(hold.agent_id));
    card.innerHTML = `
      <div class="approval-top">
        <span class="approval-amount">${escapeHtml(fmtUsd(hold.amount_usd))}</span>
        <code class="approval-action">${escapeHtml(hold.action)}</code>
      </div>
      <div class="approval-agent"><span class="dot"></span>${escapeHtml(hold.agent_id)}</div>
      <p class="approval-reason">${escapeHtml(hold.reason)}</p>
      <div class="approval-actions"></div>
    `;

    const actions = card.querySelector(".approval-actions");

    const approve = document.createElement("button");
    approve.className = "btn btn-approve";
    approve.type = "button";
    approve.textContent = "Approve & pay";
    approve.onclick = () => resolveHold(hold.id, "approve", card);

    const reject = document.createElement("button");
    reject.className = "btn btn-reject";
    reject.type = "button";
    reject.textContent = "Reject";
    reject.onclick = () => resolveHold(hold.id, "reject", card);

    actions.append(approve, reject);
    approvalsEl.appendChild(card);
  }
}

async function resolveHold(requestId, verb, card) {
  card.querySelectorAll("button").forEach((b) => (b.disabled = true));
  card.classList.add("is-resolving");
  try {
    const result = await fetchJSON(`/admin/holds/${requestId}/${verb}`, { method: "POST" });
    playVerdictSound(result.decision);
    toast({
      decision: result.decision,
      title: result.decision === "approved" ? "Released — payment settled" : "Rejected",
      message: result.reason || "",
      url: result.explorer_url || null,
    });
  } catch (e) {
    console.error("hold resolution failed", e);
    card.classList.remove("is-resolving");
    card.querySelectorAll("button").forEach((b) => (b.disabled = false));
    toast({ decision: "denied", title: "Could not resolve the hold", message: e.message });
  } finally {
    refresh();
    refreshIntegrity({ onchain: false });
  }
}

/* -------------------------------------------------------------------------
   Audit integrity

   The panel that answers "why does this need a blockchain?". Two polls, on
   purpose: the local hash-chain recomputation is cheap and runs often, but
   confirming an anchor means asking a public Algorand indexer, and hammering
   someone else's free endpoint every few seconds to re-learn a fact that
   cannot have changed would be rude and rate-limited. So the on-chain leg
   runs on a slow timer and on demand.
   ---------------------------------------------------------------------- */

const VERDICT_COPY = {
  verified: {
    label: "Verified on Algorand",
    blurb: "Every audit record hashes correctly and the chain head is notarized on-chain.",
  },
  unanchored: {
    label: "Not yet notarized",
    blurb: "The hash chain is intact, but nothing is committed on-chain yet.",
  },
  tampered: {
    label: "Tampering detected",
    blurb: "The audit trail does not match what was committed to Algorand.",
  },
  unknown: {
    label: "Integrity check unavailable",
    blurb: "Couldn't reach the policy engine to verify the ledger — this is not a tamper finding.",
  },
};

let integrityBusy = false;
// The last verdict that came from a check which actually consulted the
// chain. Local-only polls can't confirm an anchor, so on their own they'd
// report "unanchored" -- and rendering that verbatim makes the panel flip
// between "Verified on Algorand" and "Not yet notarized" every few seconds
// purely as an artifact of which poll ran last. A local poll may still
// DOWNGRADE to "tampered": recomputing the hash chain is exactly what it
// does, and that finding needs no network to be true.
let lastOnchainVerdict = null;
let lastOnchainDetail = null;

function renderIntegrity(report, { authoritative }) {
  const chainBroken = !report.chain.ok;

  let verdict = report.verdict;
  if (!authoritative && verdict === "unanchored" && lastOnchainVerdict === "verified") {
    verdict = "verified";
  }
  if (authoritative) {
    lastOnchainVerdict = verdict;
    lastOnchainDetail = report.detail;
  }

  const copy = VERDICT_COPY[verdict] ?? VERDICT_COPY.unanchored;
  integrityEls.root.dataset.verdict = verdict;
  integrityEls.state.textContent = copy.label;

  // Which detail line to show. The specific one -- "event 6 claims to
  // follow abc… but the actual previous event hashes to def…" -- is the
  // whole point of the panel, so it has to survive the next local poll
  // rather than being flattened back to a generic blurb four seconds
  // after it appears. A local poll's own detail is used only when that
  // poll genuinely found something (a broken chain needs no network to
  // detect); otherwise it re-shows the last authoritative finding.
  let detail;
  if (authoritative || chainBroken) {
    detail = report.detail || copy.blurb;
  } else if (verdict === lastOnchainVerdict && lastOnchainDetail) {
    detail = lastOnchainDetail;
  } else {
    detail = copy.blurb;
  }
  integrityEls.detail.textContent = detail;

  integrityEls.events.textContent = report.chain.event_count.toLocaleString();

  const head = report.chain.head_hash;
  integrityEls.head.textContent = head ? `${head.slice(0, 10)}…` : "—";
  integrityEls.head.title = head ? `seq ${report.chain.head_seq} · ${head}` : "the ledger is empty";

  const latest = report.anchors[0];
  if (!latest) {
    integrityEls.anchor.textContent = "never";
    integrityEls.anchor.title = "";
  } else {
    integrityEls.anchor.innerHTML = latest.explorer_url
      ? `<a href="${escapeHtml(latest.explorer_url)}" target="_blank" rel="noopener">seq ${
          latest.seq
        } · ${escapeHtml(latest.tx_id.slice(0, 8))}…${ICON_LINK}</a>`
      : `seq ${latest.seq}`;
    integrityEls.anchor.title = `${report.unanchored_events} event(s) since this anchor`;
  }

  // Only offer the undo when there's something to undo, so the demo control
  // can't be mistaken for a general "repair the ledger" button -- which is
  // exactly the thing this system must not have.
  restoreBtn.hidden = verdict !== "tampered";
}

async function refreshIntegrity({ onchain = false } = {}) {
  try {
    const report = await fetchJSON(`/audit/verify?onchain=${onchain}&anchors=1`);
    renderIntegrity(report, { authoritative: onchain });
  } catch (e) {
    // Don't paint the panel red -- "couldn't ask" is not "tampering
    // detected", and conflating them would make the red state worthless.
    // But don't leave it on "Checking…" either: a panel whose whole job is
    // reporting integrity must never look like it's still working on it
    // when it has actually given up. That exact silent-forever state (a
    // stale cached app.js throwing in here) is what made this failure so
    // hard to spot, which is the argument for saying so out loud.
    console.error("integrity check failed", e);
    integrityEls.root.dataset.verdict = "unknown";
    integrityEls.state.textContent = VERDICT_COPY.unknown.label;
    integrityEls.detail.textContent = VERDICT_COPY.unknown.blurb;
  }
}

anchorBtn.onclick = async () => {
  if (integrityBusy) return;
  integrityBusy = true;
  anchorBtn.disabled = true;
  anchorBtn.classList.add("is-working");
  try {
    const result = await fetchJSON("/audit/anchor", { method: "POST" });
    if (result.status === "anchored") {
      toast({
        decision: "approved",
        title: "Chain head notarized",
        message: `seq ${result.anchor.seq} committed to Algorand testnet.`,
        url: result.anchor.explorer_url,
      });
    } else {
      toast({
        decision: "pending",
        title: "Nothing to notarize",
        message: result.detail || result.status,
      });
    }
  } catch (e) {
    console.error("anchor failed", e);
    toast({ decision: "denied", title: "Notarization failed", message: e.message });
  } finally {
    integrityBusy = false;
    anchorBtn.disabled = false;
    anchorBtn.classList.remove("is-working");
    refreshIntegrity({ onchain: true });
  }
};

tamperBtn.onclick = async () => {
  tamperBtn.disabled = true;
  try {
    // No `value` -- the engine picks the priciest recent record and a
    // replacement that's guaranteed to differ from what's already stored.
    const result = await fetchJSON("/admin/demo/tamper", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ field: "amount_usd" }),
    });
    const was = result.original?.amount_usd;
    const now = result.replaced_with?.amount_usd;
    toast({
      decision: "denied",
      title: `Audit record #${result.seq} doctored`,
      message:
        `Amount rewritten from $${was} to $${now}, and the record's own hash recomputed ` +
        "to match — the edit is internally consistent.",
    });
  } catch (e) {
    console.error("tamper failed", e);
    toast({ decision: "denied", title: "Could not tamper", message: e.message });
  } finally {
    tamperBtn.disabled = false;
    refreshIntegrity({ onchain: true });
  }
};

restoreBtn.onclick = async () => {
  restoreBtn.disabled = true;
  try {
    const result = await fetchJSON("/admin/demo/restore", { method: "POST" });
    toast({
      decision: "approved",
      title: "Records restored",
      message: result.restored.length
        ? `Original bytes put back for event(s) ${result.restored.join(", ")}.`
        : "Nothing to restore.",
    });
  } catch (e) {
    console.error("restore failed", e);
    toast({ decision: "denied", title: "Could not restore", message: e.message });
  } finally {
    restoreBtn.disabled = false;
    refreshIntegrity({ onchain: true });
  }
};

/* -------------------------------------------------------------------------
   Polling
   ---------------------------------------------------------------------- */

function setEngineStatus(online) {
  if (online === engineOnline) return;
  engineOnline = online;
  statusChipEl.classList.toggle("is-offline", !online);
  statusLabelEl.textContent = online ? "Algorand Testnet" : "Engine offline";
  statusChipEl.title = online
    ? `Polling ${POLICY_ENGINE_URL} every 2s`
    : `Can't reach ${POLICY_ENGINE_URL} — still retrying`;
}

async function refresh() {
  try {
    const [agents, requests, approvals] = await Promise.all([
      fetchJSON("/agents"),
      fetchJSON("/requests?limit=100"),
      fetchJSON("/approvals"),
    ]);
    setEngineStatus(true);
    lastRequests = requests;
    // Snapshot before rendering: several renderers need to know which rows
    // are new, and only the last one may advance the watermark.
    const newSince = lastSeenRequestId;
    renderAgents(agents);
    renderHero(agents, requests);
    renderPulse(requests, newSince);
    renderApprovals(approvals);
    renderFeed(requests, newSince);
    if (requests.length) lastSeenRequestId = Math.max(...requests.map((r) => r.id));
  } catch (e) {
    console.error("refresh failed", e);
    setEngineStatus(false);
    // Repaint the zero-data placeholders so an unreachable engine reads as
    // "offline" rather than as a demo nobody has used yet. Cached rows are
    // left alone -- stale data beats blanking the feed on one dropped poll.
    if (lastRequests.length === 0) {
      renderPulse([], lastSeenRequestId);
      renderFeed([], lastSeenRequestId);
    }
  }
}

async function fireSpend(agentId, action, btn, amountUsd) {
  if (btn) btn.disabled = true;
  const amount = Number.isFinite(amountUsd) ? amountUsd : 0;
  try {
    // /spend requires a signature proving the caller controls agent_id's
    // Algorand key (see common/identity.py). The dashboard is a human
    // operator, not a separate cryptographic identity, so it asks the
    // policy engine's admin/sign convenience endpoint to sign on the demo
    // agent's behalf -- the same custodial key the engine already holds for
    // payment signing -- rather than ever handling private key material in
    // the browser. A real autonomous agent would sign locally instead,
    // exactly like agents/simulate.py does.
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
    const result = await fetchJSON("/spend", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent_id: agentId, action, amount_usd: amount, ...signed }),
    });

    flashCard(agentId, result.decision);
    playVerdictSound(result.decision);
    const TITLE = {
      approved: "Payment authorized",
      denied: "Blocked by policy",
      awaiting_approval: "Held for human approval",
    };
    toast({
      decision: result.decision,
      title: TITLE[result.decision] ?? "Decision recorded",
      message: result.reason || "",
      url: result.explorer_url || null,
    });
  } catch (e) {
    console.error("spend failed", e);
    toast({
      decision: "denied",
      title: "Request failed",
      message: `Could not reach the policy engine (${e.message}). Is it running on ${POLICY_ENGINE_URL}?`,
    });
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
  resetBtn.disabled = true;
  try {
    await fetchJSON("/admin/reset", { method: "POST" });
    lastSeenRequestId = 0;
    autoplayIndex = 0;
    await refresh();
    toast({ decision: "pending", title: "Audit log cleared", message: "Local demo log only — nothing on-chain was touched." });
  } catch (e) {
    console.error("reset failed", e);
  } finally {
    resetBtn.disabled = false;
  }
};

autoplayToggle.onchange = toggleAutoplay;

/* -------------------------------------------------------------------------
   Cursor spotlight -- one delegated listener, coalesced into a single rAF,
   writing two custom properties that the CSS gradient reads. No per-card
   listeners and no layout reads in the hot path.
   ---------------------------------------------------------------------- */

let spotlightPending = null;
let spotlightQueued = false;

document.addEventListener("pointermove", (e) => {
  const card = e.target.closest?.(".tile, .agent-card");
  if (!card) return;
  spotlightPending = { card, x: e.clientX, y: e.clientY };
  if (spotlightQueued) return;
  spotlightQueued = true;
  requestAnimationFrame(() => {
    spotlightQueued = false;
    const { card: c, x, y } = spotlightPending;
    const rect = c.getBoundingClientRect();
    c.style.setProperty("--mx", `${((x - rect.left) / rect.width) * 100}%`);
    c.style.setProperty("--my", `${((y - rect.top) / rect.height) * 100}%`);
  });
}, { passive: true });

/* -------------------------------------------------------------------------
   Startup

   The dashboard and the policy engine come up together (start.sh launches
   both, then prints the URL), so the first /policy call routinely races the
   engine's boot -- and a browser tab left open across a backend restart hits
   the same window. That call used to be un-caught and sat *before*
   setInterval, so a single failed fetch escaped as an unhandled rejection
   and the poll was never registered at all: the page froze on its zero
   state -- every tile at 0, a blank pulse strip, an empty feed whose filter
   tabs had nothing to filter -- and only a manual reload brought it back.
   refresh() has always handled its own failures, so the poll is started
   first and unconditionally; policy config retries independently.
   ---------------------------------------------------------------------- */

async function loadPolicy() {
  // Retries until it lands: the status chip is already reporting the outage,
  // so this backs off quietly rather than logging on every attempt forever.
  for (let delay = 500, attempt = 0; ; delay = Math.min(delay * 2, 8000), attempt++) {
    try {
      const policy = await fetchJSON("/policy");
      policyActions = policy.actions;
      if (attempt > 0) console.info(`policy loaded after ${attempt} retries`);
      return;
    } catch (e) {
      if (attempt === 0) console.warn("policy fetch failed, retrying", e);
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
  }
}

(async function init() {
  setInterval(refresh, 2000);
  refresh();

  // Two cadences on purpose -- see the "Audit integrity" section above. The
  // cheap local recomputation keeps up with live traffic; the on-chain
  // confirmation is a request to a public indexer and runs rarely.
  refreshIntegrity({ onchain: true });
  setInterval(() => refreshIntegrity({ onchain: false }), 4000);
  setInterval(() => refreshIntegrity({ onchain: true }), 25000);

  await loadPolicy();
  // Cards built before the policy landed have empty amount fields and no
  // "should deny" button (both are derived from policyActions), so force one
  // rebuild now that the real prices and action list are known.
  builtAgentIds = null;
  refresh();
})();
