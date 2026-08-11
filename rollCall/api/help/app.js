// RollCall command reference — fetches /api/v1/commands (sourced directly
// from commands_registry.py, the same data /help renders in Telegram) and
// renders a searchable, category-grouped reference. No framework — same
// vanilla-JS convention as the other web surfaces.

function esc(s){const d=document.createElement("div");d.textContent=s||"";return d.innerHTML;}

function _updateThemeBtn(){
  const dark=document.documentElement.classList.contains("dark");
  document.querySelectorAll(".btn-theme").forEach(b=>{if(b.textContent==="🌙"||b.textContent==="☀️")b.textContent=dark?"☀️":"🌙";});
}
window.toggleTheme=function(){
  const on=document.documentElement.classList.toggle("dark");
  localStorage.setItem("rc_dark",on?"1":"0");
  _updateThemeBtn();
};

let _data=null;          // raw API response
let _scope="all";        // "all" | "user" | "admin"
let _expanded=new Set(); // command names currently expanded

async function loadCommands(){
  try{
    const res=await fetch("/api/v1/commands");
    if(!res.ok)throw new Error("HTTP "+res.status);
    _data=await res.json();
  }catch(e){
    document.getElementById("categories-root").innerHTML=
      `<div class="card">Couldn't load the command list (${esc(e.message)}). Refresh to try again.</div>`;
    return;
  }
  render();
}

window.setScope=function(scope){
  _scope=scope;
  document.getElementById("scope-all").classList.toggle("active",scope==="all");
  document.getElementById("scope-user").classList.toggle("active",scope==="user");
  document.getElementById("scope-admin").classList.toggle("active",scope==="admin");
  render();
};

window.onSearchInput=function(){ render(); };

window.toggleDetail=function(name){
  if(_expanded.has(name))_expanded.delete(name);else _expanded.add(name);
  render();
};

function _scopeSet(scope){
  // "admin" here means admin-only (excludes "user"), unlike the bot's own
  // /help admin (which includes user commands too, since it's one linear
  // scrollable message) -- on a filterable page, keeping User/Admin
  // non-overlapping and letting "All" be their union is clearer than
  // showing the same command twice under two tabs.
  if(scope==="admin")return new Set(["admin","super_admin"]);
  if(scope==="all")return new Set(["user","admin","super_admin"]);
  return new Set(["user"]);
}

function _categoryOrder(scope){
  if(scope==="admin")return _data.admin_category_order;
  if(scope==="user")return _data.user_category_order;
  // "all": merge both orders, de-duplicated, user categories first.
  const seen=new Set(), merged=[];
  for(const cat of [..._data.user_category_order,..._data.admin_category_order]){
    if(!seen.has(cat)){seen.add(cat);merged.push(cat);}
  }
  return merged;
}

function _matchesQuery(cmd,q){
  if(!q)return true;
  const haystack=[cmd.name,...(cmd.aliases||[]),cmd.category,cmd.summary,cmd.details||""]
    .join(" ").toLowerCase();
  return haystack.includes(q);
}

function render(){
  if(!_data)return;
  const scopeSet=_scopeSet(_scope);
  const q=(document.getElementById("search-input").value||"").trim().toLowerCase();
  const matched=_data.commands.filter(c=>scopeSet.has(c.scope)&&_matchesQuery(c,q));

  const scopeLabel=_scope==="all"?`all ${_data.commands.length}`:_scope;
  const countEl=document.getElementById("result-count");
  countEl.textContent=q
    ? `${matched.length} command${matched.length===1?"":"s"} match "${q}"`
    : `${matched.length} command${matched.length===1?"":"s"} — ${scopeLabel} view`;

  const emptyEl=document.getElementById("empty-state");
  const rootEl=document.getElementById("categories-root");
  if(matched.length===0){
    document.getElementById("empty-query").textContent=q;
    emptyEl.classList.remove("hidden");
    rootEl.innerHTML="";
    return;
  }
  emptyEl.classList.add("hidden");

  const order=_categoryOrder(_scope);
  const byCat={};
  for(const c of matched){(byCat[c.category]=byCat[c.category]||[]).push(c);}
  const orderedCats=order.filter(cat=>byCat[cat]);
  for(const cat of Object.keys(byCat).sort()){
    if(!orderedCats.includes(cat))orderedCats.push(cat);
  }

  rootEl.innerHTML=orderedCats.map(cat=>{
    const emoji=_data.category_emoji[cat]||"";
    const cards=byCat[cat].map(cmdCard).join("");
    return `<section class="cat-section">
      <h2 class="cat-heading">${emoji?emoji+" ":""}${esc(cat)}</h2>
      <div class="cmd-grid">${cards}</div>
    </section>`;
  }).join("");
}

function cmdCard(c){
  const expanded=_expanded.has(c.name);
  const aliasStr=(c.aliases&&c.aliases.length)?c.aliases.map(a=>"/"+a).join(", "):"";
  const argsStr=c.args||"";
  // Scope badge only matters when scopes are mixed together (the "All" view);
  // User/Admin tabs are already scope-pure, so a badge there is just noise.
  const scopeBadge=(_scope==="all"&&c.scope!=="user")
    ?`<span class="cmd-scope-badge">${c.scope==="super_admin"?"owner":"admin"}</span>`:"";
  return `<div class="cmd-card${expanded?" expanded":""}" onclick="toggleDetail('${esc(c.name)}')">
    <div class="cmd-card-head">
      <span class="cmd-name">/${esc(c.name)}</span>
      ${scopeBadge}
      ${argsStr?`<span class="cmd-args">${esc(argsStr)}</span>`:""}
    </div>
    <div class="cmd-summary">${esc(c.summary)}</div>
    ${expanded?`<div class="cmd-detail">
      ${aliasStr?`<div class="cmd-detail-row"><b>Aliases:</b> ${esc(aliasStr)}</div>`:""}
      ${c.sample?`<div class="cmd-detail-row"><b>Example:</b> <code>${esc(c.sample)}</code></div>`:""}
      ${c.details?`<div class="cmd-detail-row cmd-detail-text">${esc(c.details).replace(/\n/g,"<br/>")}</div>`:""}
    </div>`:""}
  </div>`;
}

document.addEventListener("DOMContentLoaded",()=>{
  _updateThemeBtn();
  loadCommands();
});
