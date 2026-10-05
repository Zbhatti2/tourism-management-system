/*
 * Tenant website home page (templates/site/home.html):
 *   Group boxes -> click a Group -> its tours fill the middle in a slider
 *   -> click a tour -> the slider becomes that tour's photo slider, with
 *   "Tour details" on the photo, and the bottom bar shows the tour's
 *   scheduled departures, duration, group size and price.
 * Data: window.SITE (groups, tours) from blueprints/site.py. The page also
 * works without JavaScript: every box is a link (?group=, ?tour=).
 */
(function () {
  var S = window.SITE || {groups: [], tours: []};
  var pick = window.SITE_PICK || {};
  var URLS = window.SITE_URLS || {};
  var $ = function (id) { return document.getElementById(id); };
  if (!$("view-groups")) return;
  var state = {group: null, tour: null, photo: 0};

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]; }); }
  function money(v) { return v == null ? "on request" : "$" + Math.round(v).toLocaleString(); }
  function fmtDate(iso) { if (!iso) return ""; var d = new Date(iso + "T00:00:00"); return d.toLocaleDateString(undefined, {month: "short", day: "numeric", year: "numeric"}); }
  function group(id) { return S.groups.filter(function (g) { return String(g.id) === String(id) || g.slug === id; })[0]; }
  function toursOf(g) { return S.tours.filter(function (t) { return String(t.group) === String(g.id); }); }
  function tour(slug) { return S.tours.filter(function (t) { return t.slug === slug; })[0]; }
  function show(id, on) {
    $(id).classList.toggle("d-none", !on);
    if (id === "tour-bar") document.body.classList.toggle("has-bar", on);  // lift the chat button above the bar
  }

  function crumbs() {
    var bits = ['<a href="#" data-go="groups">All journeys</a>'];
    if (state.group) bits.push('<a href="#" data-go="group">' + esc(state.group.caption) + "</a>");
    if (state.tour) bits.push("<span>" + esc(state.tour.title) + "</span>");
    $("stage-crumbs").innerHTML = bits.join(' <span>›</span> ');
  }

  function showGroups() {
    state.group = null; state.tour = null;
    $("stage-heading").textContent = "Choose a journey";
    show("view-groups", true); show("view-tours", false); show("view-photos", false); show("group-strip", false); show("tour-bar", false);
    crumbs(); history.replaceState(null, "", URLS.home);
  }

  function showGroup(g) {
    state.group = g; state.tour = null;
    $("stage-heading").textContent = g.caption;
    var cards = toursOf(g).map(function (t) {
      var next = t.departures.filter(function (d) { return d.bookable; })[0] || t.departures[0];
      return '<div class="tour-card" data-tour="' + esc(t.slug) + '" tabindex="0" role="button">' +
        '<div class="pic">' + (t.cover ? '<img src="' + esc(t.cover) + '" alt="' + esc(t.title) + '">' : '<div class="no-image"><i class="bi bi-image"></i></div>') + "</div>" +
        "<h3>" + esc(t.tagline || t.title) + "</h3>" +
        '<div class="when">' + (next ? "Starting: " + fmtDate(next.start) + ", " : "") + (t.days ? t.days + " Days" : "") + "</div>" +
        (t.tagline ? '<div class="small">' + esc(t.title) + "</div>" : "") + "</div>";
    });
    $("tour-track").innerHTML = cards.join("") || '<p class="text-white">No tours in this group yet.</p>';
    show("view-groups", false); show("view-tours", true); show("view-photos", false); show("group-strip", true); show("tour-bar", false);
    markGroup(); crumbs();
    history.replaceState(null, "", URLS.home + "?group=" + encodeURIComponent(g.slug));
  }

  function markGroup() {
    document.querySelectorAll("#group-strip .group-box").forEach(function (a) {
      a.classList.toggle("active", state.group && String(a.getAttribute("data-group")) === String(state.group.id));
    });
  }

  function showTour(t) {
    state.tour = t; state.photo = 0;
    if (!state.group || String(state.group.id) !== String(t.group)) state.group = group(t.group) || state.group;
    $("stage-heading").textContent = t.title;
    $("photo-title").textContent = t.tagline || t.title;
    $("details-btn").href = t.url; $("bar-details").href = t.url;
    $("photo-thumbs").innerHTML = t.images.map(function (im, i) {
      return '<img src="' + esc(im.thumb) + '" alt="' + esc(im.caption) + '" data-i="' + i + '">';
    }).join("");
    photo(0);
    // bottom bar
    var sel = $("bar-departures");
    sel.innerHTML = '<option value="">Scheduled Departures</option>' + t.departures.map(function (d) {
      return '<option value="' + d.id + '">' + fmtDate(d.start) + (d.end ? " – " + fmtDate(d.end) : "") +
        (d.price ? " · " + money(d.price) : "") + (d.bookable ? "" : " · " + esc(d.label)) + "</option>";
    }).join("");
    if (!t.departures.length) sel.innerHTML = '<option value="">Dates on request</option>';
    $("bar-duration").textContent = t.days ? t.days + " Days" : "—";
    $("bar-group").textContent = t.min || t.max ? (t.min || 1) + (t.max ? "-" + t.max : "+") : "—";
    $("bar-price").textContent = money(t.price);
    $("bar-contact").href = URLS.contact + "?tour=" + encodeURIComponent(t.slug);
    show("view-groups", false); show("view-tours", false); show("view-photos", true); show("group-strip", true); show("tour-bar", true);
    markGroup(); crumbs();
    history.replaceState(null, "", URLS.home + "?tour=" + encodeURIComponent(t.slug));
  }

  function photo(i) {
    var t = state.tour; if (!t) return;
    var n = t.images.length;
    if (!n) { $("photo-main").removeAttribute("src"); $("photo-caption").textContent = ""; return; }
    state.photo = (i + n) % n;
    var im = t.images[state.photo];
    $("photo-main").src = im.src; $("photo-main").alt = im.caption || t.title;
    $("photo-caption").textContent = (im.caption || "") + "  ·  " + (state.photo + 1) + " / " + n;
    document.querySelectorAll("#photo-thumbs img").forEach(function (el) { el.classList.toggle("on", Number(el.getAttribute("data-i")) === state.photo); });
  }

  // clicks
  document.addEventListener("click", function (ev) {
    var gb = ev.target.closest(".group-box[data-group]");
    if (gb) { ev.preventDefault(); var g = group(gb.getAttribute("data-group")); if (g) showGroup(g); return; }
    var tc = ev.target.closest(".tour-card[data-tour]");
    if (tc) { var t = tour(tc.getAttribute("data-tour")); if (t) showTour(t); return; }
    var go = ev.target.closest("[data-go]");
    if (go) { ev.preventDefault(); if (go.getAttribute("data-go") === "groups") showGroups(); else if (state.group) showGroup(state.group); return; }
    var th = ev.target.closest("#photo-thumbs img");
    if (th) { photo(Number(th.getAttribute("data-i"))); return; }
    if (ev.target.closest(".photo-stage .prev")) photo(state.photo - 1);
    if (ev.target.closest(".photo-stage .next")) photo(state.photo + 1);
    var sb = ev.target.closest(".slider-btn");
    if (sb) { var tr = $("tour-track"); tr.scrollBy({left: (sb.classList.contains("prev") ? -1 : 1) * tr.clientWidth * 0.8, behavior: "smooth"}); }
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && ev.target.classList && ev.target.classList.contains("tour-card")) ev.target.click();
    if (state.tour && (ev.key === "ArrowLeft" || ev.key === "ArrowRight") && !/INPUT|SELECT|TEXTAREA/.test(ev.target.tagName)) photo(state.photo + (ev.key === "ArrowLeft" ? -1 : 1));
  });
  $("bar-departures").addEventListener("change", function () {
    if (this.value && state.tour) window.location = state.tour.url + "?departure=" + this.value + "#book";
  });

  // start where the address says (?group= / ?tour=)
  var t0 = pick.tour && tour(pick.tour), g0 = pick.group && group(pick.group);
  if (t0) showTour(t0); else if (g0) showGroup(g0); else crumbs();
})();
