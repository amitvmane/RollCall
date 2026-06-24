(function(){
"use strict";

const API="/api/v1";
const LS_TG_USER_ID="rc_verified_tg_user_id";
const LS_TG_NAME="rc_verified_tg_name";
const LS_VERIFY_CODE="rc_verify_code";

let _userId=parseInt(localStorage.getItem(LS_TG_USER_ID))||null;
let _userName=localStorage.getItem(LS_TG_NAME)||null;
let _pollTimer=null;
let _groups=[];

// ── Utilities ─────────────────────────────────────────────────────────────────

function $id(id){return document.getElementById(id);}
function esc(s){const d=document.createElement("div");d.textContent=s||"";return d.innerHTML;}

function toast(msg,dur=2800){
  const el=$id("toast");el.textContent=msg;el.classList.add("show");
  setTimeout(()=>el.classList.remove("show"),dur);
}

function toggleTheme(){
  const on=document.documentElement.classList.toggle("dark");
  localStorage.setItem("rc_dark",on?"1":"0");
}

async function apiFetch(path,opts={}){
  const res=await fetch(API+path,{...opts,headers:{"Content-Type":"application/json",...(opts.headers||{})}});
  if(!res.ok){
    const err=await res.json().catch(()=>({}));
    throw new Error(err.detail||res.statusText);
  }
  return res.json();
}

// ── Dark mode init ─────────────────────────────────────────────────────────────
// (applied via inline script in <head> before paint — just wire the button)
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
  const status=$id("verify-status");
  btn.disabled=true;btn.textContent="Starting…";
  status.style.display="block";status.textContent="";
  try{
    const res=await apiFetch("/auth/tg-verify/start",{method:"POST",body:"{}"});
    const code=res.code;
    localStorage.setItem(LS_VERIFY_CODE,code);
    // Open the Telegram deep link
    window.open(res.deep_link,"_blank");
    status.textContent="Telegram opened — tap Start in the bot, then return here.";
    btn.textContent="Waiting for verification…";
    _pollTimer=setInterval(()=>_checkVerify(code),2000);
  }catch(e){
    btn.disabled=false;btn.textContent="Verify with Telegram →";
    status.textContent="Error: "+e.message;
  }
}

async function _checkVerify(code){
  try{
    const res=await apiFetch("/auth/tg-verify/status/"+code);
    if(res.verified){
      clearInterval(_pollTimer);_pollTimer=null;
      localStorage.removeItem(LS_VERIFY_CODE);
      _userId=res.user_id;_userName=res.name;
      localStorage.setItem(LS_TG_USER_ID,String(_userId));
      localStorage.setItem(LS_TG_NAME,_userName);
      showApp();
    }
  }catch(e){
    // 404 = not yet verified, keep polling
  }
}

// Resume polling if a code was in-flight when the page loaded
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
  localStorage.removeItem(LS_VERIFY_CODE);
  _userId=null;_userName=null;
  $id("groups-list").innerHTML="";
  showVerifyScreen();
});

// ── Groups ────────────────────────────────────────────────────────────────────

async function loadGroups(){
  $id("groups-loading").style.display="block";
  $id("groups-empty").style.display="none";
  $id("groups-list").innerHTML="";
  try{
    const data=await apiFetch("/portal/groups?tg_user_id="+_userId);
    _groups=data.groups||[];
    $id("groups-loading").style.display="none";
    if(!_groups.length){$id("groups-empty").style.display="block";return;}
    renderGroups();
  }catch(e){
    $id("groups-loading").textContent="Error loading groups: "+e.message;
  }
}

function renderGroups(){
  const list=$id("groups-list");
  list.innerHTML=_groups.map((g,i)=>groupCardHTML(g,i)).join("");
}

function groupCardHTML(g,i){
  const name=g.group_name||("Chat "+g.chat_id);
  const rate=g.attendance_rate!=null?g.attendance_rate.toFixed(0)+"%":"—";
  const badge=g.has_active_rollcall
    ?`<span class="group-badge badge-active">● Rollcall open</span>`
    :`<span class="group-badge badge-inactive">${g.total_sessions} sessions</span>`;

  const voteBtn=g.has_active_rollcall&&g.group_web_token
    ?`<a class="vote-btn" href="/web/group/${esc(g.group_web_token)}" target="_blank">Vote Now →</a>`
    :"";

  const rank=g.rank!=null?`#${g.rank}`:"—";
  const streak=g.current_streak||0;
  const best=g.best_streak||0;

  return `
<div class="group-card" onclick="openDetail(${i})">
  <div class="group-card-header">
    <span class="group-name">${esc(name)}</span>
    ${badge}
  </div>
  <div class="group-stats">
    <div class="stat-box"><div class="stat-val">${esc(rate)}</div><div class="stat-lbl">Attendance</div></div>
    <div class="stat-box"><div class="stat-val">${esc(rank)}</div><div class="stat-lbl">Rank</div></div>
    <div class="stat-box"><div class="stat-val">${streak}</div><div class="stat-lbl">Streak</div></div>
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
  $id("detail-body").innerHTML='<div style="color:var(--sub);padding:16px 0">Loading history…</div>';
  $id("detail-overlay").classList.add("open");
  $id("detail-panel").classList.add("open");
  document.body.style.overflow="hidden";

  // Stats summary at top
  const rate=g.attendance_rate!=null?g.attendance_rate.toFixed(1)+"%":"—";
  const rank=g.rank!=null?`#${g.rank}`:"—";
  let html=`
<div class="group-stats" style="margin-bottom:16px">
  <div class="stat-box"><div class="stat-val">${esc(rate)}</div><div class="stat-lbl">Attendance</div></div>
  <div class="stat-box"><div class="stat-val">${esc(rank)}</div><div class="stat-lbl">Rank</div></div>
  <div class="stat-box"><div class="stat-val">${g.current_streak||0}</div><div class="stat-lbl">Streak</div></div>
  <div class="stat-box"><div class="stat-val">${g.best_streak||0}</div><div class="stat-lbl">Best</div></div>
</div>`;

  if(g.has_active_rollcall&&g.group_web_token){
    html+=`<a class="vote-btn" href="/web/group/${esc(g.group_web_token)}" target="_blank" style="margin-bottom:16px">Vote Now →</a>`;
  }

  // Shareable join link
  if(g.group_web_token){
    const joinUrl=window.location.origin+"/join/"+g.group_web_token;
    html+=`
<div style="background:var(--hover);border-radius:8px;padding:10px 12px;margin-bottom:16px;font-size:.82rem">
  <div style="font-weight:600;margin-bottom:4px">📎 Group invite link</div>
  <div style="display:flex;gap:8px;align-items:center">
    <code style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.78rem">${esc(joinUrl)}</code>
    <button onclick="navigator.clipboard.writeText('${joinUrl.replace(/'/g,"\\'")}').then(()=>toast('Link copied!'))" style="background:var(--accent);color:#fff;border:none;padding:4px 10px;border-radius:6px;cursor:pointer;font-size:.78rem;white-space:nowrap">Copy</button>
  </div>
</div>`;
  }

  html+=`<div style="font-weight:600;font-size:.88rem;margin-bottom:8px;color:var(--sub)">RECENT SESSIONS</div>`;
  $id("detail-body").innerHTML=html+'<div id="history-body"><div style="color:var(--sub)">Loading…</div></div>';

  try{
    const data=await apiFetch(`/portal/groups/${g.chat_id}/history?tg_user_id=${_userId}&limit=30`);
    const sessions=data.sessions||[];
    if(!sessions.length){
      $id("history-body").innerHTML='<div style="color:var(--sub);font-size:.85rem">No sessions yet.</div>';
      return;
    }
    // Sparkline (last 20, oldest first)
    const spark=sessions.slice(0,20).reverse().map(s=>{
      const cls={in:"dot-in",out:"dot-out",maybe:"dot-maybe",miss:"dot-miss"}[s.status]||"dot-miss";
      const lbl=s.title||"Session";
      return `<div class="spark-dot ${cls}" title="${esc(lbl)}"></div>`;
    }).join("");
    let rows=sessions.map(s=>{
      const cls={in:"dot-in",out:"dot-out",maybe:"dot-maybe",miss:"dot-miss"}[s.status]||"dot-miss";
      const scls="status-"+(s.status||"miss");
      const label=s.title||"Untitled";
      const dateStr=s.ended_at?s.ended_at.slice(0,10):"";
      return `<div class="session-row">
  <div class="session-dot ${cls}"></div>
  <div class="session-title">${esc(label)}</div>
  <div class="session-date">${esc(dateStr)}</div>
  <div class="session-status ${scls}">${(s.status||"miss").toUpperCase()}</div>
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

// Close on Escape
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeDetail();});

// ── Boot ──────────────────────────────────────────────────────────────────────

_updateThemeBtn();
if(_userId){
  showApp();
}else{
  showVerifyScreen();
}

})();
