(function () {
  "use strict";

  /*
   * Agent contract
   * 1. Query and calculate first. Never put example values in this file.
   * 2. Write one complete payload into #report-payload in index.html.
   * 3. Set a chart/table to status="ready" only after its evidence exists.
   * 4. Use status="no_data" plus reason when a verified query has no rows.
   * 5. Do not rename DOM ids or add chart-specific calculations here.
   */

  var REQUIRED_CHART_IDS = [
    "renewalChart",
    "wheelbaseTrendChart",
    "motorPowerTrendChart",
    "sizePowerHeatmapChart",
    "bevVoltageTrendChart",
    "bevVoltagePriceChart",
    "nevVoltageTrendChart",
    "nevVoltagePriceChart",
    "l2TrendChart",
    "l2PriceBandChart",
    "l2PriceBoxplotChart",
    "highAdasTrendChart",
    "hudRateChart",
    "screenSizeChart",
    "fridgeRateChart",
    "rearScreenRateChart",
    "zeroGravityRateChart",
    "rearMassageRateChart",
    "coreConfigTrendChart",
    "coreConfigHeatmapChart"
  ];

  var REMOVED_CHART_IDS = [
    "adasChipShareChart",
    "lidarShareChart",
    "cockpitChipRateChart",
    "cockpitChipShareChart"
  ];

  var COLORS = ["#2389ad", "#55b7bf", "#79bca8", "#b8cf9f", "#d7ad66", "#7a8c8e", "#c85c55"];
  var RENEWAL_SERIES_CONTRACT = [
    { name: "传统能源更新", type: "bar", axis: 0, stack: "updates", color: "#2389ad" },
    { name: "新能源更新", type: "bar", axis: 0, stack: "updates", color: "#55b7bf" },
    { name: "传统能源周期", type: "line", axis: 1, color: "#d7ad66" },
    { name: "新能源周期", type: "line", axis: 1, color: "#c85c55" }
  ];
  var COMPETITOR_COLUMNS = [
    { key: "brand_group", label: "品牌集团" },
    { key: "update_count", label: "更新" },
    { key: "ytd_yoy_delta", label: "同比" },
    { key: "average_cycle_days", label: "周期" }
  ];
  var COMPETITOR_BRAND_GROUPS = ["比亚迪集团", "长安集团", "奇瑞集团", "长城汽车", "吉利汽车"];
  var SIZE_POWER_X_CATEGORIES = ["0-50", "50-100", "100-150", "150-200", "200-250", "250-300", "300-350", "350-400", "400-450", "450-500", "500kW以上"];
  var SIZE_POWER_Y_CATEGORIES = ["2600以下", "2600-2650", "2650-2700", "2700-2750", "2750-2800", "2800-2850", "2850-2900", "2900-2950", "2950-3000", "3000以上"];
  var CORE_CONFIG_CONTROLS = [
    { value: "airSuspension", label: "空气悬架", title: "空气悬架" },
    { value: "lidar", label: "激光雷达", title: "激光雷达" },
    { value: "hud", label: "HUD", title: "HUD抬头显示" }
  ];
  var CORE_GRAIN_CONTROLS = [
    { value: "trim", label: "款型" },
    { value: "series", label: "车系" }
  ];
  var CORE_DIMENSION_CONTROLS = [
    { value: "price", label: "价格段", bands: ["10万以下", "10-15万", "15-20万", "20-30万", "30-50万", "50万以上"] },
    { value: "wheelbase", label: "轴距段", bands: ["2600以下", "2600-2700", "2700-2800", "2800-2900", "2900-3000", "3000以上"] },
    { value: "level", label: "级别", bands: ["A0级", "A级", "B级", "C级", "D级", "MPV"] }
  ];
  var CORE_CHART_IDS = ["coreConfigTrendChart", "coreConfigHeatmapChart"];
  var L2_PRICE_BAND_SERIES = ["10-15 万", "15-20 万", "20-30 万", "30 万以上", "行业均值"];
  var chartInstances = {};
  var currentPayload = null;

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function getPath(root, path) {
    return String(path || "").split(".").reduce(function (value, key) {
      return value == null ? undefined : value[key];
    }, root);
  }

  function displayValue(value, fallback) {
    if (value === null || value === undefined || value === "") return fallback !== undefined ? fallback : "待生成";
    return String(value);
  }

  function parseEmbeddedPayload() {
    var node = document.getElementById("report-payload");
    if (!node) throw new Error("Missing #report-payload");
    return JSON.parse(node.textContent);
  }

  function validateSeries(spec, chartId, errors) {
    var categories = Array.isArray(spec.categories) ? spec.categories : [];
    var series = Array.isArray(spec.series) ? spec.series : [];
    if (!categories.length) errors.push(chartId + ": categories is empty");
    if (!series.length) errors.push(chartId + ": series is empty");
    series.forEach(function (item, index) {
      if (!Array.isArray(item.data)) {
        errors.push(chartId + ": series[" + index + "].data must be an array");
      } else if (spec.kind !== "heatmap" && item.data.length !== categories.length) {
        errors.push(chartId + ": series[" + index + "] length does not match categories");
      }
    });
  }

  function reportPeriod(payload) {
    var value = getPath(payload, "report.scope.period");
    var match = /^(\d{4})-(0[1-9]|1[0-2])$/.exec(String(value || ""));
    return match ? { value: match[0], year: Number(match[1]), month: Number(match[2]) } : null;
  }

  function validateRenewalChart(spec, payload, errors) {
    var period = reportPeriod(payload);
    var categories = Array.isArray(spec.categories) ? spec.categories.map(String) : [];
    var series = Array.isArray(spec.series) ? spec.series : [];
    if (categories.length !== 6) errors.push("renewalChart: default view requires 6 consecutive years");
    var years = categories.map(Number);
    if (years.some(function (year) { return !Number.isInteger(year); })) {
      errors.push("renewalChart: categories must be calendar years");
    } else if (years.some(function (year, index) { return index > 0 && year !== years[index - 1] + 1; })) {
      errors.push("renewalChart: categories must be consecutive years");
    }
    if (period && years.length && years[years.length - 1] !== period.year) {
      errors.push("renewalChart: latest year must match report.scope.period");
    }
    if (series.length !== RENEWAL_SERIES_CONTRACT.length) {
      errors.push("renewalChart: exactly four fixed series are required");
      return;
    }
    RENEWAL_SERIES_CONTRACT.forEach(function (expected, index) {
      var actual = series[index] || {};
      if (actual.name !== expected.name) errors.push("renewalChart: series[" + index + "] must be " + expected.name);
      if (actual.type !== expected.type) errors.push("renewalChart: " + expected.name + " must use " + expected.type);
      if (Number(actual.axis || 0) !== expected.axis) errors.push("renewalChart: " + expected.name + " uses the wrong axis");
      if (expected.stack && actual.stack !== expected.stack) errors.push("renewalChart: update bars must use stack=updates");
    });
  }

  function validateCompetitorTable(payload, errors) {
    var spec = getPath(payload, "report.tables.competitor_updates");
    if (!isObject(spec)) {
      errors.push("competitor_updates: table contract is required");
      return;
    }
    if (["pending", "ready", "no_data"].indexOf(spec.status) === -1) {
      errors.push("competitor_updates: status must be pending, ready, or no_data");
    }
    if (spec.status === "no_data" && !spec.reason) errors.push("competitor_updates: no_data requires reason");
    if (spec.status !== "ready") return;
    var period = reportPeriod(payload);
    if (!period) errors.push("competitor_updates: report.scope.period must be YYYY-MM");
    if (period && spec.as_of_period !== period.value) errors.push("competitor_updates: as_of_period must equal report.scope.period");
    if (!spec.comparison_period) errors.push("competitor_updates: comparison_period is required");
    var columns = Array.isArray(spec.columns) ? spec.columns : [];
    if (columns.length !== COMPETITOR_COLUMNS.length) {
      errors.push("competitor_updates: exactly four fixed columns are required");
    } else {
      COMPETITOR_COLUMNS.forEach(function (expected, index) {
        var actual = columns[index] || {};
        if (actual.key !== expected.key || actual.label !== expected.label) {
          errors.push("competitor_updates: column[" + index + "] must be " + expected.label + " (" + expected.key + ")");
        }
      });
    }
    var rows = Array.isArray(spec.rows) ? spec.rows : [];
    if (rows.length !== COMPETITOR_BRAND_GROUPS.length) {
      errors.push("competitor_updates: ready table requires exactly five fixed brand groups");
    }
    rows.forEach(function (row, index) {
      if (!row || !row.brand_group) errors.push("competitor_updates: row[" + index + "] requires brand_group");
      if (row && row.brand_group !== COMPETITOR_BRAND_GROUPS[index]) {
        errors.push("competitor_updates: row[" + index + "] must be " + COMPETITOR_BRAND_GROUPS[index]);
      }
      ["update_count", "ytd_yoy_delta", "average_cycle_days"].forEach(function (key) {
        if (!Number.isFinite(Number(row && row[key]))) errors.push("competitor_updates: row[" + index + "]." + key + " must be numeric");
      });
      if (row && (!Number.isInteger(Number(row.update_count)) || Number(row.update_count) < 0)) {
        errors.push("competitor_updates: row[" + index + "].update_count must be a non-negative integer");
      }
      if (row && !Number.isInteger(Number(row.ytd_yoy_delta))) {
        errors.push("competitor_updates: row[" + index + "].ytd_yoy_delta must be an integer count delta");
      }
      if (row && Number(row.average_cycle_days) < 0) {
        errors.push("competitor_updates: row[" + index + "].average_cycle_days must be non-negative");
      }
    });
    if (!Array.isArray(spec.query_ids) || !spec.query_ids.length) {
      errors.push("competitor_updates: ready table requires query_ids");
    }
  }

  function validateSizePowerHeatmap(spec, payload, errors) {
    var period = reportPeriod(payload);
    var variants = Array.isArray(spec.variants) ? spec.variants : [];
    var expectedScope = {
      energy_type: "纯电",
      excluded_vehicle_types: ["皮卡"],
      required_fields: ["轴距", "电机总功率"],
      grain: "款型",
      measure: "款型数"
    };
    if (spec.kind !== "heatmap_variants") {
      errors.push("sizePowerHeatmapChart: kind must be heatmap_variants");
      return;
    }
    if (!period) {
      errors.push("sizePowerHeatmapChart: report.scope.period must be YYYY-MM");
      return;
    }
    if (JSON.stringify(spec.scope || {}) !== JSON.stringify(expectedScope)) {
      errors.push("sizePowerHeatmapChart: scope must be pure-electric trims, excluding pickups, with non-null wheelbase and motor power");
    }
    if (String(spec.default_year || "") !== String(period.year)) {
      errors.push("sizePowerHeatmapChart: default_year must equal the report year");
    }
    var expectedYears = Array.from({ length: 6 }, function (_, index) { return String(period.year - 5 + index); });
    if (variants.length !== expectedYears.length) {
      errors.push("sizePowerHeatmapChart: exactly six yearly variants are required");
    }
    variants.forEach(function (variant, index) {
      var expectedYear = expectedYears[index];
      var actualYear = String(getPath(variant, "filters.year") || "");
      if (variant.kind !== "heatmap") errors.push("sizePowerHeatmapChart: variant[" + index + "].kind must be heatmap");
      if (String(variant.label || "") !== expectedYear || actualYear !== expectedYear) {
        errors.push("sizePowerHeatmapChart: variant[" + index + "] must represent year " + expectedYear);
      }
      if (variant.name !== "款型数") errors.push("sizePowerHeatmapChart: variant[" + index + "].name must be 款型数");
      var xCategories = Array.isArray(variant.x_categories) ? variant.x_categories : [];
      var yCategories = Array.isArray(variant.y_categories) ? variant.y_categories : [];
      if (JSON.stringify(xCategories) !== JSON.stringify(SIZE_POWER_X_CATEGORIES)) {
        errors.push("sizePowerHeatmapChart: variant[" + index + "] must use the fixed power bands on the x-axis");
      }
      if (JSON.stringify(yCategories) !== JSON.stringify(SIZE_POWER_Y_CATEGORIES)) {
        errors.push("sizePowerHeatmapChart: variant[" + index + "] must use the fixed wheelbase bands on the y-axis");
      }
      var values = Array.isArray(variant.values) ? variant.values : [];
      var cells = {};
      values.forEach(function (cell, cellIndex) {
        var x = Number(cell && cell[0]);
        var y = Number(cell && cell[1]);
        var value = Number(cell && cell[2]);
        if (!Number.isInteger(x) || x < 0 || x >= SIZE_POWER_X_CATEGORIES.length ||
            !Number.isInteger(y) || y < 0 || y >= SIZE_POWER_Y_CATEGORIES.length) {
          errors.push("sizePowerHeatmapChart: variant[" + index + "].values[" + cellIndex + "] has an invalid cell index");
        }
        if (!Number.isInteger(value) || value < 0) {
          errors.push("sizePowerHeatmapChart: variant[" + index + "].values[" + cellIndex + "] must be a non-negative integer trim count");
        }
        cells[x + ":" + y] = true;
      });
      if (values.length !== SIZE_POWER_X_CATEGORIES.length * SIZE_POWER_Y_CATEGORIES.length ||
          Object.keys(cells).length !== SIZE_POWER_X_CATEGORIES.length * SIZE_POWER_Y_CATEGORIES.length) {
        errors.push("sizePowerHeatmapChart: variant[" + index + "] must contain the complete 10x11 matrix, including zero cells");
      }
    });
  }

  function validateCoreConfiguration(payload, errors) {
    var spec = payload.core_configuration;
    if (!isObject(spec)) {
      errors.push("core_configuration: v3 module contract is required");
      return;
    }
    if (["pending", "ready", "no_data"].indexOf(spec.status) === -1) {
      errors.push("core_configuration: status must be pending, ready, or no_data");
      return;
    }
    if (spec.status === "no_data" && !spec.reason) errors.push("core_configuration: no_data requires reason");
    if (spec.status !== "ready") return;
    if (spec.kind !== "v3_core_configuration") {
      errors.push("core_configuration: kind must be v3_core_configuration");
    }
    if (!Array.isArray(spec.query_ids) || !spec.query_ids.length) {
      errors.push("core_configuration: ready module requires query_ids");
    }
    var period = reportPeriod(payload);
    if (!period) {
      errors.push("core_configuration: report.scope.period must be YYYY-MM");
      return;
    }
    var years = Array.from({ length: 6 }, function (_, index) { return String(period.year - 5 + index); });
    if (JSON.stringify(spec.years || []) !== JSON.stringify(years)) {
      errors.push("core_configuration: years must be six consecutive years ending in the report year");
    }
    var dimensions = isObject(spec.dimensions) ? spec.dimensions : {};
    CORE_DIMENSION_CONTROLS.forEach(function (dimension) {
      var actual = dimensions[dimension.value] || {};
      if (actual.label !== dimension.label || JSON.stringify(actual.bands || []) !== JSON.stringify(dimension.bands)) {
        errors.push("core_configuration: dimension " + dimension.value + " must match the v3 bands");
      }
    });
    if (Object.keys(dimensions).sort().join("|") !== CORE_DIMENSION_CONTROLS.map(function (item) { return item.value; }).sort().join("|")) {
      errors.push("core_configuration: dimensions must contain only price, wheelbase, and level");
    }
    var configurations = isObject(spec.configurations) ? spec.configurations : {};
    if (Object.keys(configurations).sort().join("|") !== CORE_CONFIG_CONTROLS.map(function (item) { return item.value; }).sort().join("|")) {
      errors.push("core_configuration: configurations must contain only airSuspension, lidar, and hud");
    }
    CORE_CONFIG_CONTROLS.forEach(function (config) {
      var configData = configurations[config.value] || {};
      if (configData.label !== config.title) {
        errors.push("core_configuration: " + config.value + ".label must be " + config.title);
      }
      CORE_GRAIN_CONTROLS.forEach(function (grain) {
        var trend = configData[grain.value] || {};
        if (!Array.isArray(trend.counts) || trend.counts.length !== years.length || trend.counts.some(function (value) { return !Number.isInteger(Number(value)) || Number(value) < 0; })) {
          errors.push("core_configuration: " + config.value + "." + grain.value + ".counts must contain six non-negative integers");
        }
        if (!Array.isArray(trend.rates) || trend.rates.length !== years.length || trend.rates.some(function (value) { return !Number.isFinite(Number(value)) || Number(value) < 0 || Number(value) > 100; })) {
          errors.push("core_configuration: " + config.value + "." + grain.value + ".rates must contain six values in [0,100]");
        }
        CORE_DIMENSION_CONTROLS.forEach(function (dimension) {
          var matrix = getPath(configData, "heatmaps." + grain.value + "." + dimension.value);
          var validMatrix = Array.isArray(matrix) && matrix.length === dimension.bands.length && matrix.every(function (row) {
            return Array.isArray(row) && row.length === years.length && row.every(function (value) {
              return Number.isFinite(Number(value)) && Number(value) >= 0 && Number(value) <= 100;
            });
          });
          if (!validMatrix) {
            errors.push("core_configuration: " + config.value + ".heatmaps." + grain.value + "." + dimension.value + " must be a complete 6x6 rate matrix");
          }
        });
      });
    });
  }

  function validateL2PriceBandChart(spec, payload, errors) {
    var period = reportPeriod(payload);
    var years = period ? Array.from({ length: 6 }, function (_, index) { return String(period.year - 5 + index); }) : [];
    if (spec.kind !== "line") errors.push("l2PriceBandChart: kind must be line");
    if (JSON.stringify((spec.categories || []).map(String)) !== JSON.stringify(years)) {
      errors.push("l2PriceBandChart: x-axis must be six consecutive years ending in the report year");
    }
    var series = Array.isArray(spec.series) ? spec.series : [];
    if (series.length !== L2_PRICE_BAND_SERIES.length) {
      errors.push("l2PriceBandChart: exactly four price-band trends plus the industry average are required");
      return;
    }
    L2_PRICE_BAND_SERIES.forEach(function (name, index) {
      var item = series[index] || {};
      if (item.name !== name) errors.push("l2PriceBandChart: series[" + index + "] must be " + name);
      if (item.type !== "line") errors.push("l2PriceBandChart: " + name + " must be a line series");
      if (!Array.isArray(item.data) || item.data.length !== years.length || item.data.some(function (value) {
        return !Number.isFinite(Number(value)) || Number(value) < 0 || Number(value) > 100;
      })) {
        errors.push("l2PriceBandChart: " + name + " must contain six annual rates in [0,100]");
      }
    });
  }

  function validatePayload(payload) {
    var errors = [];
    var warnings = [];
    if (!isObject(payload)) return { valid: false, errors: ["Payload must be an object"], warnings: [] };
    if (payload.schema_version !== "2.1") errors.push("schema_version must be 2.1");
    if (!isObject(payload.report)) errors.push("report is required");
    if (!isObject(payload.charts)) errors.push("charts is required");

    REQUIRED_CHART_IDS.forEach(function (chartId) {
      if (CORE_CHART_IDS.indexOf(chartId) !== -1) return;
      var spec = payload.charts && payload.charts[chartId];
      if (!isObject(spec)) {
        errors.push("Missing chart contract: " + chartId);
        return;
      }
      if (["pending", "ready", "no_data"].indexOf(spec.status) === -1) {
        errors.push(chartId + ": status must be pending, ready, or no_data");
      }
      if (spec.status === "ready") {
        if (/_variants$/.test(spec.kind || "")) {
          if (!Array.isArray(spec.variants) || !spec.variants.length) errors.push(chartId + ": ready variants are empty");
        } else if (spec.kind === "heatmap") {
          if (!Array.isArray(spec.x_categories) || !Array.isArray(spec.y_categories) || !Array.isArray(spec.values)) {
            errors.push(chartId + ": heatmap requires x_categories, y_categories, and values");
          }
        } else {
          validateSeries(spec, chartId, errors);
        }
        if (!Array.isArray(spec.query_ids) || !spec.query_ids.length) {
          errors.push(chartId + ": ready chart requires query_ids");
        }
        if (chartId === "renewalChart") validateRenewalChart(spec, payload, errors);
        if (chartId === "sizePowerHeatmapChart") validateSizePowerHeatmap(spec, payload, errors);
        if (chartId === "l2PriceBandChart") validateL2PriceBandChart(spec, payload, errors);
      }
      if (spec.status === "no_data" && !spec.reason) errors.push(chartId + ": no_data requires reason");
    });

    REMOVED_CHART_IDS.forEach(function (chartId) {
      if (payload.charts && payload.charts[chartId]) errors.push("Removed chart must not be present: " + chartId);
      if (document.getElementById(chartId)) errors.push("Removed chart DOM still exists: " + chartId);
    });

    validateCompetitorTable(payload, errors);
    validateCoreConfiguration(payload, errors);

    var quality = isObject(payload.quality) ? payload.quality : {};
    if (quality.status === "final" && quality.completed_tasks !== quality.plan_tasks) {
      errors.push("Final payload requires completed_tasks === plan_tasks");
    }
    if (quality.status !== "final") warnings.push("Report is not marked final");
    return { valid: errors.length === 0, errors: errors, warnings: warnings };
  }

  function renderBindings(payload) {
    document.querySelectorAll("[data-bind]").forEach(function (node) {
      var value = getPath(payload, node.getAttribute("data-bind"));
      var missing = value === null || value === undefined || value === "";
      node.textContent = displayValue(value);
      node.classList.toggle("placeholder", missing);
      if (node.tagName === "TIME") {
        if (missing) node.removeAttribute("datetime");
        else node.setAttribute("datetime", String(value));
      }
    });
  }

  function renderIterationMeta(payload) {
    var period = reportPeriod(payload);
    var spec = getPath(payload, "charts.renewalChart") || {};
    var categories = Array.isArray(spec.categories) ? spec.categories : [];
    var firstYear = categories.length ? categories[0] : period && period.year - 5;
    var lastYear = categories.length ? categories[categories.length - 1] : period && period.year;
    var title = document.getElementById("renewalTitle");
    var subtitle = document.getElementById("renewalSubtitle");
    var competitorSubtitle = document.getElementById("competitorSubtitle");
    if (title && firstYear && lastYear) title.textContent = "年度更新次数与更新周期（" + firstYear + "–" + lastYear + "）";
    if (subtitle) {
      subtitle.textContent = "堆叠柱：传统能源·新能源更新次数；折线：两类平均上市周期（天）" +
        (period ? " | " + period.year + " 年为截至 " + period.month + " 月数据" : "");
    }
    if (competitorSubtitle) {
      competitorSubtitle.textContent = period
        ? "固定五家企业；" + period.year + " 年 1–" + period.month + " 月累计，同比为上年同期累计增减"
        : "固定五家企业；默认展示报告年份，更新同比为年初累计相对上年同期";
    }
  }

  function metricMarkup(item) {
    var node = document.createElement("div");
    node.className = "metric";
    var label = document.createElement("div");
    label.className = "metric-label";
    label.textContent = displayValue(item && item.label);
    var value = document.createElement("div");
    value.className = "metric-value " + ((item && item.tone) || "");
    var number = document.createElement("span");
    number.textContent = displayValue(item && item.value, "—");
    if (!item || item.value == null) number.className = "placeholder";
    var unit = document.createElement("small");
    unit.textContent = displayValue(item && item.unit, "");
    value.appendChild(number);
    value.appendChild(unit);
    var note = document.createElement("div");
    note.className = "metric-note";
    note.textContent = displayValue(item && item.note);
    node.appendChild(label);
    node.appendChild(value);
    node.appendChild(note);
    return node;
  }

  function insightMarkup(item) {
    var node = document.createElement("article");
    node.className = "insight";
    var kicker = document.createElement("div");
    kicker.className = "insight-kicker";
    kicker.textContent = displayValue(item && item.kicker);
    var title = document.createElement("h3");
    title.textContent = displayValue(item && item.title);
    var summary = document.createElement("p");
    summary.textContent = displayValue(item && item.summary);
    var marker = document.createElement("div");
    marker.className = "insight-marker";
    node.appendChild(kicker);
    node.appendChild(title);
    node.appendChild(summary);
    node.appendChild(marker);
    return node;
  }

  function featureMarkup(item) {
    var node = document.createElement("article");
    node.className = "feature";
    var progress = item && Number.isFinite(Number(item.progress)) ? Math.max(0, Math.min(100, Number(item.progress))) : 0;
    node.style.setProperty("--value", progress + "%");
    var label = document.createElement("div");
    label.className = "feature-label";
    label.textContent = displayValue(item && item.label);
    var value = document.createElement("div");
    value.className = "feature-value";
    value.textContent = displayValue(item && item.value, "—") + displayValue(item && item.unit, "");
    if (!item || item.value == null) value.classList.add("placeholder");
    var title = document.createElement("h3");
    title.textContent = displayValue(item && item.title);
    var summary = document.createElement("p");
    summary.textContent = displayValue(item && item.summary);
    node.appendChild(label);
    node.appendChild(value);
    node.appendChild(title);
    node.appendChild(summary);
    return node;
  }

  function methodMarkup(item, index) {
    var node = document.createElement("article");
    node.className = "method";
    var code = document.createElement("span");
    code.className = "method-index";
    code.textContent = displayValue(item && item.code, "M" + String(index + 1).padStart(2, "0"));
    var title = document.createElement("h3");
    title.textContent = displayValue(item && item.title);
    var description = document.createElement("p");
    description.textContent = displayValue(item && item.description);
    node.appendChild(code);
    node.appendChild(title);
    node.appendChild(description);
    return node;
  }

  function renderRepeaters(payload) {
    document.querySelectorAll("[data-repeat]").forEach(function (container) {
      var path = container.getAttribute("data-repeat");
      var requested = Number(container.getAttribute("data-repeat-count") || 0);
      var items = getPath(payload, path);
      items = Array.isArray(items) ? items.slice() : [];
      while (items.length < requested) items.push(null);
      container.textContent = "";
      items.forEach(function (item, index) {
        var node;
        if (path === "report.metrics") node = metricMarkup(item);
        else if (path === "report.insights") node = insightMarkup(item);
        else if (path.indexOf("report.features.") === 0) node = featureMarkup(item);
        else if (path === "report.methodology") node = methodMarkup(item, index);
        if (node) container.appendChild(node);
      });
    });
  }

  function renderTables(payload) {
    document.querySelectorAll("table[data-table]").forEach(function (table) {
      var key = table.getAttribute("data-table");
      var spec = getPath(payload, "report.tables." + key) || {};
      var columns = Array.isArray(spec.columns) ? spec.columns : [];
      var rows = Array.isArray(spec.rows) ? spec.rows : [];
      if (key === "competitor_updates") {
        columns = COMPETITOR_COLUMNS;
        rows = rows.slice(0, 5);
      }
      table.textContent = "";
      var thead = document.createElement("thead");
      var headerRow = document.createElement("tr");
      if (!columns.length) columns = [{ key: "placeholder", label: "待生成表格" }];
      columns.forEach(function (column) {
        var th = document.createElement("th");
        th.textContent = displayValue(column.label, column.key);
        headerRow.appendChild(th);
      });
      thead.appendChild(headerRow);
      table.appendChild(thead);
      var tbody = document.createElement("tbody");
      if (spec.status !== "ready" || !rows.length) {
        var emptyRow = document.createElement("tr");
        emptyRow.className = "table-empty";
        var emptyCell = document.createElement("td");
        emptyCell.colSpan = columns.length;
        emptyCell.textContent = displayValue(spec.reason, "等待 Agent 填充已验证数据");
        emptyRow.appendChild(emptyCell);
        tbody.appendChild(emptyRow);
      } else {
        rows.forEach(function (row) {
          var tr = document.createElement("tr");
          columns.forEach(function (column) {
            var td = document.createElement("td");
            var rawValue = row[column.key];
            if (key === "competitor_updates" && column.key === "ytd_yoy_delta" && Number.isFinite(Number(rawValue))) {
              var delta = Number(rawValue);
              td.textContent = delta > 0 ? "+" + delta : delta < 0 ? "−" + Math.abs(delta) : "0";
              if (delta > 0) td.classList.add("delta-up");
              if (delta < 0) td.classList.add("delta-down");
            } else if (key === "competitor_updates" && column.key === "average_cycle_days" && Number.isFinite(Number(rawValue))) {
              td.textContent = Number(rawValue).toLocaleString("zh-CN", { maximumFractionDigits: 1 }) + " 天";
            } else {
              td.textContent = displayValue(rawValue, "—");
            }
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
      }
      table.appendChild(tbody);
    });
  }

  function axisLabel() {
    return { color: "#73828a", fontSize: 9 };
  }

  function baseOption() {
    return {
      color: COLORS,
      animation: false,
      textStyle: { fontFamily: '-apple-system,"PingFang SC","Noto Sans SC",sans-serif', color: "#17232b" },
      tooltip: { trigger: "axis", backgroundColor: "rgba(16,44,57,.95)", borderWidth: 0, textStyle: { color: "#fff", fontSize: 10 } },
      legend: { bottom: 5, type: "scroll", itemWidth: 10, itemHeight: 6, textStyle: { color: "#667780", fontSize: 9 } },
      grid: { left: 44, right: 18, top: 26, bottom: 50, containLabel: true },
      xAxis: { type: "category", data: [], axisLine: { lineStyle: { color: "#cbd7dc" } }, axisTick: { show: false }, axisLabel: axisLabel() },
      yAxis: { type: "value", splitLine: { lineStyle: { color: "#edf1f3" } }, axisLabel: axisLabel(), axisLine: { show: false }, axisTick: { show: false } },
      series: []
    };
  }

  function makeCartesianOption(spec, chartId) {
    var option = baseOption();
    option.xAxis.data = spec.categories || [];
    var usesSecondAxis = (spec.series || []).some(function (series) { return Number(series.axis || 0) === 1; });
    if (usesSecondAxis) {
      option.yAxis = [option.yAxis, { type: "value", splitLine: { show: false }, axisLabel: axisLabel(), axisLine: { show: false }, axisTick: { show: false } }];
    }
    if (spec.kind === "percent_stack") {
      var targetAxis = Array.isArray(option.yAxis) ? option.yAxis[0] : option.yAxis;
      targetAxis.max = 100;
      targetAxis.axisLabel.formatter = "{value}%";
    }
    if (spec.kind === "boxplot") {
      option.series = (spec.series || []).map(function (series) {
        return { name: series.name, type: "boxplot", data: series.data, itemStyle: { color: series.color || COLORS[0] } };
      });
      return option;
    }
    option.series = (spec.series || []).map(function (series, index) {
      var renewalStyle = chartId === "renewalChart" ? RENEWAL_SERIES_CONTRACT[index] : null;
      var type = series.type || (spec.kind === "line" ? "line" : "bar");
      var output = {
        name: series.name,
        type: type,
        data: series.data,
        yAxisIndex: Number(series.axis || 0),
        itemStyle: { color: (renewalStyle && renewalStyle.color) || series.color || COLORS[index % COLORS.length] }
      };
      if (type === "bar") {
        output.barMaxWidth = chartId === "renewalChart" ? 42 : 38;
        if (chartId === "renewalChart") output.stack = "updates";
        else if (spec.kind === "percent_stack" || series.stack) output.stack = series.stack || "total";
      }
      if (type === "line") {
        output.smooth = series.smooth !== false;
        output.symbol = "circle";
        output.symbolSize = chartId === "renewalChart" ? 7 : 6;
        output.lineStyle = { width: chartId === "renewalChart" ? 2.5 : 2, color: renewalStyle && renewalStyle.color };
      }
      return output;
    });
    if (chartId === "renewalChart") {
      if (!Array.isArray(option.yAxis)) {
        option.yAxis = [option.yAxis, { type: "value", splitLine: { show: false }, axisLabel: axisLabel(), axisLine: { show: false }, axisTick: { show: false } }];
      }
      option.color = RENEWAL_SERIES_CONTRACT.map(function (item) { return item.color; });
      option.grid = { left: 52, right: 50, top: 42, bottom: 56, containLabel: true };
      option.yAxis[0].name = "更新次数";
      option.yAxis[0].nameTextStyle = { color: "#73828a", fontSize: 9 };
      option.yAxis[1].name = "周期/天";
      option.yAxis[1].nameTextStyle = { color: "#73828a", fontSize: 9 };
      option.tooltip.axisPointer = { type: "line", lineStyle: { color: "#aebbc1", type: "dashed" } };
    }
    if (chartId === "l2PriceBandChart") {
      option.yAxis.min = 0;
      option.yAxis.max = 100;
      option.yAxis.axisLabel.formatter = "{value}%";
      option.series.forEach(function (series, index) {
        series.type = "line";
        series.smooth = true;
        series.symbol = "circle";
        series.symbolSize = 5;
        series.lineStyle = { width: 2, color: COLORS[index % COLORS.length] };
        series.itemStyle = { color: COLORS[index % COLORS.length] };
      });
    }
    if (chartId === "coreConfigTrendChart") {
      if (!Array.isArray(option.yAxis)) {
        option.yAxis = [option.yAxis, { type: "value", splitLine: { show: false }, axisLabel: axisLabel(), axisLine: { show: false }, axisTick: { show: false } }];
      }
      option.color = ["#2389ad", "#79bca8"];
      option.yAxis[0].min = 0;
      option.yAxis[1].min = 0;
      option.yAxis[1].max = 100;
      option.yAxis[1].axisLabel.formatter = "{value}%";
      if (option.series[0]) {
        option.series[0].barMaxWidth = 34;
        option.series[0].itemStyle = { color: "#2389ad" };
        option.series[0].label = { show: true, position: "top", color: "#40545e", fontSize: 8 };
      }
      if (option.series[1]) {
        option.series[1].symbolSize = 7;
        option.series[1].z = 6;
        option.series[1].itemStyle = { color: "#79bca8" };
        option.series[1].lineStyle = { width: 3, color: "#79bca8" };
        option.series[1].label = { show: true, position: "top", distance: 9, color: "#456b5d", fontSize: 8, fontWeight: 700, backgroundColor: "rgba(255,255,255,.94)", borderColor: "#9bcbbb", borderWidth: 1, borderRadius: 2, padding: [2, 4], formatter: "{c}%" };
      }
    }
    return option;
  }

  function makeHeatmapOption(spec, chartId) {
    var values = spec.values || [];
    var max = values.reduce(function (current, item) { return Math.max(current, Number(item[2]) || 0); }, 0);
    var isSizePower = chartId === "sizePowerHeatmapChart";
    var isCoreConfig = chartId === "coreConfigHeatmapChart";
    return {
      animation: false,
      tooltip: isCoreConfig
        ? { position: "top", formatter: function (params) { return (spec.x_categories || [])[params.data[0]] + "<br>" + (spec.y_categories || [])[params.data[1]] + "：" + params.data[2] + "%"; } }
        : { position: "top" },
      grid: isSizePower
        ? { left: 80, right: 30, top: 20, bottom: 60 }
        : isCoreConfig
          ? { left: 22, right: 24, top: 32, bottom: 66, containLabel: true }
          : { left: 24, right: 24, top: 28, bottom: 66, containLabel: true },
      xAxis: { type: "category", data: spec.x_categories || [], axisLabel: isSizePower ? { color: "#73828a", fontSize: 8, rotate: 30 } : axisLabel(), axisTick: { show: false }, splitArea: { show: true } },
      yAxis: { type: "category", data: spec.y_categories || [], axisLabel: isSizePower ? { color: "#73828a", fontSize: 8 } : axisLabel(), axisTick: { show: false }, splitArea: { show: true } },
      visualMap: { min: 0, max: isCoreConfig ? 100 : Math.max(max, 1), calculable: false, orient: "horizontal", left: "center", bottom: isSizePower ? 5 : isCoreConfig ? 10 : 8, itemWidth: isCoreConfig ? 10 : undefined, itemHeight: isCoreConfig ? 150 : undefined, text: isCoreConfig ? ["高", "低"] : undefined, inRange: { color: ["#f4f6eb", "#b8d5c4", "#55b7bf", "#12627f"] }, textStyle: axisLabel() },
      series: [{ name: spec.name || "数值", type: "heatmap", data: values, label: { show: true, color: isCoreConfig ? "#17313b" : undefined, fontSize: isSizePower ? 7 : 8, formatter: isCoreConfig ? function (params) { return params.data[2] ? params.data[2] : ""; } : undefined }, itemStyle: { borderColor: "#fff", borderWidth: isCoreConfig ? 2 : 1 }, emphasis: isCoreConfig ? { disabled: true } : undefined }]
    };
  }

  function destroyChart(chartId) {
    if (chartInstances[chartId]) {
      chartInstances[chartId].dispose();
      delete chartInstances[chartId];
    }
  }

  function setChartEmpty(chartId, reason) {
    destroyChart(chartId);
    var panel = document.querySelector('[data-chart-panel="' + chartId + '"]');
    if (!panel) return;
    panel.classList.add("chart-empty");
    var fallback = panel.querySelector(".echart-fallback");
    if (fallback) fallback.textContent = displayValue(reason, "等待 Agent 填充已验证数据");
  }

  function renderChart(chartId, spec) {
    var element = document.getElementById(chartId);
    if (!element || !spec) return;
    var panel = document.querySelector('[data-chart-panel="' + chartId + '"]');
    if (spec.status !== "ready") {
      setChartEmpty(chartId, spec.reason);
      return;
    }
    if (chartId === "l2PriceBandChart") {
      var l2Errors = [];
      validateL2PriceBandChart(spec, currentPayload || {}, l2Errors);
      if (l2Errors.length) {
        setChartEmpty(chartId, "L2+ 年度价格带趋势未通过模板校验");
        return;
      }
    }
    if (typeof window.echarts === "undefined") {
      setChartEmpty(chartId, "ECharts 运行时未加载");
      return;
    }
    var chartSpec = spec;
    if (/_variants$/.test(spec.kind || "")) chartSpec = (spec.variants || [])[0];
    if (!chartSpec) {
      setChartEmpty(chartId, spec.reason || "没有可渲染的变体");
      return;
    }
    var hasData = chartSpec.kind === "heatmap"
      ? Array.isArray(chartSpec.values) && chartSpec.values.length > 0
      : Array.isArray(chartSpec.series) && chartSpec.series.some(function (series) { return Array.isArray(series.data) && series.data.length > 0; });
    if (!hasData) {
      setChartEmpty(chartId, chartSpec.reason || spec.reason || "已标记 ready，但数据为空");
      return;
    }
    destroyChart(chartId);
    if (panel) panel.classList.remove("chart-empty");
    var option = chartSpec.kind === "heatmap" ? makeHeatmapOption(chartSpec, chartId) : makeCartesianOption(chartSpec, chartId);
    var chart = window.echarts.init(element, null, { renderer: "svg" });
    chart.setOption(option, { notMerge: true, lazyUpdate: false, silent: true });
    chartInstances[chartId] = chart;
  }

  function fillSelect(select, options) {
    select.textContent = "";
    if (!Array.isArray(options) || !options.length) options = [{ value: "", label: "待生成" }];
    options.forEach(function (item) {
      var option = document.createElement("option");
      option.value = displayValue(item.value, "");
      option.textContent = displayValue(item.label, item.value);
      select.appendChild(option);
    });
  }

  function setupHeatmapVariants(payload) {
    var spec = payload.charts.sizePowerHeatmapChart;
    var select = document.getElementById("heatmapYearSelect");
    var variants = Array.isArray(spec.variants) ? spec.variants : [];
    fillSelect(select, variants.map(function (variant, index) {
      return { value: String(index), label: variant.label || displayValue(getPath(variant, "filters.year"), "年份 " + (index + 1)) };
    }));
    select.onchange = function () {
      var variant = variants[Number(select.value)];
      renderChart("sizePowerHeatmapChart", variant ? Object.assign({}, variant, { status: "ready", query_ids: spec.query_ids || variant.query_ids }) : spec);
    };
    if (spec.status === "ready" && variants.length) {
      select.value = String(variants.length - 1);
      select.onchange();
    }
    else renderChart("sizePowerHeatmapChart", spec);
  }

  function coreMatrixValues(matrix) {
    var values = [];
    (matrix || []).forEach(function (row, yIndex) {
      (row || []).forEach(function (value, xIndex) {
        values.push([xIndex, yIndex, value]);
      });
    });
    return values;
  }

  function buildCoreTrendSpec(spec, configKey, grainKey) {
    var config = spec.configurations[configKey];
    var grain = CORE_GRAIN_CONTROLS.find(function (item) { return item.value === grainKey; }) || CORE_GRAIN_CONTROLS[0];
    var trend = config[grainKey];
    return {
      status: "ready",
      kind: "combo",
      categories: spec.years,
      series: [
        { name: grain.label + "数", type: "bar", axis: 0, data: trend.counts },
        { name: "配置率", type: "line", axis: 1, data: trend.rates }
      ],
      query_ids: spec.query_ids
    };
  }

  function buildCoreHeatmapSpec(spec, configKey, grainKey, dimensionKey) {
    var dimension = spec.dimensions[dimensionKey];
    return {
      status: "ready",
      kind: "heatmap",
      name: "配置率",
      x_categories: spec.years,
      y_categories: dimension.bands,
      values: coreMatrixValues(spec.configurations[configKey].heatmaps[grainKey][dimensionKey]),
      query_ids: spec.query_ids
    };
  }

  function setupCoreConfiguration(payload) {
    var spec = payload.core_configuration || {};
    var configSelect = document.getElementById("coreConfigSelect");
    var grainSelect = document.getElementById("coreGrainSelect");
    var dimensionSelect = document.getElementById("coreHeatDimensionSelect");
    fillSelect(configSelect, CORE_CONFIG_CONTROLS.map(function (item) { return { value: item.value, label: item.label }; }));
    fillSelect(grainSelect, CORE_GRAIN_CONTROLS);
    fillSelect(dimensionSelect, CORE_DIMENSION_CONTROLS);
    configSelect.value = "airSuspension";
    grainSelect.value = "trim";
    dimensionSelect.value = "price";

    function update() {
      var config = CORE_CONFIG_CONTROLS.find(function (item) { return item.value === configSelect.value; }) || CORE_CONFIG_CONTROLS[0];
      var grain = CORE_GRAIN_CONTROLS.find(function (item) { return item.value === grainSelect.value; }) || CORE_GRAIN_CONTROLS[0];
      var dimension = CORE_DIMENSION_CONTROLS.find(function (item) { return item.value === dimensionSelect.value; }) || CORE_DIMENSION_CONTROLS[0];
      document.getElementById("coreTrendTitle").textContent = config.title + "趋势分析（" + grain.label + "口径）";
      var subtitle = document.getElementById("coreTrendSubtitle");
      if (subtitle) subtitle.textContent = "柱形：搭载" + grain.label + "数；折线：配置率";
      document.getElementById("coreHeatmapTitle").textContent = config.title + dimension.label + "热力图（" + grain.label + "口径）";
      renderChart("coreConfigTrendChart", buildCoreTrendSpec(spec, config.value, grain.value));
      renderChart("coreConfigHeatmapChart", buildCoreHeatmapSpec(spec, config.value, grain.value, dimension.value));
    }
    configSelect.onchange = update;
    grainSelect.onchange = update;
    dimensionSelect.onchange = update;
    var coreErrors = [];
    validateCoreConfiguration(payload, coreErrors);
    if (spec.status === "ready" && !coreErrors.length) update();
    else {
      var reason = spec.status === "ready" ? "核心配置数据未通过 v3 模块校验" : displayValue(spec.reason, "等待核心配置查询");
      setChartEmpty("coreConfigTrendChart", reason);
      setChartEmpty("coreConfigHeatmapChart", reason);
    }
  }

  function renderCharts(payload) {
    REQUIRED_CHART_IDS.forEach(function (chartId) {
      if (["sizePowerHeatmapChart", "coreConfigTrendChart", "coreConfigHeatmapChart"].indexOf(chartId) === -1) {
        renderChart(chartId, payload.charts[chartId]);
      }
    });
    setupHeatmapVariants(payload);
    setupCoreConfiguration(payload);
  }

  function setupPageActions() {
    var printButton = document.getElementById("printButton");
    if (printButton) printButton.onclick = function () { window.print(); };
    var navItems = Array.prototype.slice.call(document.querySelectorAll(".nav-item"));
    navItems.forEach(function (item) {
      item.addEventListener("click", function () {
        navItems.forEach(function (candidate) { candidate.classList.toggle("active", candidate === item); });
      });
    });
    window.addEventListener("resize", function () {
      Object.keys(chartInstances).forEach(function (chartId) { chartInstances[chartId].resize(); });
    });
  }

  function render(payload) {
    currentPayload = payload;
    var validation = validatePayload(payload);
    renderBindings(payload);
    renderIterationMeta(payload);
    renderRepeaters(payload);
    renderTables(payload);
    renderCharts(payload);
    document.documentElement.dataset.reportStatus = validation.valid ? ((payload.quality || {}).status || "draft") : "invalid";
    window.__PRODUCT_CONFIG_REPORT_VALIDATION__ = validation;
    return validation;
  }

  function initialize() {
    setupPageActions();
    try {
      render(parseEmbeddedPayload());
    } catch (error) {
      console.error("Product configuration template failed to initialize", error);
      document.documentElement.dataset.reportStatus = "invalid";
    }
  }

  window.ProductConfigReport = {
    requiredChartIds: REQUIRED_CHART_IDS.slice(),
    removedChartIds: REMOVED_CHART_IDS.slice(),
    validate: validatePayload,
    render: render,
    getPayload: function () { return currentPayload; },
    getInstances: function () { return Object.assign({}, chartInstances); }
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, { once: true });
  else initialize();
}());
