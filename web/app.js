const fallbackGps = "113.963041,22.801446";
const preferredCityName = "深圳";
const state = {
  amapReady: false,
  amapKey: "",
  map: null,
  poiSelections: {},
  lastQuery: "",
  coordSystem: "amap",
};

const els = {
  query: document.querySelector("#queryInput"),
  gps: document.querySelector("#gpsInput"),
  locate: document.querySelector("#locateBtn"),
  submit: document.querySelector("#submitBtn"),
  demo: document.querySelector("#demoBtn"),
  badge: document.querySelector("#statusBadge"),
  clarification: document.querySelector("#clarificationBox"),
  distance: document.querySelector("#distanceText"),
  eta: document.querySelector("#etaText"),
  lights: document.querySelector("#lightsText"),
  road: document.querySelector("#roadText"),
  weatherMetric: document.querySelector("#weatherMetric"),
  weather: document.querySelector("#weatherText"),
  steps: document.querySelector("#stepsList"),
  address: document.querySelector("#addressText"),
  narrationBox: document.querySelector("#narrationBox"),
  narration: document.querySelector("#narrationText"),
  map: document.querySelector("#map"),
  staticMap: document.querySelector("#staticMap"),
  fallbackMap: document.querySelector("#fallbackMap"),
  mapStatus: document.querySelector("#mapStatus"),
};

function setStatus(text, type = "") {
  els.badge.textContent = text;
  els.badge.className = `badge ${type}`.trim();
}

function setAddress(text) {
  els.address.textContent = text || "当前位置地址未知，可手动修正经纬度后再规划";
}

function showMapStatus(text = "") {
  if (!text) {
    els.mapStatus.classList.add("hidden");
    els.mapStatus.textContent = "";
    return;
  }
  els.mapStatus.textContent = text;
  els.mapStatus.classList.remove("hidden");
}

function normalizeCoord(coord) {
  if (!coord) return null;
  if (Array.isArray(coord) && coord.length >= 2) {
    const lng = Number(coord[0]);
    const lat = Number(coord[1]);
    return Number.isFinite(lng) && Number.isFinite(lat) ? [lng, lat] : null;
  }
  const parts = String(coord).split(",");
  if (parts.length < 2) return null;
  const lng = Number(parts[0]);
  const lat = Number(parts[1]);
  return Number.isFinite(lng) && Number.isFinite(lat) ? [lng, lat] : null;
}

function formatCoord(lng, lat) {
  return `${lng.toFixed(6)},${lat.toFixed(6)}`;
}

function isOutsidePreferredCity(address = "") {
  return Boolean(address) && !address.includes(preferredCityName);
}

async function usePreferredFallback(reason) {
  els.gps.value = fallbackGps;
  state.coordSystem = "amap";
  setStatus("已用深圳点", "warn");
  await updateLocationByServer(fallbackGps, "amap", { trustCity: true });
  setAddress(`${reason}，已改用深圳默认点。若仍不准确，可手动修改经纬度。`);
  renderMap({ origin: fallbackGps });
}

async function locateUser() {
  setStatus("定位中");
  setAddress("正在获取当前位置...");

  if (state.amapReady && window.AMap?.Geolocation) {
    const located = await locateByAmap();
    if (located) return;
  }

  if (!navigator.geolocation) {
    els.gps.value = fallbackGps;
    setStatus("已用默认点", "warn");
    await updateLocationByServer(fallbackGps, "amap", { trustCity: true });
    renderMap({ origin: els.gps.value });
    return;
  }

  navigator.geolocation.getCurrentPosition(
    async (position) => {
      const { longitude, latitude } = position.coords;
      const normalized = await updateLocationByServer(formatCoord(longitude, latitude), "gps");
      if (normalized?.cityMismatch) {
        await usePreferredFallback(`浏览器定位解析到${normalized.formatted_address || "非深圳位置"}`);
        return;
      }
      setStatus("GPS 已转换");
      renderMap({ origin: els.gps.value });
    },
    async () => {
      els.gps.value = fallbackGps;
      setStatus("定位被拒绝", "warn");
      await updateLocationByServer(fallbackGps, "amap", { trustCity: true });
      renderMap({ origin: fallbackGps });
    },
    { enableHighAccuracy: true, timeout: 8000, maximumAge: 30000 },
  );
}

async function locateByAmap() {
  return new Promise((resolve) => {
    const geolocation = new AMap.Geolocation({
      enableHighAccuracy: true,
      timeout: 8000,
      needAddress: true,
      convert: true,
      GeoLocationFirst: true,
    });

    geolocation.getCurrentPosition(async (status, result) => {
      if (status === "complete" && result?.position) {
        const locatedCoord = formatCoord(result.position.lng, result.position.lat);
        const normalized = await updateLocationByServer(locatedCoord, "amap");
        const address = normalized?.formatted_address || result.formattedAddress || "";
        if (normalized?.cityMismatch) {
          await usePreferredFallback(`高德自动定位解析到${address || locatedCoord}`);
          resolve(true);
          return;
        }
        els.gps.value = normalized?.location || locatedCoord;
        state.coordSystem = "amap";
        setStatus("高德已定位");
        setAddress(address ? `当前定位：${address}` : "已获取高德定位");
        if (state.map) state.map.setCenter(normalizeCoord(els.gps.value));
        renderMap({ origin: els.gps.value });
        resolve(true);
        return;
      }
      resolve(false);
    });
  });
}

async function updateLocationByServer(location, coordSystem, options = {}) {
  try {
    const res = await fetch("/api/route/normalize-location", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ location, coord_system: coordSystem }),
    });
    if (!res.ok) {
      const errorText = await res.text();
      throw new Error(`接口返回 ${res.status}: ${errorText.slice(0, 180)}`);
    }
    const data = await res.json();
    els.gps.value = data.location || location;
    state.coordSystem = data.coord_system || "amap";
    setAddress(data.formatted_address ? `当前定位：${data.formatted_address}` : "当前定位地址未知");
    return {
      ...data,
      cityMismatch: !options.trustCity && isOutsidePreferredCity(data.formatted_address || ""),
    };
  } catch {
    els.gps.value = location;
    state.coordSystem = coordSystem;
    setAddress("定位地址解析失败，可手动修正经纬度");
    return { location, coord_system: coordSystem, formatted_address: "", cityMismatch: false };
  }
}

async function loadAmap() {
  try {
    const res = await fetch("/api/route/map-config");
    const config = await res.json();
    state.amapKey = config.amap_key || "";
    const jsKey = config.amap_js_key || "";
    if (!jsKey) {
      showMapStatus("未配置高德 Web JS API key，已使用高德静态地图兜底。需要交互地图时，请配置 AMAP_JS_API_KEY。");
      return;
    }
    if (config.amap_security_js_code) {
      window._AMapSecurityConfig = { securityJsCode: config.amap_security_js_code };
    }

    await new Promise((resolve, reject) => {
      window.onAmapReady = resolve;
      const script = document.createElement("script");
      script.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(jsKey)}&callback=onAmapReady`;
      script.onerror = reject;
      document.head.appendChild(script);
      window.setTimeout(() => reject(new Error("AMap JS load timeout")), 10000);
    });

    await new Promise((resolve) => {
      AMap.plugin(["AMap.Geolocation", "AMap.Geocoder"], resolve);
    });

    state.map = new AMap.Map("map", {
      zoom: 14,
      resizeEnable: true,
      viewMode: "2D",
      mapStyle: "amap://styles/normal",
      dragEnable: true,
      scrollWheel: true,
      doubleClickZoom: true,
      keyboardEnable: true,
      touchZoom: true,
    });
    state.map.setStatus({
      dragEnable: true,
      scrollWheel: true,
      doubleClickZoom: true,
      keyboardEnable: true,
      touchZoom: true,
    });
    state.amapReady = true;
    els.map.classList.remove("hidden");
    els.staticMap.classList.add("hidden");
    els.fallbackMap.classList.add("hidden");
    showMapStatus("");
  } catch (error) {
    console.warn("AMap JS load failed", error);
    state.amapReady = false;
    els.map.classList.add("hidden");
    showMapStatus("高德 JS 地图加载失败，已使用静态地图/路线示意图兜底。若要启用交互地图，请确认 Web JS API key 与 AMAP_SECURITY_JS_CODE。");
  }
}

function collectRoutePoints(data) {
  const route = Array.isArray(data?.polyline) ? data.polyline.map(normalizeCoord).filter(Boolean) : [];
  const origin = normalizeCoord(data?.origin || els.gps.value);
  const destination = normalizeCoord(data?.destination);
  const waypoints = Array.isArray(data?.waypoints) ? data.waypoints.map(normalizeCoord).filter(Boolean) : [];
  const poiPoints = Array.isArray(data?.pois)
    ? data.pois.map((poi) => ({ ...poi, point: normalizeCoord(poi.location) })).filter((poi) => poi.point)
    : [];
  return { route, origin, destination, waypoints, poiPoints };
}

function renderMap(data) {
  if (state.amapReady && state.map) {
    renderAmap(data);
    return;
  }
  if (drawStaticMap(data)) return;
  drawFallback(data);
}

function renderAmap(data) {
  const { route, origin, destination, poiPoints } = collectRoutePoints(data);
  state.map.clearMap();

  const boundsPoints = [];
  if (route.length > 1) {
    const polyline = new AMap.Polyline({
      path: route,
      strokeColor: "#13795b",
      strokeWeight: 8,
      strokeOpacity: 0.9,
      lineJoin: "round",
      lineCap: "round",
    });
    state.map.add(polyline);
    boundsPoints.push(...route);
  }

  const markers = [];
  if (origin) markers.push({ point: origin, title: "当前位置", color: "#13795b", kind: "origin" });
  poiPoints.forEach((poi, index) => markers.push({
    point: poi.point,
    title: formatPoiTitle(poi),
    color: poi.role === "destination" ? "#c84630" : "#f0a202",
    kind: poi.role === "destination" ? "destination" : "poi",
    index: poi.candidate_index || index + 1,
  }));
  const hasDestinationPoi = destination && poiPoints.some((poi) => (
    poi.role === "destination" &&
    Math.abs(poi.point[0] - destination[0]) < 0.000001 &&
    Math.abs(poi.point[1] - destination[1]) < 0.000001
  ));
  if (destination && !hasDestinationPoi) {
    markers.push({ point: destination, title: destinationTitle(data), color: "#c84630", kind: "destination" });
  }

  markers.forEach((item) => {
    const marker = new AMap.Marker({
      position: item.point,
      title: item.title,
      content: createMapMarkerContent(item),
      offset: new AMap.Pixel(-18, -42),
      zIndex: item.kind === "origin" ? 120 : 110,
    });
    state.map.add(marker);
    boundsPoints.push(item.point);
  });

  if (boundsPoints.length > 1) {
    state.map.setFitView(null, false, [48, 48, 48, 48]);
  } else if (origin) {
    state.map.setCenter(origin);
  }
}

function destinationTitle(data = {}) {
  const rating = data.destination_rating != null ? ` · ${Number(data.destination_rating).toFixed(1)}分` : "";
  return `${data.destination_name || "目的地"}${rating}`;
}

function formatPoiTitle(poi = {}) {
  const rating = poi.rating != null ? ` · ${Number(poi.rating).toFixed(1)}分` : "";
  return `${poi.name || "POI"}${rating}`;
}

function createMapMarkerContent(item) {
  const label = item.kind === "origin" ? "起" : item.kind === "destination" ? "终" : String(item.index || "");
  const safeTitle = String(item.title || "POI").replace(/[<>&"]/g, (char) => ({
    "<": "&lt;",
    ">": "&gt;",
    "&": "&amp;",
    "\"": "&quot;",
  }[char]));
  return `
    <div class="map-marker" style="--marker-color:${item.color}">
      <div class="map-marker-dot"><span>${label}</span></div>
      <div class="map-marker-label">${safeTitle}</div>
    </div>
  `;
}

function drawFallback(data = {}) {
  const { route, origin, destination, poiPoints } = collectRoutePoints(data);
  const svg = els.fallbackMap;
  els.map.classList.toggle("hidden", !state.amapReady);
  els.staticMap.classList.add("hidden");
  svg.classList.toggle("hidden", state.amapReady);
  svg.replaceChildren();
  svg.setAttribute("viewBox", "0 0 1000 700");

  const points = [...route, origin, destination, ...poiPoints.map((poi) => poi.point)].filter(Boolean);
  if (!points.length) {
    const text = createSvg("text", { x: 500, y: 350, "text-anchor": "middle", fill: "#68746e", "font-size": 24 });
    text.textContent = "等待路线规划";
    svg.append(text);
    return;
  }

  const lngs = points.map((point) => point[0]);
  const lats = points.map((point) => point[1]);
  const minLng = Math.min(...lngs);
  const maxLng = Math.max(...lngs);
  const minLat = Math.min(...lats);
  const maxLat = Math.max(...lats);
  const spanLng = Math.max(maxLng - minLng, 0.002);
  const spanLat = Math.max(maxLat - minLat, 0.002);

  const project = ([lng, lat]) => [
    80 + ((lng - minLng) / spanLng) * 840,
    620 - ((lat - minLat) / spanLat) * 540,
  ];

  const routeForDraw = route.length > 1 ? route : [origin, destination].filter(Boolean);
  if (routeForDraw.length > 1) {
    svg.append(createSvg("polyline", {
      points: routeForDraw.map((point) => project(point).join(",")).join(" "),
      fill: "none",
      stroke: "#13795b",
      "stroke-width": 12,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
    }));
  }

  drawMarker(svg, project, origin, "当前位置", "#13795b");
  poiPoints.forEach((poi) => drawMarker(svg, project, poi.point, formatPoiTitle(poi), poi.role === "destination" ? "#c84630" : "#f0a202"));
  if (!poiPoints.some((poi) => poi.role === "destination")) {
    drawMarker(svg, project, destination, destinationTitle(data), "#c84630");
  }
}

function drawStaticMap(data = {}) {
  if (!state.amapKey || state.amapReady) return false;
  const { route, origin, destination, poiPoints } = collectRoutePoints(data);
  const points = [...route, origin, destination, ...poiPoints.map((poi) => poi.point)].filter(Boolean);
  if (!points.length) return false;

  const center = points[Math.floor(points.length / 2)];
  const params = new URLSearchParams({
    key: state.amapKey,
    location: center.join(","),
    zoom: points.length > 1 ? "13" : "16",
    size: "1024*720",
    scale: "2",
  });

  const markerParts = [];
  if (origin) markerParts.push(`mid,0x13795b,A:${origin.join(",")}`);
  poiPoints.forEach((poi, index) => {
    const label = poi.role === "destination" ? "D" : String(index + 1);
    const color = poi.role === "destination" ? "0xc84630" : "0xf0a202";
    markerParts.push(`mid,${color},${label}:${poi.point.join(",")}`);
  });
  if (destination) markerParts.push(`mid,0xc84630,D:${destination.join(",")}`);
  if (markerParts.length) params.set("markers", markerParts.join(";"));

  const pathPoints = (route.length > 1 ? route : [origin, ...poiPoints.map((poi) => poi.point), destination].filter(Boolean))
    .filter(Boolean)
    .filter((_, index, list) => list.length <= 90 || index % Math.ceil(list.length / 90) === 0);
  if (pathPoints.length > 1) {
    params.set("paths", `8,0x13795b,0.9,,,:${pathPoints.map((point) => point.join(",")).join(";")}`);
  }

  els.map.classList.add("hidden");
  els.fallbackMap.classList.add("hidden");
  els.staticMap.classList.remove("hidden");
  els.staticMap.onerror = () => {
    els.staticMap.classList.add("hidden");
    drawFallback(data);
  };
  els.staticMap.src = `https://restapi.amap.com/v3/staticmap?${params.toString()}`;
  return true;
}

function drawMarker(svg, project, point, label, color) {
  if (!point) return;
  const [x, y] = project(point);
  svg.append(createSvg("circle", { cx: x, cy: y, r: 14, fill: color, stroke: "#ffffff", "stroke-width": 5 }));
  const text = createSvg("text", { x: x + 18, y: y - 18, fill: "#18201c", "font-size": 20, "font-weight": 700 });
  text.textContent = label;
  svg.append(text);
}

function createSvg(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  return node;
}

function renderResult(data) {
  els.distance.textContent = data.distance_km != null ? `${data.distance_km} km` : "--";
  els.eta.textContent = data.eta_minutes != null ? `${data.eta_minutes} 分钟` : "--";
  els.lights.textContent = data.traffic_lights != null ? `${data.traffic_lights} 个` : "--";
  els.road.textContent = data.road_status || "--";
  if (data.weather) {
    els.weather.textContent = data.weather;
    els.weatherMetric.classList.remove("hidden");
  } else {
    els.weather.textContent = "--";
    els.weatherMetric.classList.add("hidden");
  }
  if (data.narration) {
    const narrationLabel = data.narration_source === "llm" ? "AI 播报" : "本地兜底播报";
    const label = els.narrationBox.querySelector("span");
    if (label) label.textContent = narrationLabel;
    const debugReason = data.narration_source === "llm"
      ? ""
      : data.narration_error || data.dag_error || "";
    els.narration.textContent = debugReason ? `${data.narration}\n\n兜底原因：${debugReason}` : data.narration;
    els.narrationBox.classList.remove("hidden");
  } else {
    els.narration.textContent = "";
    els.narrationBox.classList.add("hidden");
  }
  els.steps.replaceChildren();
  if (data.reason) {
    const li = document.createElement("li");
    li.textContent = data.reason;
    els.steps.append(li);
    return;
  }
  (data.steps || []).forEach((step) => {
    const li = document.createElement("li");
    li.textContent = step || "继续行驶";
    els.steps.append(li);
  });
}

function renderClarification(clarification) {
  els.clarification.replaceChildren();
  els.clarification.classList.remove("hidden");

  const title = document.createElement("h2");
  title.textContent = clarification.prompt || "需要先确认地点";
  els.clarification.append(title);

  (clarification.pending_slots || []).forEach((slot) => {
    const list = document.createElement("div");
    list.className = "choice-list";
    slot.candidates.forEach((candidate, index) => {
      const button = document.createElement("button");
      button.className = "choice";
      button.type = "button";

      const name = document.createElement("strong");
      name.textContent = `${index + 1}. ${candidate.name || "未命名地点"}`;
      const detail = document.createElement("small");
      const distance = candidate.distance_m != null ? ` · ${candidate.distance_m} 米` : "";
      const rating = candidate.rating != null ? ` · ${Number(candidate.rating).toFixed(1)}分` : "";
      detail.textContent = `${candidate.address || "地址未知"}${distance}${rating}`;
      button.append(name, detail);

      button.addEventListener("click", () => {
        state.poiSelections[slot.slot_id] = index + 1;
        submitRoute();
      });
      list.append(button);
    });
    els.clarification.append(list);
  });

  const candidatePoints = [];
  (clarification.pending_slots || []).forEach((slot) => {
    slot.candidates.forEach((candidate, index) => {
      candidatePoints.push({ ...candidate, role: "candidate", candidate_index: index + 1 });
    });
  });
  renderMap({ origin: els.gps.value, pois: candidatePoints });
}

async function submitRoute() {
  const query = els.query.value.trim();
  const currentGps = els.gps.value.trim();
  if (!query) {
    setStatus("请输入需求", "warn");
    els.query.focus();
    return;
  }
  if (!normalizeCoord(currentGps)) {
    setStatus("经纬度无效", "error");
    els.gps.focus();
    return;
  }

  if (query !== state.lastQuery) {
    state.poiSelections = {};
    state.lastQuery = query;
  }

  els.submit.disabled = true;
  const oldButtonText = els.submit.textContent;
  els.submit.textContent = "思考中...";
  setStatus("正在思考");
  els.narrationBox.classList.remove("hidden");
  els.narration.textContent = "正在思考路线、检索 POI 和规划导航...";
  els.clarification.classList.add("hidden");
  try {
    const res = await fetch("/api/route/quick", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query,
        current_gps: currentGps,
        coord_system: state.coordSystem,
        poi_selections: state.poiSelections,
      }),
    });
    if (!res.ok) {
      const errorText = await res.text();
      throw new Error(`接口返回 ${res.status}: ${errorText.slice(0, 180)}`);
    }
    const data = await res.json();

    if (data?.status === "needs_clarification") {
      setStatus("待确认", "warn");
      renderClarification(data.clarification || {});
      return;
    }

    els.clarification.classList.add("hidden");
    if (!data || data.distance_km == null) {
      setStatus("规划失败", "error");
      renderResult(data || { reason: "服务未返回有效路线" });
      renderMap({ origin: currentGps });
      return;
    }

    setStatus("规划完成");
    renderResult(data);
    renderMap(data);
  } catch (error) {
    setStatus("请求失败", "error");
    els.narrationBox.classList.remove("hidden");
    const message = error.message || "";
    const hint = message.includes("Failed to fetch")
      ? "后端服务未连接，请确认 uvicorn 正在 127.0.0.1:6006 运行。"
      : message || "后端服务未连接，请确认 uvicorn 正在 127.0.0.1:6006 运行。";
    els.narration.textContent = `请求失败：${hint}`;
    console.error(error);
  } finally {
    els.submit.disabled = false;
    els.submit.textContent = oldButtonText;
  }
}

els.locate.addEventListener("click", locateUser);
els.submit.addEventListener("click", submitRoute);
els.demo.addEventListener("click", () => {
  els.query.value = "我要去附近麦当劳，途经烤肉店";
  if (!els.gps.value.trim()) els.gps.value = fallbackGps;
  setStatus("示例已填入");
});

els.query.addEventListener("focus", () => {
  if (!els.gps.value.trim()) locateUser();
});

els.gps.addEventListener("input", () => {
  state.coordSystem = "amap";
  setAddress("已手动修改经纬度，将按高德坐标使用");
});

loadAmap().finally(() => locateUser()).then(() => {
  if (!state.amapReady) renderMap({ origin: els.gps.value || fallbackGps });
});
