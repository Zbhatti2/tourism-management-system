/*
 * The website's chat window (website_chat.py, templates/site/_chat.html).
 * The chat's key is kept in this browser (localStorage) so the visitor
 * finds their conversation again on every page. While the window is open
 * it checks for new messages every few seconds (the team's replies).
 */
(function () {
  var C = window.SITE_CHAT; if (!C) return;
  var $ = function (id) { return document.getElementById(id); };
  var token = null, lastId = 0, status = "ai", timer = null, busy = false;
  try { token = localStorage.getItem(C.key); } catch (e) { token = null; }

  function save() { try { if (token) localStorage.setItem(C.key, token); } catch (e) {} }
  function post(url, fields) {
    var fd = new FormData(); fd.append("csrf_token", C.csrf);
    Object.keys(fields).forEach(function (k) { if (fields[k] != null) fd.append(k, fields[k]); });
    return fetch(url, {method: "POST", body: fd}).then(handle);
  }
  function handle(r) { return r.json().catch(function () { return {}; }).then(function (d) { if (!r.ok) throw new Error(d.error || "Sorry, something went wrong."); return d; }); }
  var WHO = {ai: "Assistant", staff: "Our team"};
  function render(d) {
    if (d.token) { token = d.token; save(); }
    status = d.status || status;
    var body = $("chat-body");
    (d.messages || []).forEach(function (m) {
      if (m.message_id <= lastId) return;
      lastId = m.message_id;
      var el = document.createElement("div");
      el.className = "chat-msg " + m.role;
      if (WHO[m.role]) { var w = document.createElement("span"); w.className = "who"; w.textContent = WHO[m.role]; el.appendChild(w); }
      el.appendChild(document.createTextNode(m.text));
      body.appendChild(el);
    });
    body.scrollTop = body.scrollHeight;
    $("chat-status").textContent = status === "waiting" ? "Waiting for a member of our team…" :
      status === "human" ? "You're chatting with our team" : "Our assistant answers straight away";
    $("chat-person-link").classList.toggle("d-none", status === "waiting" || status === "human");
  }
  function typing(on) {
    var t = $("chat-typing");
    if (on && !t) { t = document.createElement("div"); t.id = "chat-typing"; t.className = "chat-typing"; t.textContent = "The assistant is typing…"; $("chat-body").appendChild(t); $("chat-body").scrollTop = 1e9; }
    if (!on && t) t.remove();
  }
  function error(msg) { var el = document.createElement("div"); el.className = "chat-msg system"; el.textContent = msg; $("chat-body").appendChild(el); }

  function open() {
    $("chat-panel").classList.remove("d-none"); $("chat-fab").classList.add("d-none");
    $("chat-fab").setAttribute("aria-expanded", "true");
    if (!lastId) post(C.start, {token: token, page: location.pathname}).then(render).catch(function (e) { error(e.message); });
    clearInterval(timer);
    timer = setInterval(function () {
      if (!token || busy || document.hidden) return;
      fetch(C.poll + "?token=" + encodeURIComponent(token) + "&after=" + lastId).then(handle).then(render).catch(function () {});
    }, 4000);
    setTimeout(function () { $("chat-text").focus(); }, 50);
  }
  function close() { $("chat-panel").classList.add("d-none"); $("chat-fab").classList.remove("d-none"); $("chat-fab").setAttribute("aria-expanded", "false"); clearInterval(timer); }

  $("chat-fab").addEventListener("click", open);
  $("chat-close").addEventListener("click", close);
  $("chat-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var text = $("chat-text").value.trim(); if (!text || busy) return;
    busy = true; $("chat-text").value = "";
    var echo = document.createElement("div"); echo.className = "chat-msg visitor pending"; echo.textContent = text; $("chat-body").appendChild(echo);
    if (status === "ai") typing(true);
    post(C.send, {token: token, text: text, after: lastId}).then(function (d) { echo.remove(); typing(false); render(d); })
      .catch(function (e) { echo.remove(); typing(false); $("chat-text").value = text; error(e.message); })
      .finally(function () { busy = false; });
  });
  $("chat-text").addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); $("chat-form").requestSubmit(); }
  });
  $("chat-person-link").addEventListener("click", function (ev) { ev.preventDefault(); $("chat-person-form").classList.remove("d-none"); });
  $("chat-person-cancel").addEventListener("click", function () { $("chat-person-form").classList.add("d-none"); });
  $("chat-person-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var f = ev.target;
    post(C.person, {token: token, name: f.name.value, email: f.email.value, after: lastId}).then(function (d) { f.classList.add("d-none"); render(d); })
      .catch(function (e) { error(e.message); });
  });
  // a conversation already started: reopen it where it was
  if (token && /[?&]chat=1/.test(location.search)) open();
})();
