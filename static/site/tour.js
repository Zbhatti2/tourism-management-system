/* Tenant website tour page (templates/site/tour.html): the photo slider and
   the pop-up with a place's / hotel's / restaurant's photos and description. */
(function () {
  var T = window.TOUR || {images: []};
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]; }); }
  var cur = 0;
  function photo(i) {
    var n = T.images.length; if (!n || !$("photo-main")) return;
    cur = (i + n) % n;
    $("photo-main").src = T.images[cur].src;
    $("photo-caption").textContent = (T.images[cur].caption || "") + "  ·  " + (cur + 1) + " / " + n;
    document.querySelectorAll("#photo-thumbs img").forEach(function (el) { el.classList.toggle("on", Number(el.getAttribute("data-i")) === cur); });
  }
  document.addEventListener("click", function (ev) {
    if (ev.target.closest("#gallery .prev")) photo(cur - 1);
    if (ev.target.closest("#gallery .next")) photo(cur + 1);
    var th = ev.target.closest("#photo-thumbs img"); if (th) photo(Number(th.getAttribute("data-i")));
    var chip = ev.target.closest(".chip[data-entity]"); if (chip) openEntity(chip.getAttribute("data-entity"), chip.getAttribute("data-id"));
    var bk = ev.target.closest("a[data-dep]");
    if (bk) { var sel = $("b-dep"); if (sel) { sel.value = bk.getAttribute("data-dep"); } }
  });
  function openEntity(kind, id) {
    var url = window.ENTITY_URL.replace("KIND", kind).replace(/\/0\.json$/, "/" + id + ".json");
    var modal = bootstrap.Modal.getOrCreateInstance($("entityModal"));
    $("entityTitle").textContent = "Loading…"; $("entityMeta").textContent = ""; $("entityDesc").textContent = "";
    $("entityPhotos").innerHTML = ""; $("entityLink").innerHTML = "";
    modal.show();
    fetch(url).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }).then(function (e) {
      $("entityTitle").textContent = e.name;
      $("entityMeta").textContent = [e.type, e.place, e.since ? "Since " + e.since : null].filter(Boolean).join(" · ");
      $("entityDesc").textContent = e.description || "";
      if (e.website) $("entityLink").innerHTML = '<a href="' + esc(e.website) + '" target="_blank" rel="noopener"><i class="bi bi-box-arrow-up-right"></i> Website</a>';
      if (e.images.length) {
        var id = "ec" + Date.now();
        $("entityPhotos").innerHTML = '<div id="' + id + '" class="carousel slide"><div class="carousel-inner">' +
          e.images.map(function (im, i) {
            return '<div class="carousel-item' + (i ? "" : " active") + '"><img src="' + esc(im.src) + '" alt="' + esc(im.caption) + '">' +
              '<div class="small text-muted mt-1">' + esc(im.caption) + (e.images.length > 1 ? " · " + (i + 1) + " / " + e.images.length : "") + "</div></div>";
          }).join("") + "</div>" + (e.images.length > 1 ?
          '<button class="carousel-control-prev" type="button" data-bs-target="#' + id + '" data-bs-slide="prev"><span class="carousel-control-prev-icon"></span></button>' +
          '<button class="carousel-control-next" type="button" data-bs-target="#' + id + '" data-bs-slide="next"><span class="carousel-control-next-icon"></span></button>' : "") + "</div>";
      } else {
        $("entityPhotos").innerHTML = '<div class="text-muted small">No photos yet.</div>';
      }
    }).catch(function () { $("entityTitle").textContent = "Sorry, this couldn't be loaded."; });
  }
})();
