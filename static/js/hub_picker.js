/*
 * Transport Hub picker -- a type-ahead over the shared Transport Hubs list
 * (GET /platform/transport-hubs/lookup). Markup:
 *
 *   <div class="hub-picker position-relative" data-types="AIRPORT" data-country-input="country_id">
 *     <input type="text" class="form-control hub-picker-text" value="{{ label }}" placeholder="…">
 *     <input type="hidden" name="arrival_hub_id" class="hub-picker-id" value="{{ id }}">
 *   </div>
 *
 * data-types (optional): comma-separated hub type codes to search.
 * data-country-input (optional): id of a country <select>; hubs in that
 * country are listed first. Clearing the text clears the chosen hub.
 */
(function () {
  function init(picker) {
    var text = picker.querySelector(".hub-picker-text");
    var hidden = picker.querySelector(".hub-picker-id");
    var menu = document.createElement("div");
    menu.className = "dropdown-menu shadow-sm";
    menu.style.minWidth = "100%";
    menu.style.width = "max-content";
    menu.style.maxWidth = "min(560px, 90vw)";
    menu.style.maxHeight = "320px";
    menu.style.overflowY = "auto";
    picker.appendChild(menu);
    var timer = null;
    var lastQuery = "";

    function close() { menu.classList.remove("show"); }

    function choose(item) {
      hidden.value = item.id;
      text.value = item.label;
      text.classList.add("is-valid");
      close();
    }

    function render(items) {
      menu.innerHTML = "";
      if (!items.length) {
        var empty = document.createElement("span");
        empty.className = "dropdown-item-text text-muted small";
        empty.textContent = "No matching hub. TMS maintains this list; ask your system administrator to add it.";
        menu.appendChild(empty);
      }
      items.forEach(function (item) {
        var a = document.createElement("button");
        a.type = "button";
        a.className = "dropdown-item small text-wrap";
        var icon = document.createElement("i");
        icon.className = "bi bi-" + (item.icon || "geo") + " text-muted me-2";
        a.appendChild(icon);
        a.appendChild(document.createTextNode(item.label));
        if (item.country) {
          var c = document.createElement("span");
          c.className = "text-muted ms-1";
          c.textContent = "· " + item.country;
          a.appendChild(c);
        }
        a.addEventListener("mousedown", function (e) { e.preventDefault(); choose(item); });
        menu.appendChild(a);
      });
      menu.classList.add("show");
    }

    function search() {
      var q = text.value.trim();
      if (q.length < 2) { close(); return; }
      if (q === lastQuery && menu.children.length) { menu.classList.add("show"); return; }
      lastQuery = q;
      var params = new URLSearchParams({ q: q });
      if (picker.dataset.types) params.set("types", picker.dataset.types);
      var countrySel = picker.dataset.countryInput && document.getElementById(picker.dataset.countryInput);
      if (countrySel && countrySel.value) params.set("country_id", countrySel.value);
      fetch(picker.dataset.url + "?" + params.toString(), { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : []; })
        .then(function (items) { if (text.value.trim() === q) render(items); })
        .catch(function () {});
    }

    text.addEventListener("input", function () {
      hidden.value = "";
      text.classList.remove("is-valid");
      clearTimeout(timer);
      timer = setTimeout(search, 200);
    });
    text.addEventListener("focus", function () { if (!hidden.value) search(); });
    text.addEventListener("blur", function () {
      setTimeout(close, 150);
      if (!hidden.value) text.value = "";  // only a chosen hub is kept
    });
    text.addEventListener("keydown", function (e) {
      if (e.key === "Escape") close();
      if (e.key === "Enter" && menu.classList.contains("show")) {
        var first = menu.querySelector("button.dropdown-item");
        if (first) { e.preventDefault(); first.dispatchEvent(new MouseEvent("mousedown")); }
      }
    });
    if (hidden.value) text.classList.add("is-valid");
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".hub-picker").forEach(init);
  });
})();
