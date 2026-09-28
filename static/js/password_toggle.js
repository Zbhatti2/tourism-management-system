/*
 * "Show password" toggle for every password box in TMS (login, change
 * password, new user, reset password, setup...). Passwords are HIDDEN by
 * default; clicking the eye button shows the typed text, clicking again
 * hides it. Loaded once from templates/base.html, so any future form with an
 * <input type="password"> gets the toggle automatically -- nothing to add
 * per page. Opt a field out with data-no-toggle.
 */
(function () {
  "use strict";

  function addToggle(input) {
    if (input.dataset.noToggle !== undefined || input.dataset.toggleReady) return;
    input.dataset.toggleReady = "1";

    var group = document.createElement("div");
    group.className = "input-group";
    input.parentNode.insertBefore(group, input);
    group.appendChild(input);

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-outline-secondary";
    btn.setAttribute("aria-label", "Show password");
    btn.setAttribute("title", "Show password");
    btn.setAttribute("aria-pressed", "false");
    btn.tabIndex = 0;
    btn.innerHTML = '<i class="bi bi-eye"></i>';
    group.appendChild(btn);

    btn.addEventListener("click", function () {
      var showing = input.type === "text";
      input.type = showing ? "password" : "text";
      var label = showing ? "Show password" : "Hide password";
      btn.setAttribute("aria-label", label);
      btn.setAttribute("title", label);
      btn.setAttribute("aria-pressed", showing ? "false" : "true");
      btn.innerHTML = showing ? '<i class="bi bi-eye"></i>' : '<i class="bi bi-eye-slash"></i>';
      input.focus();
    });

    // Never submit (or let the browser remember) a form while the password
    // is visible as plain text.
    if (input.form) {
      input.form.addEventListener("submit", function () {
        input.type = "password";
      });
    }
  }

  function init() {
    document.querySelectorAll('input[type="password"]').forEach(addToggle);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
