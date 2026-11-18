"use strict";

/* =====================================================================
   Оценка вариантов размещения школ — клиентская часть
   ===================================================================== */

const STORAGE_KEY = "school-placement:scenarios:v1";
const PROVISION_RAMP = ["#f7fbff", "#dbe9f6", "#b7d4ec", "#8cbade", "#5f9ccf", "#3b7fbe", "#2563a6", "#174c86", "#0b3567"];
const DIVERGING_LOW = "#d73027";
const DIVERGING_MID = "#f7f7f7";
const DIVERGING_HIGH = "#1a9850";

const state = {
  meta: null,
  baseline: null,
  baselineById: new Map(),
  scenarioById: null,
  schools: [],
  result: null,
  mode: "baseline",
  changeMetric: "provision",
  changeMax: 1,
  selected: null,
  auto: {
    jobId: null,
    running: false,
    capacity: 600,
    rows: [],
    bestIds: [],
    bestReduction: null,
    complete: false,
    errors: [],
    elapsed: 0,
    pendingThree: false,
  },
  comparison: null,
  changeRange: { provision: 1, deficit: 1 },
  blocksSort: { key: "deficit", dir: -1 },
  saved: [],
};

let map = null;
let geoLayer = null;
let markersLayer = null;
let tileLayer = null;
const blockLayers = new Map();
const centroids = new Map();

/* --------------------------------------------------------------- утилиты */

const numberFormat = new Intl.NumberFormat("ru-RU");

function fmtInt(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return numberFormat.format(Math.round(Number(value)));
}

function fmt1(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(Number(value));
}

function fmtPct(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return fmt1(Number(value) * 100) + " %";
}

function fmtSigned(value, digits = 1, unit = "") {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  const number = Number(value);
  const text = new Intl.NumberFormat("ru-RU", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(Math.abs(number));
  const sign = number > 0 ? "+" : number < 0 ? "−" : "±";
  return `${sign}${text}${unit}`;
}

function plural(count, forms) {
  const n = Math.abs(count) % 100;
  const n1 = n % 10;
  if (n > 10 && n < 20) return forms[2];
  if (n1 > 1 && n1 < 5) return forms[1];
  if (n1 === 1) return forms[0];
  return forms[2];
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch (error) {
    const badge = document.getElementById("dataCheck");
    if (badge) {
      badge.className = "badge badge-bad";
      badge.textContent = "Нет связи с сервером";
      badge.title = "Сервер расчётов не отвечает. Запустите run.bat и обновите страницу.";
    }
    throw new Error(
      "Нет связи с сервером расчётов. Убедитесь, что запущен run.bat и его окно не закрыто, затем обновите страницу.",
    );
  }
  let data = null;
  try {
    data = await response.json();
  } catch (error) {
    data = null;
  }
  if (!response.ok) {
    throw new Error((data && data.error) || `Ошибка запроса (${response.status})`);
  }
  const badge = document.getElementById("dataCheck");
  if (badge && badge.textContent === "Нет связи с сервером" && state.meta) {
    updateDataCheck(badge);
  }
  return data;
}

function post(path, payload) {
  return api(path, { method: "POST", body: JSON.stringify(payload) });
}

function toast(message, kind = "info", timeout = 5200) {
  const host = document.getElementById("toasts");
  if ([...host.children].some((element) => element.textContent === message)) {
    return;
  }
  while (host.children.length >= 4) {
    host.firstElementChild.remove();
  }
  const item = document.createElement("div");
  item.className = "toast" + (kind === "error" ? " error" : kind === "success" ? " success" : kind === "warn" ? " warn" : "");
  item.textContent = message;
  host.appendChild(item);
  setTimeout(() => item.remove(), timeout);
}

function readCapacity(input, label) {
  const raw = String(input.value ?? "").trim();
  if (!raw) throw new Error(`${label}: укажите целое положительное число мест.`);
  if (!/^\d+$/.test(raw)) throw new Error(`${label}: укажите целое положительное число мест (например, 600).`);
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value <= 0) throw new Error(`${label}: вместимость должна быть больше нуля.`);
  if (value > 100000) throw new Error(`${label}: слишком большое значение (максимум 100 000 мест).`);
  return value;
}

function hexToRgb(hex) {
  const value = hex.replace("#", "");
  return [parseInt(value.slice(0, 2), 16), parseInt(value.slice(2, 4), 16), parseInt(value.slice(4, 6), 16)];
}

function rgbToHex(rgb) {
  return "#" + rgb.map((value) => Math.round(value).toString(16).padStart(2, "0")).join("");
}

function mixColor(left, right, t) {
  const a = hexToRgb(left);
  const b = hexToRgb(right);
  const clamped = Math.max(0, Math.min(1, t));
  return rgbToHex(a.map((value, index) => value + (b[index] - value) * clamped));
}

function rampColor(palette, t) {
  const clamped = Math.max(0, Math.min(1, Number(t)));
  const position = clamped * (palette.length - 1);
  const index = Math.min(palette.length - 2, Math.floor(position));
  return mixColor(palette[index], palette[index + 1], position - index);
}

/* ------------------------------------------------------------- загрузка */

async function init() {
  const badge = document.getElementById("dataCheck");
  try {
    const [bootstrap, geometry] = await Promise.all([
      api("/api/bootstrap"),
      fetch("/api/geometry").then((response) => {
        if (!response.ok) throw new Error("Не удалось загрузить геометрию кварталов.");
        return response.json();
      }),
    ]);

    state.meta = bootstrap.meta;
    state.baseline = bootstrap.baseline.totals;
    state.baselineBlocks = bootstrap.baseline.blocks;
    state.baselineById = new Map(bootstrap.baseline.blocks.map((row) => [Number(row.block_id), row]));
    state.saved = loadSaved();

    fillBlockSelects();
    updateThreeControls();
    initMap(geometry);
    updateSummary();
    renderSchoolList();
    renderBlocksTable();
    renderSaved();
    renderAutoTable();
    renderCompareTable([]);
    renderAbout();
    updateDataCheck(badge);
    bindEvents();

    document.getElementById("changeMetric").classList.add("hidden");
  } catch (error) {
    badge.className = "badge badge-bad";
    badge.textContent = "Ошибка загрузки данных";
    toast("Не удалось загрузить данные приложения: " + error.message, "error", 12000);
    console.error(error);
  }
}

function updateDataCheck(badge) {
  const verification = state.meta && state.meta.verification;
  if (!verification) {
    badge.className = "badge badge-neutral";
    badge.textContent = "Проверка данных не выполнялась";
    return;
  }
  if (verification.ok) {
    badge.className = "badge badge-ok";
    badge.textContent = `Расчёт сверен с практикой 2 · Δ ${verification.max_abs_diff.toExponential(1)}`;
    badge.title = `Сверено кварталов: ${verification.compared_blocks}. Исходный расчёт воспроизводит сохранённые значения provision_school.`;
  } else {
    badge.className = "badge badge-bad";
    badge.textContent = "Расчёт не совпал с практикой 2";
    badge.title = JSON.stringify(verification);
  }
}

function fillBlockSelects() {
  const rows = [...state.baselineById.values()].sort((a, b) => a.block_id - b.block_id);
  const byPopulation = [...rows].sort((a, b) => (b.population || 0) - (a.population || 0));

  const options = (selectedId, { allowEmpty = false, emptyLabel = "— не сравнивать" } = {}) => {
    const empty = allowEmpty ? `<option value="">${emptyLabel}</option>` : "";
    return (
      empty +
      rows
        .map((row) => {
          const label = `Квартал ${row.block_id} — ${fmtInt(row.population)} чел.`;
          return `<option value="${row.block_id}"${Number(selectedId) === row.block_id ? " selected" : ""}>${label}</option>`;
        })
        .join("")
    );
  };

  const defaultId = byPopulation.length ? byPopulation[0].block_id : rows[0].block_id;

  document.getElementById("blockSelect").innerHTML = options(defaultId);
  document.getElementById("threeA").innerHTML = options(defaultId, { allowEmpty: true });
  const second = byPopulation[1] ? byPopulation[1].block_id : defaultId;
  const third = byPopulation[2] ? byPopulation[2].block_id : second;
  document.getElementById("threeB1").innerHTML = options(second, { allowEmpty: true });
  document.getElementById("threeB2").innerHTML = options(third, { allowEmpty: true });

  const autoSelect = document.getElementById("threeC");
  autoSelect.innerHTML =
    '<option value="">— не сравнивать (без автоподбора)</option>' + options(null);
  autoSelect.value = "";
}

/* ----------------------------------------------------------------- карта */

function initMap(geometry) {
  map = L.map("map", { preferCanvas: true, zoomControl: true, attributionControl: true });
  tileLayer = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "© OpenStreetMap",
  });
  tileLayer.addTo(map);

  markersLayer = L.layerGroup().addTo(map);

  geoLayer = L.geoJSON(geometry, {
    style: styleForFeature,
    onEachFeature: (feature, layer) => {
      const id = Number(feature.properties.block_id);
      blockLayers.set(id, layer);
      const bounds = layer.getBounds();
      centroids.set(id, bounds.getCenter());
      layer.on("click", () => selectBlock(id, true));
      layer.bindTooltip(tooltipFor(id), { sticky: true, direction: "top", opacity: 0.95 });
    },
  }).addTo(map);

  if (geometry.outline) {
    L.geoJSON(geometry.outline, {
      interactive: false,
      style: { color: "#1f2937", weight: 2, opacity: 0.85, fill: false },
    }).addTo(map);
  }

  map.fitBounds(geoLayer.getBounds(), { padding: [14, 14] });
  map.on("click", () => {
    if (state.selected !== null) {
      state.selected = null;
      document.getElementById("blockCard").classList.add("hidden");
      restyleMap();
    }
  });

  updateLegend();
}

function statsFor(id) {
  const base = state.baselineById.get(Number(id));
  const scenario = (state.scenarioById && state.scenarioById.get(Number(id))) || base;
  return { base, scenario };
}

function styleForFeature(feature) {
  const id = Number(feature.properties.block_id);
  const { base, scenario } = statsFor(id);
  let fill = "#d9dee5";

  if (state.mode === "baseline") {
    fill = base && base.demand > 0 && base.provision !== null ? rampColor(PROVISION_RAMP, base.provision) : "#d9dee5";
  } else if (state.mode === "scenario") {
    fill =
      scenario && scenario.demand > 0 && scenario.provision !== null
        ? rampColor(PROVISION_RAMP, scenario.provision)
        : "#d9dee5";
  } else if (state.mode === "change") {
    if (!state.result) {
      fill = "#e3e8ee";
    } else if (state.changeMetric === "provision") {
      const baseProvision = base && base.provision;
      const scenarioProvision = scenario && scenario.provision;
      if (baseProvision === null || scenarioProvision === null || baseProvision === undefined) {
        fill = "#d9dee5";
      } else {
        const delta = (scenarioProvision - baseProvision) * 100;
        fill = divergingColor(delta, state.changeRange.provision);
      }
    } else {
      if (!base || !scenario) {
        fill = "#d9dee5";
      } else {
        const delta = scenario.deficit - base.deficit;
        fill = divergingColor(-delta, state.changeRange.deficit);
      }
    }
  }

  return {
    color: "#4c5b6b",
    weight: state.selected === id ? 2.6 : 0.6,
    opacity: state.selected === id ? 1 : 0.55,
    fillColor: fill,
    fillOpacity: 0.86,
  };
}

function divergingColor(value, maxAbs) {
  const limit = Math.max(1e-9, maxAbs);
  const normalized = Math.max(-1, Math.min(1, value / limit));
  if (normalized >= 0) return mixColor(DIVERGING_MID, DIVERGING_HIGH, normalized);
  return mixColor(DIVERGING_MID, DIVERGING_LOW, -normalized);
}

function tooltipFor(id) {
  const { base, scenario } = statsFor(id);
  const target = state.mode === "baseline" || !state.result ? base : scenario;
  if (!target) return `Квартал ${id}`;
  const provision = target.provision === null || target.provision === undefined ? "нет населения" : fmtPct(target.provision);
  return `<strong>Квартал ${id}</strong><br>Население: ${fmtInt(target.population)} чел.<br>Обеспеченность: ${provision}<br>Дефицит: ${fmt1(target.deficit)} мест`;
}

function restyleMap() {
  if (!geoLayer) return;
  geoLayer.eachLayer((layer) => {
    const id = Number(layer.feature.properties.block_id);
    layer.setStyle(styleForFeature(layer.feature));
    if (layer.getTooltip()) {
      layer.setTooltipContent(tooltipFor(id));
    }
  });
  updateMarkerSizes();
  updateLegend();
}

function selectBlock(id, syncSelect = false, scroll = true) {
  state.selected = Number(id);
  if (syncSelect) {
    document.getElementById("blockSelect").value = String(id);
  }
  renderBlockCard();
  restyleMap();
  if (scroll) {
    document.getElementById("map").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function renderBlockCard() {
  const card = document.getElementById("blockCard");
  if (state.selected === null) {
    card.classList.add("hidden");
    return;
  }
  const id = state.selected;
  const { base, scenario } = statsFor(id);
  if (!base) {
    card.classList.add("hidden");
    return;
  }
  const hasScenario = Boolean(state.result);
  const provisionBase = base.provision === null ? "—" : fmtPct(base.provision);
  const provisionScenario = scenario && scenario.provision !== null ? fmtPct(scenario.provision) : "—";
  const capacityScenario = scenario ? scenario.capacity : base.capacity;
  const deficitScenario = scenario ? scenario.deficit : base.deficit;
  const delta = hasScenario ? deficitScenario - base.deficit : 0;
  const deltaClass = delta < -1e-9 ? "delta-up" : delta > 1e-9 ? "delta-down" : "";
  const deltaText = hasScenario ? fmtSigned(delta, 1, " мест") : "—";

  card.innerHTML = `
    <h3>Квартал ${id}<button class="close" type="button" aria-label="Закрыть">×</button></h3>
    <dl>
      <dt>Население</dt><dd>${fmtInt(base.population)} чел.</dd>
      <dt>Потребность в местах</dt><dd>${fmtInt(base.demand)}</dd>
      <dt>Вместимость школ${hasScenario ? " (база → сценарий)" : ""}</dt>
      <dd>${fmtInt(base.capacity)}${hasScenario && capacityScenario !== base.capacity ? " → " + fmtInt(capacityScenario) : ""}</dd>
      <dt>Обеспеченность${hasScenario ? " (база → сценарий)" : ""}</dt>
      <dd>${provisionBase}${hasScenario ? " → " + provisionScenario : ""}</dd>
      <dt>Дефицит мест${hasScenario ? " (база → сценарий)" : ""}</dt>
      <dd>${fmt1(base.deficit)}${hasScenario ? " → " + fmt1(deficitScenario) : ""}</dd>
      ${hasScenario ? `<dt>Изменение дефицита</dt><dd class="${deltaClass}">${deltaText}</dd>` : ""}
    </dl>`;
  card.classList.remove("hidden");
  const close = card.querySelector(".close");
  close.addEventListener("click", () => {
    state.selected = null;
    card.classList.add("hidden");
    restyleMap();
  });
}

function updateMarkerSizes() {
  if (!markersLayer) return;
  markersLayer.clearLayers();
  const schools = (state.result && state.result.schools) || state.schools;
  for (const school of schools) {
    const center = centroids.get(Number(school.block_id));
    if (!center) continue;
    const radius = Math.max(7, Math.min(23, Math.sqrt(school.capacity) / 2.1));
    L.circleMarker(center, {
      radius,
      color: "#8a2b0f",
      weight: 2,
      fillColor: "#f97316",
      fillOpacity: 0.85,
    })
      .bindTooltip(`Новая школа: ${fmtInt(school.capacity)} мест`, { direction: "top" })
      .addTo(markersLayer);
  }
}

function updateLegend() {
  const legend = document.getElementById("legend");
  let html = "";
  if (state.mode === "change") {
    if (!state.result) {
      html = `<div class="legend-title">Изменение</div><div class="legend-note">Сначала рассчитайте сценарий — карта покажет разницу с исходным состоянием.</div>`;
    } else if (state.changeMetric === "provision") {
      const max = state.changeRange.provision;
      html = `
        <div class="legend-title">Δ обеспеченности, п.п.</div>
        <div class="gradient" style="background:linear-gradient(to right, ${DIVERGING_LOW}, ${DIVERGING_MID}, ${DIVERGING_HIGH})"></div>
        <div class="scale"><span>−${fmt1(max)}</span><span>0</span><span>+${fmt1(max)}</span></div>
        <div class="legend-note">зелёный — обеспеченность выросла</div>`;
    } else {
      const max = state.changeRange.deficit;
      html = `
        <div class="legend-title">Δ дефицита, мест</div>
        <div class="gradient" style="background:linear-gradient(to right, ${DIVERGING_LOW}, ${DIVERGING_MID}, ${DIVERGING_HIGH})"></div>
        <div class="scale"><span>+${fmt1(max)}</span><span>0</span><span>−${fmt1(max)}</span></div>
        <div class="legend-note">зелёный — дефицит сократился</div>`;
    }
  } else {
    const suffix = state.mode === "scenario" ? " (сценарий)" : "";
    html = `
      <div class="legend-title">Обеспеченность местами в школах${suffix}</div>
      <div class="gradient" style="background:linear-gradient(to right, ${PROVISION_RAMP.join(",")})"></div>
      <div class="scale"><span>0 %</span><span>50 %</span><span>100 %</span></div>
      <div class="legend-note"><span class="swatch"></span> серый — нет населения</div>`;
  }
  legend.innerHTML = html;
}

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll("#mapModes button").forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === mode);
  });
  const metric = document.getElementById("changeMetric");
  metric.classList.toggle("hidden", mode !== "change");
  restyleMap();
}

function updateChangeRange() {
  if (!state.result) {
    state.changeRange = { provision: 1, deficit: 1 };
    return;
  }
  let maxProvision = 0;
  let maxDeficit = 0;
  for (const row of state.result.blocks) {
    if (row.provision !== null && row.provision_base !== null && row.provision_base !== undefined) {
      maxProvision = Math.max(maxProvision, Math.abs((row.provision - row.provision_base) * 100));
    }
    maxDeficit = Math.max(maxDeficit, Math.abs(row.deficit - row.deficit_base));
  }
  state.changeRange = {
    provision: Math.max(1, Math.ceil(maxProvision / 5) * 5),
    deficit: Math.max(1, Math.ceil(maxDeficit / 50) * 50),
  };
}

/* ------------------------------------------------------------- сценарии */

function renderSchoolList() {
  const list = document.getElementById("schoolList");
  list.innerHTML = "";
  state.schools.forEach((school, index) => {
    const item = document.createElement("li");
    item.className = "school-item";
    item.innerHTML = `
      <span class="name">Квартал ${school.block_id}</span>
      <span class="meta">${fmtInt(school.capacity)} мест</span>
      <button class="icon-btn" type="button" title="Удалить школу" aria-label="Удалить школу">×</button>`;
    item.querySelector("button").addEventListener("click", () => {
      state.schools.splice(index, 1);
      renderSchoolList();
      if (state.schools.length) {
        calculateScenario();
      } else {
        resetScenario(false);
      }
    });
    list.appendChild(item);
  });
}

function addSchool() {
  try {
    const blockId = Number(document.getElementById("blockSelect").value);
    const capacity = readCapacity(document.getElementById("capacityInput"), "Вместимость школы");
    if (!Number.isInteger(blockId) || !state.baselineById.has(blockId)) {
      throw new Error("Выберите квартал из списка.");
    }
    const existing = state.schools.find((school) => school.block_id === blockId);
    if (existing) {
      existing.capacity += capacity;
    } else {
      state.schools.push({ block_id: blockId, capacity });
    }
    renderSchoolList();
    calculateScenario();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function calculateScenario({ silent = false } = {}) {
  if (!state.schools.length) {
    resetScenario(false);
    return;
  }
  try {
    const result = await post("/api/scenario", { schools: state.schools });
    state.result = result;
    state.scenarioById = new Map(result.blocks.map((row) => [Number(row.block_id), row]));
    updateChangeRange();
    if (state.mode === "baseline") setMode("scenario");
    updateSummary();
    restyleMap();
    renderBlocksTable();
    renderBlockCard();
    if (!silent) {
      toast(
        `Сценарий рассчитан: добавлено ${fmtInt(result.capacity_added)} мест, дефицит сократился на ${fmt1(result.totals.deficit_reduction)} мест.`,
        "success",
      );
    }
  } catch (error) {
    toast("Ошибка расчёта сценария: " + error.message, "error", 9000);
  }
}

function resetScenario(showToast = true) {
  state.schools = [];
  state.result = null;
  state.scenarioById = null;
  updateChangeRange();
  setMode("baseline");
  renderSchoolList();
  updateSummary();
  renderBlocksTable();
  updateMarkerSizes();
  renderBlockCard();
  renderAutoTable();
  if (showToast) toast("Сценарий сброшен, показано исходное состояние.", "info");
}

function updateSummary() {
  const totals = state.result ? state.result.totals : state.baseline;
  const base = state.baseline;
  if (!totals || !base) return;

  document.getElementById("kpiCapacity").textContent = fmtInt(totals.capacity_total);
  document.getElementById("kpiCapacitySub").textContent = state.result
    ? `база ${fmtInt(base.capacity_total)} · +${fmtInt(totals.capacity_added)}`
    : "мест в существующих школах";

  document.getElementById("kpiDeficit").textContent = fmt1(totals.deficit_total);
  document.getElementById("kpiDeficitSub").textContent = state.result
    ? `сокращение на ${fmt1(totals.deficit_reduction)} мест`
    : "не закрыто в нормативе 15 мин";

  document.getElementById("kpiProvision").textContent = fmtPct(totals.provision_total);
  document.getElementById("kpiProvisionSub").textContent = state.result
    ? `база ${fmtPct(base.provision_total)} · ${fmtSigned(totals.provision_delta_pp, 1, " п.п.")}`
    : "доля потребности в нормативе 15 мин";

  document.getElementById("kpiAdded").textContent = state.result ? "+" + fmtInt(totals.capacity_added) : "0";
  document.getElementById("kpiAddedSub").textContent = state.result
    ? "мест добавлено в сценарии"
    : "мест добавлено";

  const pill = document.getElementById("scenarioPill");
  const hint = document.getElementById("summaryHint");
  if (state.result) {
    pill.textContent = `Сценарий · ${state.result.schools.length} ${plural(state.result.schools.length, ["школа", "школы", "школ"])}`;
    pill.classList.add("scenario");
    const percent = base.deficit_total > 0 ? (totals.deficit_reduction / base.deficit_total) * 100 : 0;
    hint.textContent = `Дефицит сократился на ${fmt1(totals.deficit_reduction)} мест (${fmt1(percent)} % исходного). Обеспеченность выросла с ${fmtPct(base.provision_total)} до ${fmtPct(totals.provision_total)}.`;
  } else {
    pill.textContent = "Исходное состояние";
    pill.classList.remove("scenario");
    hint.textContent = "Задайте школы или выполните автоматический подбор. Показатели относятся ко всей территории района.";
  }
}

/* --------------------------------------------------------- авто-подбор */

async function startAuto({ forThree = false } = {}) {
  if (state.auto.running) {
    if (forThree) {
      state.auto.pendingThree = true;
      toast("Перебор уже идёт — результат будет подставлен в вариант 3.", "info");
    }
    return;
  }
  let capacity;
  try {
    capacity = readCapacity(document.getElementById("autoCapacity"), "Вместимость новой школы");
  } catch (error) {
    toast(error.message, "error");
    return;
  }

  const button = document.getElementById("btnAuto");
  button.disabled = true;
  state.auto = {
    ...state.auto,
    running: true,
    capacity,
    rows: [],
    bestIds: [],
    bestReduction: null,
    complete: false,
    errors: [],
    elapsed: 0,
    checked: 0,
    total: state.baseline?.blocks_total || 0,
    pendingThree: forThree,
  };
  document.getElementById("autoProgress").classList.remove("hidden");
  document.getElementById("autoBar").style.width = "0%";
  document.getElementById("autoProgressText").textContent = `Проверено 0 из ${
    state.baseline.blocks_total || "…"
  } кварталов территории`;
  document.getElementById("autoSummary").innerHTML = '<span class="muted">Идёт перебор кварталов…</span>';
  document.getElementById("autoTop").innerHTML = "";
  renderAutoTable();

  try {
    const started = await post("/api/auto", { capacity });
    state.auto.jobId = started.job_id;
    pollAuto();
  } catch (error) {
    state.auto.running = false;
    button.disabled = false;
    document.getElementById("autoProgress").classList.add("hidden");
    toast(error.message, "error", 8000);
  }
}

async function pollAuto() {
  const jobId = state.auto.jobId;
  if (!jobId) return;
  try {
    const status = await api(`/api/auto/${jobId}`);
    if (jobId !== state.auto.jobId) return;

    const percent = status.total ? Math.round((status.checked / status.total) * 100) : 0;
    document.getElementById("autoBar").style.width = percent + "%";
    const remaining =
      status.checked > 0 && status.state === "running"
        ? Math.max(0, Math.round((status.elapsed / status.checked) * (status.total - status.checked)))
        : null;
    document.getElementById("autoProgressText").textContent =
      `Проверено ${status.checked} из ${status.total} кварталов территории (${percent} %)` +
      (remaining !== null ? ` · осталось ≈ ${remaining} с` : "");

    if (status.state === "running") {
      if (status.best_ids && status.best_ids.length) {
        const count = status.best_ids.length;
        const first = status.best_ids[0];
        document.getElementById("autoSummary").innerHTML =
          count === 1
            ? `<span class="ok">Рекомендованный на данный момент:</span> квартал ${first} — сокращение дефицита по району ${fmt1(status.best_reduction)} мест.`
            : `<span class="ok">Рекомендованных на данный момент: ${count}.</span> Например, квартал ${first} — сокращение дефицита по району ${fmt1(status.best_reduction)} мест.`;
      }
      setTimeout(pollAuto, 700);
      return;
    }

    state.auto.running = false;
    document.getElementById("btnAuto").disabled = false;
    document.getElementById("autoProgress").classList.add("hidden");

    if (status.state === "error") {
      document.getElementById("autoSummary").innerHTML = `<span class="bad">Ошибка автоматического подбора:</span> ${escapeHtml(
        status.error || "неизвестная ошибка",
      )}`;
      toast("Автоматический подбор завершился ошибкой: " + (status.error || "неизвестная ошибка"), "error", 9000);
      return;
    }

    state.auto.rows = status.rows || [];
    state.auto.bestIds = status.best_ids || [];
    state.auto.bestReduction = status.best_reduction;
    state.auto.complete = Boolean(status.complete);
    state.auto.errors = status.errors || [];
    state.auto.elapsed = status.elapsed;
    state.auto.checked = status.checked;
    state.auto.total = status.total;

    renderAutoSummary();
    renderAutoTop();
    renderAutoTable();

    if (!state.auto.pendingThree && state.auto.bestIds.length) {
      const bestId = state.auto.bestIds[0];
      selectBlock(bestId, false, false);
      toast(
        `Рекомендованный квартал ${bestId} подсвечен на карте. Нажмите строку таблицы, чтобы рассчитать этот вариант.`,
        "info",
        9000,
      );
    }

    if (state.auto.pendingThree && state.auto.bestIds.length) {
      const select = document.getElementById("threeC");
      select.value = String(state.auto.bestIds[0]);
      updateThreeControls();
      if (state.auto.bestIds.length > 1) {
        toast(
          `Найдено ${state.auto.bestIds.length} рекомендованных кварталов. В вариант 3 подставлен квартал ${state.auto.bestIds[0]} — выберите другой в списке при необходимости.`,
          "info",
          8000,
        );
      } else {
        toast(`Рекомендованный квартал ${state.auto.bestIds[0]} подставлен в вариант 3.`, "success");
      }
      state.auto.pendingThree = false;
    }
  } catch (error) {
    state.auto.running = false;
    document.getElementById("btnAuto").disabled = false;
    document.getElementById("autoProgress").classList.add("hidden");
    toast("Не удалось получить ход расчёта: " + error.message, "error", 9000);
  }
}

function renderAutoSummary() {
  const summary = document.getElementById("autoSummary");
  if (!state.auto.rows.length) {
    summary.innerHTML = '<span class="warn">Перебор не дал результатов.</span>';
    return;
  }
  const rows = state.auto.rows;
  const checked = state.auto.checked || rows.length;
  const total = state.auto.total || checked;
  const bestCount = state.auto.bestIds.length;
  const bestSet = new Set(state.auto.bestIds);
  const bestRows = rows.filter((row) => bestSet.has(row.block_id));
  const firstBest = bestRows[0];
  const eligibleRows = rows.filter((row) => row.eligible);
  const eligibleCount = eligibleRows.length;
  const ineligibleCount = total - eligibleCount;
  const extraCount = eligibleCount - bestCount;
  const sameReductionCount = eligibleRows.filter(
    (row) => Math.abs(row.deficit_reduction - state.auto.bestReduction) <= 1e-6,
  ).length;
  let html = "";

  if (!state.auto.complete) {
    html += `<span class="warn">Перебор неполный: проверено ${checked} из ${total} кварталов территории.</span> `;
    html +=
      "Показан лучший результат только среди проверенных кварталов — он не является лучшим среди всех кварталов территории.";
  } else {
    html += `Проверены все ${total} кварталов территории. `;
    if (!eligibleCount) {
      html += '<span class="warn">Кварталов с населением и дефицитом нет — рекомендация не сформирована.</span>';
    } else if (bestCount === 1) {
      html += `<span class="ok">Рекомендованный квартал: ${firstBest.block_id}.</span> `;
      html += `Школа нужна в ${eligibleCount} кварталах с населением и дефицитом. `;
      html += `Дефицит в рекомендованном квартале: ${fmt1(firstBest.deficit_before)} мест до сценария и ${fmt1(
        firstBest.deficit_after_block,
      )} после. `;
      html += `Сокращение дефицита по району: ${fmt1(state.auto.bestReduction)} мест.`;
      if (sameReductionCount > 1) {
        const other = sameReductionCount - 1;
        html += ` Такое же сокращение по району ${other === 1 ? "даёт" : "дают"} ещё ${other} ${plural(other, [
          "квартал",
          "квартала",
          "кварталов",
        ])}; рекомендован квартал с наибольшим дефицитом мест.`;
      }
      if (ineligibleCount > 0) {
        html += ` Кварталы без населения или без дефицита (${ineligibleCount}) проверены, но не рекомендуются.`;
      }
    } else if (bestCount === eligibleCount) {
      html += `<span class="ok">Все ${eligibleCount} кварталов с населением и дефицитом рекомендованы.</span> `;
      html += `У них одинаковое сокращение по району (${fmt1(
        state.auto.bestReduction,
      )} мест) и одинаковый дефицит в квартале (${fmt1(firstBest.deficit_before)} мест).`;
    } else {
      html += `<span class="ok">Рекомендовано ${bestCount} ${plural(bestCount, [
        "квартал",
        "квартала",
        "кварталов",
      ])} из ${eligibleCount} кварталов, где нужна школа.</span> `;
      html += `У них одинаковое сокращение по району (${fmt1(
        state.auto.bestReduction,
      )} мест) и наибольший дефицит в квартале (${fmt1(firstBest.deficit_before)} мест). `;
      if (extraCount > 0) {
        html += `Остальные ${extraCount} ${plural(extraCount, [
          "квартал",
          "квартала",
          "кварталов",
        ])} дают меньшее сокращение по району или меньший дефицит в самом квартале.`;
      }
    }
  }

  if (state.auto.complete && bestCount > 0) {
    const mark = bestCount === 1 ? "★ — рекомендованный квартал." : "★ — рекомендованные кварталы.";
    const onlyNeeded = document.getElementById("autoOnlyNeeded")?.checked;
    const tableNote = onlyNeeded
      ? `В таблице показаны ${eligibleCount} кварталов, где нужна школа; снимите фильтр, чтобы увидеть все ${total} проверенных.`
      : `В таблице показаны все ${total} проверенных кварталов.`;
    html += `<div class="hint" style="margin-top:4px">${mark} ${tableNote} Сначала сравнивается сокращение дефицита по району, при равенстве — дефицит мест в самом квартале.</div>`;
  }

  if (state.auto.errors.length) {
    const errors = state.auto.errors
      .slice(0, 5)
      .map((item) => `квартал ${item.block_id}: ${escapeHtml(item.error)}`)
      .join("; ");
    html += `<div class="bad" style="margin-top:6px">Ошибки расчёта (${state.auto.errors.length}): ${errors}${
      state.auto.errors.length > 5 ? "…" : ""
    }</div>`;
  }
  summary.innerHTML = html;
}

function renderAutoTop() {
  const host = document.getElementById("autoTop");
  if (!state.auto.rows.length) {
    host.innerHTML = "";
    return;
  }
  const rows = state.auto.rows.filter((row) => row.eligible).slice(0, 5);
  const bestSet = new Set(state.auto.bestIds);
  const body = rows
    .map((row) => {
      const isBest = bestSet.has(row.block_id);
      return `<tr class="${isBest ? "best" : ""}">
        <td>${row.block_id}${isBest ? " ★" : ""}</td>
        <td class="num">${fmtInt(row.population)}</td>
        <td class="num">${fmt1(row.deficit_before)}</td>
        <td class="num">${fmt1(row.deficit_after_block)}</td>
        <td class="num">${fmt1(row.deficit_reduction)}</td>
      </tr>`;
    })
    .join("");
  host.innerHTML = `<table>
      <thead><tr><th>Топ-5 вариантов</th><th class="num">Население</th><th class="num">Дефицит квартала<br>до, мест</th><th class="num">Дефицит квартала<br>после, мест</th><th class="num">Сокращение<br>по району, мест</th></tr></thead>
      <tbody>${body}</tbody>
    </table>`;
}

function renderAutoTable() {
  const tbody = document.querySelector("#autoTable tbody");
  const hint = document.getElementById("autoTableHint");
  const allRows = state.auto.rows;
  if (!allRows.length) {
    tbody.innerHTML = "";
    hint.textContent = state.auto.running
      ? "Идёт перебор кварталов — таблица заполнится после завершения."
      : "Автоматический подбор ещё не выполнялся.";
    return;
  }
  const onlyNeeded = document.getElementById("autoOnlyNeeded")?.checked !== false;
  const eligibleCount = allRows.filter((row) => row.eligible).length;
  const rows = onlyNeeded ? allRows.filter((row) => row.eligible) : allRows;
  const bestSet = new Set(state.auto.bestIds);
  tbody.innerHTML = rows
    .map((row, index) => {
      const isBest = bestSet.has(row.block_id);
      const status = row.eligible
        ? ""
        : row.population > 0
          ? " · нет дефицита"
          : " · нет населения";
      const classes = [isBest ? "best" : "", row.eligible ? "" : "muted"].filter(Boolean).join(" ");
      return `<tr data-block="${row.block_id}" class="${classes}" title="Показать вариант на карте">
        <td class="num">${index + 1}</td>
        <td>${row.block_id}${isBest ? " ★" : ""}${status}</td>
        <td class="num">${fmtInt(row.population)}</td>
        <td class="num">${fmtInt(row.capacity_after)}</td>
        <td class="num">${fmt1(row.deficit_before)}</td>
        <td class="num">${fmt1(row.deficit_after_block)}</td>
        <td class="num">${fmt1(row.deficit_after)}</td>
        <td class="num pos">${fmt1(row.deficit_reduction)}</td>
        <td class="num">${fmtPct(row.provision_after)}</td>
      </tr>`;
    })
    .join("");

  const checked = state.auto.checked || allRows.length;
  const total = state.auto.total || checked;
  const recommended =
    state.auto.bestIds.length === 1 ? "★ — рекомендованный квартал." : "★ — рекомендованные кварталы.";
  const shown = onlyNeeded
    ? `Показаны ${eligibleCount} кварталов, где нужна школа`
    : `Показаны все ${total} проверенных кварталов`;
  hint.textContent = state.auto.complete
    ? `Проверены все ${total} кварталов территории. ${shown}. ${recommended} Нажмите на строку, чтобы увидеть вариант на карте.`
    : `Перебор неполный: проверено ${checked} из ${total} кварталов. ${shown}.`;

  tbody.querySelectorAll("tr").forEach((row) => {
    row.addEventListener("click", () => {
      const blockId = Number(row.dataset.block);
      state.schools = [{ block_id: blockId, capacity: state.auto.capacity }];
      renderSchoolList();
      document.getElementById("blockSelect").value = String(blockId);
      calculateScenario({ silent: true }).then(() => {
        setMode("scenario");
        selectBlock(blockId);
        toast(`Показан вариант: квартал ${blockId}, ${fmtInt(state.auto.capacity)} мест.`, "info");
      });
    });
  });
}

/* -------------------------------------------------------------- таблицы */

function baselineTableRows() {
  return state.baselineBlocks.map((row) => ({
    ...row,
    capacity_base: row.capacity,
    provision_base: row.provision,
    deficit_base: row.deficit,
    deficit_delta: 0,
    capacity_added: 0,
  }));
}

function currentTableRows() {
  return state.result ? state.result.blocks : baselineTableRows();
}

function renderBlocksTable() {
  const tbody = document.querySelector("#blocksTable tbody");
  const hint = document.getElementById("blocksTableHint");
  const filter = (document.getElementById("blockFilter").value || "").trim();
  let rows = currentTableRows();

  if (filter) {
    rows = rows.filter((row) => String(row.block_id).includes(filter));
  }
  const { key, dir } = state.blocksSort;
  rows = rows.slice().sort((a, b) => {
    const left = a[key];
    const right = b[key];
    if (left === null || left === undefined) return 1;
    if (right === null || right === undefined) return -1;
    if (typeof left === "string") return left.localeCompare(right) * dir;
    return (left - right) * dir;
  });

  tbody.innerHTML = rows
    .map((row) => {
      const deltaClass = row.deficit_delta < -1e-9 ? "pos" : row.deficit_delta > 1e-9 ? "neg" : "muted";
      return `<tr data-block="${row.block_id}" class="${state.selected === row.block_id ? "selected" : ""}">
        <td>${row.block_id}</td>
        <td class="num">${fmtInt(row.population)}</td>
        <td class="num">${fmtInt(row.capacity_base)}</td>
        <td class="num">${fmtInt(row.capacity)}</td>
        <td class="num">${fmtInt(row.demand)}</td>
        <td class="num">${row.provision_base === null ? "—" : fmt1(row.provision_base * 100)}</td>
        <td class="num">${row.provision === null ? "—" : fmt1(row.provision * 100)}</td>
        <td class="num">${fmt1(row.deficit_base)}</td>
        <td class="num">${fmt1(row.deficit)}</td>
        <td class="num ${deltaClass}">${state.result ? fmtSigned(row.deficit_delta, 1) : "0,0"}</td>
      </tr>`;
    })
    .join("");

  hint.textContent = state.result
    ? `Сценарий: ${state.result.schools.length} ${plural(state.result.schools.length, ["школа", "школы", "школ"])}, добавлено ${fmtInt(state.result.capacity_added)} мест.`
    : "Показано исходное состояние. Рассчитайте сценарий, чтобы увидеть изменения.";

  tbody.querySelectorAll("tr").forEach((row) => {
    row.addEventListener("click", () => {
      selectBlock(Number(row.dataset.block));
    });
  });
}

function renderCompareTable(rows, hintText) {
  const tbody = document.querySelector("#compareTable tbody");
  const hint = document.getElementById("compareTableHint");
  if (!rows || !rows.length) {
    tbody.innerHTML = "";
    if (hintText) hint.textContent = hintText;
    return;
  }
  const valid = rows.filter((row) => !row.error && row.deficit_reduction !== null);
  const best = valid.length ? Math.max(...valid.map((row) => row.deficit_reduction)) : null;
  const baseDeficit = state.baseline ? state.baseline.deficit_total : null;
  const baseProvision = state.baseline ? state.baseline.provision_total * 100 : null;
  tbody.innerHTML = rows
    .map((row) => {
      if (row.error) {
        return `<tr class="error"><td>${escapeHtml(row.name)}</td><td colspan="6">Ошибка: ${escapeHtml(row.error)}</td></tr>`;
      }
      const isBest = best !== null && Math.abs(row.deficit_reduction - best) < 1e-6;
      const schools = row.schools
        .map((school) => `<span class="school-tag">кв. ${school.block_id} · ${fmtInt(school.capacity)}</span>`)
        .join("");
      const provisionAfter = row.provision_total * 100;
      const provisionDelta = baseProvision === null ? null : provisionAfter - baseProvision;
      const local = row.local;
      const localDeficit =
        local && local.deficit_before !== null
          ? `${fmt1(local.deficit_before)} → ${fmt1(local.deficit_after)}`
          : "—";
      const localDeficitDelta = local ? `−${fmt1(local.deficit_reduction)}` : "—";
      const localProvision =
        local && local.provision_before !== null && local.provision_after !== null
          ? `${fmt1(local.provision_before)} → ${fmt1(local.provision_after)} %`
          : "—";
      const localProvisionDelta =
        local && local.provision_delta_pp !== null ? fmtSigned(local.provision_delta_pp, 1, " п.п.") : "—";
      const affected =
        local ? `${fmtInt(local.affected_blocks)} кв. · ${fmtInt(local.affected_population)} чел.` : "—";
      const blockRows = (local?.blocks || [])
        .map((block) => {
          const blockProvision =
            block.provision_before === null || block.provision_after === null
              ? "—"
              : `${fmt1(block.provision_before)} → ${fmt1(block.provision_after)} %`;
          const blockProvisionDelta =
            block.provision_delta_pp === null ? "—" : fmtSigned(block.provision_delta_pp, 1, " п.п.");
          return `<tr>
            <td>кв. ${block.block_id}</td>
            <td class="num">${fmtInt(block.population)}</td>
            <td class="num">${fmtInt(block.capacity_base)} → ${fmtInt(block.capacity_after)}</td>
            <td class="num">${fmt1(block.deficit_before)} → ${fmt1(block.deficit_after)}</td>
            <td class="num pos">−${fmt1(block.deficit_reduction)}</td>
            <td class="num">${blockProvision}</td>
            <td class="num">${blockProvisionDelta}</td>
          </tr>`;
        })
        .join("");
      return `<tr class="${isBest ? "best" : ""}">
        <td>${escapeHtml(row.name)}${isBest ? " ★" : ""}</td>
        <td>${schools || "—"}</td>
        <td class="num">${fmtInt(row.capacity_added)}</td>
        <td class="num">${fmtInt(row.capacity_total)}</td>
        <td class="num compare-range">${baseDeficit === null ? "—" : fmt1(baseDeficit)} → ${fmt1(row.deficit_total)}</td>
        <td class="num pos">${fmt1(row.deficit_reduction)}</td>
        <td class="num compare-range">${baseProvision === null ? "—" : fmt1(baseProvision)} → ${fmt1(provisionAfter)}
          <small class="${provisionDelta !== null && provisionDelta > 0 ? "pos" : ""}">${provisionDelta === null ? "" : fmtSigned(provisionDelta, 1, " п.п.")}</small>
        </td>
      </tr>
      <tr class="compare-effect ${isBest ? "best" : ""}">
        <td colspan="7">
          <div class="compare-effect-grid">
            <span><b>В кварталах школ — дефицит:</b> <strong>${localDeficit}</strong> мест
              <span class="pos">${localDeficitDelta}</span></span>
            <span><b>Обеспеченность:</b> <strong>${localProvision}</strong>
              <span class="${local && local.provision_delta_pp > 0 ? "pos" : ""}">${localProvisionDelta}</span></span>
            <span><b>Затронуто:</b> <strong>${affected}</strong></span>
            <span class="muted">Локальный эффект — только в кварталах размещения школ.</span>
          </div>
          ${blockRows ? `<details class="compare-blocks">
            <summary>Показать по кварталам размещения</summary>
            <table class="compare-mini">
              <thead><tr><th>Квартал</th><th class="num">Население</th><th class="num">Вместимость, мест</th>
                <th class="num">Дефицит, мест</th><th class="num">Сокращение</th>
                <th class="num">Обеспеченность, %</th><th class="num">Δ, п.п.</th></tr></thead>
              <tbody>${blockRows}</tbody>
            </table>
          </details>` : ""}
        </td>
      </tr>`;
    })
    .join("");
  if (hintText) {
    hint.textContent = `${hintText} ★ — максимальное сокращение по району; при равенстве смотрите локальные показатели.`;
  }
}

async function runComparison(scenarios, hintText) {
  const response = await post("/api/compare", { scenarios });
  state.comparison = response.rows;
  renderCompareTable(response.rows, hintText);
  return response.rows;
}

/* ------------------------------------------------------ три варианта */

function threeSelectedCount() {
  const a = document.getElementById("threeA").value;
  const b1 = document.getElementById("threeB1").value;
  const b2 = document.getElementById("threeB2").value;
  const c = document.getElementById("threeC").value;
  return (a ? 1 : 0) + (b1 && b2 ? 1 : 0) + (c ? 1 : 0);
}

function updateThreeControls() {
  const button = document.getElementById("btnThreeCompare");
  if (!button) return;
  const count = threeSelectedCount();
  button.textContent =
    count === 3 ? "Сравнить три варианта" : count === 2 ? "Сравнить два варианта" : "Сравнить варианты";
  button.title =
    count < 2 ? "Выберите не меньше двух вариантов. В ненужном варианте выберите «— не сравнивать»." : "";
}

function pickAnotherBlock(currentId) {
  const current = Number(currentId);
  const candidate = [...state.baselineById.keys()].sort((a, b) => a - b).find((id) => id !== current);
  return candidate === undefined ? "" : String(candidate);
}

function syncThreeBPair(changedId) {
  const b1 = document.getElementById("threeB1");
  const b2 = document.getElementById("threeB2");
  const changed = document.getElementById(changedId);
  if (!changed.value) {
    b1.value = "";
    b2.value = "";
  } else {
    const other = changedId === "threeB1" ? b2 : b1;
    if (!other.value || other.value === changed.value) {
      other.value = pickAnotherBlock(changed.value);
    }
  }
  updateThreeControls();
}

function pickAutoForThree() {
  if (state.auto.rows.length && state.auto.bestIds.length) {
    const select = document.getElementById("threeC");
    select.value = String(state.auto.bestIds[0]);
    updateThreeControls();
    if (state.auto.bestIds.length > 1) {
      toast(
        `Найдено ${state.auto.bestIds.length} равноценных кварталов. Подставлен квартал ${state.auto.bestIds[0]} — при необходимости выберите другой.`,
        "info",
        7000,
      );
    } else {
      toast(`Подставлен квартал ${state.auto.bestIds[0]} из автоматического подбора.`, "success");
    }
    return;
  }
  startAuto({ forThree: true });
}

async function compareThree() {
  const a = document.getElementById("threeA").value;
  const b1 = document.getElementById("threeB1").value;
  const b2 = document.getElementById("threeB2").value;
  const c = document.getElementById("threeC").value;
  const scenarios = [];

  if (a) {
    scenarios.push({
      name: `Одна школа 600 мест — квартал ${a}`,
      schools: [{ block_id: Number(a), capacity: 600 }],
    });
  }

  if (b1 || b2) {
    if (!b1 || !b2) {
      toast("Для варианта 2 выберите два квартала или поставьте «— не сравнивать».", "warn");
      return;
    }
    if (b1 === b2) {
      toast("Для варианта 2 выберите два разных квартала.", "error");
      return;
    }
    scenarios.push({
      name: `Две школы по 300 мест — кварталы ${b1} и ${b2}`,
      schools: [
        { block_id: Number(b1), capacity: 300 },
        { block_id: Number(b2), capacity: 300 },
      ],
    });
  }

  if (c) {
    scenarios.push({
      name: `Школа 600 мест — автоподбор, квартал ${c}`,
      schools: [{ block_id: Number(c), capacity: 600 }],
    });
  }

  if (scenarios.length < 2) {
    toast(
      "Для сравнения выберите минимум два варианта. В ненужном варианте выберите «— не сравнивать».",
      "warn",
    );
    return;
  }

  try {
    const hintText =
      scenarios.length === 2
        ? "Сравнение двух вариантов. Верхняя строка — итоги по району, ниже — локальный эффект в кварталах размещения."
        : "Сравнение трёх вариантов (600 / 2×300 / автоподбор). Верхняя строка — итоги по району, ниже — локальный эффект в кварталах размещения.";
    const rows = await runComparison(scenarios, hintText);
    renderThreeInline(rows);
    switchTab("compare");
  } catch (error) {
    toast("Не удалось сравнить варианты: " + error.message, "error");
  }
}

function renderThreeInline(rows) {
  const host = document.getElementById("threeResult");
  const valid = rows.filter((row) => !row.error);
  const best = valid.length ? Math.max(...valid.map((row) => row.deficit_reduction)) : null;
  host.innerHTML = `<table><thead><tr><th>Вариант</th><th class="num">Сокращение по району</th>
    <th>В кварталах школ</th></tr></thead><tbody>
    ${rows
      .map((row) => {
        if (row.error) return `<tr class="error"><td>${escapeHtml(row.name)}</td><td colspan="2">Ошибка</td></tr>`;
        const isBest = best !== null && Math.abs(row.deficit_reduction - best) < 1e-6;
        const local = row.local;
        const localText =
          local && local.provision_before !== null
            ? `дефицит ${fmt1(local.deficit_before)} → ${fmt1(local.deficit_after)}<br>
               обеспеченность ${fmt1(local.provision_before)} → ${fmt1(local.provision_after)} %`
            : "—";
        return `<tr class="${isBest ? "best" : ""}"><td>${escapeHtml(row.name)}${isBest ? " ★" : ""}</td>
          <td class="num">${fmt1(row.deficit_reduction)}</td><td>${localText}</td></tr>`;
      })
      .join("")}
  </tbody></table>`;
}

/* -------------------------------------------------- сохранённые сценарии */

function loadSaved() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch (error) {
    return [];
  }
}

function persistSaved() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state.saved));
  } catch (error) {
    toast("Не удалось сохранить сценарий в браузере: " + error.message, "error");
  }
}

function saveCurrentScenario() {
  const nameInput = document.getElementById("saveName");
  const name = (nameInput.value || "").trim() || `Сценарий ${state.saved.length + 1}`;
  if (!state.schools.length) {
    toast("Сценарий пуст: сначала добавьте хотя бы одну школу.", "warn");
    return;
  }
  state.saved.push({
    id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
    name: name.slice(0, 80),
    schools: state.schools.map((school) => ({ ...school })),
    createdAt: new Date().toISOString(),
  });
  nameInput.value = "";
  persistSaved();
  renderSaved();
  toast("Сценарий сохранён.", "success");
}

function renderSaved() {
  const list = document.getElementById("savedList");
  list.innerHTML = "";
  for (const item of state.saved) {
    const capacity = item.schools.reduce((sum, school) => sum + school.capacity, 0);
    const element = document.createElement("li");
    element.className = "saved-item";
    element.innerHTML = `
      <div>
        <div class="saved-name">${escapeHtml(item.name)}</div>
        <div class="saved-meta">${item.schools.length} ${plural(item.schools.length, ["школа", "школы", "школ"])} · ${fmtInt(capacity)} мест</div>
      </div>
      <div class="saved-actions">
        <button class="btn btn-ghost" data-action="show" type="button">Показать</button>
        <button class="btn btn-ghost" data-action="delete" type="button">Удалить</button>
      </div>`;
    element.querySelector('[data-action="show"]').addEventListener("click", () => {
      state.schools = item.schools.map((school) => ({ ...school }));
      renderSchoolList();
      calculateScenario({ silent: true }).then(() => {
        setMode("scenario");
        toast(`Показан сценарий «${item.name}».`, "info");
      });
    });
    element.querySelector('[data-action="delete"]').addEventListener("click", () => {
      if (!confirm(`Удалить сценарий «${item.name}»?`)) return;
      state.saved = state.saved.filter((saved) => saved.id !== item.id);
      persistSaved();
      renderSaved();
    });
    list.appendChild(element);
  }
}

async function compareSaved() {
  if (state.saved.length < 2) {
    toast("Сохраните хотя бы два сценария, чтобы сравнить их.", "warn");
    return;
  }
  const scenarios = state.saved.map((item) => ({ name: item.name, schools: item.schools }));
  try {
    await runComparison(
      scenarios,
      "Сравнение сохранённых сценариев. Верхняя строка — итоги по району, ниже — локальный эффект в кварталах размещения.",
    );
    switchTab("compare");
  } catch (error) {
    toast("Не удалось сравнить сценарии: " + error.message, "error");
  }
}

/* ------------------------------------------------------------ экспорт CSV */

async function exportCsv() {
  try {
    const response = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ schools: state.schools }),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => null);
      throw new Error((data && data.error) || `Ошибка выгрузки (${response.status})`);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="([^"]+)"/);
    const filename = match ? match[1] : "school_scenario.csv";
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast(state.schools.length ? "Таблица сценария выгружена в CSV." : "Выгружено исходное состояние в CSV.", "success");
  } catch (error) {
    toast("Не удалось выгрузить CSV: " + error.message, "error");
  }
}

/* -------------------------------------------------------------- вкладки */

function switchTab(name) {
  document.querySelectorAll(".tabs > button[data-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === name);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === "tab-" + name);
  });
}

/* ------------------------------------------------------ методика и данные */

function renderAbout() {
  const meta = state.meta;
  const params = meta.params;
  const verification = meta.verification;
  const baseline = state.baseline;
  const verificationHtml = verification.ok
    ? `<div class="ok"><strong>Проверка выполнена.</strong> Исходный расчёт (без новых школ) совпал с сохранёнными результатами
       практики 2 по ${verification.compared_blocks} кварталам. Максимальное отклонение обеспеченности —
       ${verification.max_abs_diff.toExponential(2)}.</div>`
    : `<div class="note"><strong>Внимание:</strong> расчёт не совпал с сохранёнными результатами.
       ${escapeHtml(JSON.stringify(verification))}</div>`;

  document.getElementById("aboutBody").innerHTML = `
    <h3>Что делает приложение</h3>
    <p>Приложение сравнивает обеспеченность населения местами в общеобразовательных организациях для разных вариантов
    размещения школ по кварталам Фрунзенского района. Пользователь задаёт кварталы и вместимость новых школ, а приложение
    пересчитывает распределение потребности по всей территории и показывает, как меняются обеспеченность и дефицит мест.</p>
    <p>Выбор земельного участка, оценка возможности и стоимости строительства в приложении не выполняются — для этого нужно
    отдельное градостроительное обоснование.</p>

    <h3>Метод расчёта</h3>
    <ol>
      <li>Потребность квартала — расчётная численность населения × норматив ${params.demand_per_1000} мест на 1000 жителей
      (округление вверх): <code>ceil(население / 1000 × ${params.demand_per_1000})</code>.</li>
      <li>Существующая вместимость школ квартала сначала закрывает его собственную потребность.</li>
      <li>Оставшийся спрос распределяется между школами, до которых можно добраться за ${params.accessibility_min} минут,
      решением задачи линейного программирования (конкурентное распределение, BlocksNet <code>competitive_provision</code>).
      Новая школа увеличивает вместимость своего квартала.</li>
      <li>Обеспеченность квартала — доля потребности, закрытой в пределах норматива доступности.
      Дефицит мест — потребность минус закрытая в нормативе часть (суммарно по территории).</li>
      <li>Сценарий не меняет население, матрицу времени в пути и нормативы — пересчитывается только распределение мест.</li>
    </ol>

    <h3>Данные и источники</h3>
    <table>
      <tr><th>Данные</th><th>Источник</th></tr>
      <tr><td>Кварталы, население, вместимость школ</td><td>BlocksNet-data v0.0.1, набор Санкт-Петербурга
        (github.com/IDUclub/blocksnet-data)</td></tr>
      <tr><td>Время в пути между кварталами</td><td>Граф автомобильных дорог <code>graph_drive.pickle</code>,
        матрица практики 2 (минуты)</td></tr>
      <tr><td>Нормативы и метод</td><td>BlocksNet ${escapeHtml(meta.blocksnet_version)}, практика 2
        (<code>examples/frunzensky_blocksnet.ipynb</code>)</td></tr>
      <tr><td>Показатели кварталов</td><td><code>outputs/frunzensky_indicators.csv</code>,
        <code>outputs/accessibility_matrix_drive.parquet</code></td></tr>
    </table>
    <p>В расчёте ${meta.counts.blocks} кварталов, из них с населением — ${meta.counts.blocks_with_population},
    с существующими школами — ${meta.counts.blocks_with_school_capacity}.
    Матрица времени в пути в исходном проекте построена для ${meta.counts.context_blocks_in_source_matrix} кварталов
    (район + буфер 1 км); приложение использует подматрицу кварталов района.</p>
    <p><strong>Какие значения получены расчётным путём.</strong> Численность населения и суммарная вместимость
    существующих школ по кварталам — расчётные показатели, подготовленные в практике 2 на основе BlocksNet-data.
    Это не официальная статистика: они используются для сравнения вариантов между собой. Исходная обеспеченность
    из практики 2 сохранена отдельно и служит только для проверки воспроизведения расчёта.</p>

    <h3>Параметры расчёта</h3>
    <table>
      <tr><th>Параметр</th><th>Значение</th></tr>
      <tr><td>Норматив доступности школ</td><td>${params.accessibility_min} минут</td></tr>
      <tr><td>Норматив потребности</td><td>${params.demand_per_1000} мест на 1000 жителей</td></tr>
      <tr><td>Вид транспорта</td><td>автомобиль (время в пути из матрицы практики 2)</td></tr>
      <tr><td>Собственное покрытие квартала</td><td>${params.self_supply ? "да" : "нет"}</td></tr>
      <tr><td>Исходное состояние</td><td>население ${fmtInt(baseline.population_total)} чел.,
        вместимость ${fmtInt(baseline.capacity_total)} мест, дефицит ${fmt1(baseline.deficit_total)} мест,
        обеспеченность ${fmtPct(baseline.provision_total)}</td></tr>
    </table>
    ${verificationHtml}

    <h3>Допущения и ограничения</h3>
    <ul>
      <li>Численность населения, матрица времени в пути и нормативы остаются исходными для всех вариантов.</li>
      <li>Новая школа учитывается только как вместимость квартала; этажность, площадь участка и стоимость не моделируются.</li>
      <li>Время в пути — на личном автомобиле; интермодальные поездки и пешая доступность не учитываются.</li>
      <li>Распределение мест конкурентное: жители могут пользоваться школами соседних кварталов в пределах 15 минут.</li>
      <li>Данные отражают состояние открытого набора BlocksNet v0.0.1 и результатов практики 2 на дату подготовки.</li>
      <li>Приложение не заменяет градостроительное обоснование и не определяет пригодность земельного участка.</li>
    </ul>
    <div class="note">Автоподбор проверяет все ${meta.counts.blocks} кварталов территории. В таблице по умолчанию
    показаны только кварталы с населением и дефицитом, где школа действительно нужна; остальные проверенные кварталы
    можно увидеть, сняв фильтр. При равном сокращении дефицита по району приложение рекомендует квартал с наибольшим
    дефицитом мест. После завершения рекомендованный квартал подсвечивается на карте, но школа не добавляется
    автоматически: нажмите на строку таблицы, чтобы рассчитать этот вариант. Это расчётная рекомендация,
    а не выбор земельного участка.</div>`;
}

/* --------------------------------------------------------------- события */

function bindEvents() {
  document.getElementById("btnAddSchool").addEventListener("click", addSchool);
  document.getElementById("capacityInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") addSchool();
  });
  document.getElementById("btnCalculate").addEventListener("click", () => calculateScenario());
  document.getElementById("btnReset").addEventListener("click", () => resetScenario(true));
  document.getElementById("btnAuto").addEventListener("click", () => startAuto());
  document.getElementById("autoOnlyNeeded").addEventListener("change", () => {
    if (state.auto.rows.length) renderAutoSummary();
    renderAutoTable();
  });
  document.getElementById("btnThreeCompare").addEventListener("click", compareThree);
  document.getElementById("btnThreeAuto").addEventListener("click", pickAutoForThree);
  ["threeA", "threeB1", "threeB2", "threeC"].forEach((id) => {
    document.getElementById(id).addEventListener("change", () => {
      if (id === "threeB1" || id === "threeB2") {
        syncThreeBPair(id);
      } else {
        updateThreeControls();
      }
    });
  });
  document.getElementById("btnSave").addEventListener("click", saveCurrentScenario);
  document.getElementById("btnCompareSaved").addEventListener("click", compareSaved);
  document.getElementById("btnExport").addEventListener("click", exportCsv);
  document.getElementById("blockFilter").addEventListener("input", renderBlocksTable);

  document.querySelectorAll("#mapModes button").forEach((button) => {
    button.addEventListener("click", () => setMode(button.dataset.mode));
  });
  document.getElementById("changeMetric").addEventListener("change", (event) => {
    state.changeMetric = event.target.value;
    restyleMap();
  });
  document.getElementById("tilesToggle").addEventListener("change", (event) => {
    if (event.target.checked) {
      tileLayer.addTo(map);
    } else {
      map.removeLayer(tileLayer);
    }
  });

  document.querySelectorAll(".tabs > button[data-tab]").forEach((button) => {
    button.addEventListener("click", () => switchTab(button.dataset.tab));
  });

  document.querySelectorAll("#blocksTable thead th[data-sort]").forEach((header) => {
    header.addEventListener("click", () => {
      const key = header.dataset.sort;
      if (state.blocksSort.key === key) {
        state.blocksSort.dir *= -1;
      } else {
        state.blocksSort = { key, dir: key === "block_id" || key === "population" ? -1 : -1 };
      }
      renderBlocksTable();
    });
  });

  const dialog = document.getElementById("aboutDialog");
  document.getElementById("btnAbout").addEventListener("click", () => dialog.showModal());
  document.getElementById("btnCloseAbout").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });

  window.addEventListener("resize", () => {
    if (map) map.invalidateSize();
  });
}

document.addEventListener("DOMContentLoaded", init);
