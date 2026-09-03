/*
 * Cascading Category -> Group -> Sub-Group picker for the New Service form
 * (see templates/services/form.html and blueprints/services.py's
 * new_service()). Unlike static/js/geography_picker.js, the whole
 * taxonomy tree is small enough (a couple hundred Sub-Groups) that it's
 * embedded directly into the page via Jinja's |tojson rather than fetched
 * separately -- see initServiceCodePicker() below.
 *
 * Deprecated Sub-Groups are never included in the tree the server sends
 * (blueprints/services.py's _taxonomy_tree() excludes them entirely), so
 * nothing here needs to filter them out again; 'under_review' Sub-Groups
 * ARE included, flagged with a "(under review)" suffix on their option
 * label so the person can still choose one with their eyes open.
 *
 * Usage: give the three <select> elements ids "category_id", "group_id",
 * "subgroup_id" (only subgroup_id needs name="subgroup_id" -- that's the
 * one value the server actually reads; it derives Category/Group from
 * the chosen Sub-Group itself), plus an optional element with id
 * "service_code_preview" to show a live "TP-MN-MC-####" preview. Then:
 *
 *   initServiceCodePicker(tree);
 */
(function () {
  function el(id) { return document.getElementById(id); }

  function fillSelect(select, items, valueKey, labelFn, codeFn, placeholder) {
    select.innerHTML = "";
    var opt0 = document.createElement("option");
    opt0.value = "";
    opt0.textContent = placeholder;
    select.appendChild(opt0);
    items.forEach(function (item) {
      var opt = document.createElement("option");
      opt.value = item[valueKey];
      opt.textContent = labelFn(item);
      opt.dataset.code = codeFn(item);
      select.appendChild(opt);
    });
  }

  window.initServiceCodePicker = function (tree) {
    var categorySelect = el("category_id");
    var groupSelect = el("group_id");
    var subgroupSelect = el("subgroup_id");
    var preview = el("service_code_preview");
    var detailsFieldset = el("details-fieldset");
    var lockedNote = el("details-locked-note");
    var saveBtn = el("save_service_btn");
    if (!categorySelect || !groupSelect || !subgroupSelect) return;

    function updateDetailsLock() {
      var unlocked = !!subgroupSelect.value;
      if (detailsFieldset) detailsFieldset.disabled = !unlocked;
      if (saveBtn) saveBtn.disabled = !unlocked;
      if (lockedNote) lockedNote.classList.toggle("d-none", unlocked);
    }

    function groupsFor(categoryId) {
      var cat = tree.filter(function (c) { return String(c.category_id) === String(categoryId); })[0];
      return cat ? cat.groups : [];
    }
    function subgroupsFor(categoryId, groupId) {
      var grp = groupsFor(categoryId).filter(function (g) { return String(g.group_id) === String(groupId); })[0];
      return grp ? grp.subgroups : [];
    }
    function subgroupLabel(sg) {
      return sg.subgroup_code + " — " + sg.subgroup_name + (sg.validity_status === "under_review" ? " (under review)" : "");
    }

    function updatePreview() {
      if (!preview) return;
      var catOpt = categorySelect.options[categorySelect.selectedIndex];
      var grpOpt = groupSelect.options[groupSelect.selectedIndex];
      var sgOpt = subgroupSelect.options[subgroupSelect.selectedIndex];
      if (subgroupSelect.value && catOpt && catOpt.dataset.code && grpOpt && grpOpt.dataset.code && sgOpt && sgOpt.dataset.code) {
        preview.textContent = catOpt.dataset.code + "-" + grpOpt.dataset.code + "-" + sgOpt.dataset.code + "-####";
        preview.classList.remove("text-muted");
      } else {
        preview.textContent = "Choose a Category, Group, and Sub-Group to preview the code.";
        preview.classList.add("text-muted");
      }
    }

    function refreshGroup(categoryId, selectedGroupId) {
      var groups = groupsFor(categoryId);
      fillSelect(groupSelect, groups, "group_id",
        function (g) { return g.group_code + " — " + g.group_name; },
        function (g) { return g.group_code; },
        groups.length ? "— Select group —" : "— None —");
      groupSelect.disabled = groups.length === 0;
      if (selectedGroupId) groupSelect.value = selectedGroupId;
    }

    function refreshSubgroup(categoryId, groupId, selectedSubgroupId) {
      var subgroups = subgroupsFor(categoryId, groupId);
      fillSelect(subgroupSelect, subgroups, "subgroup_id", subgroupLabel,
        function (sg) { return sg.subgroup_code; },
        subgroups.length ? "— Select sub-group —" : "— None —");
      subgroupSelect.disabled = subgroups.length === 0;
      if (selectedSubgroupId) subgroupSelect.value = selectedSubgroupId;
      updatePreview();
      updateDetailsLock();
    }

    function fillCategory() {
      fillSelect(categorySelect, tree, "category_id",
        function (c) { return c.category_code + " — " + c.category_name; },
        function (c) { return c.category_code; },
        "— Select category —");
    }

    categorySelect.addEventListener("change", function () {
      refreshGroup(categorySelect.value, null);
      refreshSubgroup(categorySelect.value, groupSelect.value, null);
    });
    groupSelect.addEventListener("change", function () {
      refreshSubgroup(categorySelect.value, groupSelect.value, null);
    });
    subgroupSelect.addEventListener("change", function () {
      updatePreview();
      updateDetailsLock();
    });

    fillCategory();
    refreshGroup(null, null);
    refreshSubgroup(null, null, null);
    updateDetailsLock();
  };
})();
