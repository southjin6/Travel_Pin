/* Interactive Travel Bucket List & Map Planner
 * Initializes the Leaflet/OpenStreetMap map, handles click-to-pin events,
 * auto-populates country data (reverse geocode + REST Countries), and renders
 * markers for destinations already saved in SQLite.
 */
(function () {
    "use strict";

    // --- Initialize the interactive Leaflet map (Philippines only) -----------
    // Tight bounding box that hugs the Philippine archipelago.
    var PH_BOUNDS = [[4.0, 117.0], [21.3, 126.7]];
    // Mercator-centred midpoint of the archipelago (off Bohol Sea / Central Visayas).
    var PH_CENTER = [12.7, 121.8];
    var map = L.map("map", {
        center: PH_CENTER,
        zoom: 6.5,
        maxBounds: PH_BOUNDS,
        maxBoundsViscosity: 1.0,
        minZoom: 5,
        zoomSnap: 0.5
    });

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    }).addTo(map);

    // Mask everything outside the Philippines: a world-sized polygon with a
    // rectangular hole cut over the Philippine bounding box, so only the
    // country shows map tiles.
    var worldRing = [[-90, -180], [-90, 180], [90, 180], [90, -180]];
    var phHole = [[4.0, 117.0], [4.0, 126.7], [21.3, 126.7], [21.3, 117.0]];
    L.polygon([worldRing, phHole], {
        stroke: false,
        fillColor: "#0b1f3a",
        fillOpacity: 1,
        fillRule: "evenodd",
        interactive: false
    }).addTo(map);

    // Keep the map frame filled with Philippine tiles: find the least zoomed-out
    // level at which the viewport still fits inside the country box, so neither
    // the mask beyond its edges nor open sea is ever visible.
    function countryFillsFrame(zoom) {
        var sw = map.project(L.latLng(PH_BOUNDS[0][0], PH_BOUNDS[0][1]), zoom);
        var ne = map.project(L.latLng(PH_BOUNDS[1][0], PH_BOUNDS[1][1]), zoom);
        var size = map.getSize();
        return Math.abs(ne.x - sw.x) >= size.x && Math.abs(ne.y - sw.y) >= size.y;
    }

    function frameFillZoom() {
        for (var zoom = 3; zoom <= 18; zoom += map.options.zoomSnap) {
            if (countryFillsFrame(zoom)) return zoom;
        }
        return 8;
    }

    function applyMinZoom() {
        if (map.getSize().x < 60) return;   // container not laid out yet
        map.setMinZoom(frameFillZoom());
    }

    applyMinZoom();
    map.setView(PH_CENTER, map.getMinZoom());

    var resizeTimer = null;
    window.addEventListener("resize", function () {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(applyMinZoom, 200);
    });

    // Different icons for a temporary (in-progress) pin vs. saved destinations.
    var tempIcon = L.divIcon({
        className: "pin-temp",
        html: '<div class="pin-dot pin-dot-new"></div>',
        iconSize: [22, 22],
        iconAnchor: [11, 11]
    });

    function savedIcon(visited) {
        return L.divIcon({
            className: "pin-saved",
            html: '<div class="pin-dot ' + (visited ? "pin-visited" : "pin-wishlist") + '"></div>',
            iconSize: [22, 22],
            iconAnchor: [11, 11]
        });
    }

    // --- Render markers for destinations loaded from the server --------------
    var savedLayer = L.layerGroup().addTo(map);
    var bounds = [];

    function loadSavedMarkers() {
        var el = document.getElementById("destinations-data");
        if (!el) return;
        var destinations;
        try {
            destinations = JSON.parse(el.textContent);
        } catch (e) {
            return;
        }
        destinations.forEach(function (d) {
            var marker = L.marker([d.latitude, d.longitude], {
                icon: savedIcon(d.visited_status === "visited")
            });
            var flag = d.country_code
                ? '<img class="popup-flag" src="https://flagcdn.com/w40/' + d.country_code + '.png" alt="">'
                : "";
            marker.bindPopup(
                '<strong>' + escapeHtml(d.city) + "</strong><br>" +
                flag + escapeHtml(d.country) + "<br>" +
                '<span class="popup-coords">' +
                Number(d.latitude).toFixed(4) + ", " + Number(d.longitude).toFixed(4) +
                "</span>"
            );
            marker.addTo(savedLayer);
            bounds.push([d.latitude, d.longitude]);
        });
        // Note: the map stays framed on the whole Philippines (set on init),
        // rather than zooming to just the saved markers.
    }

    function escapeHtml(str) {
        return String(str == null ? "" : str).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    // --- Click-to-pin flow ---------------------------------------------------
    var form = document.getElementById("pin-form");
    var lookupStatus = document.getElementById("lookup-status");
    var tempMarker = null;

    var fields = {
        city: document.getElementById("f-city"),
        country: document.getElementById("f-country"),
        code: document.getElementById("f-country-code"),
        lat: document.getElementById("f-lat"),
        lon: document.getElementById("f-lon")
    };

    map.on("click", function (e) {
        var lat = e.latlng.lat;
        var lon = e.latlng.lng;

        // Place (or move) the temporary marker and reveal the form.
        if (tempMarker) {
            tempMarker.setLatLng(e.latlng);
        } else {
            tempMarker = L.marker(e.latlng, { icon: tempIcon, draggable: true }).addTo(map);
            tempMarker.on("dragend", function () {
                var p = tempMarker.getLatLng();
                populate(p.lat, p.lng);
            });
        }

        form.classList.remove("hidden");
        fields.lat.value = lat.toFixed(6);
        fields.lon.value = lon.toFixed(6);
        fields.city.value = "";
        fields.country.value = "";
        fields.code.value = "";
        lookupStatus.textContent = "Looking up location...";
        form.scrollIntoView({ behavior: "smooth", block: "nearest" });

        populate(lat, lon);
    });

    // Reverse-geocode the coordinates, then enrich with REST Countries data.
    function populate(lat, lon) {
        fields.lat.value = lat.toFixed(6);
        fields.lon.value = lon.toFixed(6);
        lookupStatus.textContent = "Looking up location...";

        fetch("/api/reverse?lat=" + lat + "&lon=" + lon)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.error) throw new Error(data.error);
                fields.city.value = data.city || "";
                fields.country.value = data.country || "";
                fields.code.value = data.country_code || "";
                var isPH = (data.country_code || "").toLowerCase() === "ph";
                setPHAllowed(isPH, data.country);
                if (!isPH) return null;
                return fetchCountry(data.country_code);
            })
            .catch(function () {
                lookupStatus.textContent = "Could not auto-detect the place — please type it in.";
            });
    }

    // Enable or block saving depending on whether the pin is in the Philippines.
    function setPHAllowed(isPH, country) {
        var submit = form.querySelector('button[type="submit"]');
        if (isPH) {
            if (submit) submit.disabled = false;
            return;
        }
        if (submit) submit.disabled = true;
        lookupStatus.textContent =
            "\u26A0 " + (country || "This spot") + " is outside the Philippines — only Philippine destinations can be saved.";
    }

    // Pull flag / currency / language data from the REST Countries API
    // (proxied through our Flask back-end, which holds the API key). Every pin
    // here is Philippine, so one answer is reused for the whole session.
    var countryCache = {};

    function describeCountry(c) {
        if (!c) {
            lookupStatus.textContent = "Location detected. Add it to your list!";
            return;
        }
        var parts = [];
        if (c.currencies) parts.push(c.currencies);
        if (c.languages) parts.push(c.languages.split(", ").slice(0, 3).join(", "));
        lookupStatus.innerHTML =
            '<span class="lookup-flag">' + escapeHtml(c.flag_emoji || "") + "</span> " +
            escapeHtml(c.name || "") +
            (parts.length ? " &middot; " + escapeHtml(parts.join(" · ")) : "");
    }

    function fetchCountry(code) {
        if (!code) {
            describeCountry(null);
            return Promise.resolve();
        }
        if (countryCache[code]) {
            describeCountry(countryCache[code]);
            return Promise.resolve();
        }
        return fetch("/api/country/" + encodeURIComponent(code))
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (c) {
                if (!c || c.error) return;
                countryCache[code] = c;
                describeCountry(c);
            })
            .catch(function () { /* non-fatal */ });
    }

    // --- Cancel button clears the temporary pin -----------------------------
    var cancelBtn = document.getElementById("cancel-pin");
    if (cancelBtn) {
        cancelBtn.addEventListener("click", function () {
            if (tempMarker) {
                map.removeLayer(tempMarker);
                tempMarker = null;
            }
            form.classList.add("hidden");
        });
    }

    // --- "Focus" buttons on the cards re-center the map ----------------------
    document.querySelectorAll(".focus-pin").forEach(function (btn) {
        btn.addEventListener("click", function () {
            var lat = parseFloat(btn.getAttribute("data-lat"));
            var lon = parseFloat(btn.getAttribute("data-lon"));
            map.setView([lat, lon], 8, { animate: true });
            document.getElementById("map").scrollIntoView({ behavior: "smooth", block: "center" });
        });
    });

    // --- "Add / Edit notes" opens that card's inline editor ------------------
    // Saving is a plain form POST, so the editor works without JavaScript; the
    // buttons only decide which single card is open at a time.
    document.querySelectorAll(".edit-note").forEach(function (btn) {
        btn.addEventListener("click", function () {
            var form = document.getElementById(btn.getAttribute("data-target"));
            if (!form) return;
            var wasOpen = !form.classList.contains("hidden");
            document.querySelectorAll(".note-form").forEach(function (f) {
                f.classList.add("hidden");
            });
            if (wasOpen) return;
            form.classList.remove("hidden");
            form.querySelector("textarea").focus();
        });
    });

    document.querySelectorAll(".cancel-note").forEach(function (btn) {
        btn.addEventListener("click", function () {
            btn.closest(".note-form").classList.add("hidden");
        });
    });

    // --- Nearby landmarks (Overpass / OpenStreetMap) -------------------------
    var landmarkLayer = L.layerGroup().addTo(map);
    var landmarkIcon = L.divIcon({
        className: "pin-landmark",
        html: '<div class="landmark-star">\u2605</div>',
        iconSize: [26, 26],
        iconAnchor: [13, 13]
    });

    var lmPanel = document.getElementById("landmarks-panel");
    var lmList = document.getElementById("landmarks-list");
    var lmStatus = document.getElementById("landmarks-status");
    var lmPlace = document.getElementById("landmarks-place");
    var lmClose = document.getElementById("landmarks-close");
    var activeLandmarkBtn = null;
    var activeLandmarkName = null;

    function clearLandmarks() {
        landmarkLayer.clearLayers();
        if (lmList) lmList.innerHTML = "";
        if (lmPanel) lmPanel.classList.add("hidden");
        if (activeLandmarkBtn) activeLandmarkBtn.classList.remove("is-active");
        activeLandmarkBtn = null;
        activeLandmarkName = null;
    }

    // A landmark scan is the priciest request this app makes, so each pin's
    // results are kept for the session and re-opened without the network.
    var landmarkCache = {};
    var landmarksPending = false;

    function renderLandmarks(lat, lon, data) {
        var items = data.items || [];
        if (!items.length) {
            lmStatus.textContent = "No landmarks found nearby — try a more central spot.";
            return;
        }
        lmStatus.textContent = "Showing " + items.length + " landmark" +
            (items.length > 1 ? "s" : "") + " within ~" +
            Math.round((data.radius_m || 15000) / 1000) + " km." +
            (data.stale ? " (cached copy — service is busy)" : "");

        var pts = [[lat, lon]];
        items.forEach(function (it) {
            var marker = L.marker([it.lat, it.lon], { icon: landmarkIcon });
            marker.bindPopup(
                (it.image
                    ? '<img class="popup-photo" src="' + escapeHtml(it.image) +
                      '" alt="' + escapeHtml(it.name) + '"><br>'
                    : "") +
                "<strong>" + escapeHtml(it.name) + "</strong><br>" +
                '<span class="popup-type">' + escapeHtml(it.type) + "</span> &middot; " +
                it.distance_km + " km"
            );
            marker.addTo(landmarkLayer);
            pts.push([it.lat, it.lon]);

            var li = document.createElement("li");
            var item = document.createElement("button");
            item.type = "button";
            item.className = "landmark-item";
            item.addEventListener("click", function () {
                map.setView([it.lat, it.lon], 13, { animate: true });
                marker.openPopup();
            });

            // Thumbnail when Wikimedia has one, otherwise a neutral frame.
            var photo = document.createElement(it.image ? "img" : "span");
            photo.className = it.image ? "landmark-photo" : "landmark-photo landmark-photo-none";
            photo.setAttribute("aria-hidden", "true");
            if (it.image) {
                photo.src = it.image;
                photo.alt = it.name;
                // Deliberately not loading="lazy": most rows sit below the fold
                // in the scrolling panel, and lazy images there are never
                // fetched, so the list looks like it has no pictures at all.
                photo.addEventListener("error", function () {
                    var frame = document.createElement("span");
                    frame.className = "landmark-photo landmark-photo-none";
                    frame.setAttribute("aria-hidden", "true");
                    frame.textContent = "\uD83D\uDCF7";
                    if (photo.parentNode) photo.parentNode.replaceChild(frame, photo);
                });
            } else {
                photo.textContent = "\uD83D\uDCF7";
            }

            var nm = document.createElement("span");
            nm.className = "landmark-name";
            nm.textContent = it.name;
            var meta = document.createElement("span");
            meta.className = "landmark-meta";
            meta.textContent = it.type + " · " + it.distance_km + " km";
            var text = document.createElement("span");
            text.className = "landmark-text";
            text.appendChild(nm);
            text.appendChild(meta);
            item.appendChild(photo);
            item.appendChild(text);
            li.appendChild(item);
            lmList.appendChild(li);
        });
        map.fitBounds(L.latLngBounds(pts), { padding: [40, 40], maxZoom: 13 });
        lmPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    function showLandmarks(btn) {
        if (activeLandmarkBtn === btn) { clearLandmarks(); return; }
        var lat = parseFloat(btn.getAttribute("data-lat"));
        var lon = parseFloat(btn.getAttribute("data-lon"));
        var key = lat.toFixed(3) + "," + lon.toFixed(3);

        // A cold scan takes tens of seconds; say so instead of eating the click.
        if (landmarksPending) {
            if (lmPanel.classList.contains("hidden")) {
                lmPanel.classList.remove("hidden");
                lmPlace.textContent = btn.getAttribute("data-name") || "here";
            }
            lmStatus.textContent = "Still scanning " + (activeLandmarkName || "the previous pin") +
                " — one moment.";
            return;
        }
        clearLandmarks();

        activeLandmarkBtn = btn;
        activeLandmarkName = btn.getAttribute("data-name") || "here";
        btn.classList.add("is-active");
        lmPanel.classList.remove("hidden");
        lmPlace.textContent = activeLandmarkName;

        if (landmarkCache[key]) {
            renderLandmarks(lat, lon, landmarkCache[key]);
            return;
        }

        lmStatus.textContent = "Scanning OpenStreetMap for nearby landmarks" +
            " (a first look takes up to half a minute)…";
        landmarksPending = true;
        fetch("/api/landmarks?lat=" + lat + "&lon=" + lon)
            .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
            .then(function (res) {
                var data = res.d || {};
                if (!res.ok) {
                    lmStatus.textContent = data.error || "Could not load landmarks right now.";
                    if (data.retry_after_s) {
                        lmStatus.textContent += " Next try in ~" +
                            Math.ceil(data.retry_after_s / 60) + " min.";
                    }
                    return;
                }
                if (data.items && data.items.length) landmarkCache[key] = data;
                renderLandmarks(lat, lon, data);
            })
            .catch(function () {
                lmStatus.textContent = "Could not load landmarks right now.";
            })
            .then(function () { landmarksPending = false; });
    }

    document.querySelectorAll(".btn-landmark").forEach(function (btn) {
        btn.addEventListener("click", function () { showLandmarks(btn); });
    });
    if (lmClose) lmClose.addEventListener("click", clearLandmarks);

    loadSavedMarkers();
})();
