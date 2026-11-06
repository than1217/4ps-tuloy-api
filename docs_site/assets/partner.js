// Partner page: "Getting started" and the API reference share the page; the URL hash decides which one shows.
// An empty hash shows Getting started. A hash that starts with "#/" belongs to the Elements router.
(function () {
  "use strict";
  var intro = document.getElementById("start");
  var docs = document.getElementById("docs");
  var tabStart = document.getElementById("tab-start");
  var tabRef = document.getElementById("tab-ref");

  function render() {
    var reference = window.location.hash.indexOf("#/") === 0;
    intro.hidden = reference;
    docs.hidden = !reference;
    document.body.classList.toggle("tl-view-ref", reference);
    if (reference) { tabRef.setAttribute("aria-current", "page"); tabStart.removeAttribute("aria-current"); }
    else { tabStart.setAttribute("aria-current", "page"); tabRef.removeAttribute("aria-current"); }
  }

  // Back to Getting started: drop the hash without reloading (Elements keeps its own state).
  tabStart.addEventListener("click", function (e) {
    e.preventDefault();
    history.pushState(null, "", window.location.pathname + window.location.search);
    render();
    intro.scrollTop = 0;
  });
  window.addEventListener("hashchange", render);
  window.addEventListener("popstate", render);
  render();
})();
