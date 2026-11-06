// Sign in, then load the internal spec with the token. The token stays in memory only:
// no localStorage, sessionStorage or cookies, and nothing is written to the console.
(function () {
  "use strict";
  var form = document.getElementById("signin");
  var view = document.getElementById("signin-view");
  var message = document.getElementById("message");
  var submit = document.getElementById("submit");
  var docs = document.getElementById("docs");
  var signout = document.getElementById("signout");
  var session = document.getElementById("session");
  var sessionText = document.getElementById("session-text");

  var token = null;        // in memory only
  var expiresAt = 0;
  var expiryTimer = null;
  var tickTimer = null;

  function clearTimers() {
    if (expiryTimer) clearTimeout(expiryTimer);
    if (tickTimer) clearInterval(tickTimer);
    expiryTimer = tickTimer = null;
  }

  // Drop the token and the rendered docs, then show the sign-in card.
  function showSignIn(text) {
    token = null;
    clearTimers();
    docs.replaceChildren();
    docs.hidden = true;
    session.hidden = true;
    signout.hidden = true;
    view.hidden = false;
    document.body.classList.remove("tl-view-ref", "tl-nav-open");
    // The Elements router keeps its place in the URL hash; do not leave it behind.
    history.replaceState(null, "", window.location.pathname + window.location.search);
    form.elements.password.value = "";
    message.textContent = text || "";
    submit.disabled = false;
    form.elements.username.focus();
  }

  function updateSession() {
    var minutes = Math.max(0, Math.ceil((expiresAt - Date.now()) / 60000));
    sessionText.textContent = "Signed in · " + (minutes <= 1 ? "under 1 min left" : minutes + " min left");
  }

  function showDocs(spec, seconds) {
    var api = document.createElement("elements-api");
    api.setAttribute("router", "hash");
    api.setAttribute("layout", "sidebar");
    api.setAttribute("hideSchemas", "true");
    // A string is the documented form; setting an object before the element is attached makes Elements fail to parse it.
    api.apiDescriptionDocument = JSON.stringify(spec);
    docs.replaceChildren(api);
    view.hidden = true;
    docs.hidden = false;
    session.hidden = false;
    signout.hidden = false;
    document.body.classList.add("tl-view-ref");
    expiresAt = Date.now() + seconds * 1000;
    updateSession();
    tickTimer = setInterval(updateSession, 30000);
    expiryTimer = setTimeout(function () { showSignIn("Your session ended. Sign in again."); }, seconds * 1000);
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    message.textContent = "";
    submit.disabled = true;
    // Read the form, then clear the password field straight away.
    var body = new URLSearchParams(new FormData(form));
    form.elements.password.value = "";
    try {
      var r = await fetch("/token", { method: "POST", body: body });
      body = null;
      if (!r.ok) {
        message.textContent = r.status === 429 ? "Too many attempts. Try again later." : "Sign-in failed.";
        submit.disabled = false;
        return;
      }
      var data = await r.json();
      token = data.access_token;
      var seconds = Number(data.expires_in) > 0 ? Number(data.expires_in) : 900;
      var s = await fetch("/tuloydswd/openapi.json", { headers: { Authorization: "Bearer " + token } });
      if (!s.ok) {
        token = null;
        message.textContent = s.status === 403 ? "This account has no access to the internal docs." : "Could not load the docs.";
        submit.disabled = false;
        return;
      }
      var spec = await s.json();
      showDocs(spec, seconds);
    } catch (e) {
      token = null;
      message.textContent = "Could not reach the server. Try again.";
      submit.disabled = false;
    }
  });

  signout.addEventListener("click", function () { showSignIn("You are signed out."); });
})();
