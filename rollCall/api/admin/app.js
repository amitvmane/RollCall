(function(){
"use strict";

const API="/api/v1", LS="rc_admin_token";
const S={token:"",groups:[],selCid:null,tabState:{},memberCache:{},groupNames:{}};

// ─── Helpers ──────────────────────────────────────────────────────────────────
function $id(x){return document.getElementById(x)}
window.$id=$id;
function esc(s){return String(s??"")}
function escH(s){return String(s??"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
function cidK(cid){return "c"+String(cid).replace(/[-]/g,"_")}

// Datetime: "DD-MM-YYYY HH:MM" ↔ "YYYY-MM-DDTHH:MM"
function toInputDT(s){
  if(!s)return "";
  // API returns ISO 8601 e.g. "2026-06-23T10:35:00+05:30"
  const iso=s.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if(iso)return `${iso[1]}-${iso[2]}-${iso[3]}T${iso[4]}:${iso[5]}`;
  // Legacy "DD-MM-YYYY HH:MM" format
  const m=s.match(/^(\d{2})-(\d{2})-(\d{4})\s+(\d{2}):(\d{2})$/);
  return m?`${m[3]}-${m[2]}-${m[1]}T${m[4]}:${m[5]}`:"";
}
function fromInputDT(s){
  if(!s)return "cancel";
  const m=s.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/);
  return m?`${m[3]}-${m[2]}-${m[1]} ${m[4]}:${m[5]}`:"cancel";
}

// ─── Toast ────────────────────────────────────────────────────────────────────
function toast(msg,type="info",ms=3000){
  const el=$id("toast");
  el.textContent=msg;el.className="toast "+type+" show";
  clearTimeout(el._t);el._t=setTimeout(()=>el.classList.remove("show"),ms);
}

// ─── API ──────────────────────────────────────────────────────────────────────
class ApiError extends Error{constructor(msg,status){super(msg);this.status=status;}}
async function api(path,opts={}){
  const res=await fetch(API+path,{...opts,headers:{"Content-Type":"application/json","Authorization":"Bearer "+S.token,...(opts.headers||{})}});
  if(res.status===401){
    toast("Session expired — please sign in again.","err",4000);
    signOut();
    throw new ApiError("Unauthorized",401);
  }
  if(!res.ok){
    const b=await res.json().catch(()=>({}));
    const msg=b.detail||("HTTP "+res.status);
    if(res.status===403&&msg.includes("Token bound to")){showScopeWarn();}
    throw new ApiError(msg,res.status);
  }
  return res.status===204?null:res.json();
}
// If a rollcall operation fails because the rollcall no longer exists,
// immediately refresh the list so the stale row disappears.
function handleRcGone(cid,e){
  if(e instanceof ApiError&&e.status===404){
    toast("This rollcall has already ended — refreshing.","warn",4000);
    refreshRcList(cid).catch(()=>{});
    return true;
  }
  return false;
}

function showScopeWarn(){
  const w=$id("scope-warn");if(w)w.style.display="block";
}

// ─── Sidebar ─────────────────────────────────────────────────────────────────
const sidebar=$id("sidebar"),sbbdrop=$id("sbbdrop");
function openSB(){sidebar.classList.add("open");sbbdrop.classList.add("open")}
function closeSB(){sidebar.classList.remove("open");sbbdrop.classList.remove("open")}
$id("hbtn").addEventListener("click",()=>sidebar.classList.contains("open")?closeSB():openSB());
sbbdrop.addEventListener("click",closeSB);

// ─── Auth ─────────────────────────────────────────────────────────────────────
function signOut(){
  localStorage.removeItem(LS);S.token="";
  if(S.pollTimer){clearInterval(S.pollTimer);S.pollTimer=null;}
  $id("app").style.display="none";$id("login-screen").style.display="flex";$id("ti").value="";
}
$id("logout-btn").addEventListener("click",signOut);

async function doLogin(){
  const raw=$id("ti").value.trim();if(!raw)return;
  const err=$id("login-error");err.style.display="none";S.token=raw;
  const btn=$id("login-btn"),ti=$id("ti");
  btn.disabled=true;btn.textContent="Signing in…";ti.disabled=true;
  try{
    await api("/admin/groups");
    localStorage.setItem(LS,S.token);boot();
  }catch(e){
    S.token="";
    err.textContent=e.message==="Unauthorized"?"Invalid token or insufficient scope.":"Error: "+e.message;
    err.style.display="block";
  }finally{btn.disabled=false;btn.textContent="Sign in →";ti.disabled=false;}
}
$id("login-btn").addEventListener("click",doLogin);
$id("ti").addEventListener("keydown",e=>{if(e.key==="Enter")doLogin()});
function boot(){
  $id("login-screen").style.display="none";$id("app").style.display="block";loadGroups();
  if(S.pollTimer)clearInterval(S.pollTimer);
  S.pollTimer=setInterval(()=>{
    if(S.selCid&&(S.tabState[S.selCid]||0)===0)refreshRcList(S.selCid).catch(()=>{});
  },30000);
}
S.token=localStorage.getItem(LS)||"";
if(S.token)boot();

// ─── Groups ───────────────────────────────────────────────────────────────────
async function loadGroups(){
  // Clear member cache on group list refresh so stale data doesn't persist
  S.memberCache={};
  const el=$id("glist");el.innerHTML='<div style="padding:16px;color:var(--sub);font-size:.88rem">Loading…</div>';
  try{
    S.groups=await api("/admin/groups");
    S.groups.forEach(g=>{S.groupNames[g.chat_id]=g.group_name||("Chat "+g.chat_id)});
    renderGList();
  }catch(e){el.innerHTML=`<div style="padding:16px;color:var(--danger);font-size:.85rem">Error: ${escH(e.message)}</div>`;}
}
window.loadGroups=loadGroups;

function renderGList(){
  const el=$id("glist");
  if(!S.groups.length){el.innerHTML='<div style="padding:16px;color:var(--sub);font-size:.88rem">No groups found.</div>';return;}
  el.innerHTML=S.groups.map(g=>{
    const name=g.group_name||("Chat "+g.chat_id);
    const badge=g.active_rollcalls>0?`<span class="badge">${g.active_rollcalls}</span>`:"";
    return `<div class="gi${S.selCid===g.chat_id?" active":""}" data-cid="${g.chat_id}" role="button" tabindex="0">
      <div class="gn">${escH(name)}${badge}</div>
      <div class="gm">${escH(g.timezone)}</div>
    </div>`;
  }).join("");
  el.querySelectorAll(".gi").forEach(el=>{
    const cid=parseInt(el.dataset.cid);
    el.addEventListener("click",()=>selectGroup(cid));
    el.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" ")selectGroup(cid)});
  });
}

function selectGroup(cid){
  S.selCid=cid;renderGList();closeSB();
  const name=S.groupNames[cid]||("Chat "+cid);
  $id("crumb").textContent="› "+name;
  $id("no-sel").style.display="none";
  loadGDetail(cid);
}

// ─── Group detail ─────────────────────────────────────────────────────────────
async function loadGDetail(cid){
  // Invalidate member cache for this group on every explicit refresh/tab switch
  delete S.memberCache[cid];
  const el=$id("gdetail");el.innerHTML='<div class="lc"><div class="spinner"></div></div>';
  try{
    const [settings,rcs,tmpls]=await Promise.all([
      api("/admin/groups/"+cid),
      api("/chats/"+cid+"/rollcalls"),
      api("/chats/"+cid+"/templates").catch(()=>[]),
    ]);
    renderGDetail(settings,rcs,tmpls);
  }catch(e){
    el.innerHTML=`<div class="card"><p style="color:var(--danger)">Error loading group: ${escH(e.message)}</p></div>`;
  }
}
window.loadGDetail=loadGDetail;

function renderGDetail(settings,rcs,tmpls){
  const cid=settings.chat_id;
  const name=settings.group_name||("Chat "+cid);
  S.groupNames[cid]=name;
  const el=$id("gdetail");el.innerHTML="";

  el.innerHTML+=`
    <div class="scope-warn" id="scope-warn">
      <strong>Token scope warning</strong>
      A 403 was returned — your token may be bound to a different group. For full multi-group access, use a global admin token (chat_id=0).
    </div>
    <div class="card">
      <div class="card-header">
        <div>
          <h2>${escH(name)}</h2>
          <div class="card-sub" id="gsub-${cidK(cid)}">Chat ID: ${cid} &nbsp;·&nbsp; ${escH(settings.timezone)}</div>
        </div>
        <div class="btn-row">
          <button class="btn btn-ghost btn-sm" onclick="loadGDetail(${cid})">↻ Refresh</button>
        </div>
      </div>
    </div>`;

  const tabs=["Rollcalls","Templates","Settings"];
  el.innerHTML+=`<div class="tab-wrap">
    <div class="tab-bar" id="tabs-${cidK(cid)}">
      ${tabs.map((t,i)=>`<button class="tab${(S.tabState[cid]||0)===i?" active":""}" onclick="switchTab(${cid},${i})">${t}</button>`).join("")}
    </div>
  </div>`;

  el.innerHTML+=`
    <div id="panel-${cidK(cid)}-0">${buildRcPanel(cid,rcs,name)}</div>
    <div id="panel-${cidK(cid)}-1">${buildTmplPanel(cid,tmpls)}</div>
    <div id="panel-${cidK(cid)}-2">${buildSettingsPanel(cid,settings)}</div>`;

  switchTab(cid,S.tabState[cid]||0);
}

window.switchTab=function(cid,idx){
  S.tabState[cid]=idx;
  [0,1,2].forEach(i=>{
    const p=$id(`panel-${cidK(cid)}-${i}`);if(p)p.style.display=i===idx?"block":"none";
  });
  document.querySelectorAll(`#tabs-${cidK(cid)} .tab`).forEach((t,i)=>t.classList.toggle("active",i===idx));
};

// ─── Rollcalls panel ──────────────────────────────────────────────────────────
function buildRcPanel(cid,rcs,groupName){
  const rows=rcs.length
    ?rcs.map((rc,i)=>buildRcRowHtml(cid,rc,i,groupName)).join("")
    :`<div class="rc-empty">
        <div class="icon">📋</div>
        <p>No active rollcalls</p>
        <p class="rc-empty-sub">Tap <strong>+ Start new</strong> above, or use <code>/rc</code> in Telegram.</p>
      </div>`;
  const g=S.groups.find(x=>x.chat_id===cid);
  const linkBtn=g?.group_web_token
    ?`<button class="btn btn-ghost btn-sm" onclick="copyGroupLink(${cid})" title="Copy voting link for members">🔗 Copy link</button>`:"";
  return `<div class="card" id="rc-card-${cidK(cid)}">
    <div class="card-header">
      <h2>Active Rollcalls <span style="font-size:.8rem;font-weight:400;color:var(--sub)">(${rcs.length})</span></h2>
      <div class="btn-row">
        ${linkBtn}
        <button class="btn btn-primary btn-sm" onclick="showStartForm(${cid})">+ Start new</button>
      </div>
    </div>
    <div id="start-form-${cidK(cid)}" class="start-form" style="display:none">
      <label>Title (optional)</label>
      <input id="sft-${cidK(cid)}" type="text" placeholder="e.g. Friday Football" maxlength="100" autocorrect="off" autocapitalize="words"/>
      <div class="start-actions">
        <button class="btn btn-primary btn-sm" onclick="doStartRc(${cid})">Start rollcall</button>
        <button class="btn btn-ghost btn-sm" onclick="hideStartForm(${cid})">Cancel</button>
      </div>
    </div>
    <div id="rc-list-${cidK(cid)}">${rows}</div>
  </div>`;
}

function buildRcRowHtml(cid,rc,idx,groupName){
  const num=idx+1;
  const meta=[];
  if(rc.finalize_date)meta.push(`🕐 ${escH(rc.finalize_date)}`);
  if(rc.location)meta.push(`📍 ${escH(rc.location)}`);
  if(rc.event_fee)meta.push(`💰 ${escH(rc.event_fee)}`);
  return `<div class="rc-row" id="rcrow-${cidK(cid)}-${num}">
    <div class="rc-title">#${num} — ${escH(rc.title||"Untitled")}</div>
    <div class="pills">
      <span class="pill pill-in">${rc.in_count}${rc.limit?"/"+rc.limit:""} IN</span>
      <span class="pill pill-out">${rc.out_count} OUT</span>
      ${rc.maybe_count?`<span class="pill pill-maybe">${rc.maybe_count} MAYBE</span>`:""}
      ${rc.wait_count?`<span class="pill pill-wait">${rc.wait_count} WAIT</span>`:""}
    </div>
    ${meta.length?`<div class="rc-meta">${meta.join("")}</div>`:""}
    <div class="rc-actions">
      <button class="btn btn-ghost btn-sm" onclick="showRcDetails(${cid},${num})">✏ Manage</button>
      <button class="btn btn-danger btn-sm" id="end-btn-${cidK(cid)}-${num}" onclick="promptEndRc(${cid},${num})">End</button>
    </div>
    <div id="rc-detail-${cidK(cid)}-${num}"></div>
  </div>`;
}

window.showStartForm=function(cid){$id(`start-form-${cidK(cid)}`).style.display="block";$id(`sft-${cidK(cid)}`)?.focus()};
window.hideStartForm=function(cid){$id(`start-form-${cidK(cid)}`).style.display="none"};

window.doStartRc=async function(cid){
  const title=($id(`sft-${cidK(cid)}`)?.value||"").trim();
  try{
    await api(`/chats/${cid}/rollcalls`,{method:"POST",body:JSON.stringify({title:title||null,started_by_user_id:0,started_by_name:"Admin (web)"})});
    toast("Rollcall started!","ok");hideStartForm(cid);
    await refreshRcList(cid);
  }catch(e){toast("Error: "+e.message,"err",4000);}
};

window.promptEndRc=function(cid,num){
  const btn=$id(`end-btn-${cidK(cid)}-${num}`);
  if(!btn)return;
  btn.outerHTML=`<div class="confirm-row" id="end-confirm-${cidK(cid)}-${num}">
    <span class="confirm-label">End rollcall #${num}?</span>
    <button class="btn btn-danger btn-sm" onclick="doEndRc(${cid},${num})">Yes, end</button>
    <button class="btn btn-ghost btn-sm" onclick="cancelEndRc(${cid},${num})">Cancel</button>
  </div>`;
};

window.cancelEndRc=function(cid,num){
  const el=$id(`end-confirm-${cidK(cid)}-${num}`);
  if(el)el.outerHTML=`<button class="btn btn-danger btn-sm" id="end-btn-${cidK(cid)}-${num}" onclick="promptEndRc(${cid},${num})">End</button>`;
};

window.doEndRc=async function(cid,num){
  try{
    await api(`/chats/${cid}/rollcalls/${num}`,{method:"DELETE",body:JSON.stringify({ended_by_user_id:0,ended_by_name:"Admin (web)"})});
    toast("Rollcall #"+num+" ended.","ok");
    const rcs=await api(`/chats/${cid}/rollcalls`);
    const name=S.groupNames[cid]||("Chat "+cid);
    const el=$id(`rc-list-${cidK(cid)}`);
    if(el)el.innerHTML=rcs.length
      ?rcs.map((rc,i)=>buildRcRowHtml(cid,rc,i,name)).join("")
      :`<div class="rc-empty"><div class="icon">📋</div><p>No active rollcalls</p><p class="rc-empty-sub">Tap <strong>+ Start new</strong> above, or use <code>/rc</code> in Telegram.</p></div>`;
    updateGroupBadge(cid,rcs.length);
  }catch(e){if(!handleRcGone(cid,e))toast("Error: "+e.message,"err",4000);}
};

// ─── Rollcall detail / edit panel ─────────────────────────────────────────────
window.showRcDetails=async function(cid,num){
  // Highlight the row being edited, clear others
  document.querySelectorAll(`[id^="rcrow-${cidK(cid)}-"]`).forEach(el=>el.classList.remove("rc-row--editing"));
  $id(`rcrow-${cidK(cid)}-${num}`)?.classList.add("rc-row--editing");

  const area=$id(`rc-detail-${cidK(cid)}-${num}`);
  if(!area)return;
  area.innerHTML='<div class="lc"><div class="spinner"></div></div>';
  area.scrollIntoView({behavior:"smooth",block:"nearest"});
  try{
    const rc=await api(`/chats/${cid}/rollcalls/${num}`);
    const groupName=S.groupNames[cid]||("Chat "+cid);
    area.innerHTML=buildRcEditPanel(cid,num,rc,groupName);
    ["title","location","fee","limit","when","reminder"].forEach(prop=>{
      const el=$id(`pe-${prop}-${cidK(cid)}-${num}`);
      if(el){el.addEventListener("change",()=>markChanged(cid,num));el.addEventListener("input",()=>markChanged(cid,num));}
    });
    const rsel=$id(`reminder-sel-${cidK(cid)}-${num}`);
    if(rsel)rsel.addEventListener("change",()=>{
      const custom=$id(`reminder-custom-${cidK(cid)}-${num}`);
      if(custom)custom.style.display=rsel.value==="custom"?"block":"none";
      markChanged(cid,num);
    });
  }catch(e){
    if(handleRcGone(cid,e))area.innerHTML=`<p style="color:var(--sub);padding:10px">This rollcall has ended.</p>`;
    else area.innerHTML=`<p style="color:var(--danger);padding:10px">Error: ${escH(e.message)}</p>`;
  }
};

function markChanged(cid,num){
  const btn=$id(`save-btn-${cidK(cid)}-${num}`);
  if(btn)btn.classList.add("changed-hint");
}

function buildRcEditPanel(cid,num,rc,groupName){
  const whenVal=toInputDT(rc.finalize_date||"");
  const reminderHours=rc.reminder_hours;
  const stdHours=[1,2,6,12,24];
  const isCustom=reminderHours!=null&&!stdHours.includes(reminderHours);
  const reminderSelVal=reminderHours==null?"":isCustom?"custom":String(reminderHours);

  const sections=[
    {key:"in",  label:"IN",    items:rc.in_list||[],    cls:"in"},
    {key:"out", label:"OUT",   items:rc.out_list||[],   cls:"out"},
    {key:"maybe",label:"MAYBE",items:rc.maybe_list||[], cls:"maybe"},
    {key:"wait",label:"WAIT",  items:rc.wait_list||[],  cls:"wait"},
  ];

  function userRow({key,items}){
    return items.map((u,i)=>{
      const pb=u.is_proxy?'<span class="proxy-badge">P</span>':'<span class="real-badge">TG</span>';
      const cm=u.comment?`<span class="ur-comment">— ${escH(u.comment)}</span>`:"";
      const moveOpts=["in","out","maybe"].filter(s=>s!==key)
        .map(s=>`<option value="${s}">${s.toUpperCase()}</option>`).join("");
      const canRename=u.is_proxy;
      // Use data-* attrs instead of onclick="...JSON.stringify(name)..." — inline onclick
      // with JSON.stringify produces inner double-quotes that terminate the HTML attribute,
      // silently breaking every action for every user name.
      return `<div class="ur" data-cid="${cid}" data-num="${num}" data-uname="${escH(u.name)}" data-status="${key}">
        <span class="ur-pos">${i+1}</span>
        <span class="ur-name">${pb}${escH(u.name)}${cm}</span>
        <div class="ur-acts">
          <select class="move-sel" title="Move to another list">
            <option value="">Move →</option>${moveOpts}
          </select>
          ${canRename?`<button class="btn-icon ur-rename" title="Rename">✎</button>`:""}
          <button class="btn-icon del ur-del" title="Remove">✕</button>
        </div>
      </div>`;
    }).join("");
  }

  function voteSection({key,label,items,cls}){
    const cap=key==="in"&&rc.limit?` ${items.length}/${rc.limit}`:` (${items.length})`;
    const canAdd=key!=="wait";
    return `<div class="vs">
      <div class="vs-head">
        <span class="vs-label vs-${cls}">${label}${cap}</span>
        ${canAdd?`<button class="add-btn" onclick="toggleAddForm(${cid},${num},'${key}')">+ Add</button>`:""}
      </div>
      <div class="ur-list">${userRow({key,items})||`<div class="ur-empty">Empty</div>`}</div>
      ${canAdd?addMemberForm(cid,num,key):""}
    </div>`;
  }

  function addMemberForm(cid,num,key){
    const voteOpts=["in","out","maybe"].map(v=>
      `<label class="amf-radio"><input type="radio" name="amfv-${cidK(cid)}-${num}-${key}" value="${v}"${v===key?" checked":""}> ${v.toUpperCase()}</label>`
    ).join("");
    return `<div class="add-member-form" id="amf-${cidK(cid)}-${num}-${key}" style="display:none">
      <div class="amf-field">
        <label>Display name</label>
        <input id="amf-name-${cidK(cid)}-${num}-${key}" class="amf-inp" type="text" placeholder="Full name" maxlength="64" autocorrect="off"/>
      </div>
      <div class="amf-field">
        <label>Type</label>
        <div class="amf-radios">
          <label class="amf-radio"><input type="radio" name="amft-${cidK(cid)}-${num}-${key}" value="proxy" checked onchange="toggleTgSection(${cid},${num},'${key}',false)"> Proxy (no Telegram ID)</label>
          <label class="amf-radio"><input type="radio" name="amft-${cidK(cid)}-${num}-${key}" value="telegram" onchange="toggleTgSection(${cid},${num},'${key}',true)"> Telegram user</label>
        </div>
      </div>
      <div class="tg-section" id="amf-tg-${cidK(cid)}-${num}-${key}" style="display:none">
        <div class="tg-warn">Pick from known group members below. IDs come from the bot's member-tracking table — no external verification is done. Forcing a vote for a real user ID affects their stats and ghost tracking.</div>
        <select class="member-picker" id="amf-pick-${cidK(cid)}-${num}-${key}" onchange="fillMemberFields(${cid},${num},'${key}',this)">
          <option value="">⏳ Loading members…</option>
        </select>
        <input id="amf-uid-${cidK(cid)}-${num}-${key}" class="amf-inp" type="number" placeholder="User ID (auto-filled above or enter manually)" inputmode="numeric" style="margin-bottom:6px"/>
        <input id="amf-uname-${cidK(cid)}-${num}-${key}" class="amf-inp" type="text" placeholder="@username (optional)" autocorrect="off" autocapitalize="none"/>
      </div>
      <div class="amf-field">
        <label>Add as</label>
        <div class="amf-radios">${voteOpts}</div>
      </div>
      <div class="amf-actions">
        <button class="btn btn-primary btn-sm" onclick="doAddMember(${cid},${num},'${key}')">Add member</button>
        <button class="btn btn-ghost btn-sm" onclick="toggleAddForm(${cid},${num},'${key}')">Cancel</button>
      </div>
    </div>`;
  }

  return `<div class="edit-panel">
    <div class="ep-header">
      <div>
        <div class="ep-title">${escH(groupName)} → #${num}: ${escH(rc.title||"Untitled")}</div>
        <div class="ep-sub">Edit rollcall details and manage votes</div>
      </div>
      <button class="ep-close" onclick="closeEditPanel(${cid},${num})" title="Close">✕</button>
    </div>
    <div class="props-form">
      <div class="props-grid">
        <div class="fg">
          <label>Title</label>
          <input id="pe-title-${cidK(cid)}-${num}" type="text" value="${escH(rc.title||"")}" placeholder="Rollcall title" maxlength="100" data-original="${escH(rc.title||"")}"/>
        </div>
        <div class="fg">
          <label>Location</label>
          <input id="pe-location-${cidK(cid)}-${num}" type="text" value="${escH(rc.location||"")}" placeholder="e.g. Court A" data-original="${escH(rc.location||"")}"/>
        </div>
        <div class="fg">
          <label>Event fee</label>
          <input id="pe-fee-${cidK(cid)}-${num}" type="text" value="${escH(rc.event_fee||"")}" placeholder="e.g. ₹200" data-original="${escH(rc.event_fee||"")}"/>
        </div>
        <div class="fg">
          <label>IN cap (0 = unlimited)</label>
          <input id="pe-limit-${cidK(cid)}-${num}" type="number" value="${escH(String(rc.limit||0))}" min="0" inputmode="numeric" data-original="${escH(String(rc.limit||0))}"/>
        </div>
        <div class="fg">
          <label>Closes at</label>
          <input id="pe-when-${cidK(cid)}-${num}" type="datetime-local" value="${whenVal}" data-original="${whenVal}"/>
        </div>
        <div class="fg">
          <label>Reminder before close</label>
          <div class="reminder-row">
            <div class="fg" style="margin:0">
              <select id="reminder-sel-${cidK(cid)}-${num}" style="width:100%">
                <option value="" ${reminderSelVal===""?"selected":""}>No reminder</option>
                <option value="1"  ${reminderSelVal==="1"?"selected":""}>1 hour</option>
                <option value="2"  ${reminderSelVal==="2"?"selected":""}>2 hours</option>
                <option value="6"  ${reminderSelVal==="6"?"selected":""}>6 hours</option>
                <option value="12" ${reminderSelVal==="12"?"selected":""}>12 hours</option>
                <option value="24" ${reminderSelVal==="24"?"selected":""}>24 hours</option>
                <option value="custom" ${reminderSelVal==="custom"?"selected":""}>Custom…</option>
              </select>
            </div>
            <input id="pe-reminder-${cidK(cid)}-${num}" type="number" class="reminder-custom" min="1"
              value="${escH(String(reminderHours||""))}"
              placeholder="hrs"
              data-original="${escH(String(reminderHours||""))}"
              style="display:${isCustom?"block":"none"}"/>
          </div>
        </div>
      </div>
      <div class="props-actions">
        <button class="btn btn-primary btn-sm" id="save-btn-${cidK(cid)}-${num}" onclick="saveAllProps(${cid},${num})">Save changes</button>
        <button class="btn btn-ghost btn-sm" onclick="showRcDetails(${cid},${num})">Reset</button>
      </div>
    </div>
    <hr class="sep" style="margin:0 16px"/>
    <div class="manage-header">
      <span class="manage-title">Manage members</span>
      <span class="manage-hint">Move or remove applies instantly</span>
    </div>
    <div class="vote-grid">${sections.map(voteSection).join("")}</div>
  </div>`;
}

window.closeEditPanel=function(cid,num){
  $id(`rcrow-${cidK(cid)}-${num}`)?.classList.remove("rc-row--editing");
  const area=$id(`rc-detail-${cidK(cid)}-${num}`);
  if(area)area.innerHTML="";
};

// ─── Save all changed props ───────────────────────────────────────────────────
window.saveAllProps=async function(cid,num){
  const btn=$id(`save-btn-${cidK(cid)}-${num}`);
  const vals={
    title:$id(`pe-title-${cidK(cid)}-${num}`)?.value??null,
    location:$id(`pe-location-${cidK(cid)}-${num}`)?.value??null,
    fee:$id(`pe-fee-${cidK(cid)}-${num}`)?.value??null,
    limit:$id(`pe-limit-${cidK(cid)}-${num}`)?.value??null,
    when:$id(`pe-when-${cidK(cid)}-${num}`)?.value??null,
    reminder:(()=>{
      const sel=$id(`reminder-sel-${cidK(cid)}-${num}`);
      if(!sel)return null;
      if(sel.value==="custom")return $id(`pe-reminder-${cidK(cid)}-${num}`)?.value??null;
      return sel.value;
    })(),
  };
  const changed=Object.keys(vals).filter(k=>{
    const el=$id(`pe-${k}-${cidK(cid)}-${num}`);
    if(!el)return vals[k]!==null&&String(vals[k]||"")!=="";
    return String(vals[k]??"")!==String(el.dataset?.original??"");
  });
  if(!changed.length){toast("No changes to save.","info");return;}
  if(btn){btn.disabled=true;btn.textContent="Saving…";}
  const errors=[];
  let rcGone=false;
  for(const prop of changed){
    try{await saveProp(cid,num,prop,vals[prop]);}
    catch(e){
      if(handleRcGone(cid,e)){rcGone=true;break;}
      errors.push(`${prop}: ${e.message}`);
    }
  }
  if(btn){btn.disabled=false;btn.textContent="Save changes";btn.classList.remove("changed-hint");}
  if(rcGone)return;
  if(errors.length){toast("Some saves failed: "+errors.join("; "),"err",5000);}
  else{toast("Changes saved!","ok");}
  await refreshRcList(cid);
  await showRcDetails(cid,num);
};

async function saveProp(cid,num,prop,rawVal){
  const endpoints={
    title:`/chats/${cid}/rollcalls/${num}/settings/title`,
    location:`/chats/${cid}/rollcalls/${num}/settings/location`,
    fee:`/chats/${cid}/rollcalls/${num}/settings/fee`,
    limit:`/chats/${cid}/rollcalls/${num}/settings/limit`,
    when:`/chats/${cid}/rollcalls/${num}/settings/when`,
    reminder:`/chats/${cid}/rollcalls/${num}/settings/reminder`,
  };
  const bodies={
    title:{title:String(rawVal||""),admin_user_id:0,admin_name:"Admin (web)"},
    location:{location:String(rawVal||""),admin_user_id:0,admin_name:"Admin (web)"},
    fee:{fee:String(rawVal||""),admin_user_id:0,admin_name:"Admin (web)"},
    limit:{limit:parseInt(rawVal)||0,admin_user_id:0,admin_name:"Admin (web)"},
    when:{datetime_str:fromInputDT(rawVal||""),admin_user_id:0,admin_name:"Admin (web)"},
    reminder:{hours:rawVal?parseInt(rawVal)||null:null,admin_user_id:0,admin_name:"Admin (web)"},
  };
  await api(endpoints[prop],{method:"PUT",body:JSON.stringify(bodies[prop])});
}

// ─── User management ──────────────────────────────────────────────────────────
window.doRemoveUser=async function(cid,num,name){
  try{
    await api(`/chats/${cid}/rollcalls/${num}/users/${encodeURIComponent(name)}`,
      {method:"DELETE",body:JSON.stringify({admin_user_id:0,admin_name:"Admin (web)"})});
    toast(`${name} removed.`,"ok");
    await refreshRcList(cid);await showRcDetails(cid,num);
  }catch(e){if(!handleRcGone(cid,e))toast("Error: "+e.message,"err",4000);}
};

window.doMoveUser=async function(cid,num,name,newStatus){
  if(!newStatus)return;
  try{
    await api(`/chats/${cid}/rollcalls/${num}/users/${encodeURIComponent(name)}/status`,
      {method:"PATCH",body:JSON.stringify({admin_user_id:0,admin_name:"Admin (web)",new_status:newStatus})});
    toast(`${name} → ${newStatus.toUpperCase()}`,"ok");
    await refreshRcList(cid);await showRcDetails(cid,num);
  }catch(e){if(!handleRcGone(cid,e))toast("Error: "+e.message,"err",4000);}
};

// ─── Rename proxy (delete + re-add) ──────────────────────────────────────────
// showRenameRow now receives the .ur row element from event delegation —
// avoids passing names through onclick attributes (same double-quote truncation bug).
window.showRenameRow=function(rowEl){
  // Remove stale rename row left behind by a previous click
  if(rowEl.nextElementSibling?.classList.contains('rename-row'))
    rowEl.nextElementSibling.remove();
  const{cid,num,uname,status}=rowEl.dataset;
  rowEl.insertAdjacentHTML("afterend",
    `<div class="rename-row" data-cid="${cid}" data-num="${num}" data-oldname="${escH(uname)}" data-status="${status}">
      <input class="rename-inp" value="${escH(uname)}" maxlength="64" autocorrect="off"/>
      <button class="btn btn-primary btn-xs do-rename-save">Save</button>
      <button class="btn btn-ghost btn-xs do-rename-cancel">Cancel</button>
    </div>`);
  rowEl.nextElementSibling.querySelector('.rename-inp')?.focus();
};

// newName is read from the input directly by the event delegation — no ID lookup needed.
window.doRenameProxy=async function(cid,num,oldName,currentStatus,newName){
  if(!newName){toast("Name cannot be empty","err");return;}
  if(newName===oldName){return;}
  try{
    await api(`/chats/${cid}/rollcalls/${num}/users/${encodeURIComponent(oldName)}`,
      {method:"DELETE",body:JSON.stringify({admin_user_id:0,admin_name:"Admin (web)"})});
    try{
      await api(`/chats/${cid}/rollcalls/${num}/proxy-votes`,{
        method:"POST",
        body:JSON.stringify({vote:currentStatus,admin_user_id:0,admin_name:"Admin (web)",proxy_name:newName})
      });
    }catch(e2){
      toast(`Rename failed after removing ${escH(oldName)} — re-add them manually.`,"err",6000);
      await refreshRcList(cid);await showRcDetails(cid,num);
      return;
    }
    toast(`Renamed to ${newName}`,"ok");
    await refreshRcList(cid);await showRcDetails(cid,num);
  }catch(e){if(!handleRcGone(cid,e))toast("Error: "+e.message,"err",4000);}
};

// ─── Add member ───────────────────────────────────────────────────────────────
window.toggleAddForm=function(cid,num,key){
  const el=$id(`amf-${cidK(cid)}-${num}-${key}`);
  if(!el)return;
  const opening=el.style.display==="none"||!el.style.display;
  el.style.display=opening?"block":"none";
  if(opening){
    $id(`amf-name-${cidK(cid)}-${num}-${key}`)?.focus();
    loadGroupMembers(cid).then(members=>populateMemberPicker(cid,num,key,members));
  }
};

window.toggleTgSection=function(cid,num,key,show){
  const el=$id(`amf-tg-${cidK(cid)}-${num}-${key}`);
  if(el)el.style.display=show?"block":"none";
};

// Members cache: {members: [...], _ts: timestamp_ms}. TTL = 5 minutes.
async function loadGroupMembers(cid){
  const cached=S.memberCache[cid];
  if(cached&&(Date.now()-cached._ts)<300000)return cached.members;
  try{
    const members=await api(`/chats/${cid}/members`);
    S.memberCache[cid]={members,_ts:Date.now()};
    return members;
  }catch{return[];}
}

function populateMemberPicker(cid,num,key,members){
  const sel=$id(`amf-pick-${cidK(cid)}-${num}-${key}`);
  if(!sel)return;
  if(!members.length){sel.innerHTML='<option value="">No members tracked yet</option>';return;}
  sel.innerHTML='<option value="">— Pick a member —</option>'+
    members.map(m=>{
      const label=m.first_name+(m.username?` @${m.username}`:"")+` (${m.user_id})`;
      return `<option value="${m.user_id}" data-name="${escH(m.first_name)}" data-uname="${escH(m.username||"")}">${escH(label)}</option>`;
    }).join("");
}

window.fillMemberFields=function(cid,num,key,sel){
  const opt=sel.options[sel.selectedIndex];
  const uid=sel.value;
  const nameEl=$id(`amf-uid-${cidK(cid)}-${num}-${key}`);
  const unameEl=$id(`amf-uname-${cidK(cid)}-${num}-${key}`);
  const nameInp=$id(`amf-name-${cidK(cid)}-${num}-${key}`);
  if(nameEl)nameEl.value=uid||"";
  if(unameEl)unameEl.value=opt?.dataset.uname||"";
  if(nameInp&&opt&&uid)nameInp.value=opt.dataset.name||"";
};

window.doAddMember=async function(cid,num,key){
  const nameEl=$id(`amf-name-${cidK(cid)}-${num}-${key}`);
  const name=(nameEl?.value||"").trim();
  if(!name){nameEl?.focus();toast("Name is required","err",3000);return;}
  const typeEl=document.querySelector(`[name="amft-${cidK(cid)}-${num}-${key}"]:checked`);
  const voteEl=document.querySelector(`[name="amfv-${cidK(cid)}-${num}-${key}"]:checked`);
  const memberType=typeEl?.value||"proxy";
  const vote=voteEl?.value||key;
  try{
    if(memberType==="proxy"){
      await api(`/chats/${cid}/rollcalls/${num}/proxy-votes`,{
        method:"POST",
        body:JSON.stringify({vote,admin_user_id:0,admin_name:"Admin (web)",proxy_name:name})
      });
    }else{
      const uidEl=$id(`amf-uid-${cidK(cid)}-${num}-${key}`);
      const unameEl=$id(`amf-uname-${cidK(cid)}-${num}-${key}`);
      const userId=parseInt(uidEl?.value)||0;
      if(!userId){toast("Telegram User ID is required","err",3500);uidEl?.focus();return;}
      const username=(unameEl?.value||"").trim().replace(/^@/,"");
      await api(`/chats/${cid}/rollcalls/${num}/votes`,{
        method:"POST",
        body:JSON.stringify({vote,user_id:userId,first_name:name,username:username||null})
      });
    }
    toast(`${name} added as ${vote.toUpperCase()}`,"ok");
    if(nameEl)nameEl.value="";
    $id(`amf-${cidK(cid)}-${num}-${key}`).style.display="none";
    await refreshRcList(cid);await showRcDetails(cid,num);
  }catch(e){if(!handleRcGone(cid,e))toast("Error: "+e.message,"err",4000);}
};

async function refreshRcList(cid){
  try{
    const rcs=await api(`/chats/${cid}/rollcalls`);
    const name=S.groupNames[cid]||("Chat "+cid);
    const el=$id(`rc-list-${cidK(cid)}`);
    if(el)el.innerHTML=rcs.length
      ?rcs.map((rc,i)=>buildRcRowHtml(cid,rc,i,name)).join("")
      :`<div class="rc-empty"><div class="icon">📋</div><p>No active rollcalls</p><p class="rc-empty-sub">Tap <strong>+ Start new</strong> above, or use <code>/rc</code> in Telegram.</p></div>`;
    updateGroupBadge(cid,rcs.length);
  }catch(e){toast("Could not refresh rollcall list — "+e.message,"err",4000);}
}

// ─── Templates ────────────────────────────────────────────────────────────────
const ADM_DAYS=["sunday","monday","tuesday","wednesday","thursday","friday","saturday"];
function admNextRun(schedDay,schedTime){
  const tgt=ADM_DAYS.indexOf((schedDay||"").toLowerCase());
  if(tgt<0||!schedTime)return null;
  const[h,m]=(schedTime||"00:00").split(":").map(Number);
  const now=new Date();
  let diff=(tgt-now.getDay()+7)%7;
  if(diff===0&&(now.getHours()*60+now.getMinutes())>=h*60+m)diff=7;
  const d=new Date(now);
  d.setDate(now.getDate()+diff);d.setHours(h,m,0,0);
  return d;
}
function buildTmplPanel(cid,tmpls){
  const rows=tmpls.length?tmpls.map(t=>{
    const schedEnabled=t.schedule_enabled;
    let schedBadge="";
    if(schedEnabled&&t.schedule_day&&t.schedule_time){
      const next=admNextRun(t.schedule_day,t.schedule_time);
      const nextStr=next?next.toLocaleDateString(undefined,{weekday:"short",month:"short",day:"numeric"})+" "+next.toLocaleTimeString(undefined,{hour:"2-digit",minute:"2-digit"}):"";
      schedBadge=`<span class="sched-badge">📅 ${escH(t.schedule_day)} ${escH(t.schedule_time)}${t.recurrence_type==="biweekly"?" (biweekly)":t.recurrence_type==="monthly"?" (monthly)":""}${nextStr?" · next: "+nextStr:""}</span>`;
    }
    const meta=[t.limit?`👥 ${t.limit}`:"",t.location?`📍 ${escH(t.location)}`:"",t.fee?`💰 ${escH(t.fee)}`:""].filter(Boolean).join("  ");
    const eid=`te-${cidK(cid)}-${CSS.escape?CSS.escape(t.name):t.name.replace(/[^a-z0-9]/gi,"_")}`;
    return `<div class="tmpl-row" id="tmplrow-${eid}">
      <div style="flex:1;min-width:0">
        <div class="tmpl-name">${escH(t.name)}</div>
        <div class="tmpl-meta">${escH(t.title||"")}${meta?"  "+meta:""}</div>
        ${schedBadge}
      </div>
      <div style="display:flex;gap:6px;flex-shrink:0">
        <button class="btn btn-ghost btn-sm" onclick="toggleTmplEdit(${cid},${JSON.stringify(t.name)})">✎ Edit</button>
        <button class="btn btn-success btn-sm" onclick="doStartTmpl(${cid},${JSON.stringify(escH(t.name))},this)">▶ Start</button>
      </div>
    </div>
    <div class="tmpl-edit-panel" id="tmpledit-${eid}" style="display:none">
      ${buildTmplEditForm(cid,t,eid)}
    </div>`;
  }).join("")
  :`<div class="tmpl-empty">
      <div class="te-icon">📋</div>
      <p>No templates saved for this group</p>
      <p class="te-hint">In Telegram, use <code>/template save &lt;name&gt;</code> after setting up a rollcall to save it as a reusable template.</p>
    </div>`;
  return `<div class="card"><div class="card-header"><h2>Templates</h2></div><div id="tmpl-list-${cidK(cid)}">${rows}</div></div>`;
}
function buildTmplEditForm(cid,t,eid){
  const days=["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"];
  const dayOpts=days.map(d=>`<option value="${d.toLowerCase()}"${(t.schedule_day||"").toLowerCase()===d.toLowerCase()?" selected":""}>${d}</option>`).join("");
  const recOpts=["weekly","biweekly","monthly"].map(r=>`<option value="${r}"${(t.recurrence_type||"weekly")===r?" selected":""}>${r}</option>`).join("");
  return `<div class="tmpl-ef">
    <div class="tmpl-ef-row">
      <label>Title</label>
      <input id="tef-title-${eid}" class="amf-inp" value="${escH(t.title||"")}" placeholder="Rollcall title"/>
    </div>
    <div class="tmpl-ef-row">
      <label>Location</label>
      <input id="tef-loc-${eid}" class="amf-inp" value="${escH(t.location||"")}" placeholder="Location"/>
    </div>
    <div class="tmpl-ef-row" style="display:flex;gap:8px">
      <div style="flex:1"><label>Fee</label><input id="tef-fee-${eid}" class="amf-inp" value="${escH(t.fee||"")}" placeholder="₹0"/></div>
      <div style="flex:1"><label>Cap</label><input id="tef-limit-${eid}" class="amf-inp" type="number" min="1" value="${t.limit||""}" placeholder="No limit"/></div>
    </div>
    <div class="tmpl-ef-divider">Auto-schedule</div>
    <div class="tmpl-ef-row" style="display:flex;gap:8px;align-items:flex-end">
      <div style="flex:2">
        <label>Day</label>
        <select id="tef-sday-${eid}" class="amf-inp">${dayOpts}</select>
      </div>
      <div style="flex:1">
        <label>Time</label>
        <input id="tef-stime-${eid}" class="amf-inp" type="time" value="${t.schedule_time||""}"/>
      </div>
      <div style="flex:1">
        <label>Repeat</label>
        <select id="tef-rec-${eid}" class="amf-inp">${recOpts}</select>
      </div>
    </div>
    <div class="tmpl-ef-row" style="display:flex;gap:8px">
      <div style="flex:2"><label>Event day (rollcall closes on)</label><select id="tef-eday-${eid}" class="amf-inp"><option value="">Same as schedule day</option>${days.map(d=>`<option value="${d.toLowerCase()}"${(t.event_day||"").toLowerCase()===d.toLowerCase()?" selected":""}>${d}</option>`).join("")}</select></div>
      <div style="flex:1"><label>Event time</label><input id="tef-etime-${eid}" class="amf-inp" type="time" value="${t.event_time||""}"/></div>
    </div>
    <div class="tmpl-ef-actions">
      <button class="btn btn-primary btn-sm" onclick="saveTmplEdit(${cid},${JSON.stringify(t.name)},'${eid}')">Save</button>
      <button class="btn btn-ghost btn-sm" onclick="toggleTmplEdit(${cid},${JSON.stringify(t.name)})">Cancel</button>
    </div>
  </div>`;
}
window.toggleTmplEdit=function(cid,name){
  const eid=`te-${cidK(cid)}-${name.replace(/[^a-z0-9]/gi,"_")}`;
  const el=$id(`tmpledit-${eid}`);
  if(el)el.style.display=el.style.display==="none"?"block":"none";
};
window.saveTmplEdit=async function(cid,name,eid){
  const g=id=>{const el=$id(id);return el?el.value.trim():""};
  const body={
    admin_user_id:0,admin_name:"Admin (web)",
    title:g(`tef-title-${eid}`)||null,
    location:g(`tef-loc-${eid}`)||null,
    fee:g(`tef-fee-${eid}`)||null,
    limit:parseInt(g(`tef-limit-${eid}`))||null,
    event_day:g(`tef-eday-${eid}`)||null,
    event_time:g(`tef-etime-${eid}`)||null,
  };
  const schedDay=g(`tef-sday-${eid}`);
  const schedTime=g(`tef-stime-${eid}`);
  const recType=g(`tef-rec-${eid}`)||"weekly";
  try{
    await api(`/chats/${cid}/templates/${encodeURIComponent(name)}`,{method:"PUT",body:JSON.stringify(body)});
    if(schedDay&&schedTime){
      await api(`/chats/${cid}/templates/${encodeURIComponent(name)}/schedule`,{
        method:"PUT",
        body:JSON.stringify({admin_user_id:0,admin_name:"Admin (web)",schedule_day:schedDay,schedule_time:schedTime,recurrence_type:recType})
      });
    }
    toast("Template saved!","ok");
    const tmpls=await api(`/chats/${cid}/templates`);
    const el=$id(`panel-${cidK(cid)}-1`);
    if(el)el.innerHTML=buildTmplPanel(cid,tmpls);
  }catch(e){toast("Error: "+e.message,"err",4000);}
};

window.doStartTmpl=async function(cid,name,btn){
  if(btn){btn.disabled=true;btn.textContent="Starting…";}
  try{
    await api(`/chats/${cid}/templates/${encodeURIComponent(name)}/start`,{method:"POST",body:JSON.stringify({admin_user_id:0,admin_name:"Admin (web)"})});
    toast("Rollcall started from template!","ok");
    switchTab(cid,0);
    await refreshRcList(cid);
  }catch(e){toast("Error: "+e.message,"err",4000);}finally{if(btn){btn.disabled=false;btn.textContent="▶ Start";}}
};

// ─── Settings ─────────────────────────────────────────────────────────────────
function buildSettingsPanel(cid,s){
  return `<div class="card">
    <div class="card-header"><h2>Group Settings</h2></div>
    <div class="setting-row">
      <div><div class="setting-label">Admin-only mode</div><div class="setting-desc">Only Telegram admins can use bot commands</div></div>
      <div class="setting-ctrl"><label class="toggle"><input type="checkbox" ${s.admin_rights?"checked":""} onchange="patchSetting(${cid},'admin_rights',this.checked,this)"><span class="slider"></span></label></div>
    </div>
    <div class="setting-row">
      <div><div class="setting-label">Silent mode (shh)</div><div class="setting-desc">Bot confirms quietly instead of broadcasting</div></div>
      <div class="setting-ctrl"><label class="toggle"><input type="checkbox" ${s.shh_mode?"checked":""} onchange="patchSetting(${cid},'shh_mode',this.checked,this)"><span class="slider"></span></label></div>
    </div>
    <div class="setting-row">
      <div><div class="setting-label">Ghost tracking</div><div class="setting-desc">Track members who RSVP IN but don't show up</div></div>
      <div class="setting-ctrl"><label class="toggle"><input type="checkbox" ${s.ghost_tracking_enabled?"checked":""} onchange="patchSetting(${cid},'ghost_tracking_enabled',this.checked,this)"><span class="slider"></span></label></div>
    </div>
    <div class="setting-row">
      <div><div class="setting-label">Ghost limit</div><div class="setting-desc">Missed sessions before a warning triggers</div></div>
      <div class="setting-ctrl">
        <input class="mini-inp" type="number" id="ts-lim-${cidK(cid)}" value="${escH(String(s.absent_limit))}" min="1" max="99" inputmode="numeric"/>
        <button class="btn btn-ghost btn-sm" onclick="saveAbsentLimit(${cid})">Save</button>
      </div>
    </div>
    <div class="setting-row" style="border-bottom:none;flex-wrap:wrap;gap:10px">
      <div><div class="setting-label">Timezone</div><div class="setting-desc">IANA tz for scheduling and rollcall times</div></div>
      <div style="width:100%;display:flex;gap:8px;align-items:center">
        <input list="tz-list" class="tz-inp" id="ts-tz-${cidK(cid)}" value="${escH(s.timezone)}" autocorrect="off" autocapitalize="none" spellcheck="false" placeholder="Asia/Kolkata"/>
        <button class="btn btn-ghost btn-sm" onclick="saveTz(${cid})">Save</button>
      </div>
    </div>
  </div>`;
}

window.patchSetting=async function(cid,key,value,checkboxEl){
  try{
    await api(`/admin/groups/${cid}`,{method:"PATCH",body:JSON.stringify({admin_user_id:0,admin_name:"Admin (web)",[key]:value})});
    toast("Setting saved.","ok");
  }catch(e){
    if(checkboxEl)checkboxEl.checked=!value;
    toast("Error: "+e.message,"err",4000);
  }
};

window.saveAbsentLimit=async function(cid){
  const v=parseInt($id(`ts-lim-${cidK(cid)}`)?.value);
  if(!v||v<1){toast("Limit must be ≥ 1","err",3000);return;}
  try{
    await api(`/admin/groups/${cid}`,{method:"PATCH",body:JSON.stringify({admin_user_id:0,admin_name:"Admin (web)",absent_limit:v})});
    toast("Ghost limit saved.","ok");
  }catch(e){toast("Error: "+e.message,"err",4000);}
};

window.saveTz=async function(cid){
  const tz=$id(`ts-tz-${cidK(cid)}`)?.value.trim();
  if(!tz)return;
  try{
    await api(`/admin/groups/${cid}`,{method:"PATCH",body:JSON.stringify({admin_user_id:0,admin_name:"Admin (web)",timezone:tz})});
    toast("Timezone saved.","ok");
    const sub=$id(`gsub-${cidK(cid)}`);
    if(sub)sub.textContent=`Chat ID: ${cid} · ${tz}`;
  }catch(e){toast("Error: "+e.message,"err",4000);}
};

// ─── Copy group link ──────────────────────────────────────────────────────────
window.copyGroupLink=function(cid){
  const g=S.groups.find(x=>x.chat_id===cid);
  if(!g?.group_web_token){toast("No group link set up yet","err");return;}
  const url=window.location.origin+"/web/group/"+g.group_web_token;
  if(navigator.clipboard){
    navigator.clipboard.writeText(url).then(()=>toast("Link copied — share with members","ok")).catch(()=>_fallbackCopy(url));
  }else{_fallbackCopy(url);}
};
function _fallbackCopy(text){
  const inp=document.createElement("input");
  inp.value=text;Object.assign(inp.style,{position:"fixed",opacity:"0"});
  document.body.appendChild(inp);inp.select();
  try{document.execCommand("copy");toast("Link copied","ok");}catch{toast("Copy failed — URL: "+text,"err",6000);}
  document.body.removeChild(inp);
}

// ─── Utils ────────────────────────────────────────────────────────────────────
function updateGroupBadge(cid,count){
  const g=S.groups.find(x=>x.chat_id===cid);if(g){g.active_rollcalls=count;renderGList();}
}

// ─── Delegated handlers for user-row actions ──────────────────────────────────
// User names are stored in data-uname attributes (HTML-entity-encoded).
// The browser decodes them back to plain text on dataset read, so all API
// calls receive the original unescaped name — safe for encodeURIComponent.
document.addEventListener('change',e=>{
  const sel=e.target.closest('.move-sel');
  if(!sel||!sel.value)return;
  const row=sel.closest('.ur');
  if(!row)return;
  const{cid,num,uname}=row.dataset;
  doMoveUser(Number(cid),Number(num),uname,sel.value);
  sel.value='';
});

document.addEventListener('click',e=>{
  // Remove user
  const del=e.target.closest('.ur-del');
  if(del){
    const row=del.closest('.ur');
    if(row){const{cid,num,uname}=row.dataset;doRemoveUser(Number(cid),Number(num),uname);}
    return;
  }
  // Rename proxy — show inline rename row
  const ren=e.target.closest('.ur-rename');
  if(ren){
    const row=ren.closest('.ur');
    if(row)showRenameRow(row);
    return;
  }
  // Rename save
  const sav=e.target.closest('.do-rename-save');
  if(sav){
    const rr=sav.closest('.rename-row');
    if(rr){
      const{cid,num,oldname,status}=rr.dataset;
      const newName=rr.querySelector('.rename-inp')?.value.trim();
      if(!newName){toast("Name cannot be empty","err");return;}
      if(newName===oldname){rr.remove();return;}
      doRenameProxy(Number(cid),Number(num),oldname,status,newName);
    }
    return;
  }
  // Rename cancel
  const can=e.target.closest('.do-rename-cancel');
  if(can){can.closest('.rename-row')?.remove();return;}
});

})();
