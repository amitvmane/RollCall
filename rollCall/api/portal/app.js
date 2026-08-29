(function(){
"use strict";

const API="/api/v1";
const LS_TG_USER_ID="rc_verified_tg_user_id";
const LS_TG_NAME="rc_verified_tg_name";
const LS_ID_TOKEN="rc_identity_token";
const LS_VERIFY_CODE="rc_verify_code";

let _userId=parseInt(localStorage.getItem(LS_TG_USER_ID))||null;
let _userName=localStorage.getItem(LS_TG_NAME)||null;
let _idToken=localStorage.getItem(LS_ID_TOKEN)||null;
let _pollTimer=null;
let _groups=[];
let _sortMode="active";
let _filterText="";

// ── Utilities ─────────────────────────────────────────────────────────────────

function $id(id){return document.getElementById(id);}
function esc(s){const d=document.createElement("div");d.textContent=s||"";return d.innerHTML;}

function toast(msg,dur=2800){
  const el=$id("toast");el.textContent=msg;el.classList.add("show");
  setTimeout(()=>el.classList.remove("show"),dur);
}

async function apiFetch(path,opts={}){
  const res=await fetch(API+path,{...opts,headers:{"Content-Type":"application/json",...(opts.headers||{})}});
  if(!res.ok){
    const err=await res.json().catch(()=>({}));
    throw new Error(err.detail||res.statusText);
  }
  return res.json();
}

function _updateThemeBtn(){
  const dark=document.documentElement.classList.contains("dark");
  document.querySelectorAll(".btn-theme").forEach(b=>b.textContent=dark?"☀️":"🌙");
}
window.toggleTheme=function(){
  const on=document.documentElement.classList.toggle("dark");
  localStorage.setItem("rc_dark",on?"1":"0");
  _updateThemeBtn();
};

// ── Verify flow ───────────────────────────────────────────────────────────────

async function startVerify(){
  const btn=$id("verify-btn");
  const statusEl=$id("verify-status");
  btn.disabled=true;btn.textContent="Starting…";
  statusEl.style.display="block";statusEl.textContent="";
  try{
    const res=await apiFetch("/auth/tg-verify/start",{method:"POST",body:"{}"});
    const code=res.code;
    localStorage.setItem(LS_VERIFY_CODE,code);
    window.open(res.deep_link,"_blank");
    statusEl.textContent="Telegram opened — tap Start in the bot, then return here.";
    btn.textContent="Waiting for verification…";
    _pollTimer=setInterval(()=>_checkVerify(code),2000);
  }catch(e){
    btn.disabled=false;btn.textContent="Verify with Telegram →";
    statusEl.textContent="Error: "+e.message;
  }
}

async function _checkVerify(code){
  try{
    const res=await apiFetch("/auth/tg-verify/status/"+code);
    if(res.verified){
      clearInterval(_pollTimer);_pollTimer=null;
      localStorage.removeItem(LS_VERIFY_CODE);
      _userId=res.user_id;_userName=res.name;_idToken=res.id_token||null;
      localStorage.setItem(LS_TG_USER_ID,String(_userId));
      localStorage.setItem(LS_TG_NAME,_userName);
      if(_idToken)localStorage.setItem(LS_ID_TOKEN,_idToken);
      showApp();
    }
  }catch(e){
    // 404 = not yet verified, keep polling
  }
}

(function resumeVerify(){
  const code=localStorage.getItem(LS_VERIFY_CODE);
  if(code&&!_userId){
    _pollTimer=setInterval(()=>_checkVerify(code),2000);
  }
})();

// ── Layout ────────────────────────────────────────────────────────────────────

function showVerifyScreen(){
  $id("verify-screen").style.display="";
  $id("app").style.display="none";
  $id("verify-btn").addEventListener("click",startVerify,{once:true});
  _loadLoginWidget();
}

// ── Telegram Login Widget (works without the Telegram app) ──────────────────

let _widgetLoaded=false;

async function _loadLoginWidget(){
  if(_widgetLoaded)return;
  try{
    const cfg=await apiFetch("/auth/tg-login/config");
    if(!cfg.bot_username)return;
    const s=document.createElement("script");
    s.async=true;
    s.src="https://telegram.org/js/telegram-widget.js?22";
    s.setAttribute("data-telegram-login",cfg.bot_username);
    s.setAttribute("data-size","large");
    s.setAttribute("data-onauth","onTelegramAuth(user)");
    s.setAttribute("data-request-access","write");
    $id("tg-login-widget").appendChild(s);
    $id("tg-widget-wrap").style.display="";
    _widgetLoaded=true;
  }catch(e){
    // Bot not connected yet or config endpoint unavailable — deep-link verify
    // still works, so just don't show the widget.
  }
}

window.onTelegramAuth=async function(user){
  try{
    const res=await apiFetch("/auth/tg-login",{method:"POST",body:JSON.stringify(user)});
    if(!res.verified)throw new Error("Verification failed");
    _adoptIdentity(res);
  }catch(e){
    toast("Login failed: "+e.message);
  }
};

// ── Personal login code (/mytoken — fully Telegram-independent) ──────────────

window.doMemberCodeLogin=async function(){
  const inp=$id("member-code-input");
  const code=(inp?.value||"").trim();
  if(!code){toast("Enter your login code (get one with /mytoken in Telegram).");return;}
  const btn=$id("member-code-btn");
  if(btn){btn.disabled=true;btn.textContent="…";}
  try{
    const res=await apiFetch("/auth/member-token",{method:"POST",body:JSON.stringify({token:code})});
    if(!res.verified)throw new Error("Verification failed");
    if(inp)inp.value="";
    _adoptIdentity(res);
  }catch(e){
    toast("Login failed: "+(e.message||"invalid code"));
  }finally{
    if(btn){btn.disabled=false;btn.textContent="Login";}
  }
};

function _adoptIdentity(res){
  if(_pollTimer){clearInterval(_pollTimer);_pollTimer=null;}
  localStorage.removeItem(LS_VERIFY_CODE);
  _userId=res.user_id;_userName=res.name||"";_idToken=res.id_token||null;
  localStorage.setItem(LS_TG_USER_ID,String(_userId));
  localStorage.setItem(LS_TG_NAME,_userName);
  if(_idToken)localStorage.setItem(LS_ID_TOKEN,_idToken);
  showApp();
}

function showApp(){
  $id("verify-screen").style.display="none";
  // /shared/base.css (which portal also loads, since portal builds on
  // its base styles) has a real stylesheet rule "#app{display:none}".
  // Clearing the inline style to "" only removes the INLINE override —
  // it doesn't beat that stylesheet rule, so #app stayed hidden after
  // login with zero errors anywhere (nothing to catch: this is a display
  // computation, not an exception). Must set an explicit value.
  $id("app").style.display="block";
  renderAcctControl();
  loadGroups();
}

// ── Account control ───────────────────────────────────────────────────────
// Mirrors the group page's chip-and-menu (markup + /shared/account.css) so
// identity looks and behaves the same on both surfaces. Handlers are bound
// here rather than inline because this file is an IIFE — nothing in it is
// global, so an onclick="…" in the HTML would not resolve.

function _portalSignOut(){
  if(!confirm("Sign out? You'll need to verify again to see your groups."))return;
  localStorage.removeItem(LS_TG_USER_ID);
  localStorage.removeItem(LS_TG_NAME);
  localStorage.removeItem(LS_ID_TOKEN);
  localStorage.removeItem(LS_VERIFY_CODE);
  _userId=null;_userName=null;_idToken=null;
  closeAcctMenu();
  $id("groups-list").innerHTML="";
  $id("summary-card").style.display="none";
  $id("upcoming-section").style.display="none";
  showVerifyScreen();
}

function renderAcctMenu(){
  const menu=$id("acct-menu");
  if(!menu)return;
  menu.innerHTML=
    (_userName?`<div class="acct-menu-hdr">
       <div class="acct-menu-name">${esc(_userName)}</div>
       <div class="acct-menu-sub">✅ Telegram verified</div>
     </div>`:"")+
    `<button class="acct-menu-item" role="menuitem" id="acct-groups-item">🗓 My groups</button>`+
    `<button class="acct-menu-item danger" role="menuitem" id="acct-signout-item">⏻ Sign out</button>`;
  const g=$id("acct-groups-item");if(g)g.addEventListener("click",()=>{closeAcctMenu();window.scrollTo({top:0,behavior:"smooth"});});
  const s=$id("acct-signout-item");if(s)s.addEventListener("click",_portalSignOut);
}

function openAcctMenu(){
  const menu=$id("acct-menu");
  if(!menu)return;
  renderAcctMenu();
  menu.classList.remove("hidden");
  const btn=$id("acct-btn");if(btn)btn.setAttribute("aria-expanded","true");
  if(!$id("acct-backdrop")){
    const bd=document.createElement("div");
    bd.id="acct-backdrop";bd.className="acct-backdrop";
    bd.addEventListener("click",closeAcctMenu);
    document.body.appendChild(bd);
  }
}

function closeAcctMenu(){
  const menu=$id("acct-menu");if(menu)menu.classList.add("hidden");
  const btn=$id("acct-btn");if(btn)btn.setAttribute("aria-expanded","false");
  const bd=$id("acct-backdrop");if(bd)bd.remove();
}

function renderAcctControl(){
  const av=$id("acct-av"),label=$id("acct-label");
  if(!av||!label)return;
  const who=_userName||"";
  av.textContent=(who[0]||"?").toUpperCase();
  label.textContent=who?(who.length>12?who.slice(0,11)+"…":who):"Signed in";
  const btn=$id("acct-btn");
  if(btn)btn.title=who?`Signed in as ${who}`:"Your account";
  const menu=$id("acct-menu");
  if(menu&&!menu.classList.contains("hidden"))renderAcctMenu();
}

$id("acct-btn").addEventListener("click",e=>{
  e.stopPropagation();
  const menu=$id("acct-menu");
  if(menu&&menu.classList.contains("hidden"))openAcctMenu();
  else closeAcctMenu();
});
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeAcctMenu();});

// ── Cross-group summary ───────────────────────────────────────────────────────

function renderSummary(groups){
  const card=$id("summary-card");
  if(!groups.length){card.style.display="none";return;}

  const totalGroups=groups.length;
  const totalAttended=groups.reduce((s,g)=>s+g.sessions_attended,0);
  const totalSessions=groups.reduce((s,g)=>s+g.total_sessions,0);
  const overallRate=totalSessions>0?Math.round(totalAttended/totalSessions*100):null;
  const bestStreak=groups.reduce((m,g)=>Math.max(m,g.best_streak),0);
  const liveCount=groups.filter(g=>g.has_active_rollcall).length;

  let html=`<div class="summary-grid">
    <div class="summary-item"><div class="summary-val">${totalGroups}</div><div class="summary-lbl">Groups</div></div>
    <div class="summary-item"><div class="summary-val">${totalAttended}</div><div class="summary-lbl">Sessions</div></div>
    <div class="summary-item"><div class="summary-val">${overallRate!=null?overallRate+"%":"—"}</div><div class="summary-lbl">Overall rate</div></div>
    <div class="summary-item"><div class="summary-val">${bestStreak}</div><div class="summary-lbl">Best streak</div></div>
  </div>`;
  if(liveCount>0){
    html+=`<div class="live-banner">● ${liveCount} group${liveCount>1?"s":""} with a live rollcall — scroll down to vote</div>`;
  }
  card.innerHTML=html;
  card.style.display="block";
}

// ── Upcoming scheduled rollcalls ──────────────────────────────────────────────

async function loadUpcoming(){
  const section=$id("upcoming-section");
  try{
    const data=await apiFetch("/portal/upcoming",{headers:{"X-Identity-Token":_idToken}});
    const items=data.items||[];
    if(!items.length){section.style.display="none";return;}
    const rows=items.map(item=>{
      const dt=new Date(item.scheduled_at.endsWith("Z")?item.scheduled_at:item.scheduled_at+"Z");
      const dateStr=dt.toLocaleDateString(undefined,{weekday:"short",month:"short",day:"numeric"});
      const timeStr=dt.toLocaleTimeString(undefined,{hour:"2-digit",minute:"2-digit"});
      const groupName=esc(item.group_name||("Chat "+item.chat_id));
      const titleStr=esc(item.title);
      return `<div class="upcoming-row">
  <div class="upcoming-icon">📅</div>
  <div class="upcoming-info">
    <div class="upcoming-title">${titleStr}</div>
    <div class="upcoming-meta">${groupName} · ${dateStr}, ${timeStr}</div>
  </div>
</div>`;
    }).join("");
    section.innerHTML=`<div class="section-label">UPCOMING</div>`+rows;
    section.style.display="block";
  }catch(e){
    section.style.display="none";
  }
}

// ── Groups ────────────────────────────────────────────────────────────────────

async function loadGroups(){
  $id("groups-loading").style.display="block";
  $id("groups-empty").style.display="none";
  $id("groups-list").innerHTML="";
  $id("summary-card").style.display="none";
  $id("upcoming-section").style.display="none";
  try{
    const [data] = await Promise.all([
      apiFetch("/portal/groups",{headers:{"X-Identity-Token":_idToken}}),
      loadUpcoming(),
    ]);
    _groups=data.groups||[];
    $id("groups-loading").style.display="none";
    if(!_groups.length){$id("groups-empty").style.display="block";return;}
    renderSummary(_groups);
    renderGroups();
  }catch(e){
    $id("groups-loading").textContent="Error loading groups: "+e.message;
  }
}

window.setSort=function(mode){
  _sortMode=mode;
  document.querySelectorAll(".sort-tab").forEach(t=>t.classList.remove("active"));
  const btn=$id("sort-"+mode);
  if(btn)btn.classList.add("active");
  renderGroups();
};

window.applyFilter=function(){
  _filterText=($id("group-search")?.value||"").trim().toLowerCase();
  renderGroups();
};

function renderGroups(){
  const fb=$id("filter-bar");
  if(fb)fb.style.display=_groups.length>=3?"":"none";

  let groups=[..._groups];
  if(_filterText)groups=groups.filter(g=>(g.group_name||"").toLowerCase().includes(_filterText));
  if(_sortMode==="active"){
    groups=groups.sort((a,b)=>{
      if(a.has_active_rollcall&&!b.has_active_rollcall)return -1;
      if(!a.has_active_rollcall&&b.has_active_rollcall)return 1;
      return 0;
    });
  }else{
    groups=groups.sort((a,b)=>(a.group_name||"").localeCompare(b.group_name||""));
  }
  const list=$id("groups-list");
  list.innerHTML=groups.map(g=>groupCardHTML(g,_groups.indexOf(g))).join("");
}

function groupCardHTML(g,i){
  const name=g.group_name||("Chat "+g.chat_id);
  const rate=g.attendance_rate!=null?g.attendance_rate.toFixed(0)+"%":"—";
  const badge=g.has_active_rollcall
    ?`<span class="group-badge badge-active">● Rollcall open</span>`
    :`<span class="group-badge badge-inactive">${g.total_sessions} sessions</span>`;

  // Always a direct, clickable link to the group's permanent page — not
  // just the "Vote Now" CTA that used to only appear while a rollcall was
  // active, leaving idle groups with no link at all.
  const voteBtn=g.group_web_token
    ?(g.has_active_rollcall
      ?`<a class="vote-btn" href="/web/group/${esc(g.group_web_token)}" target="_blank" onclick="event.stopPropagation()">Vote Now →</a>`
      :`<a class="vote-btn vote-btn-secondary" href="/web/group/${esc(g.group_web_token)}" target="_blank" onclick="event.stopPropagation()">Open group page →</a>`)
    :"";

  const rank=g.rank!=null?`#${g.rank}`:"—";
  const streak=g.current_streak||0;
  const best=g.best_streak||0;
  const streakDisplay=streak>=3?`${streak}🔥`:String(streak);

  return `
<div class="group-card" onclick="openDetail(${i})">
  <div class="group-card-header">
    <span class="group-name">${esc(name)}</span>
    ${badge}
  </div>
  <div class="group-stats">
    <div class="stat-box"><div class="stat-val">${esc(rate)}</div><div class="stat-lbl">Attendance</div></div>
    <div class="stat-box"><div class="stat-val">${esc(rank)}</div><div class="stat-lbl">Rank</div></div>
    <div class="stat-box"><div class="stat-val">${streakDisplay}</div><div class="stat-lbl">Streak</div></div>
    <div class="stat-box"><div class="stat-val">${best}</div><div class="stat-lbl">Best</div></div>
  </div>
  ${voteBtn}
</div>`;
}

// ── Detail panel ──────────────────────────────────────────────────────────────

window.openDetail=async function(idx){
  const g=_groups[idx];
  if(!g)return;
  const name=g.group_name||("Chat "+g.chat_id);
  $id("detail-title").textContent=name;
  $id("detail-body").innerHTML='<div style="color:var(--sub);padding:16px 0">Loading…</div>';
  $id("detail-overlay").classList.add("open");
  $id("detail-panel").classList.add("open");
  document.body.style.overflow="hidden";

  const rate=g.attendance_rate!=null?g.attendance_rate.toFixed(1)+"%":"—";
  const votingRate=g.voting_rate!=null?g.voting_rate.toFixed(0)+"%":"—";
  const rank=g.rank!=null?`#${g.rank}`:"—";
  const curStreak=g.current_streak||0;
  const bestStreak=g.best_streak||0;
  const streakDisplay=curStreak>=3?`${curStreak}🔥`:String(curStreak);

  let html=`
<div class="group-stats" style="margin-bottom:16px">
  <div class="stat-box"><div class="stat-val">${esc(rate)}</div><div class="stat-lbl">Attendance</div></div>
  <div class="stat-box"><div class="stat-val">${esc(rank)}</div><div class="stat-lbl">Rank</div></div>
  <div class="stat-box"><div class="stat-val">${streakDisplay}</div><div class="stat-lbl">Streak</div></div>
  <div class="stat-box"><div class="stat-val">${bestStreak}</div><div class="stat-lbl">Best</div></div>
</div>
${bestStreak>0?`<div class="milestone-box">🏆 Best streak ever: <strong>${bestStreak}</strong></div>`:""}
<div class="detail-meta-row">
  <span class="detail-meta-item">🗳 Voted in ${g.total_voted} of ${g.total_sessions} sessions (${esc(votingRate)})</span>
  ${g.ghost_count>0?`<span class="detail-meta-item ghost-flag">👻 ${g.ghost_count} ghost${g.ghost_count>1?"s":""}</span>`:""}
</div>`;

  if(g.has_active_rollcall&&g.group_web_token){
    html+=`<a class="vote-btn" href="/web/group/${esc(g.group_web_token)}" target="_blank" style="margin:12px 0;display:block">Vote Now →</a>`;
  }

  if(g.is_web_admin&&g.group_web_token){
    // The group page now covers everything the separate admin console
    // used to (voter management, templates, settings, stats) — and since
    // portal and the group page share the same origin, the identity token
    // already in localStorage carries over with no handoff needed; this
    // is just a plain link, unlike the old admin-console link which had
    // to pass the token through the URL fragment to mint a session there.
    html+=`<a class="vote-btn vote-btn-secondary" href="/web/group/${esc(g.group_web_token)}" style="margin:8px 0;display:block">⚙️ Manage this group →</a>`;
  }

  if(g.group_web_token){
    const joinUrl=window.location.origin+"/join/"+g.group_web_token;
    html+=`
<div class="join-link-box">
  <div style="font-weight:600;margin-bottom:4px;font-size:.82rem">📎 Permanent group link</div>
  <div style="display:flex;gap:8px;align-items:center">
    <code class="join-link-code">${esc(joinUrl)}</code>
    <button onclick="navigator.clipboard.writeText('${joinUrl.replace(/'/g,"\\'")}').then(()=>toast('Link copied!'))" class="copy-link-btn">Copy</button>
  </div>
</div>`;
  }

  const historyPane=`<div id="history-body"><div style="color:var(--sub)">Loading…</div></div>`;
  const duesPane=g.group_web_token
    ?`<div id="dues-section"><div style="color:var(--sub);font-size:.85rem;padding:24px 0;text-align:center">Loading…</div></div>`
    :`<div class="empty" style="text-align:center;padding:24px 0">Dues aren't set up for this group.</div>`;

  const tabs=["Overview","History","Dues"];
  html=`<div class="tab-bar detail-tabs" id="detail-tabs">
  ${tabs.map((t,i)=>`<button class="tab${i===0?" active":""}" onclick="switchDetailTab(${i})">${t}</button>`).join("")}
</div>
<div id="detail-pane-0">${html}</div>
<div id="detail-pane-1" style="display:none">${historyPane}</div>
<div id="detail-pane-2" style="display:none">${duesPane}</div>`;
  $id("detail-body").innerHTML=html;

  // Load history and dues in parallel
  const [,] = await Promise.allSettled([
    (async()=>{
      try{
        const data=await apiFetch(`/portal/groups/${g.chat_id}/history?limit=30`,{headers:{"X-Identity-Token":_idToken}});
        const sessions=data.sessions||[];
        if(!sessions.length){
          $id("history-body").innerHTML='<div style="color:var(--sub);font-size:.85rem">No sessions yet.</div>';
          return;
        }
        const spark=sessions.slice(0,20).reverse().map(s=>{
          const cls={in:"dot-in",out:"dot-out",maybe:"dot-maybe",miss:"dot-miss",cancelled:"dot-cancelled"}[s.status]||"dot-miss";
          return `<div class="spark-dot ${cls}" title="${esc(s.status==="cancelled"?"❌ Cancelled: "+(s.title||"Session"):s.title||"Session")}"></div>`;
        }).join("");
        const rows=sessions.map(s=>{
          const st=s.status||"miss";
          const cls={in:"dot-in",out:"dot-out",maybe:"dot-maybe",miss:"dot-miss",cancelled:"dot-cancelled"}[st]||"dot-miss";
          const scls="status-"+st;
          const dateStr=s.ended_at?s.ended_at.slice(0,10):"";
          return `<div class="session-row${st==="cancelled"?" session-cancelled":""}">
  <div class="session-dot ${cls}"></div>
  <div class="session-title">${esc(s.title||"Untitled")}</div>
  <div class="session-date">${esc(dateStr)}</div>
  <div class="session-status ${scls}">${st.toUpperCase()}</div>
</div>`;
        }).join("");
        $id("history-body").innerHTML=`<div class="sparkline" style="margin-bottom:12px">${spark}</div>`+rows;
      }catch(e){
        $id("history-body").innerHTML='<div style="color:var(--sub)">Could not load history.</div>';
      }
    })(),
    _loadDuesSection(g),
  ]);
};

window.switchDetailTab=function(idx){
  [0,1,2].forEach(i=>{
    const p=$id(`detail-pane-${i}`);if(p)p.style.display=i===idx?"block":"none";
  });
  document.querySelectorAll("#detail-tabs .tab").forEach((t,i)=>t.classList.toggle("active",i===idx));
};

const DUES_EMPTY_HTML='<div class="empty" style="text-align:center;padding:24px 0">Dues aren\'t set up for this group.</div>';

async function _loadDuesSection(g){
  const el=$id("dues-section");
  if(!el)return;
  if(!g.group_web_token||!_idToken){el.innerHTML=DUES_EMPTY_HTML;return;}

  // Try user's own balance (always allowed for verified members)
  let myDues=null;
  try{
    myDues=await apiFetch(`/web/group/${encodeURIComponent(g.group_web_token)}/dues/my`,{headers:{"X-Identity-Token":_idToken}});
  }catch(e){
    // 403 = dues not enabled, 404 = group not found
    el.innerHTML=DUES_EMPTY_HTML;
    return;
  }

  let html="";

  // Hero balance — the one number this tab exists to answer.
  const bal=myDues.balance||0;
  const upi=myDues.upi_vpa||null;
  const heroCls=bal>0?"dues-owed":bal<0?"dues-credit":"dues-settled";
  const heroLbl=bal>0?"You owe":bal<0?"You have a credit":"✅ You're all settled";
  html+=`<div class="dues-hero ${heroCls}">
  <div class="dues-hero-lbl">${heroLbl}</div>
  ${bal!==0?`<div class="dues-hero-val">₹${Math.abs(bal)}</div>`:""}
  ${(bal>0&&upi)?`<div class="dues-hero-upi">💳 Pay to <code class="dues-upi">${esc(upi)}</code>
    <button onclick="navigator.clipboard.writeText('${upi.replace(/'/g,"\\'")}').then(()=>toast('UPI copied!'))" class="copy-link-btn">Copy</button>
  </div>`:""}
</div>`;

  // Recent own entries (last 3) — secondary to the hero, own subhead.
  const entries=(myDues.entries||[]).slice(0,3);
  if(entries.length){
    const entryRows=entries.map(e=>{
      const amtCls=e.amount>0?"entry-debit":"entry-credit";
      const sign=e.amount>0?"+":"";
      const date=(e.created_at||"").slice(0,10);
      return `<div class="dues-entry-row">
  <span class="dues-entry-type">${esc(e.entry_type)}</span>
  <span class="dues-entry-date">${esc(date)}</span>
  <span class="${amtCls}">${sign}₹${Math.abs(e.amount)}</span>
</div>`;
    }).join("");
    html+=`<div class="dues-subhead">Your recent activity</div><div class="dues-entries-box">${entryRows}</div>`;
  }

  // Admin view: whole-group outstanding + fund, visually fenced off from the
  // member's own numbers above so the two are never mistaken for each other.
  try{
    const summary=await apiFetch(`/web/group/${encodeURIComponent(g.group_web_token)}/dues/summary?nonzero_only=true`,{headers:{"X-Identity-Token":_idToken}});
    const balances=summary.balances||[];
    const fundBal=summary.fund_balance||0;
    const memberRows=balances.length
      ?balances.map(b=>{
        const cls=b.balance>0?"entry-debit":"entry-credit";
        return `<div class="dues-entry-row">
  <span class="dues-entry-type">${esc(b.member_name)}</span>
  <span class="${cls}">₹${Math.abs(b.balance)}</span>
</div>`;
      }).join("")
      :`<div class="empty" style="padding:6px 0">Nobody owes anything right now.</div>`;
    html+=`<div class="dues-admin-card">
  <div class="dues-admin-eyebrow">👁 Admin view — whole group</div>
  <div class="dues-entries-box dues-entries-box--flush">${memberRows}</div>
  <div class="dues-fund-row">🏦 Fund balance: <strong>₹${fundBal}</strong></div>
</div>`;
  }catch(e){
    // Non-admin or dues not configured — fund balance still visible standalone
    try{
      const fund=await apiFetch(`/web/group/${encodeURIComponent(g.group_web_token)}/dues/fund`,{headers:{"X-Identity-Token":_idToken}});
      html+=`<div class="dues-fund-row" style="margin-top:12px">🏦 Fund balance: <strong>₹${fund.fund_balance||0}</strong></div>`;
    }catch(e2){
      // Fund endpoint failed — silently skip
    }
  }

  el.innerHTML=html||DUES_EMPTY_HTML;
}

window.closeDetail=function(){
  $id("detail-overlay").classList.remove("open");
  $id("detail-panel").classList.remove("open");
  document.body.style.overflow="";
};

document.addEventListener("keydown",e=>{if(e.key==="Escape")closeDetail();});

// ── Boot ──────────────────────────────────────────────────────────────────────

_updateThemeBtn();
if(_userId&&_idToken){
  showApp();
}else{
  showVerifyScreen();
}

})();
