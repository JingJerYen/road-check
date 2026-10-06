/* roadcheck 網頁：放圖釘、選天數與半徑，向 /api/check 查詢並把結果畫在地圖上。 */
(function () {
  "use strict";

  var TAIPEI = [25.0418, 121.5434];
  var DAY_CHOICES = [1, 3, 7, 14];
  var RADIUS_CHOICES = [50, 100, 200, 500];
  var STORE_KEY = "roadcheck.web.v1";
  var NOMINATIM = "https://nominatim.openstreetmap.org";
  var WEEKDAY = ["日", "一", "二", "三", "四", "五", "六"];
  var COLORS = { construction: "#d97706", restriction: "#dc2626", nearby: "#6b7280" };

  var $ = function (id) { return document.getElementById(id); };
  var state = { lat: null, lon: null, days: 3, radius: 100, roads: "", roadsAuto: true, address: "" };
  var status = null;
  var pending = null;     // AbortController of the in-flight check
  var pollTimer = null;

  // ---------- helpers ----------
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtDate(iso) {
    if (!iso) return "?";
    var d = new Date(iso + "T00:00:00");
    return (d.getMonth() + 1) + "/" + d.getDate() + "（" + WEEKDAY[d.getDay()] + "）";
  }
  function fmtPeriod(ev) {
    var p = ev.start === ev.end ? fmtDate(ev.start) : fmtDate(ev.start) + " – " + fmtDate(ev.end);
    return ev.time_window ? p + "　" + ev.time_window : p;
  }
  function fmtTime(isoUtc) {
    var d = new Date(isoUtc);
    if (isNaN(d)) return isoUtc;
    var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
    return (d.getMonth() + 1) + "/" + d.getDate() + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function save() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(state));
    } catch (e) { /* 私密模式等情況下沒有 storage，照樣能用 */ }
    if (state.lat != null) {
      var h = "#" + state.lat.toFixed(6) + "," + state.lon.toFixed(6) + "," + state.days + "," + state.radius;
      if (location.hash !== h) history.replaceState(null, "", h);
    }
  }
  function load() {
    // 先讀上次的設定，再用網址 hash 覆寫位置／天數／半徑；位置不同時路名重新自動帶入
    try {
      var saved = JSON.parse(localStorage.getItem(STORE_KEY) || "null");
      if (saved && typeof saved === "object") Object.assign(state, saved);
    } catch (e) { /* ignore */ }
    var m = /^#(-?[\d.]+),(-?[\d.]+)(?:,(\d+))?(?:,(\d+))?/.exec(location.hash);
    if (!m) return;
    var lat = +m[1], lon = +m[2];
    if (state.lat == null || Math.abs(lat - state.lat) > 1e-6 || Math.abs(lon - state.lon) > 1e-6) {
      state.roads = ""; state.roadsAuto = true; state.address = "";
    }
    state.lat = lat; state.lon = lon;
    if (m[3]) state.days = +m[3];
    if (m[4]) state.radius = +m[4];
  }

  // ---------- map ----------
  load();
  var map = L.map("map", { zoomControl: true }).setView(
    state.lat != null ? [state.lat, state.lon] : TAIPEI, state.lat != null ? 17 : 13);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);
  var nearbyLayer = L.featureGroup().addTo(map);
  var matchLayer = L.featureGroup().addTo(map);
  var marker = null, circle = null;
  var layersByKey = {};

  map.on("click", function (e) { setPin(e.latlng.lat, e.latlng.lng, true); });

  function setPin(lat, lon, lookupRoad) {
    state.lat = lat; state.lon = lon;
    if (!marker) {
      marker = L.marker([lat, lon], { draggable: true, title: "停車位置", keyboard: true }).addTo(map);
      marker.on("dragend", function () {
        var p = marker.getLatLng();
        setPin(p.lat, p.lng, true);
      });
      circle = L.circle([lat, lon], { radius: state.radius, color: "#1c7ed6", weight: 2, fillOpacity: 0.08 }).addTo(map);
    } else {
      marker.setLatLng([lat, lon]);
      circle.setLatLng([lat, lon]);
    }
    circle.setRadius(state.radius);
    if (lookupRoad) {
      state.address = "";
      if (state.roadsAuto) state.roads = "";
      reverseGeocode(lat, lon);
    }
    renderPin();
    save();
    check();
  }

  // ---------- controls ----------
  function renderSeg(el, choices, current, fmt, maxAllowed, onPick) {
    el.innerHTML = "";
    choices.forEach(function (v) {
      var b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", String(v === current));
      b.textContent = fmt(v);
      if (maxAllowed != null && v > maxAllowed) return;   // 伺服器沒抓那麼遠的資料（roadcheck web --fetch-days）
      b.addEventListener("click", function () { onPick(v); });
      el.appendChild(b);
    });
  }
  function renderControls() {
    var maxDays = status ? status.fetch_days : 7;
    if (state.days > maxDays) state.days = maxDays;
    var dayChoices = DAY_CHOICES.slice();
    if (dayChoices.indexOf(state.days) < 0) { dayChoices.push(state.days); dayChoices.sort(function (a, b) { return a - b; }); }
    renderSeg($("days"), dayChoices, state.days, function (v) { return "未來 " + v + " 天"; }, maxDays, function (v) {
      state.days = v; renderControls(); save(); check();
    });
    renderSeg($("radius"), RADIUS_CHOICES, state.radius, function (v) { return v + " m"; }, null, function (v) {
      state.radius = v; if (circle) circle.setRadius(v); renderControls(); save(); check();
    });
    if (document.activeElement !== $("roads")) $("roads").value = state.roads;
  }
  var roadsTimer = null;
  $("roads").addEventListener("input", function () {
    state.roads = this.value; state.roadsAuto = this.value.trim() === "";
    clearTimeout(roadsTimer);
    roadsTimer = setTimeout(function () { save(); check(); }, 400);
  });

  function renderPin() {
    if (state.lat == null) return;
    $("pin").innerHTML = "📍 <strong>" + state.lat.toFixed(6) + ", " + state.lon.toFixed(6) + "</strong>" +
      (state.address ? '<span class="addr">' + esc(state.address) + "</span>" : "");
  }

  // ---------- OpenStreetMap Nominatim（瀏覽器直接呼叫，失敗就略過） ----------
  function reverseGeocode(lat, lon) {
    var url = NOMINATIM + "/reverse?format=jsonv2&zoom=18&accept-language=zh-TW&lat=" + lat + "&lon=" + lon;
    fetch(url).then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
      if (!j || state.lat !== lat || state.lon !== lon) return;
      var a = j.address || {};
      state.address = [a.city || a.county, a.suburb || a.city_district, a.road, a.house_number ? a.house_number + "號" : ""]
        .filter(Boolean).join("");
      if (state.roadsAuto && a.road) {
        state.roads = a.road;
        renderControls();
        check();
      }
      renderPin();
      save();
    }).catch(function () { /* 連不到 Nominatim：路名請手動輸入 */ });
  }
  $("search").addEventListener("submit", function (e) {
    e.preventDefault();
    var q = $("search-input").value.trim();
    if (!q) return;
    var msg = $("search-msg");
    msg.hidden = false; msg.textContent = "搜尋中…";
    var url = NOMINATIM + "/search?format=jsonv2&limit=1&countrycodes=tw&accept-language=zh-TW&q=" + encodeURIComponent(q);
    fetch(url).then(function (r) { return r.ok ? r.json() : []; }).then(function (list) {
      if (!list || !list.length) {
        msg.textContent = "找不到這個地址。可以試著少打幾個字（例如只打路名），或直接在地圖上點選。";
        return;
      }
      msg.hidden = true;
      var lat = +list[0].lat, lon = +list[0].lon;
      map.setView([lat, lon], 18);
      setPin(lat, lon, true);
    }).catch(function () {
      msg.textContent = "搜尋服務連不上，請直接在地圖上點選位置。";
    });
  });
  $("locate").addEventListener("click", function () {
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition(function (p) {
      map.setView([p.coords.latitude, p.coords.longitude], 18);
      setPin(p.coords.latitude, p.coords.longitude, true);
    }, function () {
      var msg = $("search-msg");
      msg.hidden = false; msg.textContent = "無法取得目前位置（瀏覽器沒有授權）。";
    }, { enableHighAccuracy: true, timeout: 10000 });
  });

  // ---------- query ----------
  function check() {
    if (state.lat == null) {
      setSummary("info", "在地圖上點一下你停車的位置。", "也可以用上面的搜尋或 📍 定位。");
      return;
    }
    if (pending) pending.abort();
    pending = typeof AbortController === "function" ? new AbortController() : null;
    var qs = "lat=" + state.lat + "&lon=" + state.lon + "&radius=" + state.radius + "&days=" + state.days +
      "&roads=" + encodeURIComponent(state.roads || "");
    fetch("/api/check?" + qs, pending ? { signal: pending.signal } : {})
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.error) { setSummary("bad", "查詢失敗", data.error); return; }
        status = data.status;
        renderStatus();
        renderResults(data);
      })
      .catch(function (e) {
        if (e && e.name === "AbortError") return;
        setSummary("bad", "連不到 roadcheck 伺服器", "確認終端機裡的 roadcheck web 還在執行。");
      });
  }

  function setSummary(kind, title, sub) {
    var el = $("summary");
    el.className = "summary " + kind;
    el.innerHTML = esc(title) + (sub ? '<span class="sub">' + esc(sub) + "</span>" : "");
  }

  function drawEvent(ev, layer, style) {
    var group = L.featureGroup();
    ev.shapes.forEach(function (s) {
      var closed = s.length >= 4 && s[0][0] === s[s.length - 1][0] && s[0][1] === s[s.length - 1][1];
      (closed ? L.polygon(s, style) : L.polyline(s, Object.assign({}, style, { weight: style.weight + 2 }))).addTo(group);
    });
    if (!ev.shapes.length && ev.lat != null) {
      L.circleMarker([ev.lat, ev.lon], Object.assign({ radius: 7 }, style)).addTo(group);
    }
    if (!group.getLayers().length) return null;
    group.bindPopup("<b>" + esc(ev.title) + "</b>" + esc(fmtPeriod(ev)) +
      (ev.agency ? "<br>" + esc(ev.agency) : ""));
    group.addTo(layer);
    return group;
  }

  function whereText(ev) {
    if (ev.reason === "road") return "路名相同：" + ev.matched_roads.join("、") + "（沒有精確位置，可能不在你附近）";
    if (ev.distance_m != null && ev.distance_m < 1) return "你的位置就在範圍內";
    return "距離約 " + Math.round(ev.distance_m) + " 公尺";
  }

  function cardHtml(ev, compact) {
    var kindTag = '<span class="tag k-' + ev.kind + '">' + esc(ev.kind === "construction" ? "施工" : "管制") +
      (ev.category ? "・" + esc(ev.category) : "") + "</span>";
    var tags = kindTag +
      (ev.blocks_traffic ? '<span class="tag block">影響交通</span>' : "") +
      '<span class="tag ' + (ev.timing === "進行中" ? "plain" : "soon") + '">' + esc(ev.timing) + "</span>";
    var html = '<div class="tags">' + tags + "</div>" +
      "<h3>" + esc(ev.title) + "</h3>" +
      "<p>" + esc(fmtPeriod(ev)) + "</p>" +
      '<p class="where">' + esc(whereText(ev)) + "</p>";
    if (!compact) {
      if (ev.address && ev.address !== ev.title) html += '<p class="addr">' + esc(ev.address) + "</p>";
      var meta = [];
      if (ev.agency) meta.push("單位：" + esc(ev.agency));
      if (ev.plan_b) meta.push("替代方案：" + esc(ev.plan_b));
      if (meta.length) html += "<p>" + meta.join("　") + "</p>";
      var links = ['<a href="' + esc(ev.url) + '" target="_blank" rel="noopener">資料來源</a>'];
      if (ev.bulletin_url) links.push('<a href="' + esc(ev.bulletin_url) + '" target="_blank" rel="noopener">公告原文</a>');
      html += '<p class="small">' + links.join("　") + "</p>";
    }
    return html;
  }

  function addCards(list, events, compact) {
    list.innerHTML = "";
    events.forEach(function (ev) {
      var li = document.createElement("li");
      li.className = "card " + ev.kind;
      li.tabIndex = 0;
      li.innerHTML = cardHtml(ev, compact);
      var focus = function (e) {
        if (e && e.target && e.target.tagName === "A") return;
        var layer = layersByKey[ev.key];
        document.querySelectorAll(".card.active").forEach(function (c) { c.classList.remove("active"); });
        li.classList.add("active");
        if (layer) {
          map.fitBounds(layer.getBounds().extend(marker.getLatLng()), { maxZoom: 18, padding: [40, 40] });
          layer.openPopup();
        }
      };
      li.addEventListener("click", focus);
      li.addEventListener("keydown", function (e) { if (e.key === "Enter") focus(e); });
      list.appendChild(li);
    });
  }

  function renderResults(data) {
    matchLayer.clearLayers();
    nearbyLayer.clearLayers();
    layersByKey = {};
    var q = data.query;
    var hasData = status && status.sources.length > 0;

    data.nearby.forEach(function (ev) {
      var l = drawEvent(ev, nearbyLayer, { color: COLORS.nearby, weight: 1, opacity: 0.7, dashArray: "4 4", fillOpacity: 0.08 });
      if (l) layersByKey[ev.key] = l;
    });
    data.matches.forEach(function (ev) {
      var l = drawEvent(ev, matchLayer, { color: COLORS[ev.kind] || COLORS.restriction, weight: 2, opacity: 0.95, fillOpacity: 0.3 });
      if (l) layersByKey[ev.key] = l;
    });

    var range = "未來 " + q.days + " 天（" + fmtDate(q.today) + " – " + fmtDate(q.until) + "）";
    var n = data.matches.length;
    if (!hasData) {
      setSummary("info", status && status.refreshing ? "第一次下載資料中，請稍候…" : "還沒有資料",
        "施工資料幾秒內就好，外部管制路段約 5–10 分鐘；完成後會自動更新。");
    } else if (n === 0) {
      setSummary("ok", "✅ " + range + "，半徑 " + q.radius_m + " 公尺內沒有已知的施工或封路",
        data.nearby.length ? "附近另有 " + data.nearby.length + " 件，灰色虛線標在地圖上。" : "");
    } else {
      var blocking = data.matches.filter(function (m) { return m.blocks_traffic; }).length;
      setSummary(blocking ? "bad" : "warn", "⚠️ " + range + "，有 " + n + " 件可能影響這裡",
        blocking ? "其中 " + blocking + " 件影響交通。" : "");
    }
    addCards($("results"), data.matches, false);

    var nb = $("nearby");
    if (data.nearby.length) {
      nb.hidden = false;
      nb.querySelector("summary").textContent =
        "附近 " + Math.round(data.nearby_radius_m) + " 公尺內、不在半徑內的其他 " + data.nearby.length + " 件";
      addCards(nb.querySelector("ol"), data.nearby, true);
    } else {
      nb.hidden = true;
    }
  }

  // ---------- data status ----------
  function renderStatus() {
    if (!status) return;
    var parts = status.sources.map(function (s) {
      return s.label + " " + s.events + " 件（" + fmtTime(s.updated_at) + "）";
    });
    var text = parts.length ? "資料：" + parts.join("、") : "還沒有資料";
    if (status.refreshing) text += "　⏳ 更新中…";
    if (status.last_error) text += "　⚠️ 上次更新有錯誤：" + status.last_error;
    $("data-status").textContent = text;
    $("refresh").disabled = !!status.refreshing;
    renderControls();
    if (status.refreshing && !pollTimer) {
      pollTimer = setInterval(pollStatus, 5000);
    } else if (!status.refreshing && pollTimer) {
      clearInterval(pollTimer); pollTimer = null;
      check();
    }
  }
  function pollStatus() {
    fetch("/api/status").then(function (r) { return r.json(); }).then(function (s) {
      var was = status && status.refreshing;
      var counts = status ? JSON.stringify(status.sources) : "";
      status = s;
      renderStatus();
      if (was && JSON.stringify(s.sources) !== counts) check();   // 有來源先抓完就先更新結果
    }).catch(function () { /* 伺服器暫時沒回應 */ });
  }
  $("refresh").addEventListener("click", function () {
    fetch("/api/refresh", { method: "POST" }).then(function (r) { return r.json(); }).then(function (j) {
      status = j.status; renderStatus();
    });
  });

  // 分享連結貼到已經開著的分頁時，只有 hash 會變，頁面不會重新載入
  window.addEventListener("hashchange", function () {
    var before = [state.lat, state.lon, state.days, state.radius].join();
    load();
    if ([state.lat, state.lon, state.days, state.radius].join() === before || state.lat == null) return;
    map.setView([state.lat, state.lon], Math.max(map.getZoom(), 17));
    renderControls();
    setPin(state.lat, state.lon, true);
  });

  // ---------- start ----------
  renderControls();
  pollStatus();
  if (state.lat != null) {
    setPin(state.lat, state.lon, !state.roads);
  } else {
    check();
  }
})();
