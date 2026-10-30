/* RollCall theme — the one implementation for every web surface (/, /web,
   /portal, /help). Loaded synchronously in <head> so the right palette is on
   <html> before first paint; the button/menu wiring waits for the DOM.

   Three modes: "light", "dark", "auto" (follow the device, live). It used to
   be a two-way toggle per page, three slightly different copies of it, and
   once you tapped it there was no way back to following the device.

   Stored as rc_theme. The old rc_dark key ("1"/"0") is read once and carried
   over, so nobody's existing choice changes. Storage can throw (private mode,
   blocked site data) — every access is guarded and the page still themes
   from the device setting without it. */
(function(){
  var KEY="rc_theme",MODES=["light","dark","auto"];
  var ICON={light:"☀️",dark:"🌙",auto:"🌓"};
  var LABEL={light:"Light",dark:"Dark",auto:"Auto"};
  var mq=window.matchMedia?window.matchMedia("(prefers-color-scheme:dark)"):null;

  function read(){
    try{
      var m=localStorage.getItem(KEY);
      if(MODES.indexOf(m)>=0)return m;
      var old=localStorage.getItem("rc_dark");
      if(old==="1"||old==="0"){
        m=old==="1"?"dark":"light";
        localStorage.setItem(KEY,m);localStorage.removeItem("rc_dark");
        return m;
      }
    }catch(e){}
    return "auto";
  }
  var mode=read();

  function isDark(){return mode==="dark"||(mode==="auto"&&!!(mq&&mq.matches));}
  function apply(){
    var d=isDark(),root=document.documentElement;
    root.classList.toggle("dark",d);
    root.style.colorScheme=d?"dark":"light";
  }
  function paint(){
    var next=MODES[(MODES.indexOf(mode)+1)%MODES.length];
    var btns=document.querySelectorAll("[data-theme-toggle]");
    for(var i=0;i<btns.length;i++){
      btns[i].textContent=ICON[mode];
      btns[i].title="Theme: "+LABEL[mode]+" — tap for "+LABEL[next];
      btns[i].setAttribute("aria-label","Theme: "+LABEL[mode]+". Switch to "+LABEL[next]);
    }
    var opts=document.querySelectorAll("[data-theme-opt]");
    for(var j=0;j<opts.length;j++){
      var on=opts[j].getAttribute("data-theme-opt")===mode;
      opts[j].classList.toggle("on",on);
      opts[j].setAttribute("aria-pressed",on?"true":"false");
    }
  }
  function set(m){
    if(MODES.indexOf(m)<0)return;
    mode=m;
    try{localStorage.setItem(KEY,m);}catch(e){}
    apply();paint();
  }

  apply();
  // Auto follows the device live (e.g. phone switching to dark at sunset).
  if(mq){
    var onChange=function(){if(mode==="auto"){apply();paint();}};
    if(mq.addEventListener)mq.addEventListener("change",onChange);
    else if(mq.addListener)mq.addListener(onChange);
  }
  // Another tab changed it — keep every open page in step.
  window.addEventListener("storage",function(e){
    if(e.key===KEY&&MODES.indexOf(e.newValue)>=0){mode=e.newValue;apply();paint();}
  });
  if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",paint);
  else paint();

  window.rcTheme={
    get:function(){return mode;},
    set:set,
    // Header button: Light → Dark → Auto → Light.
    cycle:function(){set(MODES[(MODES.indexOf(mode)+1)%MODES.length]);},
    // Re-sync controls after a page renders new ones (e.g. the account menu).
    paint:paint,
    // The account-menu row, shared so /web and /portal render the same thing.
    menuHtml:function(){
      var h='<div class="acct-theme" role="group" aria-label="Theme"><span class="acct-theme-lbl">Theme</span><div class="acct-theme-seg">';
      for(var i=0;i<MODES.length;i++){
        var m=MODES[i];
        h+='<button type="button" data-theme-opt="'+m+'" class="'+(m===mode?"on":"")+'" aria-pressed="'+(m===mode)+'" data-on-click="rcTheme.set" data-args=\'["'+m+'"]\'>'+ICON[m]+' '+LABEL[m]+'</button>';
      }
      return h+'</div></div>';
    }
  };
  // Every page's header button calls this name.
  window.toggleTheme=window.rcTheme.cycle;
})();
