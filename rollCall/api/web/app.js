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

// Admin-issued weblogin redirect: ?weblogin_code=<code> lands here after the
// server peeks (not consumes) a single-use admin-issued token just to find
// which group to redirect to (GET /auth/weblogin/{token} in auth.py). The
// code itself — not the final id_token — is what's in this URL, since a
// plain browser navigation can't carry the X-Identity-Token header every
// other identity-bearing request now uses. Strip it from the URL immediately
// (so it isn't bookmarked/shared) and POST-redeem it for the real id_token,
// which arrives in a JSON response body, never a URL. Blocks the entry
// point below (via _weblogInRedeemPromise) so the rest of the page doesn't
// start loading with a stale/absent identity while this is in flight.
let _weblogInRedeemPromise=null;
(function(){
  try{
    const p=new URLSearchParams(window.location.search);
    const code=p.get("weblogin_code");
    if(code){
      p.delete("weblogin_code");
      const qs=p.toString();
      const clean=window.location.pathname+(qs?"?"+qs:"");
      history.replaceState(null,"",clean);
      _weblogInRedeemPromise=fetch("/api/v1/auth/weblogin/redeem",{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({token:code}),
        signal:AbortSignal.timeout(10000),
      }).then(async r=>{
        if(!r.ok)throw new Error((await r.json().catch(()=>({}))).detail||"Login link redemption failed");
        const data=await r.json();
        localStorage.setItem(LS_ID_TOKEN,data.id_token);
        _idToken=data.id_token;
      }).catch(e=>{
        console.warn("weblogin redeem failed:",e.message);
      });
    }
  }catch(_){}
})();

// Admin-issued guest voting link: ?guest=<name> pre-fills and saves a guest
// display name. This is a convenience deep link, NOT an identity grant —
// guest voting is already open to anyone who types any name on this page
// with no login of any kind, so this can't escalate access; it only saves
// a proxy/non-Telegram member the trouble of typing their own name. Never
// overwrites a name already set for this browser.
(function(){
  try{
    const p=new URLSearchParams(window.location.search);
    const g=p.get("guest");
    if(g&&!localStorage.getItem(LS_NAME)&&!localStorage.getItem(LS_NAME_OVERRIDE)){
      localStorage.setItem(LS_NAME,g.slice(0,64));
      p.delete("guest");
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

// Sign-in dialog. The rows it shows are BORROWED from the identity card, in
// card order — moved, never cloned, because two copies of #name-input would
// make every getElementById a coin toss (the same reason #acct-wrap is moved
// between headers rather than duplicated). _signinHome records where each row
// came from, and doubles as the "dialog is open" flag; it's declared up here
// with the other page state because renderIdentity() reads it.
const SIGNIN_ROWS=["identity-picker-row","name-input-row","tg-deeplink-row","tg-widget-wrap"];
let _signinHome=null;   // [{el, parent, next}] — where each row came from

// ── DOM ────────────────────────────────────────────────────────────────────
function $(x){return document.getElementById(x)}

// Native confirm() is not universally available. Telegram's in-app webview
// (and some iOS webviews) neuter it: it returns undefined without ever
// showing a dialog, so `if(!await _confirmAction(...))return;` silently bails and the
// button looks dead. That is every destructive action in this app — sign
// out, end rollcall, delete template, merge identity — and it only became
// reachable inside Telegram once the Mini App header was restored.
//
// Prefer Telegram's own dialog when we're in the Mini App; fall back to
// confirm(); and if confirm() answers `undefined` (i.e. it never asked),
// treat that as "go ahead" — the user pressed the button, and no dialog
// was ever shown for them to cancel. A real Cancel returns false, which is
// distinguishable from undefined.
function _confirmAction(msg){
  return new Promise(resolve=>{
    const w=window.Telegram&&window.Telegram.WebApp;
    if(w&&typeof w.showConfirm==="function"){
      try{w.showConfirm(msg,ok=>resolve(!!ok));return;}catch(_){/* fall through */}
    }
    let r;
    try{r=window.confirm(msg);}catch(_){r=undefined;}
    resolve(r===undefined?true:!!r);
  });
}

function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
// For interpolating free-text (e.g. a template name) inside a single-quoted
// JS string literal that itself sits inside an inline onclick="..." HTML
// attribute. esc() alone only protects the HTML-attribute layer (&<>") —
// it leaves ' and \ untouched, so a name containing an apostrophe breaks
// out of the JS string once the browser HTML-decodes the attribute and
// hands the result to the JS parser. Apply this FIRST, then esc() the
// result: escJsAttr(name) escapes \ and ' for the JS-string layer, then
// esc() escapes &<>" for the HTML-attribute layer around it.
function escJsAttr(s){return String(s||"").replace(/\\/g,"\\\\").replace(/'/g,"\\'")}
// Relative-time label for a sqlite "YYYY-MM-DD HH:MM:SS" timestamp (as
// returned by db.get_proxy_name_activity) — used by the merge-identities
// review list's recency badge.
function _relTime(ts){
  if(!ts)return"";
  const d=new Date(String(ts).replace(" ","T")+"Z");
  if(isNaN(d))return"";
  const days=Math.floor((Date.now()-d.getTime())/86400000);
  if(days<1)return"today";
  if(days<2)return"1d ago";
  if(days<30)return`${days}d ago`;
  const months=Math.floor(days/30);
  if(months<12)return`${months}mo ago`;
  return`${Math.floor(months/12)}y ago`;
}

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
    // One label, one destination. This button used to read "🔒 Locked" at 55%
    // opacity for verified users — a state, not an action, styled as if
    // disabled — and it was the only route to signing out. Now it opens the
    // same account menu the header chip does, so identity actions exist in
    // exactly one place regardless of where you reach them from.
    const changeBtn=$("name-change-btn");
    if(changeBtn){
      changeBtn.textContent="Manage ▾";
      changeBtn.style.opacity="";
      changeBtn.title="Change name, or sign out";
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
      // Group mode with no identity: one button, which opens the dialog.
      // The chooser itself is shown there, not here.
      if(!_signinHome)$("signin-prompt-row").classList.remove("hidden");
      $("identity-picker-row").classList.add("hidden");
      $("name-input-row").classList.add("hidden");
    }else{
      // Join link or Mini App without a name yet: go straight to name input.
      // This is "what should we call you", not a sign-in, so it stays inline.
      $("signin-prompt-row").classList.add("hidden");
      $("identity-picker-row").classList.add("hidden");
      $("name-input-row").classList.remove("hidden");
    }
  }
  if(currentName)$("signin-prompt-row").classList.add("hidden");
  // Login Widget: same rule as the "Verify with Telegram" deep-link option
  // above (needsVerify) — worth loading whenever verifying would still help.
  // Note this only ever HIDES the block; revealing it is _loadLoginWidget's
  // call, and it makes that call only once Telegram has rendered a real
  // button. Unhiding from here is what produced a labelled empty gap (or a
  // frame full of Telegram's own error text) on deployments where the widget
  // was never set up.
  const widgetWrap=$("tg-widget-wrap");
  if(widgetWrap){
    const showWidget=IS_GROUP&&!TG_NAME&&!_verifiedUserId;
    if(showWidget)_loadLoginWidget();
    else widgetWrap.classList.add("hidden");
  }
  // While the sign-in dialog has these rows on loan, a background re-render
  // must not leave it empty — but it must also not drag the user back to the
  // chooser after they've tapped Guest, so only the both-hidden case is
  // rescued. The inline prompt stays hidden meanwhile: the dialog IS the
  // prompt while it's open.
  if(_signinHome){
    $("signin-prompt-row").classList.add("hidden");
    const p=$("identity-picker-row"),n=$("name-input-row");
    if(p&&n&&p.classList.contains("hidden")&&n.classList.contains("hidden")){
      p.classList.remove("hidden");
    }
  }
  _syncIdentityCardVisibility();
  // Header chip mirrors whatever the identity card just decided, so the two
  // can never disagree about who you are.
  renderAcctControl();
}

// The identity card is a container for four rows that are each independently
// hidden — and while the sign-in dialog has them on loan it holds none of
// them. An empty card still paints its border and padding, so the page showed
// a blank rounded box above the vote buttons. Hide the card when there's
// nothing in it to show.
function _syncIdentityCardVisibility(){
  const card=$("identity-card");
  if(!card)return;
  // With no rollcall there is nothing to identify yourself FOR, so the strip
  // is just another line of text in a column whose whole job is to say
  // "nothing is on right now".
  if(IS_GROUP&&!activeRcData&&!_signinHome){card.classList.add("hidden");return;}
  const anyVisible=[...card.children].some(el=>!el.classList.contains("hidden"));
  card.classList.toggle("hidden",!anyVisible);
}

$("name-save-btn").addEventListener("click",saveName);
$("name-input").addEventListener("keydown",e=>{if(e.key==="Enter")saveName()});
// The identity strip's Manage button and the header chip open the same menu.
$("name-change-btn").addEventListener("click",e=>{
  e.stopPropagation();
  openAcctMenu();
});

// The actual change-name flow, now reached from that menu rather than from a
// button whose label described a state.
async function _doChangeName(){
  if(TG_NAME&&_idToken){
    // Inside Telegram Mini App: name is set by Telegram and cannot be changed
    // while the user is authenticated. There's no local override possible.
    toast("Your name is set by Telegram and cannot be changed here.",3500);
    return;
  }
  if(_verifiedUserId){
    const ok=await _confirmAction("Changing your name will unlink your Telegram verification.\nYou can re-verify after setting a new name.");
    if(!ok)return;
    _clearStoredIdentity();
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
}

function saveName(){
  const val=$("name-input").value.trim();if(!val){$("name-input").focus();return;}
  currentName=val.slice(0,64);
  if(TG_NAME)localStorage.setItem(LS_NAME_OVERRIDE,currentName);
  else localStorage.setItem(LS_NAME,currentName);
  // Naming yourself IS the guest sign-in, so the dialog's job is done.
  if(_signinHome){closeSignIn();return;}
  renderIdentity();detectCurrentVote();
  if(IS_GROUP)loadWebStats();
}

// ── Account control (header) ───────────────────────────────────────────────
// One always-visible place showing who you are, with an unambiguous Sign out.
// The previous sign-out was a "🔒 Locked" button at 55% opacity inside the
// identity card — labelled with a state rather than an action, styled as if
// disabled, and reachable only by guessing it was clickable.

// Drops every stored credential and identity hint. Shared by Sign out and by
// the identity card's Change-name path so the two can't drift apart.
function _clearStoredIdentity(){
  _verifiedUserId=null;_verifiedName=null;_verifiedUsername=null;_idToken=null;
  localStorage.removeItem(LS_TG_USER_ID);
  localStorage.removeItem(LS_TG_NAME);
  localStorage.removeItem(LS_TG_USERNAME);
  localStorage.removeItem(LS_ID_TOKEN);
  _stopVerifyPoll();
}

window.signOut=async function(){
  if(!await _confirmAction("Sign out of RollCall on this device?\n\nYour votes stay on the group — you'll just need to sign in again to vote or manage the group."))return;
  _clearStoredIdentity();
  currentName="";
  localStorage.removeItem(LS_NAME);
  localStorage.removeItem(LS_NAME_OVERRIDE);
  closeAcctMenu();
  // Admin state is derived from identity, so it has to go with it — otherwise
  // the admin card stays on screen for a signed-out user until a reload.
  _isWebAdmin=false;
  const ac=document.getElementById("admin-card");if(ac)ac.classList.add("hidden");
  const dac=document.getElementById("dues-admin-card");if(dac)dac.classList.add("hidden");
  const nb=document.getElementById("admin-nav-btn");if(nb)nb.classList.add("hidden");
  const ntr=document.getElementById("name-tag-row");if(ntr)ntr.classList.add("hidden");
  if(IS_GROUP&&!TG_NAME){
    const pr=document.getElementById("identity-picker-row");if(pr)pr.classList.remove("hidden");
    const nir=document.getElementById("name-input-row");if(nir)nir.classList.add("hidden");
  }
  renderIdentity();
  if(typeof detectCurrentVote==="function")detectCurrentVote();
  toast("Signed out",2500);
};

window.toggleAcctMenu=function(ev){
  if(ev)ev.stopPropagation();
  const menu=document.getElementById("acct-menu");
  if(!menu)return;
  if(menu.classList.contains("hidden"))openAcctMenu();
  else closeAcctMenu();
};

function openAcctMenu(){
  const menu=document.getElementById("acct-menu");
  if(!menu)return;
  renderAcctMenu();
  menu.classList.remove("hidden");
  const btn=document.getElementById("acct-btn");
  if(btn)btn.setAttribute("aria-expanded","true");
  if(!document.getElementById("acct-backdrop")){
    const bd=document.createElement("div");
    bd.id="acct-backdrop";bd.className="acct-backdrop";
    bd.addEventListener("click",closeAcctMenu);
    // Into the menu's OWN parent, not document.body. z-index only orders
    // elements within one stacking context, and .brand-bar is
    // position:sticky with z-index:100 — which makes it a context. The menu's
    // z-index:300 is therefore compared against its siblings inside the bar,
    // while the bar as a whole sits at 100; a backdrop appended to <body> at
    // 299 outranks the entire bar and painted over the menu. Every item was
    // then unclickable: the click landed on the backdrop, which closed the
    // menu and did nothing else, so Sign out looked broken (and so did Sign
    // in, Change name and Admin controls). Scripted .click() calls don't
    // hit-test, which is why this survived the browser checks — see the
    // elementFromPoint assertion in web_layout_check.js.
    (menu.parentNode||document.body).appendChild(bd);
  }
}

window.closeAcctMenu=function(){
  const menu=document.getElementById("acct-menu");
  if(menu)menu.classList.add("hidden");
  const btn=document.getElementById("acct-btn");
  if(btn)btn.setAttribute("aria-expanded","false");
  const bd=document.getElementById("acct-backdrop");
  if(bd)bd.remove();
};

document.addEventListener("keydown",e=>{
  if(e.key!=="Escape")return;
  closeAcctMenu();
  if(_signinHome)closeSignIn();
});

// Three states, deliberately distinct: Telegram-verified, guest (a name but no
// proof), and nothing at all.
function renderAcctMenu(){
  const menu=document.getElementById("acct-menu");
  if(!menu)return;
  const verified=!!(_verifiedUserId||(TG_NAME&&_idToken));
  const who=_verifiedName||currentName||TG_NAME||"";
  let html="";
  if(who){
    html+=`<div class="acct-menu-hdr">
      <div class="acct-menu-name">${esc(who)}</div>
      <div class="acct-menu-sub">${verified?"✅ Telegram verified":"👤 Guest on this device"}</div>
    </div>`;
  }
  if(!verified){
    // openSignIn(), not startTgVerify() directly: one entry point that offers
    // every method this page supports, so "sign in" means the same thing
    // wherever it's clicked.
    //
    // Two different situations, two different words. Offering "Sign in" to
    // someone whose name is already on the page reads as though the app
    // forgot them — they HAVE signed in, as a guest. What they can still do
    // is prove it's them, which is an upgrade, not a login.
    html+=who
      ? `<button class="acct-menu-item" role="menuitem" onclick="closeAcctMenu();openSignIn()">✈ Verify with Telegram</button>`
      : `<button class="acct-menu-item" role="menuitem" onclick="closeAcctMenu();openSignIn()">✈ Sign in</button>`;
  }
  if(who&&!TG_NAME){
    html+=`<button class="acct-menu-item" role="menuitem" onclick="closeAcctMenu();acctChangeName()">✎ Change name</button>`;
  }
  if(_isWebAdmin){
    html+=`<button class="acct-menu-item" role="menuitem" onclick="closeAcctMenu();toggleAdminPanel()">⚙ Admin controls</button>`;
  }
  // On a phone the header hides Help and Home to leave room for 44px touch
  // targets, so the menu has to carry them — otherwise they are simply gone.
  if(window.matchMedia&&window.matchMedia("(pointer:coarse)").matches){
    html+=`<button class="acct-menu-item" role="menuitem" onclick="closeAcctMenu();window.location.href='/web/'">🏠 All your groups</button>`;
    html+=`<button class="acct-menu-item" role="menuitem" onclick="closeAcctMenu();window.location.href='/help'">❓ Command reference</button>`;
  }
  if(who||_idToken){
    html+=`<button class="acct-menu-item danger" role="menuitem" onclick="signOut()">⏻ Sign out</button>`;
  }
  if(!html)html=`<div class="acct-menu-hdr"><div class="acct-menu-sub">Not signed in</div></div>`;
  menu.innerHTML=html;
}

// Calls the flow directly. It must NOT click #name-change-btn any more —
// that button now opens this very menu, so clicking it from a menu item
// would just reopen the menu instead of changing anything.
window.acctChangeName=function(){_doChangeName();};

// The single sign-in entry point. Every "sign in" affordance on the page
// routes here — the header menu, the signed-out admin note, and the identity
// card itself — so they can't drift into offering different methods or
// different wording. Reveals the chooser (Telegram / Guest, plus the QR
// widget when Telegram isn't on this device) rather than committing the user
// to one method, which is what the bare startTgVerify() calls used to do.
window.openSignIn=function(){
  closeAcctMenu();
  if(_verifiedUserId||(TG_NAME&&_idToken)){
    toast("You're already signed in",2200);
    return;
  }
  const modal=document.getElementById("signin-modal");
  const body=document.getElementById("signin-body");
  if(!modal||!body){return;}
  if(_signinHome)return;                       // already open
  _signinHome=[];
  SIGNIN_ROWS.forEach(id=>{
    const el=document.getElementById(id);
    if(!el)return;
    _signinHome.push({el,parent:el.parentNode,next:el.nextSibling});
    body.appendChild(el);
  });
  // Start on the chooser; the guest name input is one tap away from there.
  // The inline prompt is what opened this, so it stands down while it's up.
  const prompt=document.getElementById("signin-prompt-row");
  if(prompt)prompt.classList.add("hidden");
  const picker=document.getElementById("identity-picker-row");
  const nameRow=document.getElementById("name-input-row");
  if(picker)picker.classList.remove("hidden");
  if(nameRow)nameRow.classList.add("hidden");
  // The Login Widget is the answer to "Telegram isn't on this device", so it
  // belongs with the other choices. Load it, but leave the reveal to
  // _loadLoginWidget — it only shows the block once there's a working button
  // in it, so an unconfigured deployment shows two clean choices, not three
  // with one broken.
  if(typeof _loadLoginWidget==="function")_loadLoginWidget();
  _syncIdentityCardVisibility();
  modal.classList.remove("hidden");
  document.body.classList.add("modal-open");
  const first=document.getElementById("picker-tg-btn");
  if(first)first.focus();
};

// Puts every borrowed row back where it came from, then lets renderIdentity()
// decide what the identity card should show — so closing the dialog can't
// leave the inline strip in a state the page never chose.
window.closeSignIn=function(){
  const modal=document.getElementById("signin-modal");
  if(modal)modal.classList.add("hidden");
  document.body.classList.remove("modal-open");
  if(_signinHome){
    _signinHome.forEach(({el,parent,next})=>{
      if(parent)parent.insertBefore(el,next);
    });
    _signinHome=null;
  }
  renderIdentity();
};

// Keeps the header chip in sync with whatever identity render just happened.
function renderAcctControl(){
  const av=document.getElementById("acct-av");
  const label=document.getElementById("acct-label");
  if(!av||!label)return;
  const verified=!!(_verifiedUserId||(TG_NAME&&_idToken));
  const who=_verifiedName||currentName||TG_NAME||"";
  if(who){
    av.textContent=(who[0]||"?").toUpperCase();
    av.style.background=typeof avColor==="function"?avColor(who):"";
    label.textContent=who.length>12?who.slice(0,11)+"…":who;
    const btn=document.getElementById("acct-btn");
    if(btn)btn.title=verified?`Signed in as ${who} (Telegram verified)`:`${who} — guest on this device`;
  }else{
    av.textContent="?";
    av.style.background="";
    label.textContent="Sign in";
    const btn=document.getElementById("acct-btn");
    if(btn)btn.title="Sign in";
  }
  // Re-render an open menu in place so it can't show stale state.
  const menu=document.getElementById("acct-menu");
  if(menu&&!menu.classList.contains("hidden"))renderAcctMenu();
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

// Same idea for the templates/schedules list's countdown pills — only
// re-renders while that section is actually open, and only touches its
// own DOM subtree (no lists/vote state involved there either).
setInterval(()=>{
  if(_templatesScheduleOpen&&_templatesCache)renderTemplatesSchedule();
},60000);

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
  $("refresh-bar-wrap")?.classList.remove("hidden");
  $("rc-card")?.classList.remove("hidden");
  $("identity-card").classList.remove("hidden");
  // ...but only if it has a row to show — see _syncIdentityCardVisibility.
  _syncIdentityCardVisibility();
  $("vote-card").classList.remove("hidden");
  $("lists-card").classList.remove("hidden");
  _syncAdminRcControls();
  detectCurrentVote();renderLists();
}

// Admin-only per-row controls (move to another list / remove) — web parity
// for the admin console's Rollcalls-tab actions. "waiting" has no manual
// move target (it's a computed overflow list, same as the admin console).
function _rowAdminActs(name,statusKey){
  const moveOpts=["in","out","maybe"].filter(s=>s!==statusKey)
    .map(s=>`<option value="${s}">${s.toUpperCase()}</option>`).join("");
  return `<span class="li-admin-acts" data-name="${esc(name)}">
    <select class="li-move-sel" title="Move to another list">
      <option value="">Move→</option>${moveOpts}
    </select>
    <button type="button" class="li-remove-btn" title="Remove">✕</button>
  </span>`;
}

function _wireRowAdminActs(){
  document.querySelectorAll("#lists-container .li-admin-acts").forEach(el=>{
    const name=el.dataset.name;
    const sel=el.querySelector(".li-move-sel");
    const delBtn=el.querySelector(".li-remove-btn");
    sel.addEventListener("change",()=>{
      if(!sel.value)return;
      doMoveUser(name,sel.value);
      sel.value="";
    });
    delBtn.addEventListener("click",async()=>{
      if(!await _confirmAction(`Remove ${name} from this rollcall?`))return;
      doRemoveUser(name);
    });
  });
}

window.doMoveUser=async function(name,newStatus){
  if(!_idToken){toast("Verify with Telegram first.",3000);return;}
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/rollcalls/move-user`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id_token:_idToken,rollcall_num:activeTabIdx+1,name,new_status:newStatus}),
      signal:AbortSignal.timeout(8000),
    });
    if(!res.ok){const d=await res.json().catch(()=>({}));throw new Error(d.detail||"Failed to move");}
    const updated=await res.json();
    activeRcData=updated;
    if(IS_GROUP&&groupData)groupData.rollcalls[activeTabIdx]=updated;
    toast(`Moved ${name} to ${newStatus.toUpperCase()}`,2200);
    renderRollcall(updated);
  }catch(e){
    toast(e.message||"Could not move user",3500);
  }
};

window.doRemoveUser=async function(name){
  if(!_idToken){toast("Verify with Telegram first.",3000);return;}
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/rollcalls/remove-user`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id_token:_idToken,rollcall_num:activeTabIdx+1,name}),
      signal:AbortSignal.timeout(8000),
    });
    if(!res.ok){const d=await res.json().catch(()=>({}));throw new Error(d.detail||"Failed to remove");}
    const updated=await res.json();
    activeRcData=updated;
    if(IS_GROUP&&groupData)groupData.rollcalls[activeTabIdx]=updated;
    toast(`Removed ${name}`,2200);
    renderRollcall(updated);
  }catch(e){
    toast(e.message||"Could not remove user",3500);
  }
};

function renderLists(){
  if(!activeRcData)return;
  const{in:inL,out:outL,maybe:maybeL,waiting:waitL}=activeRcData;
  function section(label,cls,items,statusKey){
    const rows=items.length?items.map((u,i)=>{
      const isYou=currentName&&u.name.toLowerCase()===currentName.toLowerCase();
      const av=`<span class="av" style="background:${avColor(u.name)}">${(u.name[0]||"?").toUpperCase()}</span>`;
      const cm=u.comment?`<span class="li-comment">— ${esc(u.comment)}</span>`:"";
      const tgDot=u.is_proxy===false?'<span class="tg-dot" title="Telegram user"></span>':"";
      const adminActs=(_isWebAdmin&&statusKey!=="waiting")?_rowAdminActs(u.name,statusKey):"";
      return `<li class="${isYou?"you":""}">
        <span class="li-pos">${i+1}</span>${av}
        <span class="li-name">${esc(u.name)}${tgDot}</span>${cm}
        ${adminActs}
      </li>`;
    }).join(""):"";
    return`<div class="list-sect">
      <div class="list-lbl ${cls}">${label}<span class="list-cnt">(${items.length})</span></div>
      ${items.length?`<ul class="list-items">${rows}</ul>`:'<p class="empty" style="margin:0;padding:2px 0">—</p>'}
    </div>`;
  }
  const html=section("IN","in",inL,"in")+section("OUT","out",outL,"out")+section("MAYBE","maybe",maybeL,"maybe")+(waitL.length?section("WAIT","wait",waitL,"waiting"):"");
  $("lists-container").innerHTML=html||'<p class="empty">No votes yet.</p>';
  if(_isWebAdmin)_wireRowAdminActs();
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
// A one-time entry (Schedule -> Once) carries an exact scheduled_at instead
// of a recurring schedule_day/schedule_time — use it directly rather than
// computing a "next occurrence," which only makes sense for something that
// repeats.
function _upcomingEffectiveDate(u){
  if(u.scheduled_at){const d=new Date(u.scheduled_at);return isNaN(d)?null:d;}
  return nextScheduledDate(u.schedule_day,u.schedule_time);
}
function renderUpcoming(upcoming){
  const el=$("upcoming-card");
  if(!el)return;
  const thisWeek=(upcoming||[]).filter(u=>{
    const d=_upcomingEffectiveDate(u);
    return d&&(d-new Date())<=7*24*60*60*1000;
  }).sort((a,b)=>{
    const da=_upcomingEffectiveDate(a);
    const db=_upcomingEffectiveDate(b);
    return (da||0)-(db||0);
  });
  if(!thisWeek.length){el.classList.add("hidden");return;}
  el.classList.remove("hidden");
  // Two distinct things were previously shown as one unlabeled date/time:
  // schedule_day/time is when the rollcall AUTO-OPENS (recurring); event_
  // day/time is when the game itself happens (Event Details). Label both
  // explicitly instead of only showing the open time and calling it done.
  el.innerHTML=`<div class="upcoming-header">📅 Upcoming Rollcalls</div>`
    +thisWeek.map(u=>{
      const d=_upcomingEffectiveDate(u);
      const dateStr=d?d.toLocaleDateString(undefined,{weekday:"short",month:"short",day:"numeric"}):"";
      const timeStr=d?d.toLocaleTimeString(undefined,{hour:"2-digit",minute:"2-digit"}):"";
      const title=u.title||u.name;
      const isOnce=!!u.scheduled_at;
      const hasEvent=u.event_day&&u.event_time;
      const eventStr=hasEvent
        ?`🏟 Event: ${u.event_day[0].toUpperCase()+u.event_day.slice(1)} ${u.event_time}`
        :"";
      const meta=[u.location?`📍 ${u.location}`:"",u.fee?`💰 ${u.fee}`:"",u.limit?`👥 Cap: ${u.limit}`:""].filter(Boolean).join(" · ");
      return `<div class="upcoming-row">
        <div class="upcoming-when" title="Opens (auto-starts)"><span class="upcoming-day">${dateStr}</span><span class="upcoming-time">${timeStr}</span></div>
        <div class="upcoming-info">
          <div class="upcoming-title">${title}</div>
          <div class="upcoming-opens-lbl">${isOnce?"Opens for voting (one-time)":"Opens for voting"}</div>
          ${eventStr?`<div class="upcoming-event">${eventStr}</div>`:""}
          ${meta?`<div class="upcoming-meta">${meta}</div>`:""}
        </div>
      </div>`;
    }).join("");
}
async function loadGroup(){
  const res=await fetch(API_GROUP);
  if(!res.ok){const d=await res.json().catch(()=>({}));throw new Error(d.detail||"This group link is invalid.");}
  groupData=await res.json();
  renderGroup();
}

// Everything loadGroup() does once the data is in hand. Split out so the
// page can be re-rendered from a known groupData without a round trip —
// which is also the only way a headless check can exercise states the
// fixtures don't happen to produce, like "no rollcall running".
function renderGroup(){
  if(!groupData)return;
  const rcs=groupData.rollcalls||[];
  // Persist this group in recents + update page title
  const gname=groupData.group_name||"RollCall Group";
  _saveGroup(URL_TOKEN,gname);
  if(gname)document.title=`RollCall — ${gname}`;
  renderUpcoming(groupData.upcoming||[]);
  if(!rcs.length){
    ["rc-title","rc-meta","count-badge"].forEach(id=>{$(id)&&($(id).textContent="")});
    $("tab-card").classList.add("hidden");
    // The rollcall header card is part of the "there is a game" story; with
    // no game it was an empty bordered box above an empty column.
    $("rc-card")?.classList.add("hidden");
    $("no-rollcalls").classList.remove("hidden");
    ["identity-card","vote-card","lists-card"].forEach(id=>$(id)?.classList.add("hidden"));
    activeRcData=null;
    // Nothing is counting down, so the refresh bar is a progress indicator
    // for no progress — and the identity strip asks "who's voting?" about a
    // vote that doesn't exist. The empty state carries the column alone.
    $("refresh-bar-wrap")?.classList.add("hidden");
    renderEmptyState();
    _syncAdminRcControls();
  }else if(rcs.length===1){$("tab-card").classList.add("hidden");renderRollcall(rcs[0]);}
  else{$("tab-card").classList.remove("hidden");if(activeTabIdx>=rcs.length)activeTabIdx=0;renderTabs(rcs);renderRollcall(rcs[activeTabIdx]);}
  _syncViewTabs();
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
    // #main, not bookmark-card's parent: since the two-column layout landed
    // that parent is #col-side, which on desktop is a sticky sidebar — this
    // is a page footer and belongs across the bottom of both columns
    // (grid-column:1/-1 in the desktop block of style.css).
    const container=document.getElementById("main");
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
  // Identity is proven via the X-Identity-Token header (never a raw
  // user_id in the URL) so the server can verify who is requesting
  // personal stats and prevent IDOR — name stays a query param since it's
  // not sensitive, only used as an unverified fallback when no id_token.
  if(!_idToken&&currentName)params.set("name",currentName);
  const url=`/api/v1/web/group/${URL_TOKEN}/stats${params.size?"?"+params:""}`;
  try{
    const res=await fetch(url,{headers:_idToken?{"X-Identity-Token":_idToken}:{},signal:AbortSignal.timeout(8000)});
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

  // Group session-turnout trend from recent_history: whole-group total IN
  // count per recent session (oldest→newest) — NOT personal data, so it's
  // labeled and placed under Group Stats, distinct from the "You" block's
  // own per-session sparkline below.
  const histArr=(d.recent_history||[]).slice().reverse();
  const maxIn=histArr.length?Math.max(...histArr.map(h=>h.in_count||0),1):1;
  const trendHtml=histArr.length>=2?`
  <div class="sp-trend-label">📈 Recent group turnout</div>
  <div class="sp-trend">
    ${histArr.map(h=>{
      const barH=Math.round((h.in_count||0)/maxIn*70)+10;
      const label=(h.ended_at||'').slice(5,10)||'';
      return`<div class="sp-tbar-wrap" title="${esc(h.title||'')} · ${h.in_count} total IN">
        <div class="sp-tbar-val">${h.in_count}</div>
        <div class="sp-tbar" style="height:${barH}%"></div>
        <div class="sp-tbar-lbl">${esc(label)}</div>
      </div>`;
    }).join('')}
  </div>`:'';

  // Session history — full list (title, date, in/out/maybe), web parity for
  // the admin console's Stats tab. Newest-first, same order the backend
  // returns (the trend chart above reverses its own copy for oldest→newest
  // bars; this list intentionally doesn't).
  const shistRows=(d.recent_history||[]).map(h=>{
    const date=(h.ended_at||"").slice(0,10);
    return `<div class="shist-row">
      <span class="shist-title" title="${esc(h.title||"Untitled")}">${esc(h.title||"Untitled")}</span>
      <span class="shist-date">${esc(date)}</span>
      <span class="shist-pills">
        <span class="sp-pill sp-in">${n(h.in_count)}</span>
        <span class="sp-pill sp-out">${n(h.out_count)}</span>
        ${h.maybe_count?`<span class="sp-pill sp-maybe">${n(h.maybe_count)}</span>`:""}
      </span>
    </div>`;
  }).join("");

  // Ghost leaderboard — who's ghosted (voted IN, didn't show), most first.
  // Same "read"-scope data the admin console's Stats tab shows; kept public
  // to all members here too, consistent with the leaderboard above already
  // being group-visible rather than admin-only.
  const ghostRows=(d.ghost_leaderboard||[]).map(g=>`<div class="ghost-row">
    <span class="ghost-name">${esc(g.name||"—")}</span>
    <span class="ghost-count">👻 ${n(g.ghost_count)}</span>
  </div>`).join("");

  // Response-time leaderboard — how quickly each member typically casts
  // their first vote after a rollcall opens, fastest first.
  const fmtSecs=s=>{
    s=n(s);
    if(s<60)return `${s}s`;
    const m=Math.floor(s/60),r=s%60;
    return r?`${m}m ${r}s`:`${m}m`;
  };
  const rtRows=(d.response_time_leaderboard||[]).map(r=>`<div class="rt-row">
    <span class="rt-name">${esc(r.display_name||r.username||"—")}</span>
    <span class="rt-avg">${fmtSecs(r.avg_response_seconds)} avg</span>
    <span class="rt-best">best ${fmtSecs(r.best_response_seconds)}</span>
  </div>`).join("");

  sc.innerHTML=`
  <div class="stats-section-hdr">📊 Group Stats</div>
  <div class="sp-group-row">
    <div class="sp-g"><div class="sp-g-val">${n(d.total_rollcalls)}</div><div class="sp-g-lbl">Sessions</div></div>
    <div class="sp-g"><div class="sp-g-val">${n(d.avg_attendance)}</div><div class="sp-g-lbl">Avg Attendance</div></div>
    <div class="sp-g"><div class="sp-g-val">${n(d.total_participants)}</div><div class="sp-g-lbl">Members</div></div>
  </div>
  ${trendHtml}
  ${personalHtml}
  ${lbRows?`<div class="stats-section-hdr">🏆 Leaderboard</div><div class="slb-list">${lbRows}</div>`:""}
  ${shistRows?`<div class="stats-section-hdr">🗓 Session History</div><div class="shist-list">${shistRows}</div>`:""}
  ${rtRows?`<div class="stats-section-hdr">⚡ Fastest Voters</div><div class="rt-list">${rtRows}</div>`:""}
  ${ghostRows?`<div class="stats-section-hdr">👻 Ghosts</div><div class="ghost-list">${ghostRows}</div>`:""}`;
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

// Always-visible escape hatch: a real anchor the user can click themselves.
// A programmatic window.open can be refused for reasons the page cannot see
// (popup blocker, in-app webview, iOS Safari) and there is no error to catch —
// the click just appears to do nothing, which is exactly the "sign-in works
// sometimes" report. An <a> click always carries its own user activation.
function _showVerifyDeepLink(deepLink){
  const row=document.getElementById("tg-deeplink-row");
  const link=document.getElementById("tg-deeplink-a");
  if(!row||!link)return;
  link.href=deepLink;
  row.classList.remove("hidden");
}
function _hideVerifyDeepLink(){
  const row=document.getElementById("tg-deeplink-row");
  if(row)row.classList.add("hidden");
}

window.startTgVerify=async function(){
  const btn=document.getElementById("verify-tg-btn")||document.getElementById("picker-tg-btn");
  const _origBtnText=btn?.textContent||"";
  if(btn){btn.textContent="⏳ Opening Telegram…";btn.disabled=true;}
  // Disable the name input while verification is in progress so the user
  // can't accidentally type a different name after starting the flow.
  const nameInput=$("name-input");
  if(nameInput){nameInput.disabled=true;nameInput.placeholder="Verifying with Telegram…";}
  // Claim the popup NOW, while the click that triggered this is still the
  // current user activation. Browsers drop that activation across an await,
  // so the old `window.open(deep_link)` after the fetch was blocked on
  // Safari and often on Chrome — silently, since a blocked open just
  // returns null. Open blank first, redirect it once the link is known.
  let win=null;
  try{win=window.open("","_blank");}catch(_){win=null;}
  try{
    const res=await fetch("/api/v1/auth/tg-verify/start",{
      method:"POST",headers:{"Content-Type":"application/json"},
      signal:AbortSignal.timeout(8000),
    });
    if(!res.ok)throw new Error("Server error");
    const{code,deep_link}=await res.json();
    _verifyCode=code;
    if(win){try{win.location.href=deep_link;}catch(_){win=null;}}
    // Shown whether or not the popup landed: even when it works, the tab can
    // open behind, and the user needs a way back to it.
    _showVerifyDeepLink(deep_link);
    toast(win?"Telegram opened — tap the verify button, then return here"
             :"Tap “Open Telegram” below to finish signing in",5000);
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
    if(win){try{win.close();}catch(_){}}
    toast("Could not start verification — try again",3500);
    if(nameInput){nameInput.disabled=false;nameInput.placeholder="";}
    if(btn){btn.textContent=_origBtnText||"🔗 Verify with Telegram";btn.disabled=false;}
  }
};

// Shared by the deep-link poll and the Login Widget callback below — both
// end up with the same {user_id,name,username,id_token} shape from the
// server, just via a different verification path.
function _adoptVerifiedIdentity(data){
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
  _hideVerifyDeepLink();
  // Verification can land while the dialog is still open (the user comes back
  // to this tab from Telegram) — close it rather than leaving them staring at
  // a sign-in sheet they've already completed.
  if(_signinHome)closeSignIn();
  toast(`✅ Verified as ${data.name}! Your identity is now locked to your Telegram account.`,4500);
  renderIdentity();detectCurrentVote();
  _checkWebAdmin().catch(()=>{});
  // Re-link any existing push subscription with the now-known user ID
  _relinkPushSubscription(_verifiedUserId);
}

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
    _adoptVerifiedIdentity(data);
  }catch(_){}
}

function _stopVerifyPoll(){
  if(_verifyPollTimer){clearInterval(_verifyPollTimer);_verifyPollTimer=null;}
  _verifyCode=null;
}

// ── Telegram Login Widget — additional sign-in option for when Telegram
// isn't on this device (the deep link above only helps if it is). Lazy
// loaded the first time it needs to be visible; same backend endpoints
// the portal already uses. ──
let _tgWidgetLoaded=false;

async function _loadLoginWidget(){
  if(_tgWidgetLoaded)return;
  _tgWidgetLoaded=true; // don't retry every render even if this attempt fails
  try{
    const res=await fetch("/api/v1/auth/tg-login/config",{signal:AbortSignal.timeout(6000)});
    if(!res.ok)return;
    const cfg=await res.json();
    if(!cfg.bot_username)return;
    // Only offer the widget where the deployment says it will actually work
    // (domain registered via /setdomain in BotFather). Otherwise Telegram
    // renders its own error — "Username invalid" / "Bot domain invalid" —
    // inside an iframe we can't read, and the visitor is left reading a
    // stranger's error message under a heading promising a QR scan.
    if(!cfg.widget_enabled)return;
    const s=document.createElement("script");
    s.async=true;
    s.src="https://telegram.org/js/telegram-widget.js?22";
    s.setAttribute("data-telegram-login",cfg.bot_username);
    s.setAttribute("data-size","large");
    s.setAttribute("data-onauth","onTelegramAuth(user)");
    s.setAttribute("data-request-access","write");
    const mount=$("tg-login-widget");
    if(!mount)return;
    mount.appendChild(s);
    // Reveal the block only once Telegram has actually put something there.
    // The script is cross-origin and fires no callback, so poll briefly; if
    // nothing lands, the block stays hidden rather than leaving a labelled
    // empty gap where a button should be.
    let tries=0;
    const t=setInterval(()=>{
      if(mount.querySelector("iframe")){
        clearInterval(t);
        $("tg-widget-wrap")?.classList.remove("hidden");
      }else if(++tries>=20){          // ~5s
        clearInterval(t);
      }
    },250);
  }catch(_){}
}

window.onTelegramAuth=async function(user){
  try{
    const res=await fetch("/api/v1/auth/tg-login",{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify(user),
      signal:AbortSignal.timeout(8000),
    });
    if(!res.ok)throw new Error("Verification failed");
    const data=await res.json();
    if(!data.verified)throw new Error("Verification failed");
    _adoptVerifiedIdentity(data);
  }catch(e){toast("Telegram sign-in failed — try again",3500);}
};

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
  // The home screen had no account control and no theme toggle at all — you
  // couldn't see who you were signed in as, sign out, or switch to dark mode
  // without first opening a group. Move the one account chip into this
  // header rather than cloning it, so there is still exactly one #acct-wrap
  // in the document and the menu keeps working from either screen.
  const acct=document.getElementById("acct-wrap");
  const homeActions=document.getElementById("home-brand-actions");
  if(acct&&homeActions&&acct.parentElement!==homeActions){
    closeAcctMenu();
    homeActions.appendChild(acct);
  }
  renderAcctControl();
  const container=document.getElementById("home-groups");
  if(!container)return;

  // Two sources, deliberately merged. localStorage remembers every group whose
  // link you opened on THIS device; the server knows every group you've
  // actually voted in, on any device. Recents alone are empty on a fresh
  // install — which is precisely how this screen is reached from Telegram's
  // menu button, where there is no chat context and nothing has been visited
  // yet. The old Mini App used the server list for exactly that reason.
  const byToken=new Map();
  _loadGroups().forEach(g=>{
    if(g&&g.token)byToken.set(g.token,{token:g.token,name:g.name,last_visit:g.last_visit,local:true});
  });
  (_homeServerGroups||[]).forEach(g=>{
    const t=g.group_web_token;
    if(!t)return;
    const prev=byToken.get(t)||{token:t};
    byToken.set(t,{...prev,name:g.group_name||prev.name||"Group",
                   live:!!g.has_active_rollcall,server:true});
  });
  // Somewhere with a vote open first, then the rest by name.
  const groups=[...byToken.values()].sort((a,b)=>
    (b.live?1:0)-(a.live?1:0)||String(a.name||"").localeCompare(String(b.name||"")));

  if(!groups.length){
    container.innerHTML=_homeGroupsLoading
      ?'<p style="color:var(--sub);font-size:.85rem">Loading your groups…</p>'
      :'<p style="color:var(--sub);font-size:.85rem">No groups yet. Visit a group rollcall link and it\'ll appear here automatically — or paste one below.</p>';
    return;
  }
  container.innerHTML=groups.map(g=>{
    // Only device-local entries get a ✕: removing one is forgetting a visit,
    // and there is nothing to forget about a group the server vouches for —
    // it would reappear on the next load and look broken.
    const sub=g.live?'<span style="color:var(--in,#16a34a)">● Rollcall open</span>'
      :(g.last_visit?new Date(g.last_visit).toLocaleDateString():"");
    return `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 0;border-bottom:1px solid var(--border)">
      <div style="min-width:0">
        <div style="font-weight:600;font-size:.95rem">${esc(g.name)}</div>
        <div style="font-size:.75rem;color:var(--sub)">${sub}</div>
      </div>
      <div style="display:flex;gap:8px;flex-shrink:0">
        <button class="btn btn-primary" style="padding:8px 14px;font-size:.85rem" onclick="window.location.href='/web/group/${esc(g.token)}'">Open</button>
        ${g.server?"":`<button class="btn" style="padding:8px 10px;font-size:.85rem;background:var(--border);color:var(--sub);border-radius:8px" onclick="_removeGroup('${esc(g.token)}');renderHomeScreen()">✕</button>`}
      </div>
    </div>`;
  }).join("");
}

// Groups the server knows this user votes in — same source the standalone
// Mini App used for its group picker.
let _homeServerGroups=null,_homeGroupsLoading=false;

async function _loadHomeGroups(){
  if(!_idToken)return;
  _homeGroupsLoading=true;
  renderHomeScreen();
  try{
    const res=await fetch("/api/v1/portal/groups",
      {headers:{"X-Identity-Token":_idToken},signal:AbortSignal.timeout(8000)});
    if(res.ok){
      const d=await res.json();
      _homeServerGroups=(d.groups||[]).filter(g=>g.group_web_token);
    }
  }catch(_){/* recents still render — this is an enrichment, not the list */}
  _homeGroupsLoading=false;
  renderHomeScreen();
}

// The home screen is the Mini App's landing page: Telegram's menu button
// opens the app with no chat context, so "which group?" is the first
// question. Authenticate from initData (no sign-in needed inside Telegram),
// then fill the list from the server.
async function _bootHome(){
  if(tg&&tg.initData&&!_idToken){try{await _miniappAuth();}catch(_){}}
  await _loadHomeGroups();
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

// Admin controls appearing only sometimes was reported as "I log in and don't
// see the options, then later I do". It had two independent causes, both of
// which ended in a silent `return` that left the admin card hidden with no
// indication anything had gone wrong:
//
//   1. RACE — opening the page from a /weblogin link sets _idToken inside an
//      async redemption (_weblogInRedeemPromise). The initial call here fired
//      first, hit `!_idToken`, gave up, and never retried; only the
//      Telegram-verify path re-invoked it. Reloading "fixed" it, which is
//      exactly what made it look random.
//   2. TIMEOUT — in a group locked down with /set_admins, admin-status makes a
//      live bot.get_chat_member round-trip to Telegram. A 5s abort against a
//      slow Telegram, or any non-2xx, was swallowed by `catch(_){}`.
//
// So: wait for a pending redemption, retry transient failures, and never fail
// silently — an unknown result now says so instead of rendering as "not admin".
async function _checkWebAdmin(){
  if(!IS_GROUP)return;
  // Which group you are looking at is not an admin question, so the switcher
  // must not ride on the admin answer. It used to be loaded from
  // _applyAdminStatus, which only runs when the check SUCCEEDS — so with
  // Telegram unreachable the whole header switcher quietly vanished, on top
  // of admin controls already being hidden.
  _loadHeaderGroups().catch(()=>{});
  // 1. A /weblogin redemption may still be in flight and is what sets _idToken.
  if(!_idToken&&_weblogInRedeemPromise){
    try{await _weblogInRedeemPromise;}catch(_){}
  }
  if(!_idToken){_setAdminCheckFailed("signed-out");return;}

  // 2. Telegram round-trips can be slow; retry rather than hide the UI on one
  //    unlucky request. Delays are short because this gates visible controls.
  let lastErr=null;
  for(const delay of [0,600,1800]){
    if(delay)await new Promise(r=>setTimeout(r,delay));
    try{
      const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/admin-status`,
        {headers:{"X-Identity-Token":_idToken},signal:AbortSignal.timeout(12000)});
      if(!res.ok){
        // 401/403 are real answers ("you are not an admin"), not failures to retry.
        if(res.status>=400&&res.status<500){_applyAdminStatus(false);return;}
        lastErr=new Error(`HTTP ${res.status}`);
        continue;
      }
      const d=await res.json();
      _applyAdminStatus(!!d.is_admin);
      return;
    }catch(e){lastErr=e;}
  }
  console.warn("admin-status check failed after retries:",lastErr&&lastErr.message);
  _setAdminCheckFailed("error");
}

function _applyAdminStatus(isAdmin){
  _isWebAdmin=isAdmin;
  const card=document.getElementById("admin-card");
  if(card)card.classList.toggle("hidden",!_isWebAdmin);
  const warn=document.getElementById("admin-check-warning");
  if(warn)warn.classList.add("hidden");
  // The server gave a real answer, so neither "couldn't check" nor "signed
  // out" applies any more — clear both, including after a mid-page sign-in.
  const note=document.getElementById("admin-signedout-note");
  if(note)note.classList.add("hidden");
  // The header Admin button is the discoverable entry point; it only exists
  // for actual admins.
  const navBtn=document.getElementById("admin-nav-btn");
  if(navBtn)navBtn.classList.toggle("hidden",!_isWebAdmin);
  if(_isWebAdmin){_syncShhToggle();_syncGroupSettingsCard();_syncTimezoneDisplay();_renderWeekdayHint();_loadWeblogInMembers();renderLists();}
  _syncAdminRcControls();
  _syncViewTabs();
  _peekGhostReview().catch(()=>{});
  // The empty state offers "＋ New Rollcall" to admins, and admin status
  // usually lands after it was first rendered.
  if(!activeRcData)renderEmptyState();
  renderAcctControl();
  loadDuesSection().catch(()=>{});
}

// Opens (or closes) the admin panel from the header. The panel starts
// collapsed: it's long, and for a page whose main job is voting it shouldn't
// push the rollcall off screen just because you happen to be an admin.
// Admin is a top-level view now, so "open the admin panel" is just
// navigation. Kept under its old name because the header button and the
// account menu both call it, and both mean the same thing: take me there.
window.toggleAdminPanel=function(){
  if(_view==="admin"){showView("rollcall");return;}
  showView("admin");
};

// Admin controls are hidden — but WHY matters, and the three reasons are not
// interchangeable:
//   "signed-out" no identity token on this device. No request is ever sent, so
//               nothing appears in the server logs either; the UI is the only
//               place this can possibly be surfaced. A group owner hitting
//               this saw an ordinary member view and had no way to tell.
//   "error"     the check ran and genuinely failed (5xx / network). Offer a retry.
//   "not-admin" the server answered, and the answer is no. Say nothing — this
//               is the normal case for most members.
function _setAdminCheckFailed(reason){
  _isWebAdmin=false;
  const card=document.getElementById("admin-card");
  if(card)card.classList.add("hidden");
  const warn=document.getElementById("admin-check-warning");
  if(warn)warn.classList.toggle("hidden",reason!=="error");
  const note=document.getElementById("admin-signedout-note");
  if(note)note.classList.toggle("hidden",reason!=="signed-out");
  const navBtn=document.getElementById("admin-nav-btn");
  if(navBtn){navBtn.classList.add("hidden");navBtn.classList.remove("active");}
  _syncViewTabs();
  if(!activeRcData)renderEmptyState();
  renderAcctControl();
  loadDuesSection().catch(()=>{});
}

window.retryAdminCheck=function(){
  const warn=document.getElementById("admin-check-warning");
  if(warn)warn.classList.add("hidden");
  _checkWebAdmin().catch(()=>{});
};

function _syncShhToggle(){
  const tog=document.getElementById("shh-toggle");
  if(!tog||!groupData)return;
  tog.checked=!!groupData.shh_mode;
}

// Nothing running. Rather than one grey sentence in an otherwise blank
// column, answer what the visitor came to find out — when is the next one,
// and what can I do now — and give the two roles their own next step: an
// admin can start one, a member can ask to be told when someone does.
function renderEmptyState(){
  const sub=document.getElementById("es-sub");
  const next=document.getElementById("es-next");
  const acts=document.getElementById("es-actions");
  if(!sub||!next||!acts)return;

  const upcoming=(groupData&&groupData.upcoming)||[];
  const soonest=upcoming.filter(u=>u&&u.scheduled_at)
    .sort((a,b)=>new Date(a.scheduled_at)-new Date(b.scheduled_at))[0];

  if(soonest){
    const dt=new Date(soonest.scheduled_at.endsWith("Z")?soonest.scheduled_at:soonest.scheduled_at+"Z");
    const when=isNaN(dt)?soonest.scheduled_at
      :dt.toLocaleString(undefined,{weekday:"short",month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"});
    sub.textContent="Nothing to vote on yet — the next one is already scheduled.";
    next.innerHTML=`<span class="es-next-lbl">Next</span> <span>${esc(soonest.display_title||soonest.title||"Rollcall")}</span> <span class="es-next-lbl">·</span> <span>${esc(when)}</span>`;
    next.classList.remove("hidden");
  }else{
    sub.textContent=_isWebAdmin
      ?"Start one and the group gets the panel straight away."
      :"Nobody has opened one yet. You'll see it here the moment they do.";
    next.classList.add("hidden");
  }

  let html="";
  if(_isWebAdmin){
    html+=`<button class="btn btn-primary" onclick="openNewRollcallModal()">＋ New Rollcall</button>`;
    html+=`<button class="btn btn-secondary" onclick="showView('admin')">⚙ Admin controls</button>`;
  }else if(_pushSupported()){
    html+=`<button class="btn btn-primary" onclick="toggleNotifications()">🔔 Notify me when one opens</button>`;
  }
  acts.innerHTML=html;
}

// Push is only worth offering where it can actually work.
function _pushSupported(){
  return typeof Notification!=="undefined"&&"serviceWorker" in navigator&&!!_swReg;
}

// ── Top-level views ───────────────────────────────────────────────────────
// Rollcall / Stats / Dues / Admin. One on screen at a time; the tab bar is
// the only navigation. Replaces a single scroll that put every feature on
// every visitor's screen at once and pushed the admin panel to the bottom of
// a sidebar.
const VIEWS=["rollcall","stats","dues","admin"];
let _view="rollcall";

window.showView=function(name){
  if(!VIEWS.includes(name))return;
  // Tabs for surfaces you don't have are hidden, so asking for one by other
  // means (the header Admin button, a stale call) shouldn't strand you on a
  // blank panel.
  const tab=document.getElementById(`vn-${name}`);
  if(tab&&tab.classList.contains("hidden")){
    toast(name==="admin"
      ?(_idToken?"You're not an admin of this group":"Sign in to manage this group")
      :"Not available for this group",3000);
    return;
  }
  _view=name;
  VIEWS.forEach(v=>{
    const sec=document.getElementById(`view-${v}`);
    if(sec)sec.classList.toggle("active",v===name);
    const t=document.getElementById(`vn-${v}`);
    if(t){t.classList.toggle("active",v===name);t.setAttribute("aria-selected",String(v===name));}
  });
  const navBtn=document.getElementById("admin-nav-btn");
  if(navBtn)navBtn.classList.toggle("active",name==="admin");
  // A view swap is a page change as far as the reader is concerned.
  window.scrollTo({top:0,behavior:"smooth"});
  if(name==="admin")_showAdminLevel(null);
  if(name==="stats"&&IS_GROUP)loadWebStats();
};

// Tabs appear as their surface becomes real: Dues once the group has it on,
// Admin once the server has confirmed you are one.
function _syncViewTabs(){
  const duesTab=document.getElementById("vn-dues");
  if(duesTab)duesTab.classList.toggle("hidden",!(groupData&&groupData.dues_enabled));
  const adminTab=document.getElementById("vn-admin");
  if(adminTab)adminTab.classList.toggle("hidden",!_isWebAdmin);
  // Don't leave someone stranded on a view that just disappeared (signing
  // out is the obvious way to get here).
  const cur=document.getElementById(`vn-${_view}`);
  if(cur&&cur.classList.contains("hidden"))showView("rollcall");
}

// The two admin controls that only make sense while a rollcall is open. Both
// used to be set from renderRollcall() alone, which runs BEFORE the
// admin-status round-trip finishes on a cold load — so a real admin saw no
// End button until the next poll happened to re-render. Driven from both
// sides now: whichever of the two facts (is-admin, has-rollcall) lands last.
function _syncAdminRcControls(){
  const show=_isWebAdmin&&!!activeRcData;
  const endRow=document.getElementById("end-rc-row");
  if(endRow)endRow.style.display=show?"":"none";
  const proxyItem=document.getElementById("adm-mi-proxy");
  if(proxyItem)proxyItem.style.display=show?"":"none";
}

// ── Admin menu: two levels, one panel at a time ───────────────────────────
// The card used to be a single flat scroll of every control at once. Now the
// card body shows a group picker + a menu, and opening an entry swaps in that
// entry's panel — so "which group am I editing" and "what can I do" are both
// answerable without scrolling the whole thing.
const ADMIN_SECTIONS={
  settings:{},
  access:{},
  proxy:{},
  templates:{load:()=>_ensureTemplatesLoaded()},
  scheduled:{load:()=>_ensureScheduledOnceLoaded()},
  merge:{load:()=>_ensureIdentityMergeLoaded()},
  ghost:{load:()=>loadGhostReview()},
  admins:{load:()=>loadAdminsPanel()},
};
let _adminSection=null;

function _showAdminLevel(section){
  const menu=document.getElementById("admin-menu");
  const quick=document.querySelector("#admin-card-body .adm-quick");
  Object.keys(ADMIN_SECTIONS).forEach(k=>{
    const p=document.getElementById(`adm-panel-${k}`);
    if(p)p.classList.toggle("hidden",k!==section);
  });
  // The menu level and the panel level are mutually exclusive: leaving both
  // on screen is what made the old card endless.
  const atRoot=!section;
  if(menu)menu.classList.toggle("hidden",!atRoot);
  if(quick)quick.classList.toggle("hidden",!atRoot);
  _adminSection=section||null;
}

window.openAdminSection=async function(name){
  const def=ADMIN_SECTIONS[name];
  if(!def)return;
  _showAdminLevel(name);
  const card=document.getElementById("admin-card");
  if(card)card.scrollIntoView({behavior:"smooth",block:"start"});
  if(def.load)await def.load();
};

window.closeAdminSection=function(){
  _showAdminLevel(null);
  const card=document.getElementById("admin-card");
  if(card)card.scrollIntoView({behavior:"smooth",block:"start"});
};

// Dues has its own card (it has a member-facing half), so the menu entry
// reveals and jumps to it rather than duplicating the controls here.
window.jumpToDuesAdmin=function(){
  const card=document.getElementById("dues-admin-card");
  if(!card||card.classList.contains("hidden")){toast("Dues isn't enabled for this group",3000);return;}
  const body=document.getElementById("dues-admin-body");
  const btn=document.getElementById("dues-admin-toggle");
  if(body&&body.classList.contains("hidden")){body.classList.remove("hidden");if(btn)btn.textContent="▲";}
  // The dues cards live in the Dues view now, so this is a change of
  // destination — scrolling to an element inside a hidden section would
  // simply do nothing.
  showView("dues");
};

function _syncGroupSettingsCard(){
  if(!groupData)return;
  const ghostTog=document.getElementById("ghost-toggle");
  if(ghostTog)ghostTog.checked=groupData.ghost_tracking_enabled!==false;
  const limitInp=document.getElementById("ghost-limit-input");
  if(limitInp)limitInp.value=groupData.absent_limit||1;
  const adminTog=document.getElementById("admin-rights-toggle");
  if(adminTog)adminTog.checked=!!groupData.admin_rights;
}

function _syncTimezoneDisplay(){
  const el=document.getElementById("tz-current");
  if(!el||!groupData)return;
  el.textContent=groupData.timezone||"Asia/Kolkata";
}

window.doDetectTimezone=async function(){
  if(!_idToken){toast("Verify with Telegram first.",3000);return;}
  let detected;
  try{
    detected=Intl.DateTimeFormat().resolvedOptions().timeZone;
  }catch(_){detected=null;}
  if(!detected){toast("Couldn't detect a timezone from this browser.",3000);return;}
  const current=groupData?.timezone||"Asia/Kolkata";
  if(detected===current){toast(`Already set to ${detected}.`,2500);return;}
  if(!await _confirmAction(`Detected ${detected} from this browser.\n\nSet this as the group's timezone? (Currently: ${current})`))return;
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/settings`,{
      method:"PATCH",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id_token:_idToken,timezone:detected}),
      signal:AbortSignal.timeout(8000),
    });
    if(!res.ok){const d=await res.json().catch(()=>({}));throw new Error(d.detail||"Failed to set timezone");}
    if(groupData)groupData.timezone=detected;
    _syncTimezoneDisplay();
    toast(`🕐 Timezone set to ${detected}`,2500);
  }catch(e){toast(e.message||"Could not set timezone",4000);}
};

let _lastWebloginUrl="";
const WEBLOGIN_OTHER="__other__";

async function _loadWeblogInMembers(){
  const sel=document.getElementById("weblogin-member-select");
  if(!sel||!_idToken)return;
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/members`,{headers:{"X-Identity-Token":_idToken},signal:AbortSignal.timeout(8000)});
    if(!res.ok)throw new Error();
    const data=await res.json();
    const members=data.members||[];
    const opts=members.map(m=>{
      const label=m.username?`${esc(m.first_name||"")} (@${esc(m.username)})`:esc(m.first_name||"?");
      const value=esc(m.first_name||m.username||"");
      return `<option value="${value}">${label}</option>`;
    }).join("");
    if(members.length){
      sel.innerHTML=opts+`<option value="${WEBLOGIN_OTHER}">✏️ Someone else (guest / proxy)</option>`;
    }else{
      // No real members yet — skip straight to the guest/proxy input
      // instead of offering a placeholder the admin has to click past.
      sel.innerHTML=`<option value="${WEBLOGIN_OTHER}" selected>✏️ Someone else (guest / proxy) — no members yet</option>`;
      onWebloginSelectChange();
    }
  }catch(_){
    sel.innerHTML=`<option value="${WEBLOGIN_OTHER}" selected>✏️ Someone else (guest / proxy)</option>`;
    onWebloginSelectChange();
  }
}

window.onWebloginSelectChange=function(){
  const sel=document.getElementById("weblogin-member-select");
  const row=document.getElementById("weblogin-name-row");
  const isOther=sel&&sel.value===WEBLOGIN_OTHER;
  row?.classList.toggle("hidden",!isOther);
  if(isOther)document.getElementById("weblogin-name-input")?.focus();
};

window.doIssueWeblogin=async function(){
  if(!_idToken){toast("Verify with Telegram first.",3000);return;}
  const sel=document.getElementById("weblogin-member-select");
  const resultEl=document.getElementById("weblogin-result");
  const nameOut=document.getElementById("weblogin-result-name");
  const urlOut=document.getElementById("weblogin-result-url");

  if(sel&&sel.value&&sel.value!==WEBLOGIN_OTHER){
    // Real member — existing single-use Telegram-identity link.
    try{
      const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/issue-weblogin`,{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({id_token:_idToken,member_name:sel.value}),
        signal:AbortSignal.timeout(10000),
      });
      if(!res.ok){const d=await res.json().catch(()=>({}));throw new Error(d.detail||"Failed");}
      const data=await res.json();
      _lastWebloginUrl=data.login_url;
      if(nameOut)nameOut.textContent=`Link for ${data.member_name} — valid 7 days, single use`;
      if(urlOut)urlOut.textContent=data.login_url;
      resultEl?.classList.remove("hidden");
    }catch(e){toast(e.message||"Could not generate link.",4000);}
    return;
  }

  // Guest / proxy — plain pre-filled voting link, no backend call needed:
  // guest voting was already open to anyone typing any name, this is just
  // a convenience deep link, not an identity grant.
  const nameEl=document.getElementById("weblogin-name-input");
  const name=(nameEl?.value||"").trim();
  if(!name){toast("Enter a name.",2500);return;}
  const url=`${window.location.origin}/web/group/${URL_TOKEN}?guest=${encodeURIComponent(name.slice(0,64))}`;
  _lastWebloginUrl=url;
  if(nameOut)nameOut.textContent=`Guest link for ${name} — reusable, no expiry, pre-fills their name`;
  if(urlOut)urlOut.textContent=url;
  resultEl?.classList.remove("hidden");
  if(nameEl)nameEl.value="";
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

async function _patchGroupSetting(fields,revert){
  if(!_idToken){toast("Verify with Telegram first.",3000);revert();return;}
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/settings`,{
      method:"PATCH",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id_token:_idToken,...fields}),
      signal:AbortSignal.timeout(8000),
    });
    if(!res.ok){const d=await res.json().catch(()=>({}));throw new Error(d.detail||"Failed");}
    if(groupData)Object.assign(groupData,fields);
    return true;
  }catch(e){
    toast(e.message||"Could not save setting",3500);
    revert();
    return false;
  }
}

window.toggleGhostTracking=async function(enabled){
  const ok=await _patchGroupSetting({ghost_tracking_enabled:enabled},()=>{
    const tog=document.getElementById("ghost-toggle");
    if(tog)tog.checked=!enabled;
  });
  if(ok)toast(enabled?"👻 Ghost tracking ON":"👻 Ghost tracking OFF",2000);
};

window.toggleAdminRights=async function(enabled){
  const ok=await _patchGroupSetting({admin_rights:enabled},()=>{
    const tog=document.getElementById("admin-rights-toggle");
    if(tog)tog.checked=!enabled;
  });
  if(ok)toast(enabled?"🔒 Admin-only mode ON":"🔒 Admin-only mode OFF",2000);
};

window.doSaveGhostLimit=async function(){
  const inp=document.getElementById("ghost-limit-input");
  const v=parseInt(inp?.value,10);
  if(!v||v<1){toast("Limit must be ≥ 1",3000);return;}
  const prev=groupData?.absent_limit||1;
  const ok=await _patchGroupSetting({absent_limit:v},()=>{if(inp)inp.value=prev;});
  if(ok)toast("Ghost limit saved.",2000);
};

// ── New Rollcall modal ───────────────────────────────────────────────────
// Unifies the three previously-separate creation paths (title-only Start
// modal, title-only Schedule modal, and the full-field Templates section)
// into one form. No new storage: "Now" applies location/fee/limit/event
// day+time directly onto the rollcalls table columns start_rollcall already
// accepted (see services/rollcalls.py); "Schedule" always saves a template
// first (existing templates table) and either references it once from a
// scheduled_rollcalls row (its title column repurposed to hold the
// template's name) or sets its existing recurring schedule columns —
// exactly what the Templates section below already does.
let _nrcState={timing:"now",recurrence:"once"};

window.openNewRollcallModal=async function(){
  const m=document.getElementById("nrc-modal");
  if(m){m.style.display="flex";m.classList.remove("hidden");}
  _nrcState={timing:"now",recurrence:"once"};
  renderNewRollcallModalBody();
  if(!_templatesCache)await loadTemplatesSchedule().catch(()=>{});
  _nrcRenderTemplateOptions();
  const t=document.getElementById("nrc-title");
  if(t)setTimeout(()=>t.focus(),50);
};
window.closeNewRollcallModal=function(){
  const m=document.getElementById("nrc-modal");
  if(m){m.style.display="none";}
};

function renderNewRollcallModalBody(){
  const body=document.getElementById("nrc-body");
  if(!body)return;
  body.innerHTML=`
    <div id="nrc-template-row" style="margin-bottom:14px">
      <label style="font-size:.8rem;font-weight:600;color:var(--sub);display:block;margin-bottom:6px">Start from a template (optional)</label>
      <select id="nrc-template-select" onchange="_nrcOnTemplateSelect(this.value)" style="width:100%;box-sizing:border-box;padding:9px 12px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.88rem">
        <option value="">— Start blank —</option>
      </select>
    </div>
    <label style="font-size:.8rem;font-weight:600;color:var(--sub);display:block;margin-bottom:6px">Title</label>
    <input id="nrc-title" type="text" placeholder="e.g. Sunday Cricket" maxlength="200" style="width:100%;box-sizing:border-box;padding:10px 12px;border:1.5px solid var(--border);border-radius:10px;font-size:.95rem;background:var(--card);color:var(--text);margin-bottom:12px"/>
    <div style="display:flex;gap:8px;margin-bottom:12px">
      <input id="nrc-location" type="text" placeholder="Location" maxlength="200" style="flex:1;padding:9px 12px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.88rem"/>
      <input id="nrc-fee" type="text" placeholder="Fee" maxlength="50" style="flex:1;padding:9px 12px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.88rem"/>
    </div>
    <input id="nrc-limit" type="number" min="1" max="1000" placeholder="Capacity (max attendees)" style="width:100%;box-sizing:border-box;padding:9px 12px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.88rem;margin-bottom:16px"/>
    <label style="font-size:.8rem;font-weight:600;color:var(--sub);display:block;margin-bottom:6px">When should it start?</label>
    <div class="nrc-seg" style="margin-bottom:12px">
      <button type="button" class="nrc-seg-btn active" id="nrc-seg-now" onclick="_nrcSetTiming('now')">Now</button>
      <button type="button" class="nrc-seg-btn" id="nrc-seg-schedule" onclick="_nrcSetTiming('schedule')">Schedule</button>
    </div>
    <div id="nrc-timing-body"></div>
  `;
  _nrcRenderTimingBody();
}

// ── Close-time helpers ──────────────────────────────────────────────────
// "Closes at" is always optional in the UI — if left blank we default to
// end of day (23:59 local) rather than leaving the rollcall open forever
// by accident. This mirrors /set_rollcall_time's exact-datetime mechanic
// (services/settings.py::set_rollcall_time) applied at creation time
// instead of after the fact, and is deliberately NOT the weekday-based
// event_day/event_time picker — "next Xday" doesn't mean anything useful
// for a single one-off close time. The weekday picker is kept only for
// genuinely recurring templates (Weekly/Biweekly/Monthly below), where
// "closes next Xday" is the correct, repeatable concept.
function _nrcEndOfDayISO(baseDate){
  const d=new Date(baseDate);
  d.setHours(23,59,0,0);
  return d.toISOString();
}
function _nrcOffsetFromMs(openMs,closeMs){
  let diffMin=Math.round((closeMs-openMs)/60000);
  if(diffMin<0)diffMin=0;
  const days=Math.floor(diffMin/1440);diffMin-=days*1440;
  const hours=Math.floor(diffMin/60);const minutes=diffMin-hours*60;
  return{days,hours,minutes};
}

function _nrcRenderTimingBody(){
  const el=document.getElementById("nrc-timing-body");
  if(!el)return;
  const btn=document.getElementById("nrc-submit-btn");
  if(_nrcState.timing==="now"){
    if(btn)btn.textContent="Start →";
    el.innerHTML=`
      <label style="font-size:.78rem;font-weight:600;color:var(--sub);display:block;margin-bottom:6px">🏟 Closes at (optional) — this is also when the event happens</label>
      <input id="nrc-close-at" type="datetime-local" style="width:100%;box-sizing:border-box;padding:10px 12px;border:1.5px solid var(--border);border-radius:10px;font-size:.95rem;background:var(--card);color:var(--text);margin-bottom:4px"/>
      <div style="font-size:.72rem;color:var(--sub);margin-bottom:14px">Leave blank to auto-close at 11:59 PM today.</div>
      <label style="display:flex;align-items:center;gap:8px;margin-bottom:8px;cursor:pointer">
        <input type="checkbox" id="nrc-save-template-check" onchange="document.getElementById('nrc-save-template-row').classList.toggle('hidden',!this.checked)"/>
        <span style="font-size:.85rem;font-weight:600">💾 Also save as a reusable template</span>
      </label>
      <div id="nrc-save-template-row" class="hidden" style="margin-bottom:4px">
        <input id="nrc-template-name" type="text" placeholder="Template name (for reuse — not shown to voters)" maxlength="50" value="${esc(_nrcState.templateName||"")}" oninput="_nrcSetTemplateName(this.value)" style="width:100%;box-sizing:border-box;padding:9px 12px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.88rem"/>
      </div>
    `;
    return;
  }
  if(btn)btn.textContent="Schedule →";
  const eventDayOpts=WEEKDAYS.map(d=>`<option value="${d}" ${_nrcState.eventDay===d?"selected":""}>${d[0].toUpperCase()+d.slice(1)}</option>`).join("");
  el.innerHTML=`
    <div style="font-size:.75rem;color:var(--sub);margin-bottom:10px">Scheduling always saves these details as a template first, so it can repeat (or fire once) later.</div>
    <label style="font-size:.78rem;font-weight:600;color:var(--sub);display:block;margin-bottom:6px">Repeat</label>
    <select id="nrc-recurrence" onchange="_nrcOnRecurrenceChange(this.value)" style="width:100%;box-sizing:border-box;padding:9px 12px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.88rem;margin-bottom:12px">
      <option value="once" ${_nrcState.recurrence==="once"?"selected":""}>Just once</option>
      <option value="daily" ${_nrcState.recurrence==="daily"?"selected":""}>Daily</option>
      <option value="weekly" ${_nrcState.recurrence==="weekly"?"selected":""}>Weekly</option>
      <option value="biweekly" ${_nrcState.recurrence==="biweekly"?"selected":""}>Every 2 weeks</option>
      <option value="monthly" ${_nrcState.recurrence==="monthly"?"selected":""}>Monthly</option>
    </select>
    <div id="nrc-rec-once" style="margin-bottom:12px">
      <label style="font-size:.78rem;font-weight:600;color:var(--sub);display:block;margin-bottom:6px">Opens for voting (your local time)</label>
      <input id="nrc-sched-at" type="datetime-local" style="width:100%;box-sizing:border-box;padding:10px 12px;border:1.5px solid var(--border);border-radius:10px;font-size:.95rem;background:var(--card);color:var(--text);margin-bottom:12px"/>
      <label style="font-size:.78rem;font-weight:600;color:var(--sub);display:block;margin-bottom:6px">🏟 Closes at (optional) — this is also when the event happens</label>
      <input id="nrc-close-at-once" type="datetime-local" style="width:100%;box-sizing:border-box;padding:10px 12px;border:1.5px solid var(--border);border-radius:10px;font-size:.95rem;background:var(--card);color:var(--text);margin-bottom:4px"/>
      <div style="font-size:.72rem;color:var(--sub)">Leave blank to auto-close at 11:59 PM that day.</div>
    </div>
    <div id="nrc-rec-daily" style="display:none;margin-bottom:12px">
      <input id="nrc-rec-dailytime" type="time" style="width:100%;box-sizing:border-box;padding:9px 12px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.88rem"/>
    </div>
    <div id="nrc-rec-weekly" style="display:none;gap:8px;margin-bottom:12px">
      <select id="nrc-rec-day" style="flex:1;padding:9px 12px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.88rem">${WEEKDAYS.map(d=>`<option value="${d}">${d[0].toUpperCase()+d.slice(1)}</option>`).join("")}</select>
      <input id="nrc-rec-time" type="time" style="flex:1;padding:9px 12px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.88rem"/>
    </div>
    <div id="nrc-rec-monthly" style="display:none;gap:8px;margin-bottom:12px">
      <input id="nrc-rec-monthday" type="number" min="1" max="31" placeholder="Day (1-31)" style="flex:1;padding:9px 12px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.88rem"/>
      <input id="nrc-rec-monthtime" type="time" style="flex:1;padding:9px 12px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.88rem"/>
    </div>
    <div id="nrc-rec-eventclose" style="display:none;margin-bottom:12px">
      <label style="font-size:.78rem;font-weight:600;color:var(--sub);display:block;margin-bottom:6px">🏟 Event day &amp; time (when the game happens — closes voting, repeats every cycle)</label>
      <div style="display:flex;gap:8px">
        <select id="nrc-eventday" style="flex:1;padding:9px 12px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.88rem"><option value="">No fixed day</option>${eventDayOpts}</select>
        <input id="nrc-eventtime" type="time" value="${esc(_nrcState.eventTime||"")}" style="flex:1;padding:9px 12px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.88rem"/>
      </div>
    </div>
    <div id="nrc-rec-expiry" style="display:none;margin-bottom:12px">
      <label style="font-size:.78rem;font-weight:600;color:var(--sub);display:block;margin-bottom:6px">Auto-disable this schedule after</label>
      <select id="nrc-expiry-mode" onchange="document.getElementById('nrc-expiry-date-row').classList.toggle('hidden',this.value!=='custom')" style="width:100%;box-sizing:border-box;padding:9px 12px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.88rem;margin-bottom:8px">
        <option value="12m">12 months</option>
        <option value="6m">6 months</option>
        <option value="custom">Custom date…</option>
      </select>
      <div id="nrc-expiry-date-row" class="hidden">
        <input id="nrc-expiry-date" type="date" style="width:100%;box-sizing:border-box;padding:9px 12px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.88rem"/>
      </div>
      <div style="font-size:.72rem;color:var(--sub);margin-top:4px">The template stays — only the recurring schedule turns off, and you can re-enable it anytime.</div>
    </div>
    <label style="font-size:.8rem;font-weight:600;color:var(--sub);display:block;margin-bottom:6px">Template name</label>
    <input id="nrc-template-name" type="text" placeholder="e.g. sunday-cricket" maxlength="50" value="${esc(_nrcState.templateName||"")}" oninput="_nrcSetTemplateName(this.value)" style="width:100%;box-sizing:border-box;padding:9px 12px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.88rem"/>
  `;
  // Pre-fill the date picker default (1 hour from now) and the recurrence
  // day/time defaults the same way the old modals did.
  const atInp=document.getElementById("nrc-sched-at");
  if(atInp){
    const d=new Date(Date.now()+60*60*1000);
    const pad=n=>String(n).padStart(2,"0");
    atInp.value=`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  const dailyTime=document.getElementById("nrc-rec-dailytime");
  if(dailyTime)dailyTime.value="09:00";
  const recTime=document.getElementById("nrc-rec-time");
  if(recTime)recTime.value="09:00";
  const monthTime=document.getElementById("nrc-rec-monthtime");
  if(monthTime)monthTime.value="09:00";
  _nrcOnRecurrenceChange(_nrcState.recurrence||"once");
}

// Resolves the expiry picker's selection ("6m"/"12m"/"custom") into a
// "YYYY-MM-DD" string, or null if the picker isn't in the DOM (Once mode,
// where a recurring schedule's expiry doesn't apply).
function _nrcResolveExpiryDate(){
  const mode=document.getElementById("nrc-expiry-mode")?.value;
  if(!mode)return null;
  if(mode==="custom"){
    const v=document.getElementById("nrc-expiry-date")?.value;
    return v||null;
  }
  const months=mode==="6m"?6:12;
  const d=new Date();
  d.setMonth(d.getMonth()+months);
  const pad=n=>String(n).padStart(2,"0");
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
}

window._nrcSetTemplateName=function(v){
  _nrcState.templateName=v;
};

window._nrcSetTiming=function(mode){
  _nrcState.timing=mode;
  document.getElementById("nrc-seg-now")?.classList.toggle("active",mode==="now");
  document.getElementById("nrc-seg-schedule")?.classList.toggle("active",mode==="schedule");
  _nrcRenderTimingBody();
};

window._nrcOnRecurrenceChange=function(value){
  _nrcState.recurrence=value;
  const show=(id,on,display)=>{const e=document.getElementById(id);if(e)e.style.display=on?display:"none";};
  show("nrc-rec-once",value==="once","block");
  show("nrc-rec-daily",value==="daily","block");
  show("nrc-rec-weekly",value==="weekly"||value==="biweekly","flex");
  show("nrc-rec-monthly",value==="monthly","flex");
  show("nrc-rec-eventclose",value!=="once","block");
  show("nrc-rec-expiry",value!=="once","block");
};

function _nrcRenderTemplateOptions(){
  const sel=document.getElementById("nrc-template-select");
  if(!sel)return;
  const opts=(_templatesCache||[]).map(t=>`<option value="${esc(t.name)}">${esc(t.title||t.name)}</option>`).join("");
  sel.innerHTML=`<option value="">— Start blank —</option>${opts}`;
}

window._nrcOnTemplateSelect=function(name){
  const t=(_templatesCache||[]).find(x=>x.name===name);
  const set=(id,val)=>{const e=document.getElementById(id);if(e)e.value=val||"";};
  if(!t){
    set("nrc-title","");set("nrc-location","");set("nrc-fee","");set("nrc-limit","");
    set("nrc-eventday","");set("nrc-eventtime","");
    _nrcState.templateName="";_nrcState.eventDay="";_nrcState.eventTime="";
    const nameInp=document.getElementById("nrc-template-name");
    if(nameInp)nameInp.value="";
    return;
  }
  set("nrc-title",t.title||t.name);
  set("nrc-location",t.location);
  set("nrc-fee",t.fee);
  set("nrc-limit",t.limit||"");
  set("nrc-eventday",t.event_day||"");
  set("nrc-eventtime",t.event_time||"");
  // eventday/eventtime inputs only exist in the DOM in Schedule ->
  // Weekly/Biweekly/Monthly mode — stash the value in state too (same
  // pattern as templateName) so it's restored correctly if the user picks
  // a template while on the Now/Once tab and switches to Weekly later.
  _nrcState.eventDay=t.event_day||"";
  _nrcState.eventTime=t.event_time||"";
  _nrcState.templateName=name;
  const nameInp=document.getElementById("nrc-template-name");
  if(nameInp)nameInp.value=name;
};

window.submitNewRollcall=async function(){
  if(!_idToken){toast("Verify your Telegram identity first.",3500);return;}
  const title=(document.getElementById("nrc-title")?.value||"").trim();
  if(!title){toast("Enter a title for the rollcall.",2500);return;}
  const location=(document.getElementById("nrc-location")?.value||"").trim()||null;
  const fee=(document.getElementById("nrc-fee")?.value||"").trim()||null;
  const limitVal=document.getElementById("nrc-limit")?.value;
  const limit=limitVal?parseInt(limitVal,10):null;

  const btn=document.getElementById("nrc-submit-btn");
  if(_nrcState.timing==="now"){
    const saveAsTemplate=document.getElementById("nrc-save-template-check")?.checked;
    const templateName=(document.getElementById("nrc-template-name")?.value||"").trim();
    if(saveAsTemplate&&!templateName){toast("Enter a name for the template.",2500);return;}
    // Closes-at is optional — a one-off exact date/time (see _nrcRenderTimingBody's
    // comment), defaulting to end of today if left blank so it never stays
    // open forever by accident.
    const closeAtLocal=document.getElementById("nrc-close-at")?.value;
    let finalizeAt;
    if(closeAtLocal){
      const ms=new Date(closeAtLocal).getTime();
      if(isNaN(ms)){toast("Invalid close date/time.",2500);return;}
      finalizeAt=new Date(ms).toISOString();
    }else{
      finalizeAt=_nrcEndOfDayISO(new Date());
    }
    if(btn){btn.disabled=true;btn.textContent="Starting…";}
    try{
      const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/start-rollcall`,{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          id_token:_idToken,title,location,fee,limit,
          finalize_at:finalizeAt,
          save_as_template:saveAsTemplate?templateName:null,
        }),
        signal:AbortSignal.timeout(10000),
      });
      if(!res.ok){const d=await res.json().catch(()=>({}));throw new Error(d.detail||"Failed to start rollcall");}
      closeNewRollcallModal();
      toast("✅ Rollcall started!",2500);
      activeTabIdx=0;
      await loadGroup();
      if(saveAsTemplate){_templatesCache=null;await loadTemplatesSchedule().catch(()=>{});}
    }catch(e){
      toast(e.message||"Could not start rollcall",4000);
    }finally{
      if(btn){btn.disabled=false;btn.textContent="Start →";}
    }
    return;
  }

  // Schedule path — always saves/updates a template first, then either
  // fires it once (scheduled_rollcalls row referencing the template by
  // name) or sets its recurring schedule (existing templates columns).
  const templateName=(document.getElementById("nrc-template-name")?.value||"").trim();
  if(!templateName){toast("Enter a template name — scheduling needs one to reuse.",3000);return;}
  const recurrence=_nrcState.recurrence;

  let scheduledAt=null,schedDay=null,schedTime=null,monthDay=null;
  let offsetDays=null,offsetHours=null,offsetMinutes=null;
  let eventDay=null,eventTime=null;
  if(recurrence==="once"){
    const atLocal=document.getElementById("nrc-sched-at")?.value;
    if(!atLocal){toast("Pick a date and time.",2500);return;}
    const openMs=new Date(atLocal).getTime();
    if(isNaN(openMs)||openMs<=Date.now()){toast("Choose a future date and time.",3000);return;}
    scheduledAt=new Date(openMs).toISOString();
    // Closes-at is an exact date/time too (like the Now tab) — not a
    // weekday, since "next Xday" doesn't mean anything for a single
    // occurrence. Converted to an offset-from-open here because the fire
    // logic (services/templates.py::start_template) only learns the real
    // open time when the template actually fires, days later — offset_days/
    // hours/minutes are existing template columns built exactly for this
    // ("closes N days/hours after it opens").
    const closeAtLocal=document.getElementById("nrc-close-at-once")?.value;
    const closeMs=closeAtLocal?new Date(closeAtLocal).getTime():new Date(_nrcEndOfDayISO(new Date(openMs))).getTime();
    if(closeAtLocal&&isNaN(closeMs)){toast("Invalid close date/time.",2500);return;}
    ({days:offsetDays,hours:offsetHours,minutes:offsetMinutes}=_nrcOffsetFromMs(openMs,closeMs));
  }else if(recurrence==="monthly"){
    monthDay=parseInt(document.getElementById("nrc-rec-monthday")?.value,10);
    schedTime=document.getElementById("nrc-rec-monthtime")?.value;
    if(!monthDay||monthDay<1||monthDay>31){toast("Enter a day of month (1-31).",2500);return;}
    if(!schedTime){toast("Pick a time.",2500);return;}
    eventDay=document.getElementById("nrc-eventday")?.value||null;
    eventTime=document.getElementById("nrc-eventtime")?.value||null;
  }else if(recurrence==="daily"){
    schedTime=document.getElementById("nrc-rec-dailytime")?.value;
    if(!schedTime){toast("Pick a time.",2500);return;}
    eventDay=document.getElementById("nrc-eventday")?.value||null;
    eventTime=document.getElementById("nrc-eventtime")?.value||null;
  }else{
    schedDay=document.getElementById("nrc-rec-day")?.value;
    schedTime=document.getElementById("nrc-rec-time")?.value;
    if(!schedTime){toast("Pick a time.",2500);return;}
    eventDay=document.getElementById("nrc-eventday")?.value||null;
    eventTime=document.getElementById("nrc-eventtime")?.value||null;
  }
  if((eventDay&&!eventTime)||(!eventDay&&eventTime)){
    toast("Set both event day and time, or leave both blank.",3000);
    return;
  }
  let expiresAt=null;
  if(recurrence!=="once"){
    expiresAt=_nrcResolveExpiryDate();
    if(!expiresAt){toast("Pick a valid end date for the schedule.",2500);return;}
  }

  if(btn){btn.disabled=true;btn.textContent="Scheduling…";}
  try{
    const tmplBody={id_token:_idToken,title,location,fee,limit:limit||0,event_day:eventDay,event_time:eventTime};
    if(recurrence==="once"){
      tmplBody.offset_days=offsetDays;tmplBody.offset_hours=offsetHours;tmplBody.offset_minutes=offsetMinutes;
    }
    const tmplRes=await fetch(`/api/v1/web/group/${URL_TOKEN}/templates/${encodeURIComponent(templateName)}`,{
      method:"PUT",headers:{"Content-Type":"application/json"},
      body:JSON.stringify(tmplBody),
      signal:AbortSignal.timeout(8000),
    });
    if(!tmplRes.ok)throw new Error((await tmplRes.json().catch(()=>({}))).detail||"Failed to save template");

    if(recurrence==="once"){
      const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/scheduled-rollcalls`,{
        method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({id_token:_idToken,title:templateName,scheduled_at:scheduledAt}),
        signal:AbortSignal.timeout(10000),
      });
      if(!res.ok)throw new Error((await res.json().catch(()=>({}))).detail||"Failed to schedule rollcall");
    }else{
      const schedBody={id_token:_idToken,recurrence_type:recurrence,schedule_time:schedTime,expires_at:expiresAt};
      if(recurrence==="monthly")schedBody.monthly_day=monthDay;
      else if(recurrence!=="daily")schedBody.schedule_day=schedDay;
      const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/templates/${encodeURIComponent(templateName)}/schedule`,{
        method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(schedBody),
        signal:AbortSignal.timeout(8000),
      });
      if(!res.ok)throw new Error((await res.json().catch(()=>({}))).detail||"Failed to set schedule");
    }
    closeNewRollcallModal();
    toast(recurrence==="once"?"✅ Rollcall scheduled!":"✅ Recurring schedule set!",2500);
    _templatesCache=null;
    await Promise.all([loadTemplatesSchedule().catch(()=>{}),_loadScheduledOnceList().catch(()=>{})]);
  }catch(e){
    toast(e.message||"Could not schedule rollcall",4000);
  }finally{
    if(btn){btn.disabled=false;btn.textContent="Schedule →";}
  }
};

window.doEndRcWeb=async function(){
  if(!_idToken){toast("Verify your Telegram identity first.",3500);return;}
  if(!activeRcData){toast("No active rollcall to end.",2500);return;}
  if(!await _confirmAction(`End rollcall "${activeRcData.title}"? This cannot be undone.`))return;
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

// ── Scheduled (one-time) list — promoted out of the old schedule modal
// into its own persistent collapsible admin-card section. Only the one-off
// entries created via New Rollcall's Schedule → Just once path; recurring
// schedules live in the Templates section above. Items whose title
// references a saved template (the unified flow's one-time path always
// does) get the template's real fields from the backend for a richer row.
let _scheduledOnceOpen=false,_scheduledOnceCache=null;

// Called when the Scheduled panel is opened from the admin menu. Always
// refetches: a one-off that already fired should disappear from the list the
// next time you look at it.
async function _ensureScheduledOnceLoaded(){
  _scheduledOnceOpen=true;
  await _loadScheduledOnceList();
}

async function _loadScheduledOnceList(){
  const body=document.getElementById("scheduled-once-body");
  if(!body||!_idToken)return;
  body.innerHTML='<div class="sched-empty">Loading…</div>';
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/scheduled-rollcalls`,{headers:{"X-Identity-Token":_idToken},signal:AbortSignal.timeout(5000)});
    if(!res.ok)throw new Error((await res.json().catch(()=>({}))).detail||"Failed to load");
    const d=await res.json();
    _scheduledOnceCache=d.items||[];
    _renderScheduledOnceList();
  }catch(e){
    body.innerHTML=`<div class="sched-empty">${esc(e.message||"Could not load")}</div>`;
  }
}

function _renderScheduledOnceList(){
  const body=document.getElementById("scheduled-once-body");
  if(!body)return;
  const items=_scheduledOnceCache||[];
  if(!items.length){body.innerHTML='<div class="sched-empty">No one-time rollcalls scheduled.</div>';return;}
  body.innerHTML=items.map(item=>{
    const dt=new Date(item.scheduled_at);
    const label=isNaN(dt)?item.scheduled_at:dt.toLocaleString(undefined,{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"});
    const displayTitle=item.display_title||item.title;
    const meta=[item.location,item.fee?`₹${item.fee}`:null,item.limit?`Cap ${item.limit}`:null].filter(Boolean).join(" · ");
    return `<div class="sched-item">
      <div class="sched-item-info">
        <div class="sched-item-title">${esc(displayTitle)}</div>
        <div class="sched-item-time">📅 ${esc(label)}</div>
        ${meta?`<div class="upcoming-meta">${esc(meta)}</div>`:""}
      </div>
      <button class="sched-cancel-btn" onclick="cancelScheduledOnce(${item.id})">Cancel</button>
    </div>`;
  }).join("");
}

window.cancelScheduledOnce=async function(id){
  if(!_idToken)return;
  if(!await _confirmAction("Cancel this scheduled rollcall?"))return;
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/scheduled-rollcalls/${id}`,{
      method:"DELETE",headers:{"X-Identity-Token":_idToken},signal:AbortSignal.timeout(8000),
    });
    if(!res.ok&&res.status!==204){const d=await res.json().catch(()=>({}));throw new Error(d.detail||"Failed");}
    toast("Scheduled rollcall cancelled.",2000);
    await _loadScheduledOnceList();
  }catch(e){toast(e.message||"Could not cancel",3500);}
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
  const q=new URLSearchParams(params);
  const r=await fetch(`${DUES_API}${path}?${q}`,{headers:{"X-Identity-Token":_idToken||""},signal:AbortSignal.timeout(10000)});
  if(!r.ok){const d=await r.json().catch(()=>({}));throw new Error(d.detail||"Request failed");}
  return r.json();
}

// Populates the QR <img> once a short-lived, single-purpose token is minted
// — fire-and-forget from the (synchronous) render path rather than making
// the whole render chain async just for this one image.
async function _loadQrImage(balance){
  const img=document.getElementById("dues-qr-img");
  if(!img)return;
  try{
    const{token}=await _duesGet("/qr-token");
    // Re-check the element is still in the DOM — a fast re-render (e.g.
    // switching tabs) could have replaced it while this fetch was in flight.
    const stillThere=document.getElementById("dues-qr-img");
    if(stillThere)stillThere.src=`${DUES_API}/qr?id_token=${encodeURIComponent(token)}&amount=${balance}&_t=${Date.now()}`;
  }catch(e){
    console.warn("QR token fetch failed:",e.message);
  }
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
      // Deliberately NOT another sign-in button. This card sits low in the
      // side column, and a second competing CTA down here (with its own
      // wording, and one that jumped straight into the Telegram flow rather
      // than offering the choice) is exactly what made "sign in" feel like it
      // lived in four different places. Say what's missing and point at the
      // one control that fixes it.
      if(body)body.innerHTML=`<div style="text-align:center;padding:14px 0;color:var(--sub);font-size:.85rem">🔒 Sign in to see your dues balance</div>`;
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
      // The dues card is a sibling of the admin menu, so the menu links to
      // it — but only once we know it's actually there to link to.
      const duesItem=document.getElementById("adm-mi-dues");
      if(duesItem)duesItem.style.display="";
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
      let html=`<a href="${upiLink}" class="dues-upi-btn">💳 Pay ₹${balance} via UPI</a>`;
      html+=`<div class="dues-vpa-row">
        <span class="dues-vpa-text">${esc(vpa)}</span>
        <button class="dues-vpa-copy">📋 Copy</button>
      </div>`;
      // src is populated async below once a short-lived QR token is minted —
      // the <img> tag itself can't send the X-Identity-Token header, so the
      // long-lived id_token never goes in this URL (see dues_qr_token route).
      html+=`<div class="dues-qr-wrap"><img id="dues-qr-img" alt="UPI QR" loading="lazy"/></div>`;
      if(mode==="auto"){
        html+=`<button class="dues-self-paid-btn" id="self-paid-btn" data-amount="${balance}">✅ I've paid ₹${balance}</button>`;
      }
      payEl.innerHTML=html;
      payEl.classList.remove("hidden");
      _loadQrImage(balance);
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

// ── Recurring template schedules (self-serve — no separate API token
// needed) ──────────────────────────────────────────────────────────────
const WEEKDAYS=["monday","tuesday","wednesday","thursday","friday","saturday","sunday"];
let _templatesScheduleOpen=false, _templatesCache=null, _templatesEditingName=null, _templatesCreatingNew=false;

// Called when the Templates panel is opened from the admin menu. The cache is
// reused (templates change rarely and edits update it in place); the polling
// refresh at the top of the file keys off _templatesScheduleOpen.
async function _ensureTemplatesLoaded(){
  _templatesScheduleOpen=true;
  if(!_templatesCache)await loadTemplatesSchedule();
  else renderTemplatesSchedule();
}

async function loadTemplatesSchedule(){
  const body=document.getElementById("templates-schedule-body");
  if(!body||!_idToken)return;
  body.innerHTML='<div class="sched-empty">Loading…</div>';
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/templates`,
      {headers:{"X-Identity-Token":_idToken},signal:AbortSignal.timeout(8000)});
    if(!res.ok)throw new Error((await res.json().catch(()=>({}))).detail||"Failed to load templates");
    _templatesCache=await res.json();
    // Also load the one-time pending list (if not already cached) so a
    // template that's only referenced by a pending one-off fire — never
    // schedule_enabled — doesn't get mislabeled "Not scheduled" below.
    if(!_scheduledOnceCache){
      try{
        const r2=await fetch(`/api/v1/web/group/${URL_TOKEN}/scheduled-rollcalls`,{headers:{"X-Identity-Token":_idToken},signal:AbortSignal.timeout(5000)});
        if(r2.ok)_scheduledOnceCache=(await r2.json()).items||[];
      }catch(_){/* best-effort — Templates list still renders without it */}
    }
    renderTemplatesSchedule();
  }catch(e){
    body.innerHTML=`<div class="sched-empty">${esc(e.message||"Could not load templates")}</div>`;
  }
}

// Next-fire epoch (seconds) for a recurring template's "Opens" schedule,
// computed client-side in the browser's local time (same simplification
// the admin console's admNextRun already makes). Only daily/weekly are
// estimated reliably this way — biweekly depends on which of the two
// weeks is "on" (server-side last_scheduled_date parity) and monthly can
// land on a clamped day-of-month, so both return null (no countdown
// pill) rather than show a guess that could be wrong.
const _SCHED_DAYS_SUN_FIRST=["sunday","monday","tuesday","wednesday","thursday","friday","saturday"];
function nextRecurrenceEpoch(t){
  if(!t.schedule_enabled||!t.schedule_time)return null;
  const[h,m]=(t.schedule_time||"00:00").split(":").map(Number);
  const now=new Date();
  if(t.recurrence_type==="daily"){
    const d=new Date(now);
    d.setHours(h,m,0,0);
    if(d<=now)d.setDate(d.getDate()+1);
    return d.getTime()/1000;
  }
  if((t.recurrence_type||"weekly")==="weekly"&&t.schedule_day){
    const tgt=_SCHED_DAYS_SUN_FIRST.indexOf((t.schedule_day||"").toLowerCase());
    if(tgt<0)return null;
    let diff=(tgt-now.getDay()+7)%7;
    if(diff===0&&(now.getHours()*60+now.getMinutes())>=h*60+m)diff=7;
    const d=new Date(now);
    d.setDate(now.getDate()+diff);d.setHours(h,m,0,0);
    return d.getTime()/1000;
  }
  return null;
}

// Blank stub for the "+ New Template" form — same shape renderTemplateEditForm
// expects from a real template, just empty. Uses the "__new__" key instead
// of a real name so form element ids stay stable while the name itself is
// still being typed (a name isn't known — or valid as an id fragment —
// until the admin enters one).
function _blankNewTemplate(){
  return {name:"__new__", title:"", location:"", fee:"", limit:null,
    event_day:null, event_time:null, recurrence_type:"weekly",
    schedule_day:null, schedule_time:"09:00", schedule_enabled:false, schedule_expires_at:null};
}

window.toggleNewTemplateForm=function(){
  _templatesCreatingNew=!_templatesCreatingNew;
  if(_templatesCreatingNew)_templatesEditingName=null; // one form open at a time
  renderTemplatesSchedule();
};

function renderTemplatesSchedule(){
  const body=document.getElementById("templates-schedule-body");
  if(!body)return;
  const newBtnRow=`<div style="margin-bottom:10px">
    <button class="id-change" style="width:100%;padding:8px;text-align:center;border:1.5px dashed var(--border);border-radius:8px" onclick="toggleNewTemplateForm()">${_templatesCreatingNew?"✕ Cancel":"➕ New Template"}</button>
    ${_templatesCreatingNew?renderTemplateEditForm(_blankNewTemplate(),true):""}
  </div>`;
  if(!_templatesCache||!_templatesCache.length){
    body.innerHTML=newBtnRow+'<div class="sched-empty">No templates yet — tap "New Template" above, or create one with /set_template in the group.</div>';
    return;
  }
  body.innerHTML=newBtnRow+_templatesCache.map(t=>{
    const enabled=t.schedule_enabled;
    const recLabel={daily:"daily",weekly:"weekly",biweekly:"every 2 weeks",monthly:"monthly"}[t.recurrence_type]||t.recurrence_type;
    // A template with no recurring schedule can still have a pending
    // one-time fire referencing it (the New Rollcall modal's Schedule ->
    // Once path saves a template but never sets schedule_enabled) — cross-
    // reference so it doesn't get mislabeled "Not scheduled" when it
    // genuinely has something coming up.
    const pendingOnce=!enabled&&(_scheduledOnceCache||[]).find(p=>p.title===t.name);
    const nextEpoch=enabled?nextRecurrenceEpoch(t):(pendingOnce?new Date(pendingOnce.scheduled_at).getTime()/1000:null);
    const cd=nextEpoch?formatCountdown(nextEpoch):null;
    const cdHtml=cd?` <span class="cd-pill${cd.includes("m")&&!cd.includes("h")?" soon":""}">${esc(cd)}</span>`:"";
    const when=(enabled
      ?`Opens ${t.recurrence_type==="monthly"
        ?`day ${esc(t.schedule_day)} of each month at ${esc(t.schedule_time)}`
        :t.recurrence_type==="daily"
          ?`every day at ${esc(t.schedule_time)}`
          :`${esc((t.schedule_day||"").replace(/^./,c=>c.toUpperCase()))} ${esc(t.schedule_time)} (${recLabel})`}`
      :pendingOnce
        ?`One-time: ${esc(new Date(pendingOnce.scheduled_at).toLocaleString(undefined,{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}))}`
        :"Not scheduled")+cdHtml;
    const closes=(t.event_day&&t.event_time)
      ?`Closes ${esc((t.event_day||"").replace(/^./,c=>c.toUpperCase()))} ${esc(t.event_time)}`
      :"";
    const expires=(enabled&&t.schedule_expires_at)?`Until ${esc(t.schedule_expires_at)}`:"";
    const meta=[t.location,t.fee?`₹${t.fee}`:null,t.limit?`Cap ${t.limit}`:null].filter(Boolean).join(" · ");
    const editing=_templatesEditingName===t.name;
    return `<div class="sched-item" style="flex-direction:column;align-items:stretch">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%">
        <div class="sched-item-info">
          <div class="sched-item-title">${esc(t.title||t.name)}</div>
          <div class="sched-item-time">${when}${closes?" · "+closes:""}${expires?" · "+expires:""}</div>
          ${meta?`<div class="upcoming-meta">${esc(meta)}</div>`:""}
        </div>
        <div style="display:flex;align-items:center;gap:6px;flex-shrink:0">
          <button class="id-change" title="Start a rollcall from this template now" onclick="startTemplateNow('${esc(escJsAttr(t.name))}')">▶️</button>
          <label class="admin-toggle" title="${enabled?'Disable':'Enable'} schedule">
            <input type="checkbox" ${enabled?"checked":""} onchange="toggleTemplateSchedule('${esc(escJsAttr(t.name))}',this.checked)"/>
            <span class="admin-toggle-slider"></span>
          </label>
          <button class="id-change" onclick="toggleTemplateEditForm('${esc(escJsAttr(t.name))}')">${editing?"✕":"✏️"}</button>
          <button class="id-change" title="Delete this template" onclick="deleteTemplate('${esc(escJsAttr(t.name))}')">🗑</button>
        </div>
      </div>
      ${editing?renderTemplateEditForm(t):""}
    </div>`;
  }).join("");
}

function renderTemplateEditForm(t,isNew){
  const isMonthly=t.recurrence_type==="monthly";
  const isDaily=t.recurrence_type==="daily";
  const safeName=esc(t.name);
  // For a monthly template, schedule_day holds a day-of-month number (e.g.
  // "15"), not a weekday name — it never matches an option below. Without
  // a placeholder, the browser silently pre-selects "Monday" (the first
  // option) with nothing actually chosen; an admin switching recurrence
  // from monthly to weekly could then save that unintended default. A
  // disabled placeholder forces an explicit pick, or an empty value that
  // the backend correctly rejects instead of silently saving "Monday".
  const scheduleDayIsWeekday=WEEKDAYS.includes(t.schedule_day);
  const dayOpts=(scheduleDayIsWeekday?"":'<option value="" disabled selected>Choose a day…</option>')
    +WEEKDAYS.map(d=>`<option value="${d}" ${t.schedule_day===d?"selected":""}>${d[0].toUpperCase()+d.slice(1)}</option>`).join("");
  const eventDayOpts='<option value="">No fixed day</option>'+WEEKDAYS.map(d=>`<option value="${d}" ${t.event_day===d?"selected":""}>${d[0].toUpperCase()+d.slice(1)}</option>`).join("");
  const inp=(id,val,ph)=>`<input id="${id}" type="text" placeholder="${ph}" value="${esc(val||"")}" style="flex:1;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.85rem"/>`;
  const sublabel=text=>`<div style="font-size:.72rem;color:var(--sub);margin-top:-4px">${text}</div>`;
  // A new template is created content-only, same as one made with the
  // Telegram /set_template command — scheduling is already a separate,
  // later step for existing templates too (the row's own enable toggle),
  // so skipping the schedule section here avoids ambiguity about whether
  // default values sitting in an unfilled section should actually save.
  return `<div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border);display:flex;flex-direction:column;gap:8px">
    ${isNew?`<div class="id-prompt-label" style="text-align:left;margin-bottom:0">Name</div>
    ${sublabel("Internal identifier only — not shown to voters. Use the Title below for that.")}
    <input id="tsf-name-__new__" type="text" placeholder="e.g. sunday-badminton" maxlength="50" style="padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.85rem"/>`:""}
    <div class="id-prompt-label" style="text-align:left;margin-bottom:0">Details</div>
    ${inp(`tsf-title-${safeName}`,t.title,"Title")}
    <div style="display:flex;gap:8px">
      ${inp(`tsf-location-${safeName}`,t.location,"Location")}
      ${inp(`tsf-fee-${safeName}`,t.fee,"Fee")}
    </div>
    <input id="tsf-limit-${safeName}" type="number" min="1" max="1000" placeholder="Cap (max attendees)" value="${t.limit||""}" style="padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.85rem"/>

    <div class="id-prompt-label" style="text-align:left;margin-bottom:0;margin-top:4px">🏟 Event day &amp; time</div>
    ${sublabel("When the game itself happens — closes voting on any rollcall started from this template.")}
    <div style="display:flex;gap:8px">
      <select id="tsf-eventday-${safeName}" style="flex:1;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.85rem">${eventDayOpts}</select>
      <input id="tsf-eventtime-${safeName}" type="time" value="${esc(t.event_time||"")}" style="flex:1;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.85rem"/>
    </div>

    ${isNew?`${sublabel("You can set a recurring auto-start schedule after creating, from this list.")}`:`
    <div class="id-prompt-label" style="text-align:left;margin-bottom:0;margin-top:4px">🗓 Auto-start schedule</div>
    ${sublabel("When this template repeats and opens a new rollcall automatically — separate from the event time above.")}
    <label style="font-size:.78rem;font-weight:600;color:var(--sub)">Repeat</label>
    <div style="display:flex;gap:8px">
      <select id="tsf-rec-${safeName}" onchange="_onTsfRecurrenceChange('${esc(escJsAttr(t.name))}')" style="flex:1;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.85rem">
        <option value="daily" ${isDaily?"selected":""}>Daily</option>
        <option value="weekly" ${t.recurrence_type==="weekly"?"selected":""}>Weekly</option>
        <option value="biweekly" ${t.recurrence_type==="biweekly"?"selected":""}>Every 2 weeks</option>
        <option value="monthly" ${isMonthly?"selected":""}>Monthly</option>
      </select>
    </div>
    <div style="display:flex;gap:8px">
      <select id="tsf-day-${safeName}" style="flex:1;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.85rem;${isMonthly||isDaily?"display:none":""}">${dayOpts}</select>
      <input id="tsf-monthday-${safeName}" type="number" min="1" max="31" placeholder="Day (1-31)" value="${isMonthly?esc(t.schedule_day||""):""}" style="flex:1;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.85rem;${isMonthly?"":"display:none"}"/>
      <input id="tsf-time-${safeName}" type="time" value="${esc(t.schedule_time||"09:00")}" style="flex:1;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.85rem"/>
    </div>
    <div class="id-prompt-label" style="text-align:left;margin-bottom:0;margin-top:4px">Auto-disable after</div>
    <div style="display:flex;gap:8px">
      <select id="tsf-expmode-${safeName}" onchange="document.getElementById('tsf-expdate-${safeName}').style.display=this.value==='custom'?'':'none'" style="flex:1;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.85rem">
        <option value="12m">12 months from now</option>
        <option value="6m">6 months from now</option>
        <option value="custom" selected>Custom date</option>
      </select>
      <input id="tsf-expdate-${safeName}" type="date" value="${esc(t.schedule_expires_at||"")}" style="flex:1;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--card);color:var(--text);font-size:.85rem"/>
    </div>
    ${sublabel("The template stays — only the recurring schedule turns off, and you can re-enable it anytime.")}`}
    <button class="btn btn-primary" style="padding:9px" onclick="saveTemplate('${esc(escJsAttr(t.name))}')">${isNew?"➕ Create":"💾 Save"}</button>
  </div>`;
}

window.startTemplateNow=async function(name){
  if(!await _confirmAction(`Start a rollcall from "${name}" now?`))return;
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

window.deleteTemplate=async function(name){
  if(!await _confirmAction(`Delete template "${name}"? This cannot be undone — any recurring schedule on it will stop too.`))return;
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/templates/${encodeURIComponent(name)}`,{
      method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({id_token:_idToken}),
      signal:AbortSignal.timeout(10000),
    });
    if(!res.ok)throw new Error((await res.json().catch(()=>({}))).detail||"Failed to delete template");
    toast(`🗑 Deleted "${name}"`,2500);
    _templatesEditingName=_templatesEditingName===name?null:_templatesEditingName;
    _templatesCache=null;
    await loadTemplatesSchedule();
  }catch(e){toast(e.message||"Could not delete template",4000);}
};

window._onTsfRecurrenceChange=function(name){
  const rec=document.getElementById(`tsf-rec-${name}`).value;
  const isMonthly=rec==="monthly";
  const isDaily=rec==="daily";
  document.getElementById(`tsf-day-${name}`).style.display=(isMonthly||isDaily)?"none":"";
  document.getElementById(`tsf-monthday-${name}`).style.display=isMonthly?"":"none";
};

window.toggleTemplateEditForm=function(name){
  _templatesEditingName=_templatesEditingName===name?null:name;
  if(_templatesEditingName)_templatesCreatingNew=false; // one form open at a time
  renderTemplatesSchedule();
};

window.saveTemplate=async function(key){
  // key is either an existing template's real name, or the "__new__"
  // sentinel renderTemplateEditForm uses for the create form — resolve the
  // actual name to save under, but keep using `key` to read form element
  // ids (that's what they were rendered with).
  const isNew=key==="__new__";
  let name=key;
  if(isNew){
    name=(document.getElementById("tsf-name-__new__")?.value||"").trim();
    if(!name){toast("Enter a name for the template.",2500);return;}
    if((_templatesCache||[]).some(t=>t.name.toLowerCase()===name.toLowerCase())){
      toast(`A template named "${name}" already exists.`,3000);
      return;
    }
  }
  // Only push a schedule update if the schedule is already enabled for this
  // template — otherwise saving content-only edits would silently switch a
  // disabled schedule on using whatever defaults happen to sit in the form.
  // Always false for a new template — its create form has no schedule
  // section (see renderTemplateEditForm), scheduling is a separate step.
  const current=(_templatesCache||[]).find(t=>t.name===key);
  const scheduleWasEnabled=!isNew&&!!(current&&current.schedule_enabled);

  let scheduleBody=null;
  if(scheduleWasEnabled){
    const recurrence_type=document.getElementById(`tsf-rec-${key}`).value;
    const schedule_time=document.getElementById(`tsf-time-${key}`).value;
    if(!schedule_time){toast("Pick a time first.",2500);return;}
    scheduleBody={id_token:_idToken,recurrence_type,schedule_time};
    if(recurrence_type==="monthly"){
      const md=parseInt(document.getElementById(`tsf-monthday-${key}`).value,10);
      if(!md||md<1||md>31){toast("Enter a day of month (1-31).",2500);return;}
      scheduleBody.monthly_day=md;
    }else if(recurrence_type!=="daily"){
      scheduleBody.schedule_day=document.getElementById(`tsf-day-${key}`).value;
    }
    const expMode=document.getElementById(`tsf-expmode-${key}`)?.value;
    if(expMode==="custom"){
      const expDate=document.getElementById(`tsf-expdate-${key}`)?.value;
      if(!expDate){toast("Pick an auto-disable date, or choose 6/12 months.",2500);return;}
      scheduleBody.expires_at=expDate;
    }else if(expMode){
      const months=expMode==="6m"?6:12;
      const d=new Date();
      d.setMonth(d.getMonth()+months);
      const pad=n=>String(n).padStart(2,"0");
      scheduleBody.expires_at=`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
    }
  }
  const contentBody={
    id_token:_idToken,
    title:document.getElementById(`tsf-title-${key}`).value||null,
    location:document.getElementById(`tsf-location-${key}`).value||null,
    fee:document.getElementById(`tsf-fee-${key}`).value||null,
  };
  // 0 is the backend's "explicitly clear the cap" sentinel (no real limit
  // is ever 0) — sent when the field is left blank, so clearing it here
  // actually removes the cap instead of silently preserving the old one.
  const limitVal=document.getElementById(`tsf-limit-${key}`).value;
  contentBody.limit=limitVal?parseInt(limitVal,10):0;

  // Event day/time (when the game happens — auto-closes the rollcall) is
  // separate from the schedule above (when the template auto-opens). Both
  // or neither — a lone value silently does nothing on the backend.
  const eventDay=document.getElementById(`tsf-eventday-${key}`).value||null;
  const eventTime=document.getElementById(`tsf-eventtime-${key}`).value||null;
  if((eventDay&&!eventTime)||(!eventDay&&eventTime)){
    toast("Set both event day and time, or leave both blank.",3000);
    return;
  }
  contentBody.event_day=eventDay;
  contentBody.event_time=eventTime;

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

    if(isNew){
      _templatesCache=[...(_templatesCache||[]),updated];
      _templatesCreatingNew=false;
      toast(`➕ Created ${name}`,2500);
    }else{
      _templatesCache=_templatesCache.map(t=>t.name===name?updated:t);
      _templatesEditingName=null;
      toast(`💾 Saved ${name}`,2500);
    }
    renderTemplatesSchedule();
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

// ── Merge Identities (fold a fragmented proxy name into a real member or
// another proxy so stats/dues/ghost-tracking count them once) ──────────────
let _identityMergeOpen=false, _identitiesCache=null, _identityGroupsCache=null,
    _suggestionsCache=null, _discardedCache=null, _identityDiscardedOpen=false,
    _standaloneCache=null, _identityStandaloneOpen=false,
    _identityUnmatchedOpen=false, _imTargetCombo=null;

window._toggleExtraAliases=function(btn){
  const extra=btn.previousElementSibling;
  if(!extra)return;
  const hidden=extra.classList.contains("hidden");
  // Counts .alias-pill descendants when present (alias lists); falls back
  // to direct children for other "+N more" uses (e.g. the merge review
  // list's rows, which aren't alias pills).
  const n=extra.querySelectorAll(".alias-pill").length||extra.children.length;
  if(hidden){extra.classList.remove("hidden");btn.textContent="show less";}
  else{extra.classList.add("hidden");btn.textContent=`+${n} more`;}
};

window.toggleIdentityUnmatched=function(){
  _identityUnmatchedOpen=!_identityUnmatchedOpen;
  const body=document.getElementById("im-unmatched-body");
  const ch=document.getElementById("im-unmatched-chevron");
  if(body)body.classList.toggle("hidden",!_identityUnmatchedOpen);
  if(ch)ch.textContent=_identityUnmatchedOpen?"▲":"▼";
};

// Called when the Merge identities panel is opened from the admin menu.
async function _ensureIdentityMergeLoaded(){
  _identityMergeOpen=true;
  if(!_identitiesCache)await loadIdentityMerge();
  else renderIdentityMerge();
}

async function loadIdentityMerge(){
  const body=document.getElementById("identity-merge-body");
  if(!body||!_idToken)return;
  body.innerHTML='<div class="sched-empty">Loading…</div>';
  try{
    const[idRes,sugRes]=await Promise.all([
      fetch(`/api/v1/web/group/${URL_TOKEN}/identities`,{headers:{"X-Identity-Token":_idToken},signal:AbortSignal.timeout(8000)}),
      fetch(`/api/v1/web/group/${URL_TOKEN}/identities/suggestions`,{headers:{"X-Identity-Token":_idToken},signal:AbortSignal.timeout(8000)}),
    ]);
    if(!idRes.ok)throw new Error((await idRes.json().catch(()=>({}))).detail||"Failed to load identities");
    const idData=await idRes.json();
    _identitiesCache=idData.identities||[];
    _identityGroupsCache=idData.groups||[];
    _discardedCache=idData.discarded||[];
    _standaloneCache=idData.standalone||[];
    _suggestionsCache=sugRes.ok?(await sugRes.json()).suggestions||[]:[];
    renderIdentityMerge();
  }catch(e){
    body.innerHTML=`<div class="sched-empty">${esc(e.message||"Could not load identities")}</div>`;
  }
}

function _identityKey(kind,userId,proxyName){
  // Preserves the proxy name's original casing — this becomes the
  // manual-merge target select's option value, submitted verbatim as
  // canonical_proxy_name. Matching on the backend is case-insensitive
  // (see services/identity.py), but the FIRST time a name becomes a
  // canonical, whatever casing is sent here is what gets stored.
  return kind==="user"?`u:${userId}`:`p:${proxyName||""}`;
}

// Case-insensitive variant used only for matching an identity to its group
// of aliases — casing between get_all_proxy_names and a link's stored
// canonical_proxy_name isn't guaranteed identical, so exact-match would
// silently drop the alias list for some canonicals.
function _identityMapKey(kind,userId,proxyName){
  return kind==="user"?`u:${userId}`:`p:${(proxyName||"").toLowerCase()}`;
}

function _buildTargetOptions(excludeProxyNameLower){
  // TO list: real users first (kept prominent, per feedback that actual
  // members should anchor the target side), then canonical-eligible
  // proxies only — an already-merged proxy (merged_into set) is just an
  // alias of some other identity, so listing it here too would show the
  // same person twice (once as itself, once as whatever it resolves to).
  // Also excludes whichever proxy is currently picked as FROM, so the
  // same identity can never be selected on both sides (that's the only
  // way a manual pick could look like a self-merge/cycle; the backend
  // also rejects it, this just stops the UI from offering it).
  // Standalone names ARE offered here (that's the entire point of the
  // status) — labelled "(guest)" so it's clear they're a confirmed person
  // rather than an unreviewed name.
  const identities=_identitiesCache||[];
  const users=identities.filter(i=>i.kind==="user");
  const proxies=identities.filter(i=>i.kind==="proxy"&&!i.merged_into&&i.proxy_name.toLowerCase()!==excludeProxyNameLower);
  const opt=i=>({value:_identityKey(i.kind,i.user_id,i.proxy_name),
                 label:i.display_name+(i.kind==="proxy"?(i.standalone?" (guest)":" (proxy)"):"")});
  return users.map(opt).concat(proxies.map(opt));
}

window._onImAliasChange=function(aliasValue){
  if(!_imTargetCombo)return;
  const opts=_buildTargetOptions((aliasValue||"").toLowerCase());
  _imTargetCombo.setOptions(opts);
  _imTargetCombo.setSelected(opts[0]||null);
};

// ── Suggestion lookup + unified review-row priority (items 1-3: fold the
// old separate "Suggested merges" section into the unmatched-names list,
// flag exact matches, sort by confidence then recency/frequency) ──────────
function _buildSuggestionIndex(suggestions){
  // Maps lowercased proxy name -> {s: suggestion, otherLabel}. Every
  // suggestion is indexed under its alias_proxy_name; proxy<->proxy
  // suggestions are ALSO indexed under candidate_proxy_name, because
  // list_suggestions only ever emits the alphabetically-first name as the
  // alias side (services/identity.py) — without this second entry, the
  // alphabetically-LATER name's own row would show no hint despite being
  // an equally valid match. The merge action is always the same call
  // regardless of which row triggered it (same alias/candidate pair) —
  // only the display label of "the other side" differs per row.
  const idx={};
  suggestions.forEach(s=>{
    const aliasKey=s.alias_proxy_name.toLowerCase();
    if(!(aliasKey in idx))idx[aliasKey]={s,otherLabel:s.candidate_display_name};
    if(s.candidate_kind==="proxy"){
      const candKey=s.candidate_proxy_name.toLowerCase();
      if(!(candKey in idx))idx[candKey]={s,otherLabel:s.alias_proxy_name};
    }
  });
  return idx;
}

const _CONF_TIER={exact_username:0,exact_first_name:1,exact_proxy:2,close:3};

function _buildUnifiedReviewRows(unmatched,suggestions){
  const sugIdx=_buildSuggestionIndex(suggestions);
  const rows=unmatched.map(row=>{
    const hit=sugIdx[row.proxy_name.toLowerCase()];
    return{
      row,
      suggestion:hit?hit.s:null,
      otherLabel:hit?hit.otherLabel:null,
      tier:hit?_CONF_TIER[hit.s.confidence]:4,
      score:hit?hit.s.score:99,
      count:row.proxy_count||0,
      lastSeen:row.proxy_last_seen||"",
    };
  });
  // Confidence tier first (exact username > exact name > exact proxy >
  // close > no suggestion), then closer fuzzy score first within a tier,
  // then — per admin preference — most-recently-used first, session count
  // as the tie-break (a name used last week outranks one used 20x a year
  // ago). last_seen is a sqlite "YYYY-MM-DD HH:MM:SS" string, lexically
  // sortable; "" (never seen) always sorts last.
  rows.sort((a,b)=>
    a.tier-b.tier ||
    a.score-b.score ||
    (b.lastSeen>a.lastSeen?1:b.lastSeen<a.lastSeen?-1:0) ||
    b.count-a.count
  );
  return rows;
}

// ── Lightweight searchable combobox (no external deps — this app has no
// build step). Keeps a hidden <input> holding the selected value so
// existing callers (doMergeIdentityManual) read .value exactly as when
// these were native <select> elements. ─────────────────────────────────
// Single delegated listener (registered once, not per mount) closes any
// open combobox dropdown on an outside click — mounting fresh listeners
// on `document` itself on every re-render would leak one per render.
document.addEventListener("click",e=>{
  document.querySelectorAll(".im-combo").forEach(wrap=>{
    if(!wrap.contains(e.target)){
      const list=wrap.querySelector(".im-combo-list");
      if(list)list.classList.add("hidden");
    }
  });
});

function _mountCombo({searchId,hiddenId,listId,options,onSelect}){
  const input=document.getElementById(searchId), hidden=document.getElementById(hiddenId),
        list=document.getElementById(listId);
  if(!input||!hidden||!list)return null;
  let filtered=options, activeIdx=-1;
  const render=()=>{
    list.innerHTML=filtered.length
      ?filtered.map((o,i)=>`<div class="im-combo-opt${i===activeIdx?" active":""}" data-i="${i}">${esc(o.label)}</div>`).join("")
      :`<div class="im-combo-empty">No matches</div>`;
  };
  const close=()=>{list.classList.add("hidden");activeIdx=-1;};
  const pick=o=>{hidden.value=o.value;input.value=o.label;close();if(onSelect)onSelect(o.value);};
  input.addEventListener("input",()=>{
    const q=input.value.trim().toLowerCase();
    filtered=options.filter(o=>o.label.toLowerCase().includes(q));
    activeIdx=-1;render();list.classList.remove("hidden");
  });
  input.addEventListener("focus",()=>{filtered=options;activeIdx=-1;render();list.classList.remove("hidden");});
  input.addEventListener("keydown",e=>{
    if(e.key==="ArrowDown"){e.preventDefault();activeIdx=Math.min(activeIdx+1,filtered.length-1);render();}
    else if(e.key==="ArrowUp"){e.preventDefault();activeIdx=Math.max(activeIdx-1,0);render();}
    else if(e.key==="Enter"){e.preventDefault();if(filtered[activeIdx])pick(filtered[activeIdx]);}
    else if(e.key==="Escape"){close();}
  });
  list.addEventListener("mousedown",e=>{
    const opt=e.target.closest(".im-combo-opt");if(!opt)return;
    e.preventDefault();pick(filtered[+opt.dataset.i]);
  });
  return{
    setOptions(o){options=o;filtered=o;},
    setSelected(o){hidden.value=o?o.value:"";input.value=o?o.label:"";},
  };
}

function renderIdentityMerge(){
  const body=document.getElementById("identity-merge-body");
  if(!body)return;
  const identities=_identitiesCache||[];
  const groups=_identityGroupsCache||[];
  const suggestions=_suggestionsCache||[];
  const discarded=_discardedCache||[];
  const standalone=_standaloneCache||[];

  let html="";

  // Precompute once so the review section (needs `unmatched`) can render
  // ahead of the already-merged Identities table.
  const groupsByKey={};
  groups.forEach(g=>{groupsByKey[_identityMapKey(g.kind,g.user_id,g.proxy_name)]=g.aliases;});
  const canonicalRows=identities.filter(i=>i.kind==="user"||!i.merged_into);
  const pill=a=>`<span class="alias-pill">${esc(a)}<span class="alias-pill-x" title="Unmerge" onclick="doUnmergeIdentity('${esc(escJsAttr(a))}')">✕</span></span>`;
  const mergedRows=canonicalRows
    .map(row=>({row,aliases:groupsByKey[_identityMapKey(row.kind,row.user_id,row.proxy_name)]||[]}))
    .filter(x=>x.aliases.length>0);
  // Never merged into anything — nothing "resolved" here, but still worth
  // surfacing so obvious garbage (e.g. "2", "]") can be discarded even
  // when no fuzzy suggestion ever caught it. Standalone names are excluded:
  // the admin has already ruled "this is a real person, nothing to merge",
  // which is what lets this queue actually drain to empty. They stay in
  // `identities` (so they remain merge targets) and get their own section.
  const unmatched=canonicalRows.filter(row=>row.kind==="proxy"&&!row.standalone&&!(groupsByKey[_identityMapKey(row.kind,row.user_id,row.proxy_name)]||[]).length);

  // ── Review & merge: one unified, prioritized list (replaces the old
  // separate "Suggested merges" + "Unmatched proxy names" sections) —
  // exact matches first, then fuzzy suggestions (closer first), then
  // everything else by recency/frequency. See _buildUnifiedReviewRows.
  const reviewRows=_buildUnifiedReviewRows(unmatched,suggestions);
  if(reviewRows.length){
    const SHOW_N=12;
    const vis=reviewRows.slice(0,SHOW_N), extra=reviewRows.slice(SHOW_N);
    // exact_first_name is deliberately NOT given the same strong "chip-in"
    // styling as exact_username/exact_proxy — first names commonly collide
    // across different real people in the same group (see the confidence
    // assignment in services/identity.list_suggestions), so it's rendered
    // at the same "weaker signal" tier as a fuzzy "close" match instead of
    // looking equally certain to an admin skimming this list.
    const CONF_LABEL={exact_username:"✓ username match",exact_first_name:"≈ same first name",
                       exact_proxy:"✓ exact match",close:"≈ close spelling"};
    const STRONG_CONF=new Set(["exact_username","exact_proxy"]);
    const rowHtml=r=>{
      const{row,suggestion,otherLabel}=r;
      const confBadge=suggestion
        ?`<span class="itm-conf-badge ${STRONG_CONF.has(suggestion.confidence)?"chip-in":"chip-maybe"}">${CONF_LABEL[suggestion.confidence]}</span>`
        :"";
      const metaBits=[`${row.proxy_count||0}×`];
      if(row.proxy_last_seen)metaBits.push(_relTime(row.proxy_last_seen));
      const metaBadge=`<span class="itcount${(row.proxy_count||0)>3?" hot":""}">${esc(metaBits.join(" · "))}</span>`;
      const title=suggestion?`${esc(row.proxy_name)} ↔ ${esc(otherLabel)}`:esc(row.proxy_name);
      const mergeBtn=suggestion
        ?`<button class="id-change" title="Merge these" onclick="doMergeIdentity('${esc(escJsAttr(suggestion.alias_proxy_name))}','${suggestion.candidate_kind}',${suggestion.candidate_user_id!=null?suggestion.candidate_user_id:"null"},${suggestion.candidate_proxy_name!=null?`'${esc(escJsAttr(suggestion.candidate_proxy_name))}'`:"null"})">🔗</button>`
        :"";
      const dismissBtn=suggestion
        ?`<button class="id-change" title="Not a match" onclick="doDismissSuggestion(${_suggestionsCache.indexOf(suggestion)})">✕</button>`
        :"";
      // The "nothing to merge, and that's fine" exit — distinct from 🗑,
      // which throws the name away. Keeps it as a future merge target.
      const standaloneBtn=`<button class="id-change" title="'${esc(row.proxy_name)}' is a real person with no Telegram account — stop asking" onclick="doMarkStandalone('${esc(escJsAttr(row.proxy_name))}')">✅</button>`;
      return `<div class="sched-item">
        <div class="sched-item-info">
          <div class="sched-item-title">${title}</div>
          <div class="sched-item-time">${confBadge}${metaBadge}</div>
        </div>
        <div style="display:flex;gap:6px;flex-shrink:0">
          ${mergeBtn}${dismissBtn}${standaloneBtn}
          <button class="id-change" title="'${esc(row.proxy_name)}' is invalid/garbage — discard it" onclick="doDiscardIdentity('${esc(escJsAttr(row.proxy_name))}')">🗑</button>
        </div>
      </div>`;
    };
    html+=`<div style="font-size:.78rem;font-weight:600;color:var(--sub);margin-bottom:6px;cursor:pointer;display:flex;align-items:center;gap:6px;user-select:none" onclick="toggleIdentityUnmatched()">
      <span id="im-unmatched-chevron">${_identityUnmatchedOpen?"▲":"▼"}</span>Review & merge (${reviewRows.length})
    </div>`;
    html+=`<div id="im-unmatched-body" class="${_identityUnmatchedOpen?"":"hidden"}">`;
    html+=vis.map(rowHtml).join("");
    if(extra.length){
      html+=`<div class="alias-extra hidden">${extra.map(rowHtml).join("")}</div>`;
      html+=`<button class="alias-more-btn" onclick="_toggleExtraAliases(this)">+${extra.length} more</button>`;
    }
    html+=`</div>`;
  }

  // Dense 4-column table: Canonical | Type | Count | Aliases. Only rows
  // that actually have ≥1 alias — never-merged real members and bare
  // proxies have nothing to review here, so they're left out entirely
  // instead of cluttering the table with empty rows. Aliases render as
  // pills with their own inline ✕ (unmerge that one alias directly, no
  // dropdown/selection step); past 3 pills the rest collapse behind a
  // "+N more" toggle so one person with many aliases doesn't blow out
  // their row's height, and Count is highlighted once it crosses that
  // same threshold as a quick "worth a second look" signal.
  html+=`<div style="font-size:.78rem;font-weight:600;color:var(--sub);margin:${reviewRows.length?"14px":"0"} 0 6px">Identities</div>`;
  if(!mergedRows.length){
    html+=`<div class="sched-empty">No merges yet — use Review & merge above or pick manually below.</div>`;
  }else{
    html+=`<div class="identity-tbl-wrap"><table class="identity-tbl">
      <colgroup><col class="c-name"><col class="c-type"><col class="c-count"><col class="c-aliases"></colgroup>
      <thead><tr><th>Canonical</th><th>Type</th><th>Count</th><th>Aliases</th></tr></thead>
      <tbody>`;
    html+=mergedRows.map(({row,aliases})=>{
      const vis=aliases.slice(0,3), extra=aliases.slice(3);
      const cell=vis.map(pill).join("")
        +(extra.length?`<span class="alias-extra hidden">${extra.map(pill).join("")}</span><button class="alias-more-btn" onclick="_toggleExtraAliases(this)">+${extra.length} more</button>`:"");
      return `<tr>
        <td class="itn">${esc(row.display_name)}</td>
        <td><span class="itn-tag${row.kind==="proxy"?" proxy":""}">${row.kind==="proxy"?"Proxy":"Member"}</span></td>
        <td class="itcount${aliases.length>3?" hot":""}">${aliases.length}</td>
        <td>${cell}</td>
      </tr>`;
    }).join("");
    html+=`</tbody></table></div>`;
  }

  // Confirmed real people with no Telegram account. Out of the review
  // queue, but still live identities — restoring one puts it straight back
  // in the queue, and it stays pickable as a merge target either way.
  if(standalone.length){
    html+=`<div style="font-size:.78rem;font-weight:600;color:var(--sub);margin:14px 0 6px;cursor:pointer;display:flex;align-items:center;gap:6px;user-select:none" onclick="toggleIdentityStandalone()">
      <span id="im-standalone-chevron">${_identityStandaloneOpen?"▲":"▼"}</span>✅ Standalone people (${standalone.length})
    </div>`;
    html+=`<div id="im-standalone-body" class="${_identityStandaloneOpen?"":"hidden"}">`;
    html+=`<div style="font-size:.72rem;color:var(--sub);margin-bottom:6px">No Telegram account — still valid merge targets if a similar name turns up later.</div>`;
    html+=standalone.map(name=>`
      <span class="alias-pill">${esc(name)}<span class="alias-pill-x" title="Put back in the review queue" onclick="doUnmarkStandalone('${esc(escJsAttr(name))}')">✕</span></span>`).join("");
    html+=`</div>`;
  }

  if(discarded.length){
    html+=`<div style="font-size:.78rem;font-weight:600;color:var(--sub);margin:14px 0 6px;cursor:pointer;display:flex;align-items:center;gap:6px;user-select:none" onclick="toggleIdentityDiscarded()">
      <span id="im-discarded-chevron">${_identityDiscardedOpen?"▲":"▼"}</span>🗑 Discarded (${discarded.length})
    </div>`;
    html+=`<div id="im-discarded-body" class="${_identityDiscardedOpen?"":"hidden"}">`;
    html+=`<div class="discarded-grid">`;
    html+=discarded.map(name=>`
      <label style="display:flex;align-items:center;gap:5px;font-size:.8rem;color:var(--sub);min-width:0;padding:2px 0;cursor:pointer">
        <input type="checkbox" class="im-discard-cb" value="${esc(escJsAttr(name))}" style="flex-shrink:0">
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(name)}">${esc(name)}</span>
      </label>`).join("");
    html+=`</div>`;
    html+=`<button class="id-change" style="margin-top:8px" onclick="doUndiscardSelected()">↩ Restore selected</button>`;
    html+=`</div>`;
  }

  // Manual merge: FROM picker only offers unmerged proxies (a proxy that's
  // already an alias can't itself be re-picked as FROM without unmerging
  // first) that aren't confirmed standalone people — a standalone name is
  // a settled identity, so it may be merged INTO but never folded away
  // from here (restore it from the Standalone section first if that's
  // really the intent). TO picker excludes whatever's currently picked as
  // FROM (see _onImAliasChange) so the same identity can never appear on
  // both sides. Both pickers are searchable comboboxes (mounted below,
  // after the HTML is in the DOM) rather than native <select> — see
  // _mountCombo.
  const aliasProxies=identities.filter(i=>i.kind==="proxy"&&!i.merged_into&&!i.standalone);

  html+=`<div style="font-size:.78rem;font-weight:600;color:var(--sub);margin:14px 0 6px">Merge manually</div>`;
  if(!aliasProxies.length){
    html+=`<div class="sched-empty">No unmerged proxy names to merge.</div>`;
  }else{
    html+=`
      <div class="im-combo" style="margin-bottom:8px">
        <input type="text" id="im-alias-search" class="im-combo-input" placeholder="Search proxy names…" autocomplete="off">
        <input type="hidden" id="im-alias-select">
        <div class="im-combo-list hidden" id="im-alias-list"></div>
      </div>
      <div style="font-size:.75rem;color:var(--sub);margin-bottom:4px">merges into →</div>
      <div class="im-combo" style="margin-bottom:10px">
        <input type="text" id="im-target-search" class="im-combo-input" placeholder="Search members/proxies…" autocomplete="off">
        <input type="hidden" id="im-target-select">
        <div class="im-combo-list hidden" id="im-target-list"></div>
      </div>
      <button class="btn btn-primary" style="width:100%;padding:9px" onclick="doMergeIdentityManual()">🔗 Merge</button>`;
  }

  body.innerHTML=html;

  if(aliasProxies.length){
    const aliasOpts=aliasProxies.map(i=>({value:i.proxy_name,label:`${i.display_name} (proxy)`}));
    const aliasCombo=_mountCombo({
      searchId:"im-alias-search",hiddenId:"im-alias-select",listId:"im-alias-list",
      options:aliasOpts,
      onSelect:value=>window._onImAliasChange(value),
    });
    aliasCombo.setSelected(aliasOpts[0]);

    const targetOpts=_buildTargetOptions((aliasOpts[0].value||"").toLowerCase());
    _imTargetCombo=_mountCombo({
      searchId:"im-target-search",hiddenId:"im-target-select",listId:"im-target-list",
      options:targetOpts,
    });
    _imTargetCombo.setSelected(targetOpts[0]||null);
  }else{
    _imTargetCombo=null;
  }
}

window.doMergeIdentity=async function(aliasProxyName,candidateKind,candidateUserId,candidateProxyName){
  if(!await _confirmAction(`Merge "${aliasProxyName}" into this identity? Their stats and dues history will combine.`))return;
  try{
    const payload={id_token:_idToken,alias_proxy_name:aliasProxyName};
    if(candidateKind==="user")payload.canonical_user_id=candidateUserId;
    else payload.canonical_proxy_name=candidateProxyName;
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/identities/merge`,{
      method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload),
      signal:AbortSignal.timeout(10000),
    });
    if(!res.ok)throw new Error((await res.json().catch(()=>({}))).detail||"Failed to merge");
    toast(`🔗 Merged "${aliasProxyName}"`,2500);
    _identitiesCache=null;
    await loadIdentityMerge();
  }catch(e){toast(e.message||"Could not merge",4000);}
};

window.doMergeIdentityManual=async function(){
  const alias=document.getElementById("im-alias-select").value;
  const target=document.getElementById("im-target-select").value;
  if(!alias||!target)return;
  const candidateKind=target.startsWith("u:")?"user":"proxy";
  const candidateUserId=candidateKind==="user"?parseInt(target.slice(2),10):null;
  const candidateProxyName=candidateKind==="proxy"?target.slice(2):null;
  await window.doMergeIdentity(alias,candidateKind,candidateUserId,candidateProxyName);
};

window.doUnmergeIdentity=async function(aliasProxyName){
  if(!await _confirmAction(`Unmerge "${aliasProxyName}"? Its stats/dues will show separately again.`))return;
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/identities/unmerge`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id_token:_idToken,alias_proxy_name:aliasProxyName}),
      signal:AbortSignal.timeout(10000),
    });
    if(!res.ok)throw new Error((await res.json().catch(()=>({}))).detail||"Failed to unmerge");
    toast(`✂️ Unmerged "${aliasProxyName}"`,2500);
    _identitiesCache=null;
    await loadIdentityMerge();
  }catch(e){toast(e.message||"Could not unmerge",4000);}
};

window.doDismissSuggestion=async function(index){
  const s=(_suggestionsCache||[])[index];
  if(!s)return;
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/identities/suggestions/dismiss`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        id_token:_idToken,alias_proxy_name:s.alias_proxy_name,
        candidate_user_id:s.candidate_kind==="user"?s.candidate_user_id:null,
        candidate_proxy_name:s.candidate_kind==="proxy"?s.candidate_proxy_name:null,
      }),
      signal:AbortSignal.timeout(8000),
    });
    if(!res.ok)throw new Error((await res.json().catch(()=>({}))).detail||"Failed to dismiss");
    _suggestionsCache=_suggestionsCache.filter((_,i)=>i!==index);
    renderIdentityMerge();
  }catch(e){toast(e.message||"Could not dismiss",4000);}
};

window.doDiscardIdentity=async function(aliasProxyName){
  if(!await _confirmAction(`Discard "${aliasProxyName}"? It won't show up in suggestions or the merge picker anymore (this is reversible).`))return;
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/identities/discard`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id_token:_idToken,alias_proxy_name:aliasProxyName}),
      signal:AbortSignal.timeout(8000),
    });
    if(!res.ok)throw new Error((await res.json().catch(()=>({}))).detail||"Failed to discard");
    toast(`🗑 Discarded "${aliasProxyName}"`,2500);
    _identitiesCache=null;
    await loadIdentityMerge();
  }catch(e){toast(e.message||"Could not discard",4000);}
};

window.doUndiscardIdentity=async function(aliasProxyName){
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/identities/undiscard`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id_token:_idToken,alias_proxy_name:aliasProxyName}),
      signal:AbortSignal.timeout(8000),
    });
    if(!res.ok)throw new Error((await res.json().catch(()=>({}))).detail||"Failed to restore");
    toast(`↩ Restored "${aliasProxyName}"`,2500);
    _identitiesCache=null;
    await loadIdentityMerge();
  }catch(e){toast(e.message||"Could not restore",4000);}
};

window.doMarkStandalone=async function(aliasProxyName){
  if(!await _confirmAction(`Mark "${aliasProxyName}" as a real person with no Telegram account? It leaves the review queue but stays available as a merge target if a similar name shows up later.`))return;
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/identities/standalone`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id_token:_idToken,alias_proxy_name:aliasProxyName}),
      signal:AbortSignal.timeout(8000),
    });
    if(!res.ok)throw new Error((await res.json().catch(()=>({}))).detail||"Failed to mark standalone");
    toast(`✅ "${aliasProxyName}" marked standalone`,2500);
    _identitiesCache=null;
    await loadIdentityMerge();
  }catch(e){toast(e.message||"Could not mark standalone",4000);}
};

window.doUnmarkStandalone=async function(aliasProxyName){
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/identities/unstandalone`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id_token:_idToken,alias_proxy_name:aliasProxyName}),
      signal:AbortSignal.timeout(8000),
    });
    if(!res.ok)throw new Error((await res.json().catch(()=>({}))).detail||"Failed to restore");
    toast(`↩ "${aliasProxyName}" back in review`,2500);
    _identitiesCache=null;
    _identityStandaloneOpen=true;
    await loadIdentityMerge();
  }catch(e){toast(e.message||"Could not restore",4000);}
};

window.toggleIdentityStandalone=function(){
  _identityStandaloneOpen=!_identityStandaloneOpen;
  const body=document.getElementById("im-standalone-body");
  const ch=document.getElementById("im-standalone-chevron");
  if(body)body.classList.toggle("hidden",!_identityStandaloneOpen);
  if(ch)ch.textContent=_identityStandaloneOpen?"▲":"▼";
};

window.toggleIdentityDiscarded=function(){
  _identityDiscardedOpen=!_identityDiscardedOpen;
  const body=document.getElementById("im-discarded-body");
  const ch=document.getElementById("im-discarded-chevron");
  if(body)body.classList.toggle("hidden",!_identityDiscardedOpen);
  if(ch)ch.textContent=_identityDiscardedOpen?"▲":"▼";
};

window.doUndiscardSelected=async function(){
  const names=[...document.querySelectorAll(".im-discard-cb:checked")].map(cb=>cb.value);
  if(!names.length){toast("Select at least one to restore",2500);return;}
  try{
    const results=await Promise.all(names.map(name=>fetch(`/api/v1/web/group/${URL_TOKEN}/identities/undiscard`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id_token:_idToken,alias_proxy_name:name}),
      signal:AbortSignal.timeout(8000),
    })));
    const failed=results.filter(r=>!r.ok).length;
    if(failed)toast(`Restored ${names.length-failed}/${names.length} — ${failed} failed`,3500);
    else toast(`↩ Restored ${names.length}`,2500);
    _identitiesCache=null;
    _identityDiscardedOpen=true;
    await loadIdentityMerge();
  }catch(e){toast(e.message||"Could not restore selected",4000);}
};

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
// Waits for a pending weblogin redemption (see _weblogInRedeemPromise above)
// so load() doesn't run against a stale/absent _idToken while that's still
// in flight — a no-op .then(load) when there's nothing to wait for.
if(URL_TOKEN&&(URL_MODE==="join"||URL_MODE==="group")){
  if(_weblogInRedeemPromise)_weblogInRedeemPromise.then(load);
  else load();
}else{
  // No token in URL — show the home screen. This is also where Telegram's
  // menu button lands (it carries no chat context), so the boot below signs
  // in from initData and fills the group list from the server.
  renderHomeScreen();
  _bootHome().catch(()=>{});
}


// ── Ghost review (web half of the after-game "who ghosted?" prompt) ────────
// The prompt only ever existed in Telegram. Answering it is what FORGIVES
// everyone who turned up, so an admin who lives on this page had no way to
// clear absences and the reconfirm prompt slowly followed people around.
let _ghostSessions=null,_ghostSel={},_ghostShowOut={};

async function loadGhostReview(){
  const body=document.getElementById("ghost-review-body");
  if(!body||!_idToken)return;
  body.innerHTML='<div class="sched-empty">Loading…</div>';
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/ghost/sessions`,
      {headers:{"X-Identity-Token":_idToken},signal:AbortSignal.timeout(8000)});
    if(!res.ok)throw new Error((await res.json().catch(()=>({}))).detail||"Failed to load");
    const d=await res.json();
    _ghostSessions=d;
    _ghostSel={};_ghostShowOut={};
    renderGhostReview();
  }catch(e){
    body.innerHTML=`<div class="sched-empty">${esc(e.message||"Could not load")}</div>`;
  }
}

function _ghostKey(c){return c.proxy_name?("p:"+c.proxy_name):("u:"+c.user_id);}

function renderGhostReview(){
  const body=document.getElementById("ghost-review-body");
  if(!body||!_ghostSessions)return;
  const {sessions,ghost_tracking_enabled,autoforgive_days}=_ghostSessions;

  _syncGhostBadge();

  if(!ghost_tracking_enabled){
    body.innerHTML=`<div class="adm-row-s">Ghost tracking is off for this group, so there is nothing to review. Turn it on under Group settings if you want to track who doesn't turn up.</div>`;
    return;
  }
  if(!sessions.length){
    body.innerHTML=`<div class="sched-empty">Nothing waiting — every finished game has been reviewed. 🎉</div>`;
    return;
  }

  body.innerHTML=`<div class="adm-row-s" style="margin-bottom:12px">
    Tick anyone who didn't turn up. Everyone you leave unticked gets one past
    absence forgiven — that's what answering this actually does, so it's worth
    doing even when nobody ghosted.${autoforgive_days?` Unreviewed games are treated as "everyone attended" after ${autoforgive_days} days.`:""}
  </div>`+sessions.map(s=>_ghostSessionHtml(s)).join("");
}

function _ghostSessionHtml(s){
  const sel=_ghostSel[s.rollcall_id]||(_ghostSel[s.rollcall_id]=new Set());
  const showOut=!!_ghostShowOut[s.rollcall_id];
  const stayed=s.candidates.filter(c=>!c.was_out);
  const dropped=s.candidates.filter(c=>c.was_out);
  const when=s.ended_at?new Date(s.ended_at.endsWith("Z")?s.ended_at:s.ended_at+"Z"):null;
  const whenStr=when&&!isNaN(when)?when.toLocaleDateString(undefined,{weekday:"short",month:"short",day:"numeric"}):"";

  const row=c=>{
    const k=_ghostKey(c);
    const on=sel.has(k);
    return `<label class="gr-row${on?" on":""}">
      <input type="checkbox" ${on?"checked":""}
             onchange="toggleGhostPick(${s.rollcall_id},'${esc(escJsAttr(k))}')"/>
      <span class="gr-name">${esc(c.name)}</span>
      ${c.was_out?'<span class="gr-tag">was OUT</span>':""}
    </label>`;
  };

  // Drop-outs stay behind a disclosure: the normal question is "who said IN
  // and didn't come", and someone who told you they were out is only a
  // no-show if it was too late to replace them. That's a judgement call, so
  // it shouldn't be pre-mixed into the list.
  const dropHtml=dropped.length?(showOut
    ? `<div class="gr-sub">Dropped out late</div>${dropped.map(row).join("")}
       <button class="gr-more" onclick="toggleGhostOut(${s.rollcall_id})">Hide late drop-outs</button>`
    : `<button class="gr-more" onclick="toggleGhostOut(${s.rollcall_id})">＋ Someone who dropped out late (${dropped.length})</button>`
  ):"";

  return `<div class="gr-card">
    <div class="gr-hdr">
      <span class="gr-title">${esc(s.title)}</span>
      ${whenStr?`<span class="gr-when">${esc(whenStr)}</span>`:""}
    </div>
    ${stayed.length?stayed.map(row).join(""):'<div class="adm-row-s">Nobody was on the IN list.</div>'}
    ${dropHtml}
    <div class="gr-actions">
      <button class="btn btn-primary" style="flex:1" onclick="submitGhostReview(${s.rollcall_id})">
        ${sel.size?`Record ${sel.size} no-show${sel.size>1?"s":""}`:"Everyone showed up"}
      </button>
    </div>
  </div>`;
}

window.toggleGhostPick=function(rcId,key){
  const sel=_ghostSel[rcId]||(_ghostSel[rcId]=new Set());
  if(sel.has(key))sel.delete(key);else sel.add(key);
  renderGhostReview();
};

window.toggleGhostOut=function(rcId){
  _ghostShowOut[rcId]=!_ghostShowOut[rcId];
  renderGhostReview();
};

window.submitGhostReview=async function(rcId){
  if(!_idToken)return;
  const sel=[..._ghostSel[rcId]||[]];
  const names=sel.filter(k=>k.startsWith("p:")).map(k=>k.slice(2));
  const ids=sel.filter(k=>k.startsWith("u:")).map(k=>parseInt(k.slice(2),10)).filter(n=>!isNaN(n));
  const msg=sel.length
    ? `Record ${sel.length} no-show${sel.length>1?"s":""}? Everyone else gets one past absence forgiven.`
    : "Mark everyone as having turned up? Each of them gets one past absence forgiven.";
  if(!await _confirmAction(msg))return;
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/ghost/review`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id_token:_idToken,rollcall_id:rcId,
                           ghost_user_ids:ids,ghost_proxy_names:names}),
      signal:AbortSignal.timeout(10000),
    });
    if(!res.ok){const d=await res.json().catch(()=>({}));throw new Error(d.detail||"Failed");}
    const d=await res.json();
    toast(d.ghosts?`👻 ${d.ghosts} recorded · ${d.forgiven} forgiven`
                  :`✅ All present · ${d.forgiven} forgiven`,3500);
    await loadGhostReview();
  }catch(e){toast(e.message||"Could not save the review",4000);}
};

// A number on the menu entry, so a pending review is visible without opening
// the panel — an unanswered review is the thing that quietly keeps people on
// the reconfirm prompt, and nothing used to surface it here at all.
function _syncGhostBadge(){
  const item=document.getElementById("adm-mi-ghost");
  const badge=document.getElementById("adm-ghost-badge");
  if(!item||!badge)return;
  const n=(_ghostSessions&&_ghostSessions.ghost_tracking_enabled)
    ?(_ghostSessions.sessions||[]).length:0;
  item.classList.remove("hidden");
  badge.textContent=n?String(n):"›";
  badge.classList.toggle("gr-badge",!!n);
}

// Pull the pending count once admin status is known, so the badge is there
// before anyone opens the panel.
async function _peekGhostReview(){
  if(!_idToken||!_isWebAdmin)return;
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/ghost/sessions`,
      {headers:{"X-Identity-Token":_idToken},signal:AbortSignal.timeout(6000)});
    if(!res.ok)return;
    _ghostSessions=await res.json();
    _syncGhostBadge();
  }catch(_){}
}


// ── Owners & admins ───────────────────────────────────────────────────────
// Every grant used to be equal — there was no owner. Roles matter once a
// group stops asking Telegram who its admins are; they're surfaced now, while
// Telegram is still the authority, because after that switch nobody would
// have standing to grant ownership to anyone.
let _adminsData=null;

async function loadAdminsPanel(){
  const body=document.getElementById("admins-body");
  if(!body||!_idToken)return;
  body.innerHTML='<div class="sched-empty">Loading…</div>';
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/admins`,
      {headers:{"X-Identity-Token":_idToken},signal:AbortSignal.timeout(8000)});
    if(!res.ok)throw new Error((await res.json().catch(()=>({}))).detail||"Failed to load");
    _adminsData=await res.json();
    renderAdminsPanel();
  }catch(e){
    body.innerHTML=`<div class="sched-empty">${esc(e.message||"Could not load")}</div>`;
  }
}

function renderAdminsPanel(){
  const body=document.getElementById("admins-body");
  if(!body||!_adminsData)return;
  const {admins,you_are_owner,admin_source}=_adminsData;

  const source=admin_source==="local"
    ? `This group manages its own admin list.`
    : `Admins come from Telegram — whoever is an admin of the group is an admin here.`;

  const owners=admins.filter(a=>a.role==="owner").length;

  body.innerHTML=`<div class="adm-row-s" style="margin-bottom:12px">${source}${
    you_are_owner?"":" Only an owner can change roles."}</div>`+
    admins.map(a=>{
      const isOwner=a.role==="owner";
      // The last owner can't be stepped down — a group with none is one
      // nobody can administer again. Say why rather than failing on click.
      const lastOwner=isOwner&&owners<=1;
      const btn=!you_are_owner?""
        :lastOwner?`<span class="gr-tag">only owner</span>`
        :`<button class="btn btn-secondary" style="padding:6px 10px;font-size:.78rem"
             onclick="setAdminRole(${a.tg_user_id},'${isOwner?"admin":"owner"}')">${
             isOwner?"Step down":"Make owner"}</button>`;
      return `<div class="gr-row" style="cursor:default">
        <span class="gr-name">${esc(a.tg_name||("User "+a.tg_user_id))}${
          a.is_you?' <span class="gr-tag">you</span>':""}</span>
        ${isOwner?'<span class="gr-tag">👑 owner</span>':""}
        ${btn}
      </div>`;
    }).join("");
}

window.setAdminRole=async function(userId,role){
  if(!_idToken)return;
  const msg=role==="owner"
    ? "Make this person an owner? Owners can add and remove other owners."
    : "Step this owner down to admin?";
  if(!await _confirmAction(msg))return;
  try{
    const res=await fetch(`/api/v1/web/group/${URL_TOKEN}/admins/role`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id_token:_idToken,tg_user_id:userId,role}),
      signal:AbortSignal.timeout(8000),
    });
    if(!res.ok){const d=await res.json().catch(()=>({}));throw new Error(d.detail||"Failed");}
    _adminsData=await res.json();
    renderAdminsPanel();
    toast(role==="owner"?"👑 Owner added":"Stepped down to admin",2500);
  }catch(e){toast(e.message||"Could not change the role",4000);}
};


// ── Header group switcher ─────────────────────────────────────────────────
// Switching groups used to live inside the admin panel: admin-only, three
// taps deep, and framed as an admin action — but "wrong group" applies just
// as much to reading stats or casting a vote. It's a header control now, and
// it uses /portal/groups (every group you play in) rather than the admin-only
// list, so a member with two groups gets it too.
async function _loadHeaderGroups(){
  const wrap=document.getElementById("brand-group");
  const sel=document.getElementById("group-switch");
  if(!wrap||!sel||!IS_GROUP||!_idToken)return;
  try{
    // TWO sources, because neither is the whole picture:
    //   /portal/groups      groups you have VOTING HISTORY in
    //   /auth/admin/groups  groups you ADMINISTER
    // An admin who runs four groups but only plays in one appears in the
    // first list once — which hid the switcher from exactly the person most
    // likely to need it. The admin-panel switcher this replaced used the
    // second list, so using only the first was a narrowing.
    const hdrs={"X-Identity-Token":_idToken};
    const [mine,admin]=await Promise.all([
      fetch("/api/v1/portal/groups",{headers:hdrs,signal:AbortSignal.timeout(8000)})
        .then(r=>r.ok?r.json():{groups:[]}).catch(()=>({groups:[]})),
      fetch("/api/v1/auth/admin/groups",{headers:hdrs,signal:AbortSignal.timeout(8000)})
        .then(r=>r.ok?r.json():{groups:[]}).catch(()=>({groups:[]})),
    ]);
    const byToken=new Map();
    [...(mine.groups||[]),...(admin.groups||[])].forEach(g=>{
      if(!g||!g.group_web_token)return;
      const prev=byToken.get(g.group_web_token)||{};
      byToken.set(g.group_web_token,{...prev,...g,
        // has_active_rollcall only comes from the portal list; don't let the
        // admin entry blank it out when it merges second.
        has_active_rollcall:prev.has_active_rollcall||g.has_active_rollcall});
    });
    const groups=[...byToken.values()];

    // The group you're in might not be in that list — it lists groups you've
    // VOTED in, and you can be looking at one you haven't yet.
    if(!groups.some(g=>g.group_web_token===URL_TOKEN)){
      groups.unshift({group_web_token:URL_TOKEN,
                      group_name:(groupData&&groupData.group_name)||"This group"});
    }
    // One group needs no switcher — a dropdown with a single entry is furniture.
    if(groups.length<2){wrap.classList.add("hidden");return;}

    groups.sort((a,b)=>String(a.group_name||"").localeCompare(String(b.group_name||"")));
    sel.innerHTML=groups.map(g=>
      `<option value="${esc(g.group_web_token)}"${g.group_web_token===URL_TOKEN?" selected":""}>${
        esc(g.group_name||"Group")}${g.has_active_rollcall&&g.group_web_token!==URL_TOKEN?" ●":""}</option>`
    ).join("");
    wrap.classList.remove("hidden");
    // Tell the bar it now has one more thing to fit. On a phone the wordmark
    // yields to it: "which group" is information you need, "ROLLCALL" is
    // decoration you're already looking at.
    document.getElementById("brand-bar")?.classList.add("has-group-switch");
  }catch(_){/* the switcher is a convenience — never block the page on it */}
}

window.onHeaderGroupChange=function(token){
  if(!token||token===URL_TOKEN)return;
  window.location.href=`/web/group/${encodeURIComponent(token)}`;
};

})();
