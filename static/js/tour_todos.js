/*
 * Tour Design: the user's own To-Do List (tour_tools.py), after the User
 * Notes of Zeb's Book to Movie app. The To-Do button beside Help opens
 * templates/tour_planner/_todo_modal.html; this file talks to
 * /tour-planner/todos (blueprints/tour_tools.py). Inside a Tour Design the
 * Subject starts as the design's name and new tasks are linked to it.
 */
(function () {
  var script = document.getElementById("tour-todos-js");
  if (!script) return;
  var URL = script.getAttribute("data-url"), CSRF = script.getAttribute("data-csrf");
  var btn = document.getElementById("todo-btn");
  var modalEl = document.getElementById("todo-modal");
  if (!btn || !modalEl) return;
  var planId = btn.getAttribute("data-plan-id"), planName = btn.getAttribute("data-plan-name");
  var sortDir = "desc", modal = null;

  function byId(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtWhen(s) { return s ? new Date(s.replace(" ", "T") + "Z").toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : ""; }
  function post(url, fields) {
    var fd = new FormData();
    fd.append("csrf_token", CSRF);
    Object.keys(fields || {}).forEach(function (k) { fd.append(k, fields[k]); });
    return fetch(url, { method: "POST", body: fd }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) throw new Error(d.error || ("Error " + r.status));
        return d;
      });
    });
  }
  function showError(m) { var e = byId("todo-error"); e.textContent = m; e.classList.remove("d-none"); }
  function clearError() { byId("todo-error").classList.add("d-none"); }
  function setCount(n) {
    var b = byId("todo-open-count");
    b.textContent = n; b.classList.toggle("d-none", !n);
  }

  function render(rows) {
    byId("todo-rows").innerHTML = rows.length ? rows.map(function (t) {
      var open = t.status === "open";
      return '<tr class="' + (open ? "" : "text-muted") + '">' +
        '<td class="small">' + esc(fmtWhen(t.created_at)) + "</td>" +
        "<td>" + esc(t.subject) + (t.plan_url && String(t.plan_id) !== String(planId)
          ? '<div class="small"><a href="' + t.plan_url + '">' + esc(t.plan_name || "Tour Design") + "</a></div>" : "") + "</td>" +
        '<td style="white-space: pre-wrap;">' + esc(t.task) + "</td>" +
        '<td><button type="button" class="badge border-0 todo-status ' + (open ? "bg-warning-subtle text-warning-emphasis" : "bg-success-subtle text-success") +
          '" data-id="' + t.todo_id + '" data-status="' + t.status + '" title="Click to mark ' + (open ? "closed" : "open") + '">' +
          (open ? "Open" : "Closed") + "</button></td>" +
        '<td><button type="button" class="btn btn-link btn-sm p-0 text-muted todo-del" data-id="' + t.todo_id + '" title="Delete"><i class="bi bi-x-lg"></i></button></td>' +
        "</tr>";
    }).join("") : '<tr><td colspan="5" class="text-muted">No tasks' + (byId("todo-filter-status").value ? " here" : " yet") + ".</td></tr>";
    byId("todo-count").textContent = rows.length + " task" + (rows.length === 1 ? "" : "s");
  }

  function load() {
    var qs = new URLSearchParams();
    var subj = byId("todo-filter-subject").value.trim(), st = byId("todo-filter-status").value;
    if (subj) qs.set("subject", subj);
    if (st) qs.set("status", st);
    qs.set("sort", byId("todo-sort").value);
    qs.set("dir", sortDir);
    if (planId && byId("todo-only-plan").checked) { qs.set("plan", planId); qs.set("only_plan", "1"); }
    return fetch(URL + "?" + qs.toString()).then(function (r) { return r.json(); }).then(function (d) {
      render(d.todos || []); setCount(d.open || 0);
    }).catch(function (e) {
      byId("todo-rows").innerHTML = '<tr><td colspan="5" class="text-muted">Couldn’t load the list (' + esc(e.message) + ").</td></tr>";
    });
  }

  function dirLabel() {
    var f = byId("todo-sort").value;
    var l = f === "subject" ? (sortDir === "asc" ? "A → Z" : "Z → A")
      : f === "status" ? (sortDir === "asc" ? "Closed first" : "Open first")
      : (sortDir === "asc" ? "Oldest first" : "Newest first");
    byId("todo-sort-dir").textContent = (sortDir === "asc" ? "↑ " : "↓ ") + l;
  }

  function init() {
    if (!window.bootstrap) return;
    modal = window.bootstrap.Modal.getOrCreateInstance(modalEl);
    if (planId) {
      byId("todo-plan-note").textContent = "Tasks added here are linked to this Tour Design: " + planName + ".";
      byId("todo-plan-note").classList.remove("d-none");
      byId("todo-only-plan-wrap").classList.remove("d-none");
    }
    btn.addEventListener("click", function () {
      clearError();
      if (planName && !byId("todo-subject").value) byId("todo-subject").value = planName;
      modal.show(); load();
    });
    byId("todo-add-form").addEventListener("submit", function (ev) {
      ev.preventDefault(); clearError();
      var s = byId("todo-subject"), t = byId("todo-task");
      post(URL, { subject: s.value, task: t.value, plan_id: planId || "" }).then(function () {
        t.value = ""; t.focus(); return load();
      }).catch(function (e) { showError(e.message); });
    });
    ["todo-filter-status", "todo-sort", "todo-only-plan"].forEach(function (id) { byId(id).addEventListener("change", load); });
    byId("todo-sort").addEventListener("change", dirLabel);
    var timer = null;
    byId("todo-filter-subject").addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(load, 250); });
    byId("todo-sort-dir").addEventListener("click", function () { sortDir = sortDir === "asc" ? "desc" : "asc"; dirLabel(); load(); });
    byId("todo-rows").addEventListener("click", function (ev) {
      var st = ev.target.closest(".todo-status"), del = ev.target.closest(".todo-del");
      if (st) {
        st.disabled = true;
        post(URL + st.getAttribute("data-id") + "/status", { status: st.getAttribute("data-status") === "open" ? "closed" : "open" })
          .then(load).catch(function (e) { showError(e.message); st.disabled = false; });
      } else if (del && confirm("Delete this task?")) {
        post(URL + del.getAttribute("data-id") + "/delete").then(load).catch(function (e) { showError(e.message); });
      }
    });
    // the open count on the button, without opening the list
    fetch(URL + "?status=open").then(function (r) { return r.json(); }).then(function (d) { setCount(d.open || 0); }).catch(function () {});
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
