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

  try{
    const res=await fetch("/api/v1/web/"+token+"/vote",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        name:currentName,vote:voteType,
        ...(TG_USER?.id?{tg_user_id:TG_USER.id}:{}),
        ...(comment?{comment}:{})
      })
    });
    if(!res.ok){const d=await res.json().catch(()=>({}));throw new Error(d.detail||"Vote failed");}
    const updated=await res.json();
    activeRcData=updated;
    if(IS_GROUP&&groupData)groupData.rollcalls[activeTabIdx]=updated;
    if($("comment-input"))$("comment-input").value="";
    detectCurrentVote();renderLists();renderCapBar(updated);
  }catch(err){toast(err.message||"Could not cast vote — try again.");}
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

async function loadGroup(){
  const res=await fetch(API_GROUP);
  if(!res.ok){const d=await res.json().catch(()=>({}));throw new Error(d.detail||"This group link is invalid.");}
  groupData=await res.json();
  const rcs=groupData.rollcalls;
  if(!rcs.length){
    ["rc-title","rc-meta","count-badge"].forEach(id=>{$(id)&&($(id).textContent="")});
    $("tab-card").classList.add("hidden");
    $("no-rollcalls").classList.remove("hidden");
    ["identity-card","vote-card","lists-card"].forEach(id=>$(id)?.classList.add("hidden"));
  }else if(rcs.length===1){$("tab-card").classList.add("hidden");renderRollcall(rcs[0]);}
  else{$("tab-card").classList.remove("hidden");if(activeTabIdx>=rcs.length)activeTabIdx=0;renderTabs(rcs);renderRollcall(rcs[activeTabIdx]);}
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
    }
  }catch{}
  scheduleRefresh();
}

function showError(msg){$("loading").classList.add("hidden");$("main").classList.add("hidden");$("error-msg").textContent=msg;$("error-screen").classList.remove("hidden");}
$("retry-btn").addEventListener("click",()=>{$("error-screen").classList.add("hidden");$("loading").classList.remove("hidden");activeTabIdx=0;load();});

if(URL_TOKEN&&(URL_MODE==="join"||URL_MODE==="group"))load();
})();
