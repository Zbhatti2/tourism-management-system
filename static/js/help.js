/*
 * In-app contextual Help panel: the Bootstrap Offcanvas drawer defined in
 * templates/_help_drawer.html, filled in from the JSON blueprints/help.py
 * serves (GET /help/tree, GET /help/page/<help_id>).
 *
 * Ported from "Book to Movie" studio's identical help.js at Zeb's request
 * ("create a 'Help Module' for TMS exactly following the pattern of the
 * Help features in [that] project ... with a tree structure").
 *
 * One Help trigger for the whole app -- the "Help" button fixed in the
 * top-right corner on every signed-in page (see base.html's `.app-help`
 * nav). That button carries a `data-help-id` attribute that base.html sets
 * from request.blueprint by default (e.g. "suppliers/overview"), or from
 * whatever `help_id` the current page explicitly passed to render_template
 * (same convention TMS already uses for `active_nav`-style sidebar
 * highlighting) -- so clicking it opens the drawer already scoped to
 * wherever you are.
 *
 * Same "fetch once, keep client-side" shape as the rest of this app's
 * small dynamic bits: the tree is small (a couple dozen pages) and rarely
 * changes within one visit, so it's fetched once per page load and reused
 * for every subsequent open/search in that session.
 */
(function () {
  var EXPANDED_STORAGE_KEY = "tms_help_expanded_sections";
  var TRIGGER_SELECTOR = "#app-help-btn";

  var treeCache = null; // sections array from GET /help/tree, once loaded
  var treePromise = null;
  var currentHelpId = null;
  var offcanvasEl = null;
  var offcanvasInstance = null;

  function byId(id) { return document.getElementById(id); }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function getExpanded() {
    try {
      var raw = window.localStorage.getItem(EXPANDED_STORAGE_KEY);
      var parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch (e) {
      return []; // private window / storage disabled -- fall back to "nothing remembered"
    }
  }

  function setExpanded(keys) {
    try {
      window.localStorage.setItem(EXPANDED_STORAGE_KEY, JSON.stringify(keys));
    } catch (e) {
      // Nothing to do -- a lost "which sections were open" preference isn't worth surfacing.
    }
  }

  function loadTree() {
    if (treePromise) return treePromise;
    treePromise = fetch("/help/tree", { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : { sections: [] }; })
      .then(function (data) {
        treeCache = data.sections || [];
        return treeCache;
      })
      .catch(function () {
        treeCache = [];
        return treeCache;
      });
    return treePromise;
  }

  function sectionKeyOf(helpId) {
    return helpId && helpId.indexOf("/") > -1 ? helpId.split("/")[0] : null;
  }

  function renderTree(sections, opts) {
    opts = opts || {};
    var activeHelpId = opts.activeHelpId || null;
    var filterText = (opts.filterText || "").trim().toLowerCase();
    var expanded = {};
    getExpanded().forEach(function (k) { expanded[k] = true; });
    var activeSection = sectionKeyOf(activeHelpId);
    if (activeSection) expanded[activeSection] = true;

    var html = "";
    var matchedAny = false;

    sections.forEach(function (section) {
      var pages = section.pages;
      if (filterText) {
        pages = pages.filter(function (p) {
          var haystack = p.title.toLowerCase() + " " + (p.keywords || []).join(" ").toLowerCase();
          return haystack.indexOf(filterText) > -1;
        });
        if (!pages.length) return; // section has no matches -- omit it entirely while searching
      }
      matchedAny = true;
      var isOpen = filterText ? true : !!expanded[section.key];
      var domId = "helpSec-" + section.key;
      html += '<div class="help-section">';
      // A single fixed chevron-right icon, always -- CSS rotates it 90deg
      // when aria-expanded is true (which Bootstrap keeps in sync on this
      // button whenever the collapse it controls opens/closes), so the
      // arrow tracks the actual state through manual toggles too, not just
      // the state at the moment this HTML was rendered.
      html += '<button type="button" class="help-section-toggle" data-bs-toggle="collapse" data-bs-target="#' + domId + '" ' +
        'aria-expanded="' + (isOpen ? "true" : "false") + '" aria-controls="' + domId + '">' +
        '<span class="help-chevron">&rsaquo;</span>' + escapeHtml(section.title) +
        "</button>";
      html += '<div class="collapse' + (isOpen ? " show" : "") + '" id="' + domId + '" data-section-key="' + section.key + '">';
      html += '<ul class="help-page-list">';
      pages.forEach(function (p) {
        var active = p.help_id === activeHelpId;
        html += '<li><a href="#" class="help-page-link' + (active ? " active" : "") + '" data-help-id="' +
          escapeHtml(p.help_id) + '">' + escapeHtml(p.title) + "</a></li>";
      });
      html += "</ul></div></div>";
    });

    if (!matchedAny) {
      html = '<div class="text-muted small p-3">No help pages match "' + escapeHtml(opts.filterText) + '".</div>';
    }

    byId("helpTree").innerHTML = html;
  }

  function refreshTree(opts) {
    if (treeCache) renderTree(treeCache, opts);
  }

  function renderBreadcrumb(sectionTitle, pageTitle) {
    byId("helpBreadcrumb").innerHTML = escapeHtml(sectionTitle) + ' <span class="mx-1">&rsaquo;</span> ' + escapeHtml(pageTitle);
  }

  function loadPage(helpId) {
    var pane = byId("helpPage");
    pane.innerHTML = '<div class="text-muted">Loading…</div>';
    fetch("/help/page/" + helpId.split("/").map(encodeURIComponent).join("/"), { headers: { "Accept": "application/json" } })
      .then(function (r) {
        if (!r.ok) throw new Error("not found");
        return r.json();
      })
      .then(function (data) {
        currentHelpId = data.help_id;
        pane.innerHTML = '<h4 class="h6 mb-3">' + escapeHtml(data.title) + "</h4>" + data.html;
        renderBreadcrumb(data.section_title, data.title);
        // Keep whatever search filter is currently typed rather than
        // silently reverting to the full tree out from under it.
        refreshTree({ activeHelpId: currentHelpId, filterText: byId("helpSearch").value });
      })
      .catch(function () {
        pane.innerHTML = '<p class="text-muted">' +
          "That help page couldn't be found. Try browsing the tree on the left, or searching above.</p>";
        renderBreadcrumb("Help", "Not found");
      });
  }

  function openFirstAvailablePage(sections) {
    for (var i = 0; i < sections.length; i++) {
      if (sections[i].pages.length) return loadPage(sections[i].pages[0].help_id);
    }
    byId("helpPage").innerHTML = '<p class="text-muted">No help content has been added yet.</p>';
  }

  function open(helpId) {
    if (!offcanvasInstance) return; // Bootstrap JS not loaded -- nothing we can do
    offcanvasInstance.show();
    loadTree().then(function (sections) {
      renderTree(sections, { activeHelpId: helpId });
      if (helpId) {
        loadPage(helpId);
      } else if (!currentHelpId) {
        openFirstAvailablePage(sections);
      }
    });
  }

  /** Update the trigger's help_id without opening the drawer -- so a page
   *  with several tabs under one URL could keep Help pointed at the
   *  visible tab as you switch, the same way the source project's
   *  workspace.html does (see help_content/README.md's note on that
   *  pattern, if TMS ever grows a page shaped like that). */
  function setHelpId(helpId) {
    var trigger = document.querySelector(TRIGGER_SELECTOR);
    if (trigger) trigger.setAttribute("data-help-id", helpId || "");
  }

  function onSectionCollapseToggle(shown) {
    return function (ev) {
      var key = ev.target.getAttribute("data-section-key");
      if (!key) return;
      var expanded = getExpanded();
      var idx = expanded.indexOf(key);
      if (shown && idx === -1) expanded.push(key);
      if (!shown && idx > -1) expanded.splice(idx, 1);
      setExpanded(expanded);
    };
  }

  function init() {
    offcanvasEl = byId("helpDrawer");
    if (!offcanvasEl || !window.bootstrap) return; // page has no drawer (logged out) or Bootstrap JS missing
    offcanvasInstance = window.bootstrap.Offcanvas.getOrCreateInstance(offcanvasEl);

    document.addEventListener("click", function (ev) {
      var trigger = ev.target.closest(TRIGGER_SELECTOR);
      if (trigger) {
        ev.preventDefault();
        open(trigger.getAttribute("data-help-id") || null);
        return;
      }
      var link = ev.target.closest(".help-page-link");
      if (link && offcanvasEl.contains(link)) {
        ev.preventDefault();
        loadPage(link.getAttribute("data-help-id"));
      }
    });

    // Remember which sections are expanded across opens (and across page loads).
    offcanvasEl.addEventListener("shown.bs.collapse", onSectionCollapseToggle(true));
    offcanvasEl.addEventListener("hidden.bs.collapse", onSectionCollapseToggle(false));

    var search = byId("helpSearch");
    search.addEventListener("input", function () {
      refreshTree({ activeHelpId: currentHelpId, filterText: search.value });
    });
    // Offcanvas hides the field but the browser keeps its value -- clear the
    // filter on close so reopening shows the full tree, not a stale search.
    offcanvasEl.addEventListener("hidden.bs.offcanvas", function () {
      search.value = "";
    });

    // "?" opens contextual help from anywhere, unless the person is typing.
    document.addEventListener("keydown", function (ev) {
      if (ev.key !== "?" || ev.ctrlKey || ev.metaKey || ev.altKey) return;
      var tag = (ev.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || ev.target.isContentEditable) return;
      ev.preventDefault();
      var trigger = document.querySelector(TRIGGER_SELECTOR);
      open(trigger ? trigger.getAttribute("data-help-id") : null);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.TMSHelp = { open: open, setHelpId: setHelpId };
})();
