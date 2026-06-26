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
}

function showApp(){
  $id("verify-screen").style.display="none";
  $id("app").style.display="";
  $id("portal-identity").textContent=_userName||"";
  loadGroups();
}

$id("unlink-btn").addEventListener("click",()=>{
  if(!confirm("Unlink your Telegram identity? You'll need to verify again to see your groups."))return;
  localStorage.removeItem(LS_TG_USER_ID);
  localStorage.removeItem(LS_TG_NAME);
  localStorage.removeItem(LS_ID_TOKEN);
  localStorage.removeItem(LS_VERIFY_CODE);
  _userId=null;_userName=null;_idToken=null;
  $id("groups-list").innerHTML="";
  $id("summary-card").style.display="none";
  $id("upcoming-section").style.display="none";
  showVerifyScreen();
});

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
    const data=await apiFetch("/portal/upcoming?id_token="+encodeURIComponent(_idToken));
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
      apiFetch("/portal/groups?id_token="+encodeURIComponent(_idToken)),
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

  const voteBtn=g.has_active_rollcall&&g.group_web_token
    ?`<a class="vote-btn" href="/web/group/${esc(g.group_web_token)}" target="_blank" onclick="event.stopPropagation()">Vote Now →</a>`
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

  html+=`<div class="section-label" style="margin-top:16px">RECENT SESSIONS</div>`;
  $id("detail-body").innerHTML=html+'<div id="history-body"><div style="color:var(--sub)">Loading…</div></div>';

  try{
    const data=await apiFetch(`/portal/groups/${g.chat_id}/history?id_token=${encodeURIComponent(_idToken)}&limit=30`);
    const sessions=data.sessions||[];
    if(!sessions.length){
      $id("history-body").innerHTML='<div style="color:var(--sub);font-size:.85rem">No sessions yet.</div>';
      return;
    }
    // Sparkline — oldest first, last 20
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
};

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
