// Foyer boot: apply the theme and wallpaper before first paint (blocking, tiny, no imports).
(function () {
  var d = document.documentElement;
  var p = {};
  try { p = JSON.parse(localStorage.getItem("foyer.device.v1") || "{}") || {}; } catch (e) { p = {}; }
  var dark = true;
  try { dark = matchMedia("(prefers-color-scheme: dark)").matches; } catch (e) {}
  var t = p.theme === "light" || p.theme === "dark" ? p.theme : dark ? "dark" : "light";
  var w = p.wallpaper || {};
  var dw = ["dusk", "ember", "fjord", "graphite"], lw = ["dawn", "linen", "mist", "paper"];
  d.setAttribute("data-theme", t);
  d.setAttribute("data-wallpaper", t === "dark" ? (dw.indexOf(w.dark) >= 0 ? w.dark : "dusk") : (lw.indexOf(w.light) >= 0 ? w.light : "dawn"));
  if (navigator.standalone === true || (window.matchMedia && matchMedia("(display-mode: standalone)").matches)) d.className += " standalone";
  var m = document.querySelectorAll('meta[name="theme-color"]');
  for (var i = 0; i < m.length; i++) m[i].setAttribute("content", t === "dark" ? "#0b0e17" : "#eff1f6");
})();
