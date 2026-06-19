/* RollCall Mini App — vanilla JS, no build step */
'use strict';

const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

// ── State ────────────────────────────────────────────────────────────────────
const state = {
  token: null,
  chatId: null,
  userId: null,
  rollcalls: [],
  activeIdx: 0,   // which rollcall tab is shown
};

// ── DOM refs ─────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const $loading     = $('loading');
const $errorScreen = $('error-screen');
const $errorMsg    = $('error-msg');
const $retryBtn    = $('retry-btn');
const $main        = $('main');
const $chatTitle   = $('chat-title');
const $rcTabs      = $('rc-tabs');
const $rcList      = $('rollcall-list');
const $emptyState  = $('empty-state');

// ── API helpers ──────────────────────────────────────────────────────────────
const API = window.location.origin + '/api/v1';

async function apiFetch(path, opts = {}) {
  const res = await fetch(API + path, {
    ...opts,
    headers: {
      'Content-Type': 'application/json',
      ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}),
      ...(opts.headers || {}),
    },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

// ── Auth ─────────────────────────────────────────────────────────────────────
async function auth() {
  const initData = tg.initData;
  if (!initData) {
    // Dev mode: show a "no initData" message so devs know what's happening
    throw new Error('No Telegram initData — open this page inside the Telegram app.');
  }

  const data = await apiFetch('/auth/telegram/miniapp', {
    method: 'POST',
    body: JSON.stringify({ init_data: initData }),
  });

  state.token  = data.token;
  state.chatId = data.chat_id;
  state.userId = data.user_id;
}

// ── Data loading ─────────────────────────────────────────────────────────────
async function loadRollcalls() {
  const data = await apiFetch(`/chats/${state.chatId}/rollcalls`);
  // API returns a plain array of rollcall objects
  state.rollcalls = Array.isArray(data) ? data : (data.rollcalls || []);
}

// ── Voting ───────────────────────────────────────────────────────────────────
async function castVote(rcNumber, voteType, comment = '') {
  const user = tg.initDataUnsafe?.user || {};
  const firstName = user.first_name || 'User';
  const username  = user.username   || null;

  const body = {
    vote:       voteType,
    user_id:    state.userId,
    first_name: firstName,
    username,
  };
  if (comment) body.comment = comment;

  const ep = `/chats/${state.chatId}/rollcalls/${rcNumber}/votes`;
  return apiFetch(ep, { method: 'POST', body: JSON.stringify(body) });
}

// ── Rendering ─────────────────────────────────────────────────────────────────
function myStatus(rc) {
  const uid = state.userId;
  if (rc.in_list?.some(u => u.user_id === uid))   return 'in';
  if (rc.out_list?.some(u => u.user_id === uid))  return 'out';
  if (rc.maybe_list?.some(u => u.user_id === uid))return 'maybe';
  if (rc.wait_list?.some(u => u.user_id === uid)) return 'wait';
  return null;
}

function renderName(u) {
  return u.name || u.first_name || `User ${u.user_id}`;
}

function chipHtml(u, isMe) {
  const cls = isMe ? 'person-chip me' : 'person-chip';
  const name = renderName(u);
  const comment = u.comment ? ` · ${u.comment}` : '';
  return `<li class="${cls}" title="${escHtml(name + comment)}">${escHtml(name)}${comment ? `<span style="opacity:.7">${escHtml(comment)}</span>` : ''}</li>`;
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function renderRollcall(rc, rcIdx) {
  const uid    = state.userId;
  const status = myStatus(rc);
  const limit  = rc.limit;
  const inCount = rc.in_count || 0;
  const filled  = limit ? Math.min(inCount / limit, 1) : 0;

  const metaParts = [];
  if (limit) metaParts.push(`<span>👥 ${inCount}/${limit}</span>`);
  else if (inCount) metaParts.push(`<span>👥 ${inCount} in</span>`);
  if (rc.location) metaParts.push(`<span>📍 ${escHtml(rc.location)}</span>`);
  if (rc.finalize_date) {
    const dt = new Date(rc.finalize_date);
    metaParts.push(`<span>⏰ ${dt.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</span>`);
  }

  let limitBarHtml = '';
  if (limit) {
    const pct = Math.round(filled * 100);
    const fullCls = inCount >= limit ? ' full' : '';
    limitBarHtml = `<div class="limit-bar"><div class="limit-fill${fullCls}" style="width:${pct}%"></div></div>`;
  }

  const inHtml    = (rc.in_list || []).map(u => chipHtml(u, u.user_id === uid)).join('');
  const outHtml   = (rc.out_list || []).map(u => chipHtml(u, u.user_id === uid)).join('');
  const maybeHtml = (rc.maybe_list || []).map(u => chipHtml(u, u.user_id === uid)).join('');
  const waitHtml  = (rc.wait_list || []).map(u => chipHtml(u, u.user_id === uid)).join('');

  const btnClass = v => `btn btn-${v}${status === v ? ' selected' : ''}`;
  const waitNote = status === 'wait'
    ? `<div class="vote-status">You're on the waitlist${limit ? ` (${inCount}/${limit} spots filled)` : ''}.</div>`
    : `<div class="vote-status" id="vstatus-${rcIdx}">${status ? `You voted <b>${status}</b>.` : ''}</div>`;

  return `
<div class="rc-card" id="rc-${rcIdx}">
  ${metaParts.length ? `<div class="rc-meta">${metaParts.join('')}</div>` : ''}
  ${limitBarHtml}
  <div class="comment-row">
    <input class="comment-input" id="comment-${rcIdx}" placeholder="Add a comment (optional)" maxlength="120" />
  </div>
  <div class="vote-bar">
    <button class="${btnClass('in')}"    onclick="vote(${rcIdx},'in')"   >✅ In</button>
    <button class="${btnClass('out')}"   onclick="vote(${rcIdx},'out')"  >❌ Out</button>
    <button class="${btnClass('maybe')}" onclick="vote(${rcIdx},'maybe')">🤔 Maybe</button>
  </div>
  ${waitNote}

  ${inHtml    ? `<div class="section-label">In <span class="count-badge">${rc.in_count}</span></div><ul class="people-list">${inHtml}</ul>` : ''}
  ${outHtml   ? `<div class="section-label">Out <span class="count-badge">${rc.out_count}</span></div><ul class="people-list">${outHtml}</ul>` : ''}
  ${maybeHtml ? `<div class="section-label">Maybe <span class="count-badge">${rc.maybe_count}</span></div><ul class="people-list">${maybeHtml}</ul>` : ''}
  ${waitHtml  ? `<div class="section-label">Waiting <span class="count-badge">${rc.wait_count}</span></div><ul class="people-list">${waitHtml}</ul>` : ''}
</div>`;
}

function renderTabs() {
  $rcTabs.innerHTML = state.rollcalls.map((rc, i) =>
    `<button class="tab${i === state.activeIdx ? ' active' : ''}" onclick="switchTab(${i})">${escHtml(rc.title || `#${i + 1}`)}</button>`
  ).join('');
}

function renderActive() {
  const rcs = state.rollcalls;
  if (!rcs.length) {
    $rcList.innerHTML = '';
    $emptyState.classList.remove('hidden');
    return;
  }
  $emptyState.classList.add('hidden');
  const rc = rcs[state.activeIdx] || rcs[0];
  $rcList.innerHTML = renderRollcall(rc, state.activeIdx);
}

function render() {
  if (state.rollcalls.length > 1) {
    renderTabs();
    $rcTabs.classList.remove('hidden');
  } else {
    $rcTabs.innerHTML = '';
  }
  renderActive();
}

// ── Public event handlers (called from inline onclick) ───────────────────────
window.switchTab = function(idx) {
  state.activeIdx = idx;
  renderTabs();
  renderActive();
};

window.vote = async function(rcIdx, voteType) {
  const rc = state.rollcalls[rcIdx];
  if (!rc) return;

  const commentInput = document.getElementById(`comment-${rcIdx}`);
  const comment = commentInput ? commentInput.value.trim() : '';

  // Optimistic UI: disable all vote buttons while request is in flight
  const card = document.getElementById(`rc-${rcIdx}`);
  card?.querySelectorAll('.btn').forEach(b => b.disabled = true);

  try {
    await castVote(rc.number, voteType, comment);
    tg.HapticFeedback?.impactOccurred('light');
    // Re-fetch and re-render this rollcall
    await loadRollcalls();
    render();
  } catch (err) {
    tg.showPopup?.({
      title: 'Vote failed',
      message: err.message || 'Please try again.',
      buttons: [{ type: 'ok' }],
    });
    // Re-enable on error
    card?.querySelectorAll('.btn').forEach(b => b.disabled = false);
  }
};

// ── Boot ─────────────────────────────────────────────────────────────────────
function showError(msg) {
  $loading.classList.add('hidden');
  $main.classList.add('hidden');
  $errorMsg.textContent = msg;
  $errorScreen.classList.remove('hidden');
}

function showMain() {
  $loading.classList.add('hidden');
  $errorScreen.classList.add('hidden');
  $main.classList.remove('hidden');
}

async function boot() {
  $loading.classList.remove('hidden');
  $main.classList.add('hidden');
  $errorScreen.classList.add('hidden');

  try {
    await auth();
    await loadRollcalls();

    // Set header title from chat info if available
    const chat = tg.initDataUnsafe?.chat;
    if (chat?.title) $chatTitle.textContent = chat.title;

    showMain();
    render();

    // Telegram WebApp back button — go back if user navigates to a tab
    tg.BackButton?.onClick(() => {
      if (state.activeIdx > 0) {
        window.switchTab(0);
        tg.BackButton.hide();
      }
    });

  } catch (err) {
    showError(err.message || 'Failed to load RollCall.');
  }
}

$retryBtn.addEventListener('click', boot);
boot();
