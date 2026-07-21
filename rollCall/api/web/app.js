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

// Admin-issued weblogin redirect: ?login_token=<id_token> lands here after the
// server validates the single-use token and issues an identity token. Store it,
// strip the param from the URL so it isn't bookmarked or shared accidentally,
// then continue with normal page load.
(function(){
  try{
    const p=new URLSearchParams(window.location.search);
    const lt=p.get("login_token");
    if(lt){
      localStorage.setItem(LS_ID_TOKEN,lt);
      _idToken=lt;
      p.delete("login_token");
      const qs=p.toString();
      const clean=window.location.pathname+(qs?"?"+qs:"");
      history.replaceState(null,"",clean);
    }
  }catch(_){}
})();

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
    $("identity-picker-row").classList.add("hidden");
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
    $("name-tag-row").classList.add("hidden");
    if(IS_GROUP&&!TG_NAME){
      // Group mode with no identity: show the picker (Telegram vs guest)
      $("identity-picker-row").classList.remove("hidden");
      $("name-input-row").classList.add("hidden");
    }else{
      // Join link or Mini App without a name yet: go straight to name input
      $("identity-picker-row").classList.add("hidden");
      $("name-input-row").classList.remove("hidden");
    }
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
  $("name-tag-row").classList.add("hidden");
  if(IS_GROUP&&!TG_NAME){
    $("name-input-row").classList.add("hidden");
    $("identity-picker-row").classList.remove("hidden");
  }else{
    $("name-input").value="";
    $("name-input-row").classList.remove("hidden");
    $("name-input").focus();
  }
});

function saveName(){
  const val=$("name-input").value.trim();if(!val){$("name-input").focus();return;}
  currentName=val.slice(0,64);
  if(TG_NAME)localStorage.setItem(LS_NAME_OVERRIDE,currentName);
  else localStorage.setItem(LS_NAME,currentName);
  renderIdentity();detectCurrentVote();
  if(IS_GROUP)loadWebStats();
}

window.showGuestInput=function(){
  $("identity-picker-row").classList.add("hidden");
  $("name-input-row").classList.remove("hidden");
  $("name-back-row")?.classList.remove("hidden");
  $("name-input").focus();
};
window.showIdentityPicker=function(){
  $("name-input-row").classList.add("hidden");
  $("identity-picker-row").classList.remove("hidden");
};

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

function renderRcMeta(rc){
  const meta=[];
  if(rc.finalize_date){
    const cd=formatCountdown(rc.finalize_epoch);
    const cdHtml=cd?`<span class="cd-pill${cd.includes("m")&&!cd.includes("h")?" soon":""}">${esc(cd)}</span>`:"";
    meta.push("🕐 Closes: "+esc(rc.finalize_date)+(cdHtml?" "+cdHtml:""));
  }
  if(rc.location)meta.push("📍 "+esc(rc.location));
  if(rc.fee){
    // rc.fee is the TOTAL event cost (set via /event_fee) — it was
    // mislabeled "/person" here. Mirror the Telegram panel's "Event Fee" +
    // "Individual Fee" pair: show the total, plus the live per-head split
    // once someone's actually IN (matches models.py's _ind_fee rounding).
    let feeLine=`<strong style="color:var(--accent)">💰 Fee: ${esc(rc.fee)} total</strong>`;
    const inCount=(rc.in||[]).length;
    const feeNum=parseFloat(String(rc.fee).replace(/[^0-9.]/g,""));
    if(inCount>0&&!isNaN(feeNum)){
      const perHead=Math.round((feeNum/inCount)*100)/100;
      feeLine+=` <span style="opacity:.75;font-weight:500">(₹${perHead}/person)</span>`;
    }
    meta.push(feeLine);
  }
  $("rc-meta").innerHTML=meta.map(m=>`<span>${m}</span>`).join("<br/>");
}

// Live countdown: refresh the meta row every 30s so the "in Xh Ym" pill
// stays accurate while the tab sits open. Meta-only — doesn't touch lists
// or vote state, so it can't disrupt an in-progress interaction.
setInterval(()=>{
  if(activeRcData&&activeRcData.finalize_epoch)renderRcMeta(activeRcData);
},30000);

function renderRollcall(rc){
  activeRcData=rc;
  const totalRc=IS_GROUP&&groupData?groupData.rollcalls.length:1;
  $("rc-title").textContent=totalRc>1?`#${activeTabIdx+1} · ${rc.title}`:rc.title;
  renderRcMeta(rc);
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
  const proxyRow=document.getElementById("proxy-vote-row");
  if(proxyRow)proxyRow.style.display=_isWebAdmin?"":"none";
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
  renderGroupCta();
}

// "Create your own group" footer — the viral loop for guests who arrived via
// a shared link. Deep-links to the bot with ?startgroup so Telegram prompts
// them to pick a group to add it to. Names and the username only — no tokens.
function renderGroupCta(){
  const u=groupData&&groupData.bot_username;
  if(!u)return;
  let el=document.getElementById("group-cta");
  if(!el){
    el=document.createElement("div");
    el.id="group-cta";
    el.className="group-cta";
    const container=document.getElementById("bookmark-card")?.parentElement;
    if(!container)return;
    container.appendChild(el);
  }
  el.innerHTML=`⚡ Made with <strong>RollCall</strong> · <a href="https://t.me/${esc(u)}?startgroup=true" target="_blank" rel="noopener">Create your own group →</a>`;
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
          // Reuse the single meta-row builder (was duplicated inline here,
          // out of sync, and silently dropped the fee line on every 30s
          // background refresh).
          renderRcMeta(_rc);
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

// Weekday scheduling hint — needs both stats data AND admin status, which
// resolve independently; whichever finishes second triggers the render.
let _weekdayStats=null;
function _renderWeekdayHint(){
  if(!_isWebAdmin||!_weekdayStats||_weekdayStats.length<2)return;
  const card=document.getElementById("admin-card");
  if(!card)return;
  let el=document.getElementById("weekday-hint");
  if(!el){
    el=document.createElement("div");
    el.id="weekday-hint";
    el.style.cssText="border-top:1px solid var(--border);padding-top:12px;margin-top:10px";
    card.appendChild(el);
  }
  const best=_weekdayStats[0];
  const rows=_weekdayStats.map(w=>
    `<div style="display:flex;justify-content:space-between;font-size:.8rem;padding:3px 0">
      <span>${esc(w.weekday)}</span>
      <span style="color:var(--sub)">${w.sessions} game${w.sessions===1?"":"s"} · <strong style="color:var(--text)">${w.avg_in} avg IN</strong></span>
    </div>`).join("");
  el.innerHTML=`
    <div style="font-size:.82rem;font-weight:600;color:var(--sub);margin-bottom:6px">📅 Best days (last 90 days)</div>
    ${rows}
    <div style="font-size:.73rem;color:var(--sub);margin-top:6px">💡 ${esc(best.weekday)}s draw the most players — worth scheduling around.</div>`;
}

function renderStats(d){
  const sc=$("stats-card");if(!sc)return;
  _weekdayStats=d.weekday_stats||null;
  _renderWeekdayHint();
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
    const chips=(e.badges||[]).map(b=>`<span class="slb-badge" title="${b.startsWith("🔥")?"Current attendance streak":"Games played milestone"}">${esc(b)}</span>`).join("");
    return `<div class="slb-row${isMe?" slb-you":""}${e.kind==="proxy"?" slb-proxy":""}">
      <span class="slb-rank">#${e.rank??i+1}</span>
      <span class="slb-name">${esc(e.display_name||"—")}${chips}${isMe?" ← you":""}</span>
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
  const btn=document.getElementById("verify-tg-btn")||document.getElementById("picker-tg-btn");
  const _origBtnText=btn?.textContent||"";
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
        if(btn){btn.textContent=_origBtnText||"🔗 Verify with Telegram";btn.disabled=false;}
      }
    },660000);
  }catch(e){
    toast("Could not start verification — try again",3500);
    if(nameInput){nameInput.disabled=false;nameInput.placeholder="";}
    if(btn){btn.textContent=_origBtnText||"🔗 Verify with Telegram";btn.disabled=false;}
  }
};

async function _pollVerify(){
  if(!_verifyCode)return;
  try{
    const res=await fetch(`/api/v1/auth/tg-verify/status/${_verifyCode}`,{signal:AbortSignal.timeout(5000)});
    if(res.status===404||res.status===410){
      _stopVerifyPoll();
      const _vb=document.getElementById("verify-tg-btn")||document.getElementById("picker-tg-btn");
      if(_vb){_vb.textContent=_vb.id==="picker-tg-btn"?"✈ Continue with Telegram":"🔗 Verify with Telegram";_vb.disabled=false;}
      toast("Verification link expired — try again",4000);
      renderIdentity();
      return;
    }
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

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/web/sw.js", { scope: "/web/" })
    .then(reg => {
      _swReg = reg;
      _initPushUI();
    })
    .catch(e => console.warn("[sw] registration failed", e));
}

// Always install to the home screen (/web/) so multi-group users land on their
// full group list. The group-specific dynamic manifest is not used for install —
// groups are auto-saved to localStorage when visited and appear on the home screen.
{
  const _ml = document.createElement("link");
  _ml.rel = "manifest";
  _ml.href = "/web/manifest.json";
  document.head.appendChild(_ml);
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

function _showInstallBtn(){
  if(tg)return; // already inside Telegram — no native install UX needed
  document.querySelectorAll(".brand-install-btn").forEach(b=>b.classList.remove("hidden"));
}
function _hideInstallBtn(){
  document.querySelectorAll(".brand-install-btn").forEach(b=>b.classList.add("hidden"));
}

window.addEventListener("beforeinstallprompt", e => {
  e.preventDefault();
  _installPrompt = e;
  _showInstallBtn();
});

window.addEventListener("appinstalled", () => {
  _installPrompt = null;
  _hideInstallBtn();
  toast("✅ RollCall installed! Open it from your home screen.", 4000);
});

window.triggerInstall = async function(){
  if(!_installPrompt)return;
  _installPrompt.prompt();
  const{outcome} = await _installPrompt.userChoice;
  if(outcome === "accepted"){
    _installPrompt = null;
    _hideInstallBtn();
  }
};

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
    container.innerHTML='<p style="color:var(--sub);font-size:.85rem">No groups yet. Visit a group rollcall link and it\'ll appear here automatically — or paste one below.</p>';
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
    if(_isWebAdmin){_syncShhToggle();_renderWeekdayHint();}
  }catch(_){}
  // Load dues after admin status is resolved — both member and admin sections
  loadDuesSection().catch(()=>{});
}

function _syncShhToggle(){
  const tog=document.getElementById("shh-toggle");
  if(!tog||!groupData)return;
  tog.checked=!!groupData.shh_mode;
}

let _lastWebloginUrl="";

window.doIssueWeblogin=async function(){
  if(!_idToken){toast("Verify with Telegram first.",3000);return;}
  const nameEl=document.getElementById("weblogin-name-input");
  const name=(nameEl?.value||"").trim();
  if(!name){toast("Enter a name or @username.",2500);return;}
  const resultEl=document.getElementById("weblogin-result");
  const nameOut=document.getElementById("weblogin-result-name");
  const urlOut=document.getElementById("weblogin-result-url");
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/issue-weblogin`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id_token:_idToken,member_name:name}),
      signal:AbortSignal.timeout(10000),
    });
    if(!res.ok){const d=await res.json().catch(()=>({}));throw new Error(d.detail||"Failed");}
    const data=await res.json();
    _lastWebloginUrl=data.login_url;
    if(nameOut)nameOut.textContent=`Link for ${data.member_name} — valid 7 days, single use`;
    if(urlOut)urlOut.textContent=data.login_url;
    resultEl?.classList.remove("hidden");
    if(nameEl)nameEl.value="";
  }catch(e){toast(e.message||"Could not generate link.",4000);}
};

window.copyWebloginUrl=function(){
  if(!_lastWebloginUrl)return;
  if(navigator.clipboard){
    navigator.clipboard.writeText(_lastWebloginUrl)
      .then(()=>toast("Link copied!",2000))
      .catch(()=>toast(_lastWebloginUrl,5000));
  }else{
    toast(_lastWebloginUrl,5000);
  }
};

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

// ── Proxy vote (admin votes for a non-Telegram member — /sif parity) ─────
let _proxyVoting=false;
window.doProxyVoteWeb=async function(voteType){
  if(!_idToken){toast("Verify your Telegram identity first.",3500);return;}
  if(!activeRcData){toast("No active rollcall.",2500);return;}
  if(_proxyVoting)return;
  const nameEl=document.getElementById("proxy-name-input");
  const name=(nameEl?.value||"").trim();
  if(!name){toast("Enter the member's name first.",2500);return;}
  _proxyVoting=true;
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/proxy-vote`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id_token:_idToken,rollcall_num:activeTabIdx+1,proxy_name:name,vote:voteType}),
      signal:AbortSignal.timeout(10000),
    });
    if(!res.ok){
      const d=await res.json().catch(()=>({}));
      throw new Error(d.detail||"Failed to cast proxy vote");
    }
    const updated=await res.json();
    activeRcData=updated;
    if(IS_GROUP&&groupData)groupData.rollcalls[activeTabIdx]=updated;
    if(nameEl)nameEl.value="";
    toast(`🗳 ${name} → ${voteType.toUpperCase()}`,2500);
    renderRollcall(updated);
  }catch(e){
    toast(e.message||"Could not cast proxy vote",4000);
  }finally{
    _proxyVoting=false;
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

// ── Dues ───────────────────────────────────────────────────────────────────
const DUES_API=`/api/v1/web/group/${URL_TOKEN}/dues`;
let _duesSettings=null;    // cached settings (upi_vpa, mode)
let _duesMemberData=null;  // my_dues response
let _duesSummaryData=null; // summary response (admin)
let _duesPreviewData=null; // close-preview response (admin)

async function _duesGet(path,params={}){
  const q=new URLSearchParams({...params,id_token:_idToken||""});
  const r=await fetch(`${DUES_API}${path}?${q}`,{signal:AbortSignal.timeout(10000)});
  if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.detail||"Request failed");}
  return r.json();
}

async function _duesPost(path,body={}){
  const r=await fetch(`${DUES_API}${path}`,{
    method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({...body,id_token:_idToken||""}),
    signal:AbortSignal.timeout(12000),
  });
  if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.detail||"Request failed");}
  return r.json();
}

async function _duesPatch(path,body={}){
  const r=await fetch(`${DUES_API}${path}`,{
    method:"PATCH",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({...body,id_token:_idToken||""}),
    signal:AbortSignal.timeout(10000),
  });
  if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.detail||"Request failed");}
}

async function loadDuesSection(){
  if(!IS_GROUP)return;
  const duesEnabled=groupData&&groupData.dues_enabled;
  const memberCard=document.getElementById("dues-member-card");
  const adminCard=document.getElementById("dues-admin-card");
  if(!duesEnabled){
    memberCard?.classList.add("hidden");
    adminCard?.classList.add("hidden");
    return;
  }
  // Show member card stub so guests see a CTA to verify
  if(!_idToken){
    if(memberCard){
      memberCard.classList.remove("hidden");
      const body=document.getElementById("dues-member-body");
      if(body)body.innerHTML=`<div style="text-align:center;padding:14px 0;color:var(--sub);font-size:.85rem">🔒 <a href="#" onclick="startTgVerify();return false;" style="color:var(--accent)">Verify with Telegram</a> to see your dues balance</div>`;
    }
    return;
  }
  // Verified member: fetch /my (contains VPA + self_paid_mode already)
  try{
    _duesMemberData=await _duesGet("/my");
    // Also fetch fund balance which any member can see
    const fundData=await _duesGet("/fund").catch(()=>null);
    if(fundData)_duesMemberData._fund_balance=fundData.fund_balance;
    renderMemberDues();
    memberCard?.classList.remove("hidden");
  }catch(e){console.warn("dues/my failed:",e.message);}
  // Admin: fetch settings, summary, and close preview
  if(_isWebAdmin){
    try{
      [_duesSettings,_duesSummaryData,_duesPreviewData]=await Promise.all([
        _duesGet("/settings"),
        _duesGet("/summary"),
        _duesGet("/close-preview").catch(()=>null),
      ]);
      // Update fund row in member card now that summary is loaded
      const fundEl=document.getElementById("dues-fund-row");
      if(fundEl)fundEl.innerHTML=`🏦 Group fund: <span class="dues-fund-balance">₹${_duesSummaryData.fund_balance||0}</span>`;
      renderAdminDues();
      // Auto-open the Members section for admins
      _adminSectionOpen.balances=true;
      const balBody=document.getElementById("dues-balances-body");
      const balCh=document.getElementById("balances-chevron");
      if(balBody)balBody.classList.remove("hidden");
      if(balCh)balCh.textContent="▲";
      adminCard?.classList.remove("hidden");
    }catch(e){console.warn("dues/summary failed:",e.message);}
  }
}

function renderMemberDues(){
  if(!_duesMemberData)return;
  const{balance,entries,upi_vpa,dues_self_paid_mode}=_duesMemberData;
  const vpa=upi_vpa||(_duesSettings&&_duesSettings.upi_vpa)||null;
  const mode=dues_self_paid_mode||"auto";

  // Balance row — don't show ₹0 when settled, just the tick
  const balEl=document.getElementById("dues-balance-row");
  if(balEl){
    if(balance>0){
      balEl.innerHTML=`<div class="dues-balance-amount owed">₹${balance}</div><div class="dues-balance-label">you owe</div>`;
    }else if(balance<0){
      balEl.innerHTML=`<div class="dues-balance-amount credit">₹${Math.abs(balance)}</div><div class="dues-balance-label">credit (you're ahead)</div>`;
    }else{
      balEl.innerHTML=`<div class="dues-balance-amount settled" style="font-size:2.5rem">✓</div><div class="dues-balance-label">All settled</div>`;
    }
  }

  // Pay row (only when balance > 0 and VPA is set)
  const payEl=document.getElementById("dues-pay-row");
  if(payEl){
    if(balance>0&&vpa){
      const upiLink=`upi://pay?pa=${encodeURIComponent(vpa)}&am=${balance}&cu=INR&tn=RollCall`;
      // Cache-bust QR so refresh after payment shows correct amount
      const qrSrc=`${DUES_API}/qr?id_token=${encodeURIComponent(_idToken||"")}&amount=${balance}&_t=${Date.now()}`;
      let html=`<a href="${upiLink}" class="dues-upi-btn">💳 Pay ₹${balance} via UPI</a>`;
      html+=`<div class="dues-vpa-row">
        <span class="dues-vpa-text">${esc(vpa)}</span>
        <button class="dues-vpa-copy">📋 Copy</button>
      </div>`;
      html+=`<div class="dues-qr-wrap"><img src="${qrSrc}" alt="UPI QR" loading="lazy"/></div>`;
      if(mode==="auto"){
        html+=`<button class="dues-self-paid-btn" id="self-paid-btn" data-amount="${balance}">✅ I've paid ₹${balance}</button>`;
      }
      payEl.innerHTML=html;
      payEl.classList.remove("hidden");
      // Attach self-paid listener safely (avoids onclick-in-attribute XSS surface)
      const spBtn=payEl.querySelector("#self-paid-btn");
      if(spBtn)spBtn.addEventListener("click",()=>doSelfPaid(balance));
      // Attach copy listener with VPA in closure (avoids JS-context escaping)
      const cpBtn=payEl.querySelector(".dues-vpa-copy");
      if(cpBtn)cpBtn.addEventListener("click",()=>copyVpa(vpa));
    }else{
      payEl.classList.add("hidden");
    }
  }

  // Ledger — open by default when entries exist
  const ledEl=document.getElementById("dues-ledger-body");
  const ledCh=document.getElementById("dues-ledger-chevron");
  if(ledEl){
    if(!entries||!entries.length){
      ledEl.innerHTML=`<p class="dues-ledger-empty">No entries yet.</p>`;
      ledEl.classList.add("hidden");
      if(ledCh)ledCh.textContent="▼";
    }else{
      const rows=entries.slice(0,20).map(e=>{
        const isCr=e.amount<0;
        const amtCls=isCr?"credit":"owed";
        const sign=isCr?"−":"";
        const typeLabel=_entryTypeLabel(e.entry_type);
        const date=(e.created_at||"").slice(0,10);
        return `<tr><td>${esc(typeLabel)}<br/><span style="font-size:.72rem;color:var(--sub)">${esc(date)}</span></td><td class="${amtCls}">${sign}₹${Math.abs(e.amount)}</td></tr>`;
      }).join("");
      ledEl.innerHTML=`<table class="dues-ledger-table">${rows}</table>`;
      ledEl.classList.remove("hidden");
      if(ledCh)ledCh.textContent="▲";
    }
  }

  // Fund — show from either admin summary or member's own /fund fetch
  const fundEl=document.getElementById("dues-fund-row");
  if(fundEl){
    const bal=(_duesSummaryData&&_duesSummaryData.fund_balance)
              ??(_duesMemberData&&_duesMemberData._fund_balance);
    if(bal!=null)fundEl.innerHTML=`🏦 Group fund: <span class="dues-fund-balance">₹${bal}</span>`;
  }
}

function _entryTypeLabel(t){
  const map={share:"Game share",adhoc:"Late joiner",penalty_late:"Late penalty",
    penalty_ditch:"No-show penalty",payment:"Payment",waiver:"Waiver",
    reimbursement:"Reimbursement",cancel_credit:"Reversal",adjustment:"Adjustment"};
  return map[t]||t;
}

function copyVpa(vpa){
  const text=typeof vpa==="string"?vpa:(vpa&&vpa.getAttribute&&vpa.getAttribute("data-vpa"))||"";
  if(!text)return;
  if(navigator.clipboard){navigator.clipboard.writeText(text).then(()=>toast("VPA copied!",2000)).catch(()=>toast(text,4000));}
  else toast(text,4000);
}

function doSelfPaid(amount){
  if(!_idToken){toast("Verify with Telegram first.",3000);return;}
  _showDuesModal(
    "Record payment",
    `Confirm the amount you paid (₹):`,
    String(amount),
    async(val)=>{
      const amt=Math.min(parseInt(val)||amount,amount); // cap at outstanding
      if(amt<=0)throw new Error("Enter a positive amount.");
      await _duesPost("/self-paid",{amount:amt});
      toast("✅ Recorded!",2500);
      _duesMemberData=await _duesGet("/my");
      const fundData=await _duesGet("/fund").catch(()=>null);
      if(fundData)_duesMemberData._fund_balance=fundData.fund_balance;
      renderMemberDues();
      if(_isWebAdmin){_duesSummaryData=await _duesGet("/summary");renderAdminDues();}
    }
  );
}

window.toggleDuesMember=function(){
  const body=document.getElementById("dues-member-body");
  const btn=document.getElementById("dues-member-toggle");
  if(!body||!btn)return;
  const hidden=body.classList.toggle("hidden");
  btn.textContent=hidden?"▼":"▲";
};

window.toggleDuesLedger=function(){
  const body=document.getElementById("dues-ledger-body");
  const ch=document.getElementById("dues-ledger-chevron");
  if(!body)return;
  const hidden=body.classList.toggle("hidden");
  if(ch)ch.textContent=hidden?"▼":"▲";
};

// ── Admin dues ────────────────────────────────────────────────────────────
function renderAdminDues(){
  renderBalancesTable();
  renderCloseGame();
  renderFundAdmin();
  renderSettingsAdmin();
}

function renderBalancesTable(){
  const el=document.getElementById("dues-balances-body");
  if(!el)return;
  const balances=(_duesSummaryData&&_duesSummaryData.balances)||[];
  if(!balances.length){el.innerHTML=`<p class="dues-ledger-empty">No dues entries yet.</p>`;return;}
  const sorted=[...balances].sort((a,b)=>b.balance-a.balance);
  const rows=sorted.map(b=>{
    const cls=b.balance>0?"owed":"settled";
    const label=b.balance>0?`₹${b.balance}`:b.balance<0?`₹${Math.abs(b.balance)} cr`:"Settled";
    const btn=b.balance>0?`<button class="mark-paid-btn" data-name="${esc(b.member_name)}" data-amount="${b.balance}">Mark paid</button>`:"";
    return `<tr><td>${esc(b.member_name)}</td><td class="${cls}">${esc(label)}</td><td>${btn}</td></tr>`;
  }).join("");
  el.innerHTML=`<table class="dues-tbl"><thead><tr><th>Member</th><th>Balance</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
  // Attach listeners after DOM insertion (safe — data attrs, no innerHTML injection)
  el.querySelectorAll(".mark-paid-btn").forEach(btn=>{
    btn.addEventListener("click",()=>openMarkPaid(btn.dataset.name,parseInt(btn.dataset.amount)));
  });
}

function renderCloseGame(){
  const el=document.getElementById("dues-close-body");
  if(!el)return;
  const p=_duesPreviewData;
  if(!p||!p.available){
    el.innerHTML=`<p class="dues-ledger-empty">No closeable game found. End a rollcall with a fee set first.</p>`;
    return;
  }
  el.innerHTML=`
    <div class="dues-close-preview" id="dues-close-preview-box">
      <div class="dues-close-preview-row"><span>${esc(p.title)}</span></div>
      <div class="dues-close-preview-row"><span>Ground cost</span><span>₹${p.ground_cost}</span></div>
      <div class="dues-close-preview-row"><span>Players IN</span><span>${p.in_count}</span></div>
      <div class="dues-close-preview-row"><span>Fund subsidy</span><span id="preview-subsidy-display">₹0</span></div>
      <div class="dues-close-preview-row total"><span>Per head</span><span id="preview-per-head">₹${p.per_head}</span></div>
      <div class="dues-close-preview-row"><span style="color:var(--sub);font-size:.77rem">→ Fund +₹${p.remainder} rounding</span></div>
    </div>
    <div class="dues-subsidy-row">
      <label for="close-subsidy">Subsidy (₹)</label>
      <input id="close-subsidy" type="number" min="0" max="${p.fund_balance}" value="0" oninput="updateClosePreview()"/>
    </div>
    <button class="btn btn-primary" style="width:100%;padding:12px" onclick="doCloseGame()">Close Game →</button>
    <div style="font-size:.75rem;color:var(--sub);margin-top:6px;text-align:center">Fund balance: ₹${p.fund_balance}${p.has_active?" · Ends active rollcall":""}</div>
  `;
}

let _previewDebounce=null;
window.updateClosePreview=function(){
  clearTimeout(_previewDebounce);
  _previewDebounce=setTimeout(async()=>{
    const inp=document.getElementById("close-subsidy");
    const subsidy=parseInt(inp?.value||0)||0;
    try{
      const data=await _duesGet("/close-preview",{subsidy});
      document.getElementById("preview-per-head").textContent=`₹${data.per_head}`;
      document.getElementById("preview-subsidy-display").textContent=`₹${subsidy}`;
    }catch(_){}
  },400);
};

window.doCloseGame=async function(){
  if(!_idToken){toast("Verify with Telegram first.",3000);return;}
  const subsidy=parseInt(document.getElementById("close-subsidy")?.value||0)||0;
  const ph=document.getElementById("preview-per-head")?.textContent||"?";
  _showDuesModal(
    "Close game?",
    `${ph}/person · subsidy ₹${subsidy}. This writes all ledger entries.`,
    "",
    async()=>{
      const r=await fetch(`${DUES_API}/close-game`,{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({id_token:_idToken,subsidy}),
        signal:AbortSignal.timeout(20000),
      });
      if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.detail||"Close failed");}
      toast("✅ Game closed!",2500);
      await refreshDues();
    },
    {confirmLabel:"Close →",hideInput:true}
  );
};

window.openMarkPaid=function(name,amount){
  _showDuesModal(
    `Mark paid — ${name}`,
    `Amount (₹):`,
    String(amount),
    async(val)=>{
      const amt=parseInt(val)||amount;
      await fetch(`${DUES_API}/mark-paid`,{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({id_token:_idToken,member_name:name,amount:amt}),
        signal:AbortSignal.timeout(12000),
      }).then(r=>{if(!r.ok)return r.json().then(d=>{throw new Error(d.detail||"Failed");});})
      .catch(e=>{throw e;});
      toast(`✅ ₹${amt} recorded for ${name}`,2800);
      await refreshDues();
    }
  );
};

function renderFundAdmin(){
  const el=document.getElementById("dues-fund-admin-body");
  if(!el)return;
  const bal=(_duesSummaryData&&_duesSummaryData.fund_balance)||0;
  el.innerHTML=`
    <div class="dues-fund-stat"><div class="dues-fund-stat-val">₹${bal}</div><div class="dues-fund-stat-lbl">Fund balance</div></div>
    <div style="font-size:.82rem;font-weight:600;color:var(--sub);margin-bottom:6px">Log expense</div>
    <div class="dues-fund-form">
      <input id="fund-exp-amount" type="number" min="1" placeholder="Amount (₹)"/>
      <input id="fund-exp-desc" type="text" placeholder="Description (e.g. Shuttlecocks)"/>
      <button class="btn btn-secondary" style="width:100%;padding:10px;border-radius:10px" onclick="doLogExpense()">Log Expense →</button>
    </div>
    <div style="font-size:.82rem;font-weight:600;color:var(--sub);margin:12px 0 6px">Add top-up</div>
    <div class="dues-fund-form">
      <input id="fund-top-amount" type="number" min="1" placeholder="Amount (₹)"/>
      <input id="fund-top-desc" type="text" placeholder="Note (optional)"/>
      <button class="btn btn-primary" style="width:100%;padding:10px;border-radius:10px" onclick="doFundTopup()">Add to Fund →</button>
    </div>`;
}

window.doLogExpense=async function(){
  const amt=parseInt(document.getElementById("fund-exp-amount")?.value||0);
  const desc=(document.getElementById("fund-exp-desc")?.value||"").trim();
  if(!amt||amt<=0){toast("Enter a valid amount.",2500);return;}
  if(!desc){toast("Enter a description.",2500);return;}
  try{
    await _duesPost("/fund/expense",{amount:amt,description:desc});
    toast(`✅ Expense ₹${amt} logged`,2800);
    document.getElementById("fund-exp-amount").value="";
    document.getElementById("fund-exp-desc").value="";
    await refreshDues();
  }catch(e){toast(e.message||"Failed.",4000);}
};

window.doFundTopup=async function(){
  const amt=parseInt(document.getElementById("fund-top-amount")?.value||0);
  const desc=(document.getElementById("fund-top-desc")?.value||"").trim();
  if(!amt||amt<=0){toast("Enter a valid amount.",2500);return;}
  try{
    await _duesPost("/fund/topup",{amount:amt,description:desc});
    toast(`✅ Fund +₹${amt}`,2800);
    document.getElementById("fund-top-amount").value="";
    document.getElementById("fund-top-desc").value="";
    await refreshDues();
  }catch(e){toast(e.message||"Failed.",4000);}
};

function renderSettingsAdmin(){
  const el=document.getElementById("dues-settings-body");
  if(!el||!_duesSettings)return;
  const vpa=_duesSettings.upi_vpa||"";
  const step=_duesSettings.dues_round_step||10;
  const mode=_duesSettings.dues_self_paid_mode||"auto";
  el.innerHTML=`
    <div class="dues-settings-row">
      <div><div class="dues-settings-label">UPI VPA</div><div class="dues-settings-sub">e.g. name@upi</div></div>
      <input class="dues-settings-input" id="settings-vpa" type="text" value="${esc(vpa)}" placeholder="name@bank"/>
    </div>
    <div class="dues-settings-row">
      <div><div class="dues-settings-label">Round step</div><div class="dues-settings-sub">₹ rounding for per-head</div></div>
      <input class="dues-settings-input" id="settings-step" type="number" min="1" value="${step}"/>
    </div>
    <div class="dues-settings-row">
      <div><div class="dues-settings-label">Self-paid</div><div class="dues-settings-sub">Members can self-report payments</div></div>
      <label class="admin-toggle">
        <input type="checkbox" id="settings-self-paid" ${mode==="auto"?"checked":""} onchange="toggleSelfPaidMode(this.checked)"/>
        <span class="admin-toggle-slider"></span>
      </label>
    </div>
    <button class="btn btn-primary" style="width:100%;padding:10px;border-radius:10px;margin-top:12px" onclick="saveSettings()">Save Settings</button>`;
}

window.saveSettings=async function(){
  const vpa=(document.getElementById("settings-vpa")?.value||"").trim()||null;
  const step=parseInt(document.getElementById("settings-step")?.value||10);
  const body={};
  if(vpa)body.upi_vpa=vpa;
  if(step>0)body.dues_round_step=step;
  try{
    await _duesPatch("/settings",body);
    toast("✅ Settings saved",2500);
    _duesSettings=await _duesGet("/settings");
    renderSettingsAdmin();
    await refreshDues();
  }catch(e){toast(e.message||"Save failed.",4000);}
};

window.toggleSelfPaidMode=async function(enabled){
  try{
    await _duesPatch("/settings",{dues_self_paid_mode:enabled?"auto":"off"});
    if(_duesSettings)_duesSettings.dues_self_paid_mode=enabled?"auto":"off";
    toast(enabled?"Self-paid ON":"Self-paid OFF",2000);
    renderMemberDues();
  }catch(e){
    toast(e.message||"Failed.",4000);
    const tog=document.getElementById("settings-self-paid");
    if(tog)tog.checked=!enabled;
  }
};

window.toggleDuesAdmin=function(){
  const body=document.getElementById("dues-admin-body");
  const btn=document.getElementById("dues-admin-toggle");
  if(!body||!btn)return;
  const hidden=body.classList.toggle("hidden");
  btn.textContent=hidden?"▼":"▲";
};

const _adminSectionOpen={balances:false,close:false,fund:false,settings:false};
window.toggleAdminSection=function(name){
  _adminSectionOpen[name]=!_adminSectionOpen[name];
  const ch=document.getElementById(`${name}-chevron`);
  const bodyMap={balances:"dues-balances-body",close:"dues-close-body",fund:"dues-fund-admin-body",settings:"dues-settings-body"};
  const bodyEl=document.getElementById(bodyMap[name]);
  if(bodyEl)bodyEl.classList.toggle("hidden",!_adminSectionOpen[name]);
  if(ch)ch.textContent=_adminSectionOpen[name]?"▲":"▼";
};

// ── Recurring template schedules (self-serve — no server/API-token needed,
// unlike the separate /admin/ console) ──────────────────────────────────
const WEEKDAYS=["monday","tuesday","wednesday","thursday","friday","saturday","sunday"];
let _templatesScheduleOpen=false, _templatesCache=null, _templatesEditingName=null;

window.toggleTemplatesSchedule=async function(){
  _templatesScheduleOpen=!_templatesScheduleOpen;
  const body=document.getElementById("templates-schedule-body");
  const ch=document.getElementById("templates-chevron");
  if(body)body.classList.toggle("hidden",!_templatesScheduleOpen);
  if(ch)ch.textContent=_templatesScheduleOpen?"▲":"▼";
  if(_templatesScheduleOpen&&!_templatesCache)await loadTemplatesSchedule();
};

async function loadTemplatesSchedule(){
  const body=document.getElementById("templates-schedule-body");
  if(!body||!_idToken)return;
  body.innerHTML='<div class="sched-empty">Loading…</div>';
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/templates?id_token=${encodeURIComponent(_idToken)}`,
      {signal:AbortSignal.timeout(8000)});
    if(!res.ok)throw new Error((await res.json().catch(()=>({}))).detail||"Failed to load templates");
    _templatesCache=await res.json();
    renderTemplatesSchedule();
  }catch(e){
    body.innerHTML=`<div class="sched-empty">${esc(e.message||"Could not load templates")}</div>`;
  }
}

function renderTemplatesSchedule(){
  const body=document.getElementById("templates-schedule-body");
  if(!body)return;
  if(!_templatesCache||!_templatesCache.length){
    body.innerHTML='<div class="sched-empty">No templates yet — create one with /set_template in the group.</div>';
    return;
  }
  body.innerHTML=_templatesCache.map(t=>{
    const enabled=t.schedule_enabled;
    const recLabel={weekly:"weekly",biweekly:"every 2 weeks",monthly:"monthly"}[t.recurrence_type]||t.recurrence_type;
    const when=enabled
      ?(t.recurrence_type==="monthly"
        ?`Day ${esc(t.schedule_day)} of each month at ${esc(t.schedule_time)}`
        :`${esc((t.schedule_day||"").replace(/^./,c=>c.toUpperCase()))} ${esc(t.schedule_time)} (${recLabel})`)
      :"Not scheduled";
    const meta=[t.location,t.fee?`₹${t.fee}`:null,t.limit?`Cap ${t.limit}`:null].filter(Boolean).join(" · ");
    const editing=_templatesEditingName===t.name;
    return `<div class="sched-item" style="flex-direction:column;align-items:stretch">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%">
        <div class="sched-item-info">
          <div class="sched-item-title">${esc(t.title||t.name)}</div>
          <div class="sched-item-time">${when}</div>
          ${meta?`<div class="upcoming-meta">${esc(meta)}</div>`:""}
        </div>
        <div style="display:flex;align-items:center;gap:6px;flex-shrink:0">
          <button class="id-change" title="Start a rollcall from this template now" onclick="startTemplateNow('${esc(t.name)}')">▶️</button>
          <label class="admin-toggle" title="${enabled?'Disable':'Enable'} schedule">
            <input type="checkbox" ${enabled?"checked":""} onchange="toggleTemplateSchedule('${esc(t.name)}',this.checked)"/>
            <span class="admin-toggle-slider"></span>
          </label>
          <button class="id-change" onclick="toggleTemplateEditForm('${esc(t.name)}')">${editing?"✕":"✏️"}</button>
        </div>
      </div>
      ${editing?renderTemplateEditForm(t):""}
    </div>`;
  }).join("");
}

function renderTemplateEditForm(t){
  const isMonthly=t.recurrence_type==="monthly";
  const dayOpts=WEEKDAYS.map(d=>`<option value="${d}" ${t.schedule_day===d?"selected":""}>${d[0].toUpperCase()+d.slice(1)}</option>`).join("");
  const inp=(id,val,ph)=>`<input id="${id}" type="text" placeholder="${ph}" value="${esc(val||"")}" style="flex:1;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.85rem"/>`;
  return `<div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border);display:flex;flex-direction:column;gap:8px">
    <div class="id-prompt-label" style="text-align:left;margin-bottom:0">Details</div>
    ${inp(`tsf-title-${t.name}`,t.title,"Title")}
    <div style="display:flex;gap:8px">
      ${inp(`tsf-location-${t.name}`,t.location,"Location")}
      ${inp(`tsf-fee-${t.name}`,t.fee,"Fee")}
    </div>
    <input id="tsf-limit-${t.name}" type="number" min="1" max="1000" placeholder="Cap (max attendees)" value="${t.limit||""}" style="padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.85rem"/>
    <div class="id-prompt-label" style="text-align:left;margin-bottom:0;margin-top:4px">Schedule</div>
    <div style="display:flex;gap:8px">
      <select id="tsf-rec-${t.name}" onchange="_onTsfRecurrenceChange('${esc(t.name)}')" style="flex:1;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.85rem">
        <option value="weekly" ${t.recurrence_type==="weekly"?"selected":""}>Weekly</option>
        <option value="biweekly" ${t.recurrence_type==="biweekly"?"selected":""}>Every 2 weeks</option>
        <option value="monthly" ${isMonthly?"selected":""}>Monthly</option>
      </select>
    </div>
    <div style="display:flex;gap:8px">
      <select id="tsf-day-${t.name}" style="flex:1;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.85rem;${isMonthly?"display:none":""}">${dayOpts}</select>
      <input id="tsf-monthday-${t.name}" type="number" min="1" max="31" placeholder="Day (1-31)" value="${isMonthly?esc(t.schedule_day||""):""}" style="flex:1;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.85rem;${isMonthly?"":"display:none"}"/>
      <input id="tsf-time-${t.name}" type="time" value="${esc(t.schedule_time||"09:00")}" style="flex:1;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.85rem"/>
    </div>
    <button class="btn btn-primary" style="padding:9px" onclick="saveTemplate('${esc(t.name)}')">💾 Save</button>
  </div>`;
}

window.startTemplateNow=async function(name){
  if(!confirm(`Start a rollcall from "${name}" now?`))return;
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/templates/${encodeURIComponent(name)}/start`,{
      method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id_token:_idToken}),
      signal:AbortSignal.timeout(10000),
    });
    if(!res.ok)throw new Error((await res.json().catch(()=>({}))).detail||"Failed to start rollcall");
    toast(`✅ Started from ${name}`,2500);
    activeTabIdx=0;
    await loadGroup();
  }catch(e){toast(e.message||"Could not start rollcall",4000);}
};

window._onTsfRecurrenceChange=function(name){
  const isMonthly=document.getElementById(`tsf-rec-${name}`).value==="monthly";
  document.getElementById(`tsf-day-${name}`).style.display=isMonthly?"none":"";
  document.getElementById(`tsf-monthday-${name}`).style.display=isMonthly?"":"none";
};

window.toggleTemplateEditForm=function(name){
  _templatesEditingName=_templatesEditingName===name?null:name;
  renderTemplatesSchedule();
};

window.saveTemplate=async function(name){
  // Only push a schedule update if the schedule is already enabled for this
  // template — otherwise saving content-only edits would silently switch a
  // disabled schedule on using whatever defaults happen to sit in the form.
  const current=(_templatesCache||[]).find(t=>t.name===name);
  const scheduleWasEnabled=!!(current&&current.schedule_enabled);

  let scheduleBody=null;
  if(scheduleWasEnabled){
    const recurrence_type=document.getElementById(`tsf-rec-${name}`).value;
    const schedule_time=document.getElementById(`tsf-time-${name}`).value;
    if(!schedule_time){toast("Pick a time first.",2500);return;}
    scheduleBody={id_token:_idToken,recurrence_type,schedule_time};
    if(recurrence_type==="monthly"){
      const md=parseInt(document.getElementById(`tsf-monthday-${name}`).value,10);
      if(!md||md<1||md>31){toast("Enter a day of month (1-31).",2500);return;}
      scheduleBody.monthly_day=md;
    }else{
      scheduleBody.schedule_day=document.getElementById(`tsf-day-${name}`).value;
    }
  }
  const contentBody={
    id_token:_idToken,
    title:document.getElementById(`tsf-title-${name}`).value||null,
    location:document.getElementById(`tsf-location-${name}`).value||null,
    fee:document.getElementById(`tsf-fee-${name}`).value||null,
  };
  const limitVal=document.getElementById(`tsf-limit-${name}`).value;
  contentBody.limit=limitVal?parseInt(limitVal,10):null;

  try{
    // Content first, then schedule (only when already enabled) — either can
    // fail independently; both errors surface clearly rather than silently
    // only saving one half.
    const contentRes=await fetch(`/api/v1/web/group/${URL_TOKEN}/templates/${encodeURIComponent(name)}`,{
      method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(contentBody),
      signal:AbortSignal.timeout(8000),
    });
    if(!contentRes.ok)throw new Error((await contentRes.json().catch(()=>({}))).detail||"Failed to save template details");
    let updated=await contentRes.json();

    if(scheduleBody){
      const schedRes=await fetch(`/api/v1/web/group/${URL_TOKEN}/templates/${encodeURIComponent(name)}/schedule`,{
        method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(scheduleBody),
        signal:AbortSignal.timeout(8000),
      });
      if(!schedRes.ok)throw new Error((await schedRes.json().catch(()=>({}))).detail||"Details saved, but schedule failed to save");
      updated=await schedRes.json();
    }

    _templatesCache=_templatesCache.map(t=>t.name===name?updated:t);
    _templatesEditingName=null;
    renderTemplatesSchedule();
    toast(`💾 Saved ${name}`,2500);
  }catch(e){toast(e.message||"Could not save template",4000);}
};

window.toggleTemplateSchedule=async function(name,enabled){
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/templates/${encodeURIComponent(name)}/schedule/${enabled?"enable":"disable"}`,{
      method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id_token:_idToken}),
      signal:AbortSignal.timeout(8000),
    });
    if(!res.ok)throw new Error((await res.json().catch(()=>({}))).detail||"Failed to update schedule");
    const updated=await res.json();
    _templatesCache=_templatesCache.map(t=>t.name===name?updated:t);
    renderTemplatesSchedule();
    toast(enabled?`🟢 ${name} enabled`:`🔴 ${name} disabled`,2000);
  }catch(e){
    toast(e.message||"Could not update schedule",4000);
    renderTemplatesSchedule(); // revert the toggle's optimistic UI state
  }
};

async function refreshDues(){
  if(!_idToken)return;
  try{
    _duesMemberData=await _duesGet("/my");
    renderMemberDues();
    if(_isWebAdmin){
      [_duesSummaryData,_duesPreviewData]=await Promise.all([
        _duesGet("/summary"),
        _duesGet("/close-preview").catch(()=>null),
      ]);
      renderBalancesTable();
      renderCloseGame();
      renderFundAdmin();
    }
  }catch(e){console.warn("refreshDues:",e.message);}
}

// ── Modal helper ──────────────────────────────────────────────────────────
// opts: { confirmLabel?: string, hideInput?: bool }
function _showDuesModal(title,sublabel,defaultVal,onConfirm,opts={}){
  let m=document.getElementById("dues-confirm-modal");
  if(!m){
    m=document.createElement("div");m.id="dues-confirm-modal";
    m.innerHTML=`<div class="dues-modal-sheet">
      <div class="dues-modal-title" id="dm-title"></div>
      <div class="dues-modal-sub" id="dm-sub"></div>
      <input class="dues-modal-input" id="dm-input" type="number" min="1"/>
      <div class="dues-modal-btns">
        <button class="btn btn-secondary" onclick="document.getElementById('dues-confirm-modal').classList.add('hidden')">Cancel</button>
        <button class="btn btn-primary" id="dm-confirm-btn">Confirm</button>
      </div>
    </div>`;
    document.body.appendChild(m);
  }
  document.getElementById("dm-title").textContent=title;
  document.getElementById("dm-sub").textContent=sublabel;
  const inp=document.getElementById("dm-input");
  const hideInput=!!opts.hideInput;
  inp.classList.toggle("hidden",hideInput);
  if(!hideInput){inp.value=defaultVal;inp.focus();}
  const confirmLabel=opts.confirmLabel||"Confirm";
  const btn=document.getElementById("dm-confirm-btn");
  btn.textContent=confirmLabel;
  m.classList.remove("hidden");
  if(!hideInput)setTimeout(()=>inp.focus(),50);
  btn.onclick=async()=>{
    btn.disabled=true;btn.textContent="…";
    try{await onConfirm(hideInput?null:inp.value);m.classList.add("hidden");}
    catch(e){toast(e.message||"Failed.",4000);btn.disabled=false;btn.textContent=confirmLabel;}
  };
}

// ── Entry point ────────────────────────────────────────────────────────────
if(URL_TOKEN&&(URL_MODE==="join"||URL_MODE==="group")){
  load();
}else{
  // No token in URL — show home screen
  renderHomeScreen();
}
})();
