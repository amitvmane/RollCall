(function(){
"use strict";

const parts=window.location.pathname.split("/").filter(Boolean);
const URL_MODE=parts[1], URL_TOKEN=parts[2];
const IS_GROUP=URL_MODE==="group";
const API_GROUP="/api/v1/web/group/"+URL_TOKEN;
const LS_NAME="rollcall_name";
const LS_NAME_OVERRIDE="rollcall_name_override";

if(!URL_TOKEN||(URL_MODE!=="join"&&URL_MODE!=="group")){
  $("loading").classList.add("hidden");
  showError("Invalid URL. Use the link shared in your group.");
}

// ── Telegram detection ─────────────────────────────────────────────────────
const tg=window.Telegram&&window.Telegram.WebApp;
let TG_USER=null;
if(tg&&tg.initDataUnsafe&&tg.initDataUnsafe.user){
  TG_USER=tg.initDataUnsafe.user;
  document.body.classList.add("tg-mode");
  tg.ready();tg.expand();
}
const TG_NAME=TG_USER?(TG_USER.first_name||(TG_USER.username?"@"+TG_USER.username:null))||null:null;

// ── State ──────────────────────────────────────────────────────────────────
// TG users can override display name; stored under a separate LS key so it
// doesn't bleed into guest sessions on the same device.
let currentName;
if(TG_NAME){
  currentName=localStorage.getItem(LS_NAME_OVERRIDE)||TG_NAME;
}else{
  currentName=localStorage.getItem(LS_NAME)||"";
}
let currentVote=null, activeRcData=null, groupData=null, activeTabIdx=0, voting=false;

// ── DOM ────────────────────────────────────────────────────────────────────
function $(x){return document.getElementById(x)}
function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}

// ── Theme toggle ───────────────────────────────────────────────────────────
function updateThemeBtn(){
  const btn=$("theme-btn");if(!btn)return;
  btn.textContent=document.documentElement.classList.contains("dark")?"☀":"🌙";
}
window.toggleTheme=function(){
  const isDark=document.documentElement.classList.contains("dark");
  document.documentElement.classList.toggle("dark",!isDark);
  localStorage.setItem("rc_dark",isDark?"0":"1");
  updateThemeBtn();
};
document.addEventListener("DOMContentLoaded",updateThemeBtn);

// ── Toast ──────────────────────────────────────────────────────────────────
function toast(msg,ms=2800){
  const el=$("toast-local");el.textContent=msg;el.classList.add("show");
  clearTimeout(el._t);el._t=setTimeout(()=>el.classList.remove("show"),ms);
}

// ── Copy link ──────────────────────────────────────────────────────────────
window.copyPageLink=function(){
  if(!IS_GROUP){
    toast("⚠ This link expires when the rollcall ends. Ask an admin for the permanent group link.",4000);
  }
  const url=window.location.href;
  if(navigator.clipboard){navigator.clipboard.writeText(url).then(()=>toast("Link copied! Share it with your group.")).catch(()=>toast(url,5000));}
  else{toast(url,5000);}
};

// ── Identity ───────────────────────────────────────────────────────────────
function renderIdentity(){
  if(currentName){
    $("name-input-row").classList.add("hidden");
    $("name-tag-row").classList.remove("hidden");
    const badge=$("id-badge");
    if(TG_NAME){
      const isOverride=currentName!==TG_NAME;
      badge.className="id-badge tg";
      badge.innerHTML=`✈ ${esc(currentName)} <span style="font-size:.72rem;font-weight:500;opacity:.75">${isOverride?"via Telegram ✎":"via Telegram"}</span>`;
    }else{
      badge.className="id-badge guest";
      badge.innerHTML=`👤 ${esc(currentName)}`;
    }
  }else{
    $("name-input-row").classList.remove("hidden");
    $("name-tag-row").classList.add("hidden");
  }
}

$("name-save-btn").addEventListener("click",saveName);
$("name-input").addEventListener("keydown",e=>{if(e.key==="Enter")saveName()});
$("name-change-btn").addEventListener("click",()=>{
  currentName="";
  if(TG_NAME)localStorage.removeItem(LS_NAME_OVERRIDE);
  else localStorage.removeItem(LS_NAME);
  $("name-input").value="";
  $("name-tag-row").classList.add("hidden");
  $("name-input-row").classList.remove("hidden");
  $("name-input").focus();
});

function saveName(){
  const val=$("name-input").value.trim();if(!val){$("name-input").focus();return;}
  currentName=val.slice(0,64);
  if(TG_NAME)localStorage.setItem(LS_NAME_OVERRIDE,currentName);
  else localStorage.setItem(LS_NAME,currentName);
  renderIdentity();detectCurrentVote();
  if(IS_GROUP)loadWebStats();
}

// ── Vote detection ─────────────────────────────────────────────────────────
function detectCurrentVote(){
  if(!activeRcData||!currentName){currentVote=null;renderVoteUI();return;}
  const n=currentName.toLowerCase();
  if(activeRcData.in.some(u=>u.name.toLowerCase()===n))currentVote="in";
  else if(activeRcData.out.some(u=>u.name.toLowerCase()===n))currentVote="out";
  else if(activeRcData.maybe.some(u=>u.name.toLowerCase()===n))currentVote="maybe";
  else currentVote=null;
  renderVoteUI();
}

const VOTE_ICONS={in:"✅",out:"❌",maybe:"🤔"};

function renderVoteUI(){
  const hasName=!!currentName&&!!activeRcData;
  const statusRow=$("vote-status-row");
  if(currentVote&&hasName){
    statusRow.innerHTML=`<div class="vote-status ${currentVote}">
      <span class="vs-label">${VOTE_ICONS[currentVote]} You're <strong>${currentVote.toUpperCase()}</strong></span>
      <span class="vs-change">Change vote ↓</span>
    </div>`;
    $("vote-hint").style.display="none";
  }else if(hasName){
    statusRow.innerHTML="";
    $("vote-hint").style.display="block";
  }else{
    statusRow.innerHTML="";
    $("vote-hint").style.display="none";
  }
  // Comment row: show once user has a name and there's an active rollcall
  const cr=$("comment-row");
  if(cr)cr.classList.toggle("hidden",!hasName);
  ["btn-in","btn-out","btn-maybe"].forEach(id=>{
    const btn=$(id);
    btn.disabled=!hasName||voting;
    btn.classList.toggle("active",!voting&&currentVote===btn.dataset.vote);
  });
}

// ── Vote ───────────────────────────────────────────────────────────────────
let _spinBtn=null;

async function castVote(voteType){
  if(!currentName||voting||!activeRcData)return;
  const token=activeRcData.web_token;
  if(!token){toast("This rollcall can't be voted on via web.");return;}
  const comment=($("comment-input")?.value||"").trim()||null;

  voting=true;
  // Show spinner on the tapped button
  _spinBtn=$({"in":"btn-in","out":"btn-out","maybe":"btn-maybe"}[voteType]);
  if(_spinBtn)_spinBtn.classList.add("spinning");
  renderVoteUI();

  const ac=new AbortController();
  const _tid=setTimeout(()=>ac.abort(),30000);
  try{
    const res=await fetch("/api/v1/web/"+token+"/vote",{
      method:"POST",signal:ac.signal,
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        name:currentName,vote:voteType,
        ...(TG_USER?.id?{tg_user_id:TG_USER.id}:{}),
        ...(comment?{comment}:{})
      })
    });
    clearTimeout(_tid);
    if(!res.ok){
      const d=await res.json().catch(()=>({}));
      const msg=d.detail||"Vote failed";
      if(res.status===404){showError("This rollcall has ended.");return;}
      throw new Error(msg);
    }
    const updated=await res.json();
    activeRcData=updated;
    if(IS_GROUP&&groupData)groupData.rollcalls[activeTabIdx]=updated;
    if($("comment-input"))$("comment-input").value="";
    detectCurrentVote();renderLists();renderCapBar(updated);
  }catch(err){
    clearTimeout(_tid);
    if(err.name==="AbortError"){toast("Vote timed out — server is busy. Try again.",4000);return;}
    toast(err.message||"Could not cast vote — try again.");
  }
  finally{
    voting=false;
    if(_spinBtn)_spinBtn.classList.remove("spinning");
    _spinBtn=null;
    renderVoteUI();
  }
}
$("btn-in").addEventListener("click",()=>castVote("in"));
$("btn-out").addEventListener("click",()=>castVote("out"));
$("btn-maybe").addEventListener("click",()=>castVote("maybe"));

// ── Avatar ─────────────────────────────────────────────────────────────────
const AV_COLORS=["#4f46e5","#0891b2","#16a34a","#d97706","#7c3aed","#0284c7","#059669","#b45309"];
function avColor(name){let h=0;for(const c of String(name))h=(h*31+c.charCodeAt(0))>>>0;return AV_COLORS[h%AV_COLORS.length];}

// ── Countdown ──────────────────────────────────────────────────────────────
function formatCountdown(epoch){
  if(!epoch)return null;
  const diff=Math.floor(epoch*1000-Date.now());
  if(diff<=0)return null;
  const h=Math.floor(diff/3600000);
  const m=Math.floor((diff%3600000)/60000);
  if(h>72)return`in ${Math.floor(h/24)}d`;
  if(h>=1)return`in ${h}h ${m}m`;
  return`in ${m}m`;
}

// ── Render rollcall ────────────────────────────────────────────────────────
function renderCapBar(rc){
  const inCount=rc.in.length;
  if(rc.limit){
    $("cap-row").classList.remove("hidden");
    const pct=Math.min(100,Math.round(inCount/rc.limit*100));
    $("cap-fill").style.width=pct+"%";
    const rem=rc.limit-inCount;
    $("cap-text").textContent=rem>0?`${inCount}/${rc.limit} — ${rem} spot${rem===1?"":"s"} left`:`${inCount}/${rc.limit} — Full`;
    $("cap-fill").style.background=rem<=0?"var(--maybe)":"var(--in)";
  }else{$("cap-row").classList.add("hidden");}
}

function renderRollcall(rc){
  activeRcData=rc;
  const totalRc=IS_GROUP&&groupData?groupData.rollcalls.length:1;
  $("rc-title").textContent=totalRc>1?`#${activeTabIdx+1} · ${rc.title}`:rc.title;
  const meta=[];
  if(rc.finalize_date){
    const cd=formatCountdown(rc.finalize_epoch);
    const cdHtml=cd?`<span class="cd-pill${cd.includes("m")&&!cd.includes("h")?" soon":""}">${esc(cd)}</span>`:"";
    meta.push("🕐 Closes: "+esc(rc.finalize_date)+(cdHtml?" "+cdHtml:""));
  }
  if(rc.location)meta.push("📍 "+esc(rc.location));
  $("rc-meta").innerHTML=meta.map(m=>`<span>${m}</span>`).join("<br/>");
  $("count-badge").textContent=rc.limit?rc.in.length+"/"+rc.limit+" IN":rc.in.length+" IN";

  // Label copy button for join mode
  if(!IS_GROUP){
    const cb=document.querySelector(".copy-btn");
    if(cb)cb.innerHTML='⚠ Link expires with rollcall';
  }

  renderCapBar(rc);
  $("no-rollcalls").classList.add("hidden");
  $("identity-card").classList.remove("hidden");
  $("vote-card").classList.remove("hidden");
  $("lists-card").classList.remove("hidden");
  detectCurrentVote();renderLists();
}

function renderLists(){
  if(!activeRcData)return;
  const{in:inL,out:outL,maybe:maybeL,waiting:waitL}=activeRcData;
  function section(label,cls,items){
    const rows=items.length?items.map((u,i)=>{
      const isYou=currentName&&u.name.toLowerCase()===currentName.toLowerCase();
      const av=`<span class="av" style="background:${avColor(u.name)}">${(u.name[0]||"?").toUpperCase()}</span>`;
      const cm=u.comment?`<span class="li-comment">— ${esc(u.comment)}</span>`:"";
      const tgDot=u.is_proxy===false?'<span class="tg-dot" title="Telegram user"></span>':"";
      return `<li class="${isYou?"you":""}">
        <span class="li-pos">${i+1}</span>${av}
        <span class="li-name">${esc(u.name)}${tgDot}</span>${cm}
      </li>`;
    }).join(""):"";
    return`<div class="list-sect">
      <div class="list-lbl ${cls}">${label}<span class="list-cnt">(${items.length})</span></div>
      ${items.length?`<ul class="list-items">${rows}</ul>`:'<p class="empty" style="margin:0;padding:2px 0">—</p>'}
    </div>`;
  }
  const html=section("IN","in",inL)+section("OUT","out",outL)+section("MAYBE","maybe",maybeL)+(waitL.length?section("WAIT","wait",waitL):"");
  $("lists-container").innerHTML=html||'<p class="empty">No votes yet.</p>';
}

// ── Tabs ───────────────────────────────────────────────────────────────────
function renderTabs(rcs){
  $("tab-bar").innerHTML="";
  rcs.forEach((rc,idx)=>{
    const btn=document.createElement("button");
    btn.className="tab-btn"+(idx===activeTabIdx?" active":"");
    btn.innerHTML=`<span class="tn">#${idx+1}</span><span class="tt">${esc(rc.title)}</span>`;
    btn.addEventListener("click",()=>switchTab(idx));
    $("tab-bar").appendChild(btn);
  });
}
function switchTab(idx){
  activeTabIdx=idx;
  if(!groupData)return;
  renderTabs(groupData.rollcalls);
  renderRollcall(groupData.rollcalls[idx]);
}

// ── Load ───────────────────────────────────────────────────────────────────
async function load(){
  try{IS_GROUP?await loadGroup():await loadJoin();}
  catch(e){showError(e.message||"Could not connect. Check your internet and tap Retry.");return;}
  $("loading").classList.add("hidden");
  $("main").classList.remove("hidden");
  renderIdentity();scheduleRefresh();
}

async function loadJoin(){
  const res=await fetch("/api/v1/web/"+URL_TOKEN);
  if(!res.ok){const d=await res.json().catch(()=>({}));throw new Error(d.detail||"This link is invalid or has ended.");}
  $("tab-card").classList.add("hidden");
  renderRollcall(await res.json());
}

// ── Upcoming scheduled rollcalls ───────────────────────────────────────────
const DAYS=["sunday","monday","tuesday","wednesday","thursday","friday","saturday"];
function nextScheduledDate(schedDay,schedTime){
  const tgt=DAYS.indexOf((schedDay||"").toLowerCase());
  if(tgt<0||!schedTime)return null;
  const[h,m]=(schedTime||"00:00").split(":").map(Number);
  const now=new Date();
  let diff=(tgt-now.getDay()+7)%7;
  if(diff===0&&(now.getHours()*60+now.getMinutes())>=h*60+m)diff=7;
  const d=new Date(now);
  d.setDate(now.getDate()+diff);d.setHours(h,m,0,0);
  return d;
}
function renderUpcoming(upcoming){
  const el=$("upcoming-card");
  if(!el)return;
  const thisWeek=(upcoming||[]).filter(u=>{
    const d=nextScheduledDate(u.schedule_day,u.schedule_time);
    return d&&(d-new Date())<=7*24*60*60*1000;
  }).sort((a,b)=>{
    const da=nextScheduledDate(a.schedule_day,a.schedule_time);
    const db=nextScheduledDate(b.schedule_day,b.schedule_time);
    return (da||0)-(db||0);
  });
  if(!thisWeek.length){el.classList.add("hidden");return;}
  el.classList.remove("hidden");
  el.innerHTML=`<div class="upcoming-header">📅 Coming Up This Week</div>`
    +thisWeek.map(u=>{
      const d=nextScheduledDate(u.schedule_day,u.schedule_time);
      const dateStr=d?d.toLocaleDateString(undefined,{weekday:"short",month:"short",day:"numeric"}):"";
      const timeStr=d?d.toLocaleTimeString(undefined,{hour:"2-digit",minute:"2-digit"}):"";
      const title=u.title||u.name;
      const meta=[u.location?`📍 ${u.location}`:"",u.fee?`💰 ${u.fee}`:"",u.limit?`👥 Cap: ${u.limit}`:""].filter(Boolean).join(" · ");
      return `<div class="upcoming-row">
        <div class="upcoming-when"><span class="upcoming-day">${dateStr}</span><span class="upcoming-time">${timeStr}</span></div>
        <div class="upcoming-info"><div class="upcoming-title">${title}</div>${meta?`<div class="upcoming-meta">${meta}</div>`:""}</div>
      </div>`;
    }).join("");
}
async function loadGroup(){
  const res=await fetch(API_GROUP);
  if(!res.ok){const d=await res.json().catch(()=>({}));throw new Error(d.detail||"This group link is invalid.");}
  groupData=await res.json();
  const rcs=groupData.rollcalls;
  renderUpcoming(groupData.upcoming||[]);
  if(!rcs.length){
    ["rc-title","rc-meta","count-badge"].forEach(id=>{$(id)&&($(id).textContent="")});
    $("tab-card").classList.add("hidden");
    $("no-rollcalls").classList.remove("hidden");
    ["identity-card","vote-card","lists-card"].forEach(id=>$(id)?.classList.add("hidden"));
  }else if(rcs.length===1){$("tab-card").classList.add("hidden");renderRollcall(rcs[0]);}
  else{$("tab-card").classList.remove("hidden");if(activeTabIdx>=rcs.length)activeTabIdx=0;renderTabs(rcs);renderRollcall(rcs[activeTabIdx]);}
  loadWebStats();
}

// ── Auto-refresh ───────────────────────────────────────────────────────────
let _refreshTimer=null;
function scheduleRefresh(){
  if(_refreshTimer)clearTimeout(_refreshTimer);
  const fill=$("refresh-fill");
  if(fill){fill.style.transition="none";fill.style.width="100%";requestAnimationFrame(()=>requestAnimationFrame(()=>{fill.style.transition="width 30s linear";fill.style.width="0%";}))}
  _refreshTimer=setTimeout(silentRefresh,30000);
}

function showRefreshLabel(text){
  const el=$("refresh-label");
  if(!el)return;
  el.textContent=text;el.classList.add("show");
  clearTimeout(el._t);el._t=setTimeout(()=>el.classList.remove("show"),2000);
}

async function silentRefresh(){
  showRefreshLabel("• syncing");
  try{
    if(IS_GROUP){
      const res=await fetch(API_GROUP);
      if(res.ok){
        groupData=await res.json();const rcs=groupData.rollcalls;
        renderUpcoming(groupData.upcoming||[]);
        if(!rcs.length){
          $("tab-card").classList.add("hidden");$("no-rollcalls").classList.remove("hidden");
          ["identity-card","vote-card","lists-card"].forEach(id=>$(id)?.classList.add("hidden"));
        }else{
          $("no-rollcalls").classList.add("hidden");
          if(rcs.length>1){$("tab-card").classList.remove("hidden");renderTabs(rcs);}
          else $("tab-card").classList.add("hidden");
          if(activeTabIdx>=rcs.length)activeTabIdx=0;
          activeRcData=rcs[activeTabIdx];
          const _rc=activeRcData;
          $("rc-title").textContent=rcs.length>1?`#${activeTabIdx+1} · ${_rc.title}`:_rc.title;
          const _m=[];
          if(_rc.finalize_date){const _cd=formatCountdown(_rc.finalize_epoch);const _cdH=_cd?`<span class="cd-pill${_cd.includes("m")&&!_cd.includes("h")?" soon":""}">${esc(_cd)}</span>`:"";_m.push("🕐 Closes: "+esc(_rc.finalize_date)+(_cdH?" "+_cdH:""));}
          if(_rc.location)_m.push("📍 "+esc(_rc.location));
          $("rc-meta").innerHTML=_m.map(x=>`<span>${x}</span>`).join("<br/>");
          detectCurrentVote();renderLists();renderCapBar(activeRcData);
          $("count-badge").textContent=activeRcData.limit?activeRcData.in.length+"/"+activeRcData.limit+" IN":activeRcData.in.length+" IN";
        }
      }
    }else{
      const res=await fetch("/api/v1/web/"+URL_TOKEN);
      if(res.ok){activeRcData=await res.json();detectCurrentVote();renderLists();renderCapBar(activeRcData);}
      else if(res.status===404||res.status===422){
        const d=await res.json().catch(()=>({}));
        showError(d.detail||"This rollcall has ended.");
        return;
      }
    }
  }catch{}
  scheduleRefresh();
}

function showError(msg){$("loading").classList.add("hidden");$("main").classList.add("hidden");$("error-msg").textContent=msg;$("error-screen").classList.remove("hidden");}
$("retry-btn").addEventListener("click",()=>{$("error-screen").classList.add("hidden");$("loading").classList.remove("hidden");activeTabIdx=0;load();});

// ── Stats ──────────────────────────────────────────────────────────────────
const TG_USER_ID=TG_USER?.id||null;

async function loadWebStats(){
  const sc=$("stats-card");if(!sc)return;
  const params=new URLSearchParams();
  if(TG_USER_ID)params.set("user_id",TG_USER_ID);
  else if(currentName)params.set("name",currentName);
  const url=`/api/v1/web/group/${URL_TOKEN}/stats${params.size?"?"+params:""}`;
  try{
    const res=await fetch(url,{signal:AbortSignal.timeout(8000)});
    if(!res.ok)return;
    const data=await res.json();
    renderStats(data);
    sc.classList.remove("hidden");
  }catch(_){}
}

function renderStats(d){
  const sc=$("stats-card");if(!sc)return;
  const pct=v=>v==null?"—":`${v}%`;
  const n=v=>v??0;
  const me=d.personal;

  let personalHtml="";
  if(me){
    const rankStr=me.rank&&d.total_participants?`#${me.rank} of ${d.total_participants}`:"";
    const attRate=pct(me.attendance_rate);
    const attW=Math.min(100,me.attendance_rate||0);
    personalHtml=`
    <div class="sp-personal">
      <div class="sp-you-header">
        <span class="sp-you-label">👤 You</span>
        ${rankStr?`<span class="sp-rank">${rankStr}</span>`:""}
      </div>
      <div class="sp-mini-stats">
        <div class="sp-mini"><div class="sp-mini-val">${n(me.sessions_attended)}</div><div class="sp-mini-lbl">Sessions</div></div>
        <div class="sp-mini"><div class="sp-mini-val">${attRate}</div><div class="sp-mini-lbl">Attended</div></div>
        <div class="sp-mini"><div class="sp-mini-val">${n(me.current_streak)}</div><div class="sp-mini-lbl">Streak</div></div>
        <div class="sp-mini"><div class="sp-mini-val">${n(me.best_streak)}</div><div class="sp-mini-lbl">Best</div></div>
      </div>
      <div class="sp-bar-row">
        <div class="sp-bar"><div class="sp-bar-fill" style="width:${attW}%"></div></div>
        <span class="sp-bar-lbl">${attRate} attendance</span>
      </div>
      <div class="sp-vote-row">
        <span class="sp-pill sp-in">✅ ${n(me.total_in_votes)} IN</span>
        <span class="sp-pill sp-out">❌ ${n(me.total_out_votes)} OUT</span>
        <span class="sp-pill sp-maybe">🤔 ${n(me.total_maybe_votes)} MAYBE</span>
        ${me.ghost_count?`<span class="sp-pill sp-ghost">👻 ${me.ghost_count} ghost</span>`:""}
      </div>
    </div>`;
  }

  const lbRows=(d.leaderboard||[]).map((e,i)=>{
    const isMe=me&&(
      (TG_USER_ID&&e.user_id===TG_USER_ID)||
      (currentName&&e.display_name&&e.display_name.toLowerCase()===currentName.toLowerCase())
    );
    const w=Math.min(100,e.attendance_rate||0);
    return `<div class="slb-row${isMe?" slb-you":""}${e.kind==="proxy"?" slb-proxy":""}">
      <span class="slb-rank">#${e.rank??i+1}</span>
      <span class="slb-name">${esc(e.display_name||"—")}${isMe?" ← you":""}</span>
      <div class="slb-bar-wrap"><div class="slb-bar"><div class="slb-fill" style="width:${w}%"></div></div></div>
      <span class="slb-pct">${pct(e.attendance_rate)}</span>
    </div>`;
  }).join("");

  sc.innerHTML=`
  <div class="stats-section-hdr">📊 Group Stats</div>
  <div class="sp-group-row">
    <div class="sp-g"><div class="sp-g-val">${n(d.total_rollcalls)}</div><div class="sp-g-lbl">Sessions</div></div>
    <div class="sp-g"><div class="sp-g-val">${n(d.avg_attendance)}</div><div class="sp-g-lbl">Avg Attendance</div></div>
    <div class="sp-g"><div class="sp-g-val">${n(d.total_participants)}</div><div class="sp-g-lbl">Members</div></div>
  </div>
  ${personalHtml}
  ${lbRows?`<div class="stats-section-hdr">🏆 Leaderboard</div><div class="slb-list">${lbRows}</div>`:""}`;
}

// ── Telegram connectivity banner ───────────────────────────────────────────
(async function checkTgStatus(){
  if(!IS_GROUP)return;
  try{
    const r=await fetch("/api/v1/health",{signal:AbortSignal.timeout(4000)});
    if(!r.ok)return;
    const d=await r.json();
    if(d.telegram_ok===false){
      const bar=$("tg-offline-bar");
      if(bar)bar.classList.remove("hidden");
    }
  }catch(_){}
})();

if(URL_TOKEN&&(URL_MODE==="join"||URL_MODE==="group"))load();
})();
