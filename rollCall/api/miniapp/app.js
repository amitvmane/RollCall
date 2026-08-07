/* RollCall Mini App — vanilla JS, no build step */
'use strict';

// window.Telegram is injected by the telegram-web-app.js CDN script (see
// index.html) — it can be absent if that script hasn't finished loading yet,
// failed to load (network/ad-blocker), or the page was opened outside
// Telegram entirely. Falling through to null here (instead of crashing on
// window.Telegram.WebApp) lets auth()/boot()'s existing try/catch route this
// into the normal error screen instead of an unrecoverable blank "Loading…".
const tg = (window.Telegram && window.Telegram.WebApp) || null;
if (tg) { tg.ready(); tg.expand(); }

// ── State ────────────────────────────────────────────────────────────────────
const state = {
  token: null,
  chatId: null,
  userId: null,
  rollcalls: [],
  activeIdx: 0,   // which rollcall tab is shown
  // id_token + groupToken let the Mini App reuse the same id_token-gated
  // /web/group/{token}/... admin routes the group web page already uses
  // (e.g. settings) instead of needing a parallel bearer-scoped admin
  // surface — the vote-scoped bearer token above isn't enough for those.
  idToken: null,
  groupToken: null,
  isWebAdmin: false,
  timezone: 'Asia/Kolkata',
  // True only when Telegram actually told us which group this launch came
  // from (see MiniAppAuthResponse.chat_is_group server-side). The button
  // this app ships with today (a private-chat-only default menu button)
  // never gets that — so this is normally false, and the cross-group
  // picker below is the everyday path, not a fallback for an edge case.
  chatIsGroup: false,
  groups: [],      // GET /portal/groups results, when chatIsGroup is false
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
const $settingsBtn = $('settings-btn');
const $settingsPanel = $('settings-panel');
const $tzCurrent   = $('tz-current');
const $backBtn     = $('back-btn');
const $groupPicker = $('group-picker');
const $groupsEmpty = $('groups-empty');

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
  if (!tg) {
    throw new Error('Telegram Mini App API failed to load — open this page inside the Telegram app, or try again.');
  }
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
  state.idToken = data.id_token || null;
  state.groupToken = data.group_token || null;
  state.isWebAdmin = !!data.is_web_admin;
  state.timezone = data.timezone || 'Asia/Kolkata';
  state.chatIsGroup = !!data.chat_is_group;
}

// ── Data loading ─────────────────────────────────────────────────────────────
async function loadRollcalls() {
  const data = await apiFetch(`/chats/${state.chatId}/rollcalls`);
  // API returns a plain array of rollcall objects
  state.rollcalls = Array.isArray(data) ? data : (data.rollcalls || []);
}

// ── Cross-group picker ──────────────────────────────────────────────────────
// Used whenever the launch didn't come with real chat context (the normal
// case for the current entry point — see state.chatIsGroup above). Lists
// every group this Telegram user is known in (same source the portal's
// dashboard uses) so they can pick which one to vote in.
async function loadGroups() {
  if (!state.idToken) {
    throw new Error("Couldn't verify your identity — try reopening from Telegram.");
  }
  const data = await apiFetch('/portal/groups', { headers: { 'X-Identity-Token': state.idToken } });
  state.groups = data.groups || [];
}

async function switchToGroup(chatId) {
  const data = await apiFetch('/auth/telegram/miniapp/group', {
    method: 'POST',
    body: JSON.stringify({ id_token: state.idToken, chat_id: chatId }),
  });
  state.token = data.token;
  state.chatId = data.chat_id;
  state.idToken = data.id_token || state.idToken;
  state.groupToken = data.group_token || null;
  state.isWebAdmin = !!data.is_web_admin;
  state.timezone = data.timezone || 'Asia/Kolkata';
  state.activeIdx = 0;

  await loadRollcalls();
  const picked = state.groups.find(g => g.chat_id === chatId);
  $chatTitle.textContent = picked?.group_name || `Chat ${chatId}`;
  showGroupView();
  render();
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

// ── Cross-group picker rendering ────────────────────────────────────────────
function groupCardHtml(g) {
  const name = g.group_name || `Chat ${g.chat_id}`;
  const badge = g.has_active_rollcall
    ? `<span class="group-pick-badge badge-live">● Live</span>`
    : `<span class="group-pick-badge badge-idle">${g.total_sessions || 0} sessions</span>`;
  return `
<div class="group-pick-card" onclick="openGroup(${g.chat_id})">
  <div>
    <div class="group-pick-name">${escHtml(name)}</div>
    <div class="group-pick-meta">${g.attendance_rate != null ? g.attendance_rate.toFixed(0) + '% attendance' : 'No stats yet'}</div>
  </div>
  ${badge}
</div>`;
}

function renderPicker() {
  if (!state.groups.length) {
    $groupPicker.innerHTML = '';
    $groupPicker.classList.add('hidden');
    $groupsEmpty.classList.remove('hidden');
    return;
  }
  $groupsEmpty.classList.add('hidden');
  $groupPicker.classList.remove('hidden');
  // Groups with a live rollcall first — that's what someone opening the
  // app right now most likely wants to act on.
  const sorted = [...state.groups].sort((a, b) => {
    if (a.has_active_rollcall && !b.has_active_rollcall) return -1;
    if (!a.has_active_rollcall && b.has_active_rollcall) return 1;
    return (a.group_name || '').localeCompare(b.group_name || '');
  });
  $groupPicker.innerHTML = sorted.map(groupCardHtml).join('');
}

function showPickerView() {
  $chatTitle.textContent = 'Your groups';
  $backBtn.classList.add('hidden');
  $settingsBtn.classList.add('hidden');
  $settingsPanel.classList.add('hidden');
  $rcTabs.classList.add('hidden');
  $rcTabs.innerHTML = '';
  $rcList.innerHTML = '';
  $emptyState.classList.add('hidden');
  renderPicker();
}

function showGroupView() {
  $groupPicker.classList.add('hidden');
  $groupsEmpty.classList.add('hidden');
  // Only a picker-reached session can navigate back — a genuine group
  // launch (chat_is_group) never had a groups list to go back to.
  if (!state.chatIsGroup) $backBtn.classList.remove('hidden');
  if (state.isWebAdmin) $settingsBtn.classList.remove('hidden');
  if ($tzCurrent) $tzCurrent.textContent = state.timezone;
}

// ── Public event handlers (called from inline onclick) ───────────────────────
window.openGroup = async function(chatId) {
  $groupPicker.innerHTML = '<p class="hint" style="padding:16px 0">Opening…</p>';
  try {
    await switchToGroup(chatId);
  } catch (err) {
    tg.showPopup?.({
      title: "Couldn't open group",
      message: err.message || 'Please try again.',
      buttons: [{ type: 'ok' }],
    });
    renderPicker();
  }
};

window.backToGroups = async function() {
  showPickerView();
  try {
    await loadGroups();
    renderPicker();
  } catch (err) {
    $groupPicker.innerHTML = `<p class="hint" style="padding:16px 0">${escHtml(err.message || 'Failed to load groups.')}</p>`;
  }
};
window.switchTab = function(idx) {
  state.activeIdx = idx;
  renderTabs();
  renderActive();
};

window.toggleSettings = function() {
  $settingsPanel?.classList.toggle('hidden');
};

// Telegram's own dialog styling reads as native inside the Mini App —
// browser alert()/confirm() would look out of place. Falls back to the
// browser versions if showAlert/showConfirm aren't available (very old
// clients), same defensive spirit as the top-of-file `tg` null check.
function _tgAlert(msg) {
  if (tg && typeof tg.showAlert === 'function') tg.showAlert(msg);
  else alert(msg);
}
function _tgConfirm(msg) {
  return new Promise(resolve => {
    if (tg && typeof tg.showConfirm === 'function') tg.showConfirm(msg, ok => resolve(ok));
    else resolve(confirm(msg));
  });
}

window.detectTimezone = async function() {
  if (!state.idToken || !state.groupToken) {
    _tgAlert("Can't set timezone from here — try the group web page instead.");
    return;
  }
  let detected;
  try {
    detected = Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch (_) { detected = null; }
  if (!detected) { _tgAlert("Couldn't detect a timezone from this device."); return; }
  if (detected === state.timezone) { _tgAlert(`Already set to ${detected}.`); return; }
  const ok = await _tgConfirm(`Detected ${detected} on this device. Set this as the group's timezone? (Currently: ${state.timezone})`);
  if (!ok) return;
  try {
    const res = await fetch(`${window.location.origin}/api/v1/web/group/${state.groupToken}/settings`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id_token: state.idToken, timezone: detected }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || 'Failed to set timezone');
    }
    state.timezone = detected;
    if ($tzCurrent) $tzCurrent.textContent = detected;
    _tgAlert(`Timezone set to ${detected}`);
  } catch (e) {
    _tgAlert(e.message || 'Could not set timezone');
  }
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

    if (state.chatIsGroup) {
      // Real chat context from Telegram — go straight to this group's
      // rollcalls, same as before.
      await loadRollcalls();
      const chat = tg.initDataUnsafe?.chat;
      if (chat?.title) $chatTitle.textContent = chat.title;
      showMain();
      showGroupView();
      render();
    } else {
      // No reliable chat context (the normal case for today's private-chat
      // menu button) — show every group this user is known in and let
      // them pick one.
      await loadGroups();
      showMain();
      showPickerView();
    }

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
