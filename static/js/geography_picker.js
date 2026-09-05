/*
 * Cascading Region -> Country -> Province/State -> City picker, shared by
 * the Contacts and Personal Accounts address forms.
 *
 * Fetches the whole geography tree once (GET /geography/tree — global,
 * same for every tenant) and does all cascade filtering client-side, so
 * picking a level never needs another round trip.
 *
 * Usage: give the <select> elements ids `<prefix>country_id`,
 * `<prefix>state_id`, `<prefix>city_id` (prefix may be ''), plus two
 * free-text fallback inputs `<prefix>state_province_text` and
 * `<prefix>city_text` for provinces/cities that aren't seeded in the
 * lookup tables yet. A `<prefix>region_id` <select> is OPTIONAL: include
 * it to gate Country behind Region (the Contacts/Organizations/Suppliers/
 * HR/POI address forms all do); omit it and Country lists every country
 * on file, ungated. Route Stop (packages/stop_form.html) omits it
 * deliberately -- countries.region_id is only populated for a handful of
 * the 17 regions on file today (most of the newer, more granular regions
 * like "Middle-East" have zero countries assigned), so gating Country
 * selection behind Region there would dead-end for most regions,
 * including ones the seeded data actually needs (Pakistan currently
 * carries region_id for "South Asia", not the broader "Asia"). Forms that
 * do include Region aren't touched by this -- same behavior as before.
 * Then call:
 *
 *   initGeographyPicker({
 *     prefix: '',
 *     initial: { region_id, country_id, state_id, city_id,
 *                state_province_text, city_text }
 *   });
 *
 * `initial` (optional) pre-selects a saved record's values on the edit
 * form — the cascade is walked bottom-up from country_id/state_id/city_id
 * so the right region/country/province are already selected and their
 * children already populated before the page is interactive.
 */
(function () {
  function el(id) { return document.getElementById(id); }

  function fillSelect(select, items, valueKey, labelKey, placeholder) {
    select.innerHTML = "";
    var opt0 = document.createElement("option");
    opt0.value = "";
    opt0.textContent = placeholder;
    select.appendChild(opt0);
    items.forEach(function (item) {
      var opt = document.createElement("option");
      opt.value = item[valueKey];
      opt.textContent = item[labelKey];
      select.appendChild(opt);
    });
  }

  window.initGeographyPicker = function (opts) {
    opts = opts || {};
    var prefix = opts.prefix || "";
    var initial = opts.initial || {};

    var regionSelect = el(prefix + "region_id"); // optional -- see file header
    var countrySelect = el(prefix + "country_id");
    var stateSelect = el(prefix + "state_id");
    var cityId = el(prefix + "city_id");
    var stateText = el(prefix + "state_province_text");
    var cityText = el(prefix + "city_text");

    if (!countrySelect || !stateSelect || !cityId) {
      return; // form doesn't use the picker
    }

    var tree = null;

    function statesFor(countryId) {
      if (!tree || !countryId) return [];
      return tree.states.filter(function (s) { return String(s.country_id) === String(countryId); });
    }
    function citiesFor(stateId) {
      if (!tree || !stateId) return [];
      return tree.cities.filter(function (c) { return String(c.state_id) === String(stateId); });
    }
    function countriesFor(regionId) {
      if (!tree || !regionId) return [];
      return tree.countries.filter(function (c) { return String(c.region_id) === String(regionId); });
    }

    // Toggle structured-pick vs free-text fallback: shown/enabled only when
    // the current province (for state) or current country's provinces (for
    // city) have no seeded rows to pick from.
    function refreshStateUI(countryId, selectedStateId) {
      var states = statesFor(countryId);
      fillSelect(stateSelect, states, "state_id", "label", states.length ? "— Select province/state —" : "— None on file —");
      stateSelect.disabled = states.length === 0;
      if (selectedStateId) stateSelect.value = selectedStateId;
      if (stateText) {
        stateText.closest(".geo-fallback-wrap") && (stateText.closest(".geo-fallback-wrap").hidden = states.length > 0);
      }
    }

    function refreshCityUI(stateId, selectedCityId) {
      var cities = citiesFor(stateId);
      fillSelect(cityId, cities, "city_id", "label", cities.length ? "— Select city —" : "— None on file —");
      cityId.disabled = cities.length === 0;
      if (selectedCityId) cityId.value = selectedCityId;
      if (cityText) {
        cityText.closest(".geo-fallback-wrap") && (cityText.closest(".geo-fallback-wrap").hidden = cities.length > 0);
      }
    }

    function onRegionChange(selectedCountryId) {
      var countries = countriesFor(regionSelect.value);
      fillSelect(countrySelect, countries, "country_id", "label", "— Select country —");
      if (selectedCountryId) countrySelect.value = selectedCountryId;
      onCountryChange();
    }

    function onCountryChange(selectedStateId) {
      refreshStateUI(countrySelect.value, selectedStateId);
      onStateChange();
    }

    function onStateChange(selectedCityId) {
      refreshCityUI(stateSelect.value, selectedCityId);
    }

    if (regionSelect) {
      regionSelect.addEventListener("change", function () { onRegionChange(); });
    }
    countrySelect.addEventListener("change", function () { onCountryChange(); });
    stateSelect.addEventListener("change", function () { onStateChange(); });

    fetch("/geography/tree")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        tree = data;

        var initCountryId = initial.country_id || "";

        if (regionSelect) {
          fillSelect(regionSelect, tree.regions, "region_id", "label", "— Select region —");

          // Walk the cascade bottom-up from whatever the edit form already
          // knows, so every level is correctly pre-populated and enabled.
          var initRegionId = initial.region_id || "";
          if (!initRegionId && initCountryId) {
            var c = tree.countries.find(function (c) { return String(c.country_id) === String(initCountryId); });
            if (c) initRegionId = c.region_id;
          }
          if (initRegionId) regionSelect.value = initRegionId;
          onRegionChange(initCountryId);
        } else {
          // No Region gate on this form -- Country lists every country on
          // file directly (see file header for why).
          fillSelect(countrySelect, tree.countries, "country_id", "label", "— Select country —");
          if (initCountryId) countrySelect.value = initCountryId;
          onCountryChange();
        }

        if (initial.state_id) {
          stateSelect.value = initial.state_id;
          onStateChange(initial.city_id || "");
        }
      })
      .catch(function () {
        // Network hiccup: leave the selects showing just a placeholder
        // rather than throwing — free-text fallback fields still work.
        if (regionSelect) {
          fillSelect(regionSelect, [], null, null, "— Unable to load —");
        } else {
          fillSelect(countrySelect, [], null, null, "— Unable to load —");
        }
      });
  };
})();
