// Shared page behaviour for both docs portals: colour scheme, mobile menu, copy buttons.
// No network calls, no storage, no logging.
(function () {
  "use strict";

  // Elements themes itself from data-theme on <html>; follow the operating system setting.
  var query = window.matchMedia("(prefers-color-scheme: dark)");
  function applyTheme() { document.documentElement.setAttribute("data-theme", query.matches ? "dark" : "light"); }
  applyTheme();
  if (query.addEventListener) query.addEventListener("change", applyTheme);

  // Small accessibility fixes for what Elements renders: name the icon-only copy buttons, and let
  // keyboard users scroll code samples and wide tables.
  var pending = false;
  function enhance() {
    pending = false;
    Array.prototype.forEach.call(document.querySelectorAll(".sl-elements button:not([aria-label])"), function (b) {
      var icon = b.querySelector("svg[data-icon]");
      if (!b.textContent.trim() && icon && icon.getAttribute("data-icon") === "copy") b.setAttribute("aria-label", "Copy to clipboard");
    });
    Array.prototype.forEach.call(document.querySelectorAll(".sl-markdown-viewer table:not([tabindex]), .sl-code-viewer__scroller:not([tabindex])"), function (el) {
      el.setAttribute("tabindex", "0");
    });
  }
  new MutationObserver(function () {
    if (!pending) { pending = true; requestAnimationFrame(enhance); }
  }).observe(document.documentElement, { childList: true, subtree: true });

  document.addEventListener("DOMContentLoaded", function () {
    var origin = window.location.origin;
    Array.prototype.forEach.call(document.querySelectorAll("[data-origin]"), function (el) { el.textContent = origin; });

    // Skip link: a plain #hash would be taken by the Elements router, so move focus by script.
    var skip = document.querySelector(".tl-skip");
    var main = document.getElementById("main");
    if (skip && main) skip.addEventListener("click", function (e) { e.preventDefault(); main.focus(); });

    // Copy buttons sit inside a .tl-code block next to the <pre>.
    Array.prototype.forEach.call(document.querySelectorAll(".tl-copy"), function (btn) {
      btn.addEventListener("click", function () {
        var pre = btn.parentNode.querySelector("pre");
        if (!pre || !navigator.clipboard) return;
        navigator.clipboard.writeText(pre.textContent).then(function () {
          btn.textContent = "Copied";
          setTimeout(function () { btn.textContent = "Copy"; }, 1500);
        }, function () { btn.textContent = "Press Ctrl+C"; });
      });
    });

    // Sidebar drawer on narrow screens.
    var menu = document.getElementById("menu");
    function setNav(open) {
      document.body.classList.toggle("tl-nav-open", open);
      if (menu) menu.setAttribute("aria-expanded", open ? "true" : "false");
    }
    if (menu) menu.addEventListener("click", function () { setNav(!document.body.classList.contains("tl-nav-open")); });
    window.addEventListener("hashchange", function () { setNav(false); });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") setNav(false); });
    document.addEventListener("click", function (e) {
      if (!document.body.classList.contains("tl-nav-open")) return;
      var t = e.target;
      if (menu && menu.contains(t)) return;
      if (!(t.closest && t.closest(".sl-elements-api > .sl-flex:first-child"))) setNav(false);
    });
  });
})();
