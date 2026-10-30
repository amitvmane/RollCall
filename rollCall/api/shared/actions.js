/* RollCall — declarative event wiring for every web surface.

   Why this exists: every button used to carry an inline onclick attribute
   holding JavaScript source. Inline handlers only run when the
   Content-Security-Policy allows 'unsafe-inline' scripts — and with that
   allowed, any HTML that slips past escaping can run arbitrary JavaScript,
   so the CSP was no XSS defence at all. Markup now says WHAT to call:

     <button  data-on-click = doThing   data-args = ["x"]  >   (quoted)

   and this one listener calls it. The policy can then refuse inline script,
   and injected markup can at most name one of the page's own functions.

   Rules:
   - data-on-<event> holds one function name, a dotted path (rcTheme.set),
     or several separated by spaces (called in order with the same args).
   - Only functions the page itself defined are callable: built-ins
     (fetch, open, eval, setTimeout…) report "[native code]" and are refused.
   - data-args is a JSON array. {"$":"this"|"value"|"checked"|"event"}
     stands for the element, its value/checked state, or the event.
   - "$stop" as the name ends the walk: ancestors' handlers don't run (what
     an inline event.stopPropagation() did for a link inside a clickable card).
   - Handlers bubble like inline ones did: the target's handler, then each
     ancestor's, until one calls event.stopPropagation().

   It listens in the CAPTURE phase on window, so a handler that calls
   stopPropagation() (toggleAcctMenu does) still keeps the click from the
   page's own document-level "click outside closes the menu" listeners —
   exactly as when it ran inline on the element. */
(function(){
  "use strict";
  var EVENTS=["click","change","input","keydown"];
  var NAME=/^[A-Za-z_$][\w$]*$/;
  var fnToString=Function.prototype.toString;

  function resolve(path){
    var parts=path.split("."),ctx=window,obj=window;
    for(var i=0;i<parts.length;i++){
      if(!NAME.test(parts[i])||obj==null)return null;
      ctx=obj;obj=obj[parts[i]];
    }
    if(typeof obj!=="function")return null;
    if(/\[native code\]\s*\}\s*$/.test(fnToString.call(obj)))return null;
    return {fn:obj,ctx:ctx};
  }

  function arg(a,el,ev){
    if(a&&typeof a==="object"&&!Array.isArray(a)&&typeof a.$==="string"){
      if(a.$==="this")return el;
      if(a.$==="value")return el.value;
      if(a.$==="checked")return el.checked;
      if(a.$==="event")return ev;
    }
    return a;
  }

  function run(el,attr,ev){
    var names=(el.getAttribute(attr)||"").trim().split(/\s+/);
    if(names[0]==="$stop")return "stop";
    var raw=el.getAttribute("data-args"),args=[];
    if(raw){
      try{args=JSON.parse(raw);}catch(e){console.error("rc-actions: bad data-args",raw);return;}
      if(!Array.isArray(args))args=[args];
    }
    args=args.map(function(a){return arg(a,el,ev);});
    for(var i=0;i<names.length;i++){
      var r=resolve(names[i]);
      if(!r){console.error("rc-actions: no such page function",names[i]);continue;}
      r.fn.apply(r.ctx,args);
    }
  }

  function dispatch(ev){
    var attr="data-on-"+ev.type;
    for(var el=ev.target;el&&el.nodeType===1;el=el.parentElement){
      if(!el.hasAttribute(attr))continue;
      if(run(el,attr,ev)==="stop")return;
      if(ev.cancelBubble)return;      // a handler called stopPropagation()
    }
  }

  for(var i=0;i<EVENTS.length;i++)window.addEventListener(EVENTS[i],dispatch,true);

  /* Building data-args inside a JS template: rcArgs(a, b) → HTML-escaped
     JSON, safe in a double-quoted attribute whatever the strings contain.
     This replaces the old escJsAttr()-then-esc() pairing, whose ORDER was
     the only thing standing between a member's name and script execution. */
  window.RC_THIS={$:"this"};window.RC_VALUE={$:"value"};
  window.RC_CHECKED={$:"checked"};window.RC_EVENT={$:"event"};
  window.rcArgs=function(){
    return JSON.stringify(Array.prototype.slice.call(arguments))
      .replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/'/g,"&#39;")
      .replace(/</g,"&lt;").replace(/>/g,"&gt;");
  };

  /* Small page-agnostic actions that used to be written inline. */
  window.navTo=function(url){
    // Same-origin paths only — never a scheme an injected attribute could pick.
    if(typeof url==="string"&&url.charAt(0)==="/"&&url.charAt(1)!=="/")window.location.href=url;
  };
  window.copyText=function(text,msg){
    navigator.clipboard.writeText(String(text)).then(function(){
      if(typeof window.toast==="function"&&msg)window.toast(msg);
    });
  };
  window.uiHide=function(id){var e=document.getElementById(id);if(e)e.classList.add("hidden");};
  window.uiHideParent=function(el){if(el&&el.parentElement)el.parentElement.classList.add("hidden");};
  // Show row `id` while checkbox `el` is ticked.
  window.uiShowIfChecked=function(id,el){
    var e=document.getElementById(id);if(e)e.classList.toggle("hidden",!el.checked);
  };
  // Show row `id` while select `el` has value `want`.
  window.uiShowIfValue=function(id,el,want){
    var e=document.getElementById(id);if(e)e.classList.toggle("hidden",el.value!==want);
  };
  // Show the date input next to select `el` only for the "custom" option.
  window.uiShowSiblingDate=function(el){
    var d=el.parentElement&&el.parentElement.querySelector("input[type=date]");
    if(d)d.style.display=el.value==="custom"?"":"none";
  };
})();
