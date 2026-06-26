(function(){
"use strict";

const parts=window.location.pathname.split("/").filter(Boolean);
const URL_MODE=parts[1], URL_TOKEN=parts[2];
const IS_GROUP=URL_MODE==="group";
const API_GROUP="/api/v1/web/group/"+URL_TOKEN;
const LS_NAME="rollcall_name";
const LS_NAME_OVERRIDE="rollcall_name_override";
const LS_TG_USER_ID="rc_verified_tg_user_id";
const LS_TG_NAME="rc_verified_tg_name";
const LS_TG_USERNAME="rc_verified_tg_username";
const LS_ID_TOKEN="rc_identity_token";

// Verified Telegram identity from deep-link verification (persists across sessions)
let _verifiedUserId=parseInt(localStorage.getItem(LS_TG_USER_ID))||null;
let _verifiedName=localStorage.getItem(LS_TG_NAME)||null;
// For Mini App sessions TG_USER.username is already set from the SDK; for
// tg-verify sessions it comes back from the status endpoint and is stored.
let _verifiedUsername=localStorage.getItem(LS_TG_USERNAME)||null;
// Signed proof of identity (from tg-verify or Mini App auth). Presented to the
// server in place of a raw, forgeable user id on identity-sensitive calls.
let _idToken=localStorage.getItem(LS_ID_TOKEN)||null;

// Migration: users who verified before id_tokens existed have a remembered
// user id but no signed token, leaving them unable to attribute votes or use
// admin actions and with no visible way to re-verify. Drop the stale verified
// flag (keeping their name) so the "Verify with Telegram" CTA reappears. TG
// (Mini App) users mint a fresh token via _miniappAuth and are unaffected.
if(_verifiedUserId&&!_idToken){
  _verifiedUserId=null;
  localStorage.removeItem(LS_TG_USER_ID);
}

// Only show "invalid URL" when a token IS present but the mode is wrong (corrupted link).
// No token = home screen, handled at the bottom of the file.
if(URL_TOKEN&&(URL_MODE!=="join"&&URL_MODE!=="group")){
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

// ── Mini App session token (HMAC-verified identity) ────────────────────────
const MA_TOKEN_KEY="rc_ma_token";
let _maToken=sessionStorage.getItem(MA_TOKEN_KEY);

async function _miniappAuth(){
  if(!tg||!tg.initData)return;
  try{
    const r=await fetch("/api/v1/auth/telegram/miniapp",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({init_data:tg.initData}),
      signal:AbortSignal.timeout(8000),
    });
    if(!r.ok)return;
    const d=await r.json();
    _maToken=d.token;
    sessionStorage.setItem(MA_TOKEN_KEY,_maToken);
    if(d.id_token){_idToken=d.id_token;localStorage.setItem(LS_ID_TOKEN,_idToken);}
    renderIdentity();
  }catch(_){}
}

// ── State ──────────────────────────────────────────────────────────────────
// TG users can override display name; stored under a separate LS key so it
// doesn't bleed into guest sessions on the same device.
let currentName;
if(TG_NAME){
  currentName=localStorage.getItem(LS_NAME_OVERRIDE)||TG_NAME;
}else{
  currentName=localStorage.getItem(LS_NAME)||_verifiedName||"";
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
      const label=isOverride?"via Telegram ✎":_maToken?"✅ Verified":"via Telegram";
      badge.innerHTML=`✈ ${esc(currentName)} <span style="font-size:.72rem;font-weight:500;opacity:.75">${label}</span>`;
    }else{
      badge.className="id-badge guest";
      if(_verifiedUserId){
        badge.innerHTML=`✅ ${esc(currentName)} <span style="font-size:.72rem;font-weight:500;opacity:.75">Telegram verified</span>`;
      }else{
        badge.innerHTML=`👤 ${esc(currentName)}`;
      }
    }
    // Style change button: lock icon + muted when identity is Telegram-verified
    const changeBtn=$("name-change-btn");
    if(changeBtn){
      if(_verifiedUserId||(TG_NAME&&_idToken)){
        changeBtn.textContent="🔒 Locked";
        changeBtn.style.opacity="0.55";
        changeBtn.title="Your name is locked to your Telegram identity. Click to unlink.";
      }else{
        changeBtn.textContent="✎ Change";
        changeBtn.style.opacity="";
        changeBtn.title="Change name";
      }
    }
    // Show "Verify with Telegram" only for non-TG, non-verified users in group mode
    const actions=document.querySelector(".id-inner .id-actions");
    if(actions){
      let vBtn=document.getElementById("verify-tg-btn");
      const needsVerify=IS_GROUP&&!TG_NAME&&!_verifiedUserId;
      if(needsVerify&&!vBtn){
        vBtn=document.createElement("button");
        vBtn.id="verify-tg-btn";
        vBtn.className="id-change";
        vBtn.style.color="var(--tg-theme-link-color,#2563eb)";
        vBtn.title="Link your Telegram identity to this browser";
        vBtn.textContent="🔗 Verify with Telegram";
        vBtn.onclick=()=>startTgVerify();
        actions.appendChild(vBtn);
      }else if(!needsVerify&&vBtn){
        vBtn.remove();
      }
    }
  }else{
    $("name-input-row").classList.remove("hidden");
    $("name-tag-row").classList.add("hidden");
  }
}

$("name-save-btn").addEventListener("click",saveName);
$("name-input").addEventListener("keydown",e=>{if(e.key==="Enter")saveName()});
$("name-change-btn").addEventListener("click",()=>{
  if(TG_NAME&&_idToken){
    // Inside Telegram Mini App: name is set by Telegram and cannot be changed
    // while the user is authenticated. There's no local override possible.
    toast("Your name is set by Telegram and cannot be changed here.",3500);
    return;
  }
  if(_verifiedUserId){
    const ok=confirm("Changing your name will unlink your Telegram verification.\nYou can re-verify after setting a new name.");
    if(!ok)return;
    _verifiedUserId=null;_verifiedName=null;_verifiedUsername=null;_idToken=null;
    localStorage.removeItem(LS_TG_USER_ID);localStorage.removeItem(LS_TG_NAME);localStorage.removeItem(LS_TG_USERNAME);localStorage.removeItem(LS_ID_TOKEN);
    _stopVerifyPoll();
  }
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

  // Inside Telegram the identity proof is fetched in the background; if the
  // user taps before it lands, finish it first so the vote attributes to their
  // real account instead of falling back to a name-only proxy entry.
  if(tg&&tg.initData&&!_idToken){try{await _miniappAuth();}catch(_){}}

  voting=true;
  // Show spinner on the tapped button
  _spinBtn=$({"in":"btn-in","out":"btn-out","maybe":"btn-maybe"}[voteType]);
  if(_spinBtn)_spinBtn.classList.add("spinning");
  renderVoteUI();

  const ac=new AbortController();
  const _tid=setTimeout(()=>ac.abort(),30000);
  try{
    const _hdrs={"Content-Type":"application/json"};
    if(_maToken)_hdrs["Authorization"]="Bearer "+_maToken;
    const res=await fetch("/api/v1/web/"+token+"/vote",{
      method:"POST",signal:ac.signal,headers:_hdrs,
      body:JSON.stringify({
        name:currentName,vote:voteType,
        ...(_idToken?{id_token:_idToken}:{}),
        // Username sent so server can format "First (@handle)" when a proxy
        // with the same first name exists (tg-verify stores _verifiedUsername;
        // Mini App has it from the SDK).
        ...((_verifiedUsername||TG_USER?.username)?{username:_verifiedUsername||TG_USER?.username}:{}),
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
  if(rc.fee)meta.push(`<strong style="color:var(--accent)">💰 Fee: ${esc(rc.fee)}/person</strong>`);
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
  const endRow=document.getElementById("end-rc-row");
  if(endRow)endRow.style.display=_isWebAdmin?"":"none";
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
  // Mini App auth fires in parallel with page data; errors are silent
  if(tg&&tg.initData)_miniappAuth().catch(()=>{});
  try{IS_GROUP?await loadGroup():await loadJoin();}
  catch(e){showError(e.message||"Could not connect. Check your internet and tap Retry.");return;}
  $("loading").classList.add("hidden");
  $("main").classList.remove("hidden");
  renderIdentity();scheduleRefresh();
  if(IS_GROUP){fetchPresence();_checkWebAdmin().catch(()=>{});}
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
  // Persist this group in recents + update page title
  const gname=groupData.group_name||"RollCall Group";
  _saveGroup(URL_TOKEN,gname);
  if(gname)document.title=`RollCall — ${gname}`;
  renderUpcoming(groupData.upcoming||[]);
  if(!rcs.length){
    ["rc-title","rc-meta","count-badge"].forEach(id=>{$(id)&&($(id).textContent="")});
    $("tab-card").classList.add("hidden");
    $("no-rollcalls").classList.remove("hidden");
    ["identity-card","vote-card","lists-card"].forEach(id=>$(id)?.classList.add("hidden"));
    const endRow=document.getElementById("end-rc-row");
    if(endRow)endRow.style.display="none";
  }else if(rcs.length===1){$("tab-card").classList.add("hidden");renderRollcall(rcs[0]);}
  else{$("tab-card").classList.remove("hidden");if(activeTabIdx>=rcs.length)activeTabIdx=0;renderTabs(rcs);renderRollcall(rcs[activeTabIdx]);}
  loadWebStats();
  // Show bookmark card + share button in group mode
  const bc=document.getElementById("bookmark-card");
  if(bc)bc.classList.remove("hidden");
  const sb=document.getElementById("share-btn");
  if(sb&&navigator.share)sb.style.display="";
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
const TG_USER_ID=TG_USER?.id||_verifiedUserId||null;

async function loadWebStats(){
  const sc=$("stats-card");if(!sc)return;
  const params=new URLSearchParams();
  // Pass a signed identity token (never a raw user_id) so the server can
  // verify who is requesting personal stats and prevent IDOR.
  if(_idToken)params.set("id_token",_idToken);
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
      ${(me.recent_sessions||[]).length>=3?`
      <div class="sp-spark-label">Last ${me.recent_sessions.length} sessions</div>
      <div class="sp-spark">${(me.recent_sessions).slice().reverse().map(s=>{
        const cls=s.status==="in"?"sp-dot-in":s.status==="out"?"sp-dot-out":s.status==="maybe"?"sp-dot-maybe":"sp-dot-miss";
        const ttl=s.status==="miss"?"Didn't vote":(s.status||"").toUpperCase();
        return`<span class="sp-dot ${cls}" title="${esc(ttl)} · ${esc((s.ended_at||'').slice(0,10))}"></span>`;
      }).join('')}</div>`:""}
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

  // Attendance trend chart from recent_history (oldest→newest)
  const histArr=(d.recent_history||[]).slice().reverse();
  const maxIn=histArr.length?Math.max(...histArr.map(h=>h.in_count||0),1):1;
  const trendHtml=histArr.length>=2?`
  <div class="sp-trend-label">📈 Recent Attendance</div>
  <div class="sp-trend">
    ${histArr.map(h=>{
      const barH=Math.round((h.in_count||0)/maxIn*70)+10;
      const label=(h.ended_at||'').slice(5,10)||'';
      return`<div class="sp-tbar-wrap" title="${esc(h.title||'')} · ${h.in_count} IN">
        <div class="sp-tbar-val">${h.in_count}</div>
        <div class="sp-tbar" style="height:${barH}%"></div>
        <div class="sp-tbar-lbl">${esc(label)}</div>
      </div>`;
    }).join('')}
  </div>`:'';

  sc.innerHTML=`
  <div class="stats-section-hdr">📊 Group Stats</div>
  <div class="sp-group-row">
    <div class="sp-g"><div class="sp-g-val">${n(d.total_rollcalls)}</div><div class="sp-g-lbl">Sessions</div></div>
    <div class="sp-g"><div class="sp-g-val">${n(d.avg_attendance)}</div><div class="sp-g-lbl">Avg Attendance</div></div>
    <div class="sp-g"><div class="sp-g-val">${n(d.total_participants)}</div><div class="sp-g-lbl">Members</div></div>
  </div>
  ${trendHtml}
  ${personalHtml}
  ${lbRows?`<div class="stats-section-hdr">🏆 Leaderboard</div><div class="slb-list">${lbRows}</div>`:""}`;
}

// ── Presence / heartbeat ──────────────────────────────────────────────────
let _sessionId = sessionStorage.getItem("rc_sid");
if (!_sessionId) {
  _sessionId = ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g, c =>
    (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16));
  sessionStorage.setItem("rc_sid", _sessionId);
}

async function sendHeartbeat() {
  if (!IS_GROUP) return;
  try {
    await fetch(`/api/v1/web/group/${URL_TOKEN}/heartbeat`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({session_id: _sessionId}),
      signal: AbortSignal.timeout(5000),
    });
  } catch(_) {}
}

async function fetchPresence() {
  if (!IS_GROUP) return;
  try {
    const r = await fetch(`/api/v1/web/group/${URL_TOKEN}/presence`, {signal: AbortSignal.timeout(5000)});
    if (!r.ok) return;
    const d = await r.json();
    const badge = $("presence-badge");
    if (!badge) return;
    const now = d.active_now || 0;
    const total = d.total_views || 0;
    if (total > 0) {
      badge.textContent = now >= 1 ? `👁 ${now} viewing` : `👁 ${total} views`;
      badge.title = `${now} viewing now · ${total} total views`;
      badge.classList.remove("hidden");
    }
  } catch(_) {}
}

if (IS_GROUP) {
  sendHeartbeat();
  setInterval(sendHeartbeat, 30000);
  setInterval(fetchPresence, 35000);
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

// ── Telegram deep-link identity verification ───────────────────────────────
let _verifyCode=null, _verifyPollTimer=null;

window.startTgVerify=async function(){
  const btn=document.getElementById("verify-tg-btn");
  if(btn){btn.textContent="⏳ Opening Telegram…";btn.disabled=true;}
  // Disable the name input while verification is in progress so the user
  // can't accidentally type a different name after starting the flow.
  const nameInput=$("name-input");
  if(nameInput){nameInput.disabled=true;nameInput.placeholder="Verifying with Telegram…";}
  try{
    const res=await fetch("/api/v1/auth/tg-verify/start",{
      method:"POST",headers:{"Content-Type":"application/json"},
      signal:AbortSignal.timeout(8000),
    });
    if(!res.ok)throw new Error("Server error");
    const{code,deep_link}=await res.json();
    _verifyCode=code;
    window.open(deep_link,"_blank");
    toast("Telegram opened — tap the verify button, then return here",5000);
    if(btn){btn.textContent="⏳ Waiting for Telegram…";}
    _verifyPollTimer=setInterval(_pollVerify,2000);
    // Auto-stop after 11 minutes (code TTL is 10 min)
    setTimeout(()=>{
      if(_verifyPollTimer){
        _stopVerifyPoll();
        if(nameInput){nameInput.disabled=false;nameInput.placeholder="";}
        if(btn){btn.textContent="🔗 Verify with Telegram";btn.disabled=false;}
      }
    },660000);
  }catch(e){
    toast("Could not start verification — try again",3500);
    if(nameInput){nameInput.disabled=false;nameInput.placeholder="";}
    if(btn){btn.textContent="🔗 Verify with Telegram";btn.disabled=false;}
  }
};

async function _pollVerify(){
  if(!_verifyCode)return;
  try{
    const res=await fetch(`/api/v1/auth/tg-verify/status/${_verifyCode}`,{signal:AbortSignal.timeout(5000)});
    if(res.status===404||res.status===410){_stopVerifyPoll();toast("Verification link expired — try again",4000);renderIdentity();return;}
    if(!res.ok)return;
    const data=await res.json();
    if(!data.verified)return;
    _stopVerifyPoll();
    // Re-enable name input (it was disabled during polling — now locked via identity)
    const nameInput=$("name-input");
    if(nameInput){nameInput.disabled=false;nameInput.placeholder="";}
    _verifiedUserId=data.user_id;
    _verifiedName=data.name;
    _verifiedUsername=data.username||null;
    _idToken=data.id_token||null;
    localStorage.setItem(LS_TG_USER_ID,String(_verifiedUserId));
    localStorage.setItem(LS_TG_NAME,_verifiedName);
    if(_verifiedUsername)localStorage.setItem(LS_TG_USERNAME,_verifiedUsername);
    if(_idToken)localStorage.setItem(LS_ID_TOKEN,_idToken);
    // Auto-populate name from verified Telegram identity and lock it
    currentName=_verifiedName;
    localStorage.setItem(LS_NAME,currentName);
    toast(`✅ Verified as ${data.name}! Your identity is now locked to your Telegram account.`,4500);
    renderIdentity();detectCurrentVote();
    _checkWebAdmin().catch(()=>{});
    // Re-link any existing push subscription with the now-known user ID
    _relinkPushSubscription(_verifiedUserId);
  }catch(_){}
}

function _stopVerifyPoll(){
  if(_verifyPollTimer){clearInterval(_verifyPollTimer);_verifyPollTimer=null;}
  _verifyCode=null;
}

async function _relinkPushSubscription(userId){
  try{
    if(!_swReg)return;
    const existing=await _swReg.pushManager.getSubscription();
    if(!existing)return;
    const j=existing.toJSON();
    await fetch(`/api/v1/web/group/${URL_TOKEN}/push-subscribe`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({endpoint:j.endpoint,keys:{p256dh:j.keys.p256dh,auth:j.keys.auth},tg_user_id:userId}),
      signal:AbortSignal.timeout(5000),
    });
  }catch(_){}
}

// ── PWA: service worker + push notifications ───────────────────────────────
let _swReg = null;

if ("serviceWorker" in navigator && IS_GROUP) {
  navigator.serviceWorker.register("/web/sw.js", { scope: "/web/" })
    .then(reg => {
      _swReg = reg;
      _initPushUI();
    })
    .catch(e => console.warn("[sw] registration failed", e));
}

// Inject dynamic manifest once we know the group token
if (IS_GROUP && URL_TOKEN) {
  const link = document.createElement("link");
  link.rel = "manifest";
  link.href = `/api/v1/web/group/${URL_TOKEN}/manifest.json`;
  document.head.appendChild(link);
}

function _urlB64ToUint8Array(b64) {
  const pad = "=".repeat((4 - b64.length % 4) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from([...raw].map(c => c.charCodeAt(0)));
}

function _initPushUI() {
  const btn = $("notify-btn");
  if (!btn || !("PushManager" in window)) return;
  // Show the bell button only in group mode (not TG mini-app — Telegram has its own notifications)
  if (tg) return;
  btn.classList.remove("hidden");
  _updateNotifyBtn();
}

function _updateNotifyBtn() {
  const btn = $("notify-btn");
  if (!btn) return;
  const perm = Notification.permission;
  if (perm === "granted") {
    btn.textContent = "🔔";
    btn.title = "Notifications ON — tap to turn off";
    btn.classList.add("notify-on");
  } else if (perm === "denied") {
    btn.textContent = "🔕";
    btn.title = "Notifications blocked in browser settings";
    btn.classList.add("notify-blocked");
  } else {
    btn.textContent = "🔔";
    btn.title = "Get notified when a rollcall opens";
    btn.classList.remove("notify-on", "notify-blocked");
  }
}

window.toggleNotifications = async function() {
  if (!_swReg) { toast("Notifications not available on this browser"); return; }
  const perm = Notification.permission;

  if (perm === "denied") {
    toast("Notifications are blocked — enable them in browser settings", 4000);
    return;
  }

  // Check if already subscribed
  const existing = await _swReg.pushManager.getSubscription();

  if (existing) {
    // Unsubscribe
    try {
      await fetch(`/api/v1/web/group/${URL_TOKEN}/push-unsubscribe`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ endpoint: existing.endpoint }),
        signal: AbortSignal.timeout(5000),
      });
      await existing.unsubscribe();
      toast("Notifications turned off");
      _updateNotifyBtn();
    } catch (e) { toast("Could not unsubscribe: " + e.message, 3000); }
    return;
  }

  // Subscribe
  try {
    const keyResp = await fetch("/api/v1/web/vapid-public-key", { signal: AbortSignal.timeout(5000) });
    const { public_key } = await keyResp.json();
    const sub = await _swReg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: _urlB64ToUint8Array(public_key),
    });
    const j = sub.toJSON();
    await fetch(`/api/v1/web/group/${URL_TOKEN}/push-subscribe`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        endpoint: j.endpoint,
        keys: { p256dh: j.keys.p256dh, auth: j.keys.auth },
        ...(TG_USER?.id||_verifiedUserId?{tg_user_id:TG_USER?.id||_verifiedUserId}:{}),
      }),
      signal: AbortSignal.timeout(5000),
    });
    toast("🔔 You'll be notified when a rollcall opens!", 3500);
    _updateNotifyBtn();
  } catch (e) {
    if (e.name === "NotAllowedError") {
      toast("Notification permission denied", 3000);
    } else {
      toast("Could not enable notifications: " + e.message, 4000);
    }
    _updateNotifyBtn();
  }
};

// ── PWA install prompt ─────────────────────────────────────────────────────
let _installPrompt = null;
window.addEventListener("beforeinstallprompt", e => {
  e.preventDefault();
  _installPrompt = e;
});

// ── Recent groups (localStorage) ───────────────────────────────────────────
const LS_GROUPS="rc_groups";

function _loadGroups(){
  try{return JSON.parse(localStorage.getItem(LS_GROUPS)||"[]");}catch(_){return[];}
}
function _saveGroup(token,name){
  const groups=_loadGroups().filter(g=>g.token!==token);
  groups.unshift({token,name:name||"Group",last_visit:Date.now()});
  localStorage.setItem(LS_GROUPS,JSON.stringify(groups.slice(0,10)));
}
function _removeGroup(token){
  localStorage.setItem(LS_GROUPS,JSON.stringify(_loadGroups().filter(g=>g.token!==token)));
}

// ── Home screen (no URL token) ────────────────────────────────────────────
function renderHomeScreen(){
  const hs=document.getElementById("home-screen");
  if(!hs)return;
  hs.classList.remove("hidden");
  document.getElementById("app").classList.add("hidden");
  const groups=_loadGroups();
  const container=document.getElementById("home-groups");
  if(!container)return;
  if(!groups.length){
    container.innerHTML='<p style="color:var(--sub);font-size:.85rem">No saved groups yet. Paste a group link below.</p>';
    return;
  }
  container.innerHTML=groups.map(g=>`
    <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 0;border-bottom:1px solid var(--border)">
      <div>
        <div style="font-weight:600;font-size:.95rem">${esc(g.name)}</div>
        <div style="font-size:.75rem;color:var(--sub)">${new Date(g.last_visit).toLocaleDateString()}</div>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn btn-primary" style="padding:8px 14px;font-size:.85rem" onclick="window.location.href='/web/group/${esc(g.token)}'">Open</button>
        <button class="btn" style="padding:8px 10px;font-size:.85rem;background:var(--border);color:var(--sub);border-radius:8px" onclick="_removeGroup('${esc(g.token)}');renderHomeScreen()">✕</button>
      </div>
    </div>
  `).join("");
}

window.homeOpenLink=function(){
  const val=(document.getElementById("home-link-input")?.value||"").trim();
  if(!val){return;}
  // Accept full URL or just the token
  const m=val.match(/\/web\/group\/([a-f0-9]+)/);
  if(m){window.location.href=`/web/group/${m[1]}`;return;}
  // Try as a bare token
  if(/^[a-f0-9]{24,}$/.test(val)){window.location.href=`/web/group/${val}`;return;}
  toast("That doesn't look like a valid group link.",3500);
};

// ── Web admin check + start rollcall ─────────────────────────────────────
let _isWebAdmin=false;

async function _checkWebAdmin(){
  if(!IS_GROUP||!_idToken)return;
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/admin-status?id_token=${encodeURIComponent(_idToken)}`,{signal:AbortSignal.timeout(5000)});
    if(!res.ok)return;
    const d=await res.json();
    _isWebAdmin=!!d.is_admin;
    const card=document.getElementById("admin-card");
    if(card)card.classList.toggle("hidden",!_isWebAdmin);
    if(_isWebAdmin)_syncShhToggle();
  }catch(_){}
}

function _syncShhToggle(){
  const tog=document.getElementById("shh-toggle");
  if(!tog||!groupData)return;
  tog.checked=!!groupData.shh_mode;
}

window.toggleShhMode=async function(enabled){
  if(!_idToken){toast("Verify with Telegram first.",3000);return;}
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/settings`,{
      method:"PATCH",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id_token:_idToken,shh_mode:enabled}),
      signal:AbortSignal.timeout(8000),
    });
    if(!res.ok){const d=await res.json().catch(()=>({}));throw new Error(d.detail||"Failed");}
    if(groupData)groupData.shh_mode=enabled;
    toast(enabled?"🔇 Silent mode ON":"🔔 Silent mode OFF",2000);
  }catch(e){
    toast(e.message||"Could not update silent mode",3500);
    // Revert toggle on error
    const tog=document.getElementById("shh-toggle");
    if(tog)tog.checked=!enabled;
  }
};

window.openStartModal=function(){
  const m=document.getElementById("start-modal");
  if(m){m.style.display="flex";m.classList.remove("hidden");}
  const inp=document.getElementById("start-title");
  if(inp){inp.value="";inp.focus();}
};
window.closeStartModal=function(){
  const m=document.getElementById("start-modal");
  if(m){m.style.display="none";}
};
window.submitStartRollcall=async function(){
  if(!_idToken){toast("Verify your Telegram identity first.",3500);return;}
  const title=(document.getElementById("start-title")?.value||"").trim();
  if(!title){toast("Enter a title for the rollcall.",2500);return;}
  const btn=document.getElementById("start-submit-btn");
  if(btn){btn.disabled=true;btn.textContent="Starting…";}
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/start-rollcall`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id_token:_idToken,title}),
      signal:AbortSignal.timeout(10000),
    });
    if(!res.ok){
      const d=await res.json().catch(()=>({}));
      throw new Error(d.detail||"Failed to start rollcall");
    }
    closeStartModal();
    toast("✅ Rollcall started!",2500);
    // Reload group data to show the new rollcall
    activeTabIdx=0;
    await loadGroup();
  }catch(e){
    toast(e.message||"Could not start rollcall",4000);
  }finally{
    if(btn){btn.disabled=false;btn.textContent="Start →";}
  }
};

window.doEndRcWeb=async function(){
  if(!_idToken){toast("Verify your Telegram identity first.",3500);return;}
  if(!activeRcData){toast("No active rollcall to end.",2500);return;}
  if(!confirm(`End rollcall "${activeRcData.title}"? This cannot be undone.`))return;
  const btn=document.getElementById("end-rc-btn");
  if(btn){btn.disabled=true;btn.textContent="Ending…";}
  try{
    const rollcall_num=activeTabIdx+1;
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/end-rollcall`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id_token:_idToken,rollcall_num}),
      signal:AbortSignal.timeout(10000),
    });
    if(!res.ok){
      const d=await res.json().catch(()=>({}));
      throw new Error(d.detail||"Failed to end rollcall");
    }
    toast("✅ Rollcall ended!",2500);
    activeTabIdx=0;
    await loadGroup();
  }catch(e){
    toast(e.message||"Could not end rollcall",4000);
  }finally{
    if(btn){btn.disabled=false;btn.textContent="⏹ End Active Rollcall";}
  }
};

// ── Schedule rollcall ────────────────────────────────────────────────────
window.openScheduleModal=async function(){
  const m=document.getElementById("schedule-modal");
  if(m){m.style.display="flex";m.classList.remove("hidden");}
  // Pre-fill date to 1 hour from now (local time)
  const inp=document.getElementById("sched-at");
  if(inp){
    const d=new Date(Date.now()+60*60*1000);
    // datetime-local needs "YYYY-MM-DDTHH:MM"
    const pad=n=>String(n).padStart(2,"0");
    inp.value=`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  const titleInp=document.getElementById("sched-title");
  if(titleInp)titleInp.value="";
  await _loadScheduledList();
};
window.closeScheduleModal=function(){
  const m=document.getElementById("schedule-modal");
  if(m){m.style.display="none";}
};

async function _loadScheduledList(){
  const container=document.getElementById("sched-list");
  if(!container||!_idToken)return;
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/scheduled-rollcalls?id_token=${encodeURIComponent(_idToken)}`,{signal:AbortSignal.timeout(5000)});
    if(!res.ok){container.innerHTML="";return;}
    const d=await res.json();
    if(!d.items||!d.items.length){container.innerHTML=`<div class="sched-empty">No scheduled rollcalls yet.</div>`;return;}
    container.innerHTML=d.items.map(item=>{
      const dt=new Date(item.scheduled_at);
      const label=isNaN(dt)?item.scheduled_at:dt.toLocaleString(undefined,{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"});
      return `<div class="sched-item">
        <div class="sched-item-info">
          <div class="sched-item-title">${esc(item.title)}</div>
          <div class="sched-item-time">📅 ${esc(label)}</div>
        </div>
        <button class="sched-cancel-btn" onclick="cancelScheduled(${item.id})">Cancel</button>
      </div>`;
    }).join("");
  }catch(_){container.innerHTML="";}
}

window.cancelScheduled=async function(id){
  if(!_idToken)return;
  if(!confirm("Cancel this scheduled rollcall?"))return;
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/scheduled-rollcalls/${id}?id_token=${encodeURIComponent(_idToken)}`,{
      method:"DELETE",signal:AbortSignal.timeout(8000),
    });
    if(!res.ok&&res.status!==204){const d=await res.json().catch(()=>({}));throw new Error(d.detail||"Failed");}
    toast("Scheduled rollcall cancelled.",2000);
    await _loadScheduledList();
  }catch(e){toast(e.message||"Could not cancel",3500);}
};

window.submitScheduleRollcall=async function(){
  if(!_idToken){toast("Verify with Telegram first.",3500);return;}
  const title=(document.getElementById("sched-title")?.value||"").trim();
  if(!title){toast("Enter a title.",2500);return;}
  const atLocal=document.getElementById("sched-at")?.value;
  if(!atLocal){toast("Pick a date and time.",2500);return;}
  // Convert datetime-local (local time, no zone) to UTC ISO string
  const localMs=new Date(atLocal).getTime();
  if(isNaN(localMs)||localMs<=Date.now()){toast("Choose a future date and time.",3000);return;}
  const scheduledAt=new Date(localMs).toISOString();
  const btn=document.getElementById("sched-submit-btn");
  if(btn){btn.disabled=true;btn.textContent="Scheduling…";}
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/scheduled-rollcalls`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id_token:_idToken,title,scheduled_at:scheduledAt}),
      signal:AbortSignal.timeout(10000),
    });
    if(!res.ok){const d=await res.json().catch(()=>({}));throw new Error(d.detail||"Failed to schedule rollcall");}
    toast("✅ Rollcall scheduled!",2500);
    document.getElementById("sched-title").value="";
    await _loadScheduledList();
  }catch(e){
    toast(e.message||"Could not schedule rollcall",4000);
  }finally{
    if(btn){btn.disabled=false;btn.textContent="Schedule →";}
  }
};

// ── Bookmark / share group URL ────────────────────────────────────────────
window.copyGroupLink=function(){
  const url=window.location.origin+`/web/group/${URL_TOKEN}`;
  if(navigator.clipboard){
    navigator.clipboard.writeText(url).then(()=>toast("📋 Link copied — share it!",2800)).catch(()=>toast(url,5000));
  }else{toast(url,5000);}
};
window.shareGroupLink=function(){
  const url=window.location.origin+`/web/group/${URL_TOKEN}`;
  if(navigator.share){navigator.share({title:"RollCall",url}).catch(()=>{});}
};

// ── Entry point ────────────────────────────────────────────────────────────
if(URL_TOKEN&&(URL_MODE==="join"||URL_MODE==="group")){
  load();
}else{
  // No token in URL — show home screen
  renderHomeScreen();
}
})();
