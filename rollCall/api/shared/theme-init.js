(function(){
  var p=localStorage.getItem("rc_dark");
  var sys=window.matchMedia&&window.matchMedia("(prefers-color-scheme:dark)").matches;
  if(p==="1"||(p!=="0"&&sys))document.documentElement.classList.add("dark");
})();
