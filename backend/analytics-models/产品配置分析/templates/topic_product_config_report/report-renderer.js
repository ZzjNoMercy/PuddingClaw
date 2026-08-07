(function () {
  "use strict";

  var ALLOWED_TONES = ["neutral", "accent", "positive", "caution"];
  var ALLOWED_KINDS = ["chart", "small_multiples", "table"];
  var ALLOWED_CHART_TYPES = ["line", "bar", "stacked_bar", "combo", "heatmap", "scatter"];
  var ALLOWED_TABLE_FORMATS = ["text", "integer", "decimal", "percent", "delta"];
  var FORBIDDEN_PAYLOAD_KEYS = ["option", "options", "formatter", "javascript", "script", "html", "innerHTML"];
  var THEME_STORAGE_KEY = "puddingclaw-topic-report-theme";
  var chartRecords = [];
  var resizeTimer = null;

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function text(value, fallback) {
    if (value === null || value === undefined || value === "") return fallback || "";
    return String(value);
  }

  function createElement(tag, className, content) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = String(content);
    return node;
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  function getNode(id) {
    return document.getElementById(id);
  }

  function parseEmbeddedPayload() {
    var node = getNode("report-payload");
    if (!node) throw new Error("Missing #report-payload");
    return JSON.parse(node.textContent);
  }

  function scanForbiddenKeys(value, path, errors) {
    if (Array.isArray(value)) {
      value.forEach(function (item, index) {
        scanForbiddenKeys(item, path + "[" + index + "]", errors);
      });
      return;
    }
    if (!isObject(value)) return;
    Object.keys(value).forEach(function (key) {
      if (FORBIDDEN_PAYLOAD_KEYS.indexOf(key) !== -1) {
        errors.push(path + "." + key + " is not allowed");
      }
      scanForbiddenKeys(value[key], path + "." + key, errors);
    });
  }

  function validateQueryIds(item, path, evidence, errors, required) {
    var ids = asArray(item && item.query_ids);
    if (required && !ids.length) errors.push(path + ".query_ids is required");
    ids.forEach(function (queryId) {
      if (!Object.prototype.hasOwnProperty.call(evidence, String(queryId))) {
        errors.push(path + ".query_ids references missing evidence: " + queryId);
      }
    });
  }

  function validateSeriesChart(chart, path, errors) {
    var categories = asArray(chart.categories);
    var series = asArray(chart.series);
    if (!categories.length) errors.push(path + ".categories is required");
    if (!series.length) errors.push(path + ".series is required");
    series.forEach(function (item, index) {
      var seriesPath = path + ".series[" + index + "]";
      if (!isObject(item)) {
        errors.push(seriesPath + " must be an object");
        return;
      }
      if (!text(item.name)) errors.push(seriesPath + ".name is required");
      if (!Array.isArray(item.data)) {
        errors.push(seriesPath + ".data must be an array");
      } else if (item.data.length !== categories.length) {
        errors.push(seriesPath + ".data length must match categories");
      }
      if (chart.type === "combo") {
        if (["line", "bar"].indexOf(item.type) === -1) {
          errors.push(seriesPath + ".type must be line or bar for combo charts");
        }
        if (item.axis !== undefined && [0, 1].indexOf(Number(item.axis)) === -1) {
          errors.push(seriesPath + ".axis must be 0 or 1");
        }
      }
    });
  }

  function validateChart(chart, path, errors) {
    if (!isObject(chart)) {
      errors.push(path + " must be an object");
      return;
    }
    if (ALLOWED_CHART_TYPES.indexOf(chart.type) === -1) {
      errors.push(path + ".type is not supported");
      return;
    }
    if (["line", "bar", "stacked_bar", "combo"].indexOf(chart.type) !== -1) {
      validateSeriesChart(chart, path, errors);
    }
    if (chart.type === "heatmap") {
      if (!asArray(chart.x_categories).length) errors.push(path + ".x_categories is required");
      if (!asArray(chart.y_categories).length) errors.push(path + ".y_categories is required");
      if (!asArray(chart.data).length) errors.push(path + ".data is required");
      asArray(chart.data).forEach(function (point, index) {
        if (!Array.isArray(point) || point.length < 3) {
          errors.push(path + ".data[" + index + "] must be [xIndex, yIndex, value]");
        }
      });
    }
    if (chart.type === "scatter") {
      if (!asArray(chart.series).length) errors.push(path + ".series is required");
      asArray(chart.series).forEach(function (series, seriesIndex) {
        asArray(series && series.data).forEach(function (point, pointIndex) {
          if (!Array.isArray(point) || point.length < 2) {
            errors.push(path + ".series[" + seriesIndex + "].data[" + pointIndex + "] must contain x and y");
          }
        });
      });
    }
  }

  function validateBlock(block, index, evidence, ids, errors) {
    var path = "blocks[" + index + "]";
    if (!isObject(block)) {
      errors.push(path + " must be an object");
      return;
    }
    if (!/^[A-Za-z0-9_-]+$/.test(text(block.id))) errors.push(path + ".id is invalid");
    if (ids[block.id]) errors.push(path + ".id must be unique");
    ids[block.id] = true;
    if (ALLOWED_KINDS.indexOf(block.kind) === -1) errors.push(path + ".kind is not supported");
    if (["ready", "no_data", "pending"].indexOf(block.status) === -1) {
      errors.push(path + ".status must be ready, no_data, or pending");
    }
    if (!text(block.title)) errors.push(path + ".title is required");
    if (block.status !== "ready" && !text(block.reason)) errors.push(path + ".reason is required");
    validateQueryIds(block, path, evidence, errors, block.status === "ready");
    if (block.status !== "ready") return;

    if (block.kind === "chart") validateChart(block.chart, path + ".chart", errors);
    if (block.kind === "small_multiples") {
      var items = asArray(block.items);
      if (!items.length) errors.push(path + ".items is required");
      items.forEach(function (item, itemIndex) {
        var itemPath = path + ".items[" + itemIndex + "]";
        if (!isObject(item) || !text(item.title)) errors.push(itemPath + ".title is required");
        validateChart(item && item.chart, itemPath + ".chart", errors);
        if (item && item.chart && ["line", "bar"].indexOf(item.chart.type) === -1) {
          errors.push(itemPath + ".chart.type must be line or bar");
        }
      });
    }
    if (block.kind === "table") {
      var columns = asArray(block.columns);
      if (!columns.length) errors.push(path + ".columns is required");
      if (!Array.isArray(block.rows)) errors.push(path + ".rows must be an array");
      columns.forEach(function (column, columnIndex) {
        var columnPath = path + ".columns[" + columnIndex + "]";
        if (!isObject(column) || !text(column.key) || !text(column.label)) {
          errors.push(columnPath + " requires key and label");
        }
        if (column && column.align && ["left", "center", "right"].indexOf(column.align) === -1) {
          errors.push(columnPath + ".align is invalid");
        }
        if (column && column.format && ALLOWED_TABLE_FORMATS.indexOf(column.format) === -1) {
          errors.push(columnPath + ".format is invalid");
        }
      });
    }
  }

  function validatePayload(payload) {
    var errors = [];
    if (!isObject(payload)) return ["payload must be an object"];
    scanForbiddenKeys(payload, "payload", errors);
    if (payload.schema_version !== "1.0") errors.push("schema_version must be 1.0");
    var report = isObject(payload.report) ? payload.report : {};
    var quality = isObject(payload.quality) ? payload.quality : {};
    var evidence = isObject(payload.evidence) ? payload.evidence : {};
    var ready = quality.status === "ready";
    if (["draft", "ready"].indexOf(quality.status) === -1) errors.push("quality.status must be draft or ready");
    if (ready && !text(report.title)) errors.push("report.title is required when ready");
    if (ready && !text(report.data_cutoff)) errors.push("report.data_cutoff is required when ready");
    if (!Array.isArray(payload.kpis)) errors.push("kpis must be an array");
    if (!Array.isArray(payload.insights)) errors.push("insights must be an array");
    if (!Array.isArray(payload.blocks)) errors.push("blocks must be an array");
    if (!Array.isArray(payload.methodology)) errors.push("methodology must be an array");

    asArray(payload.kpis).forEach(function (item, index) {
      var path = "kpis[" + index + "]";
      if (!isObject(item) || !text(item.label) || item.value === null || item.value === undefined || item.value === "") {
        errors.push(path + " requires label and value");
      }
      if (item && item.tone && ALLOWED_TONES.indexOf(item.tone) === -1) errors.push(path + ".tone is invalid");
      validateQueryIds(item, path, evidence, errors, true);
    });
    asArray(payload.insights).forEach(function (item, index) {
      var path = "insights[" + index + "]";
      if (!isObject(item) || !text(item.title) || !text(item.summary)) {
        errors.push(path + " requires title and summary");
      }
      if (item && item.tone && ALLOWED_TONES.indexOf(item.tone) === -1) errors.push(path + ".tone is invalid");
      validateQueryIds(item, path, evidence, errors, true);
    });
    var ids = {};
    asArray(payload.blocks).forEach(function (block, index) {
      validateBlock(block, index, evidence, ids, errors);
    });
    asArray(payload.methodology).forEach(function (item, index) {
      if (!isObject(item) || !text(item.title) || !text(item.description)) {
        errors.push("methodology[" + index + "] requires title and description");
      }
      validateQueryIds(item, "methodology[" + index + "]", evidence, errors, false);
    });
    if (ready && !asArray(payload.blocks).length) errors.push("ready report requires at least one block");
    return errors;
  }

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function chartTheme() {
    var dark = document.documentElement.getAttribute("data-theme") === "dark";
    return {
      surface: css("--surface"),
      ink: css("--ink"),
      secondary: css("--ink-secondary"),
      muted: css("--ink-muted"),
      grid: css("--grid"),
      axis: css("--axis"),
      palette: dark
        ? ["#3987e5", "#d95926", "#199e70", "#c98500", "#797973"]
        : ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#a7a59d"],
      heat: dark
        ? ["#16202e", "#1b3350", "#256abf", "#3987e5", "#86b6ef"]
        : ["#eef5fd", "#cde2fb", "#86b6ef", "#5598e7", "#1c5cab"]
    };
  }

  function baseAxis(theme) {
    return {
      axisLine: { lineStyle: { color: theme.axis } },
      axisTick: { show: false },
      axisLabel: { color: theme.muted, fontSize: 11 },
      splitLine: { lineStyle: { color: theme.grid, width: 1 } },
      nameTextStyle: { color: theme.muted, fontSize: 11 }
    };
  }

  function tooltip(theme, trigger) {
    return {
      trigger: trigger || "axis",
      backgroundColor: theme.surface,
      borderColor: theme.axis,
      textStyle: { color: theme.ink, fontSize: 11, fontFamily: css("--font-sans") },
      axisPointer: { type: "cross", lineStyle: { color: theme.grid } }
    };
  }

  function legend(theme) {
    return {
      top: 4,
      right: 8,
      itemWidth: 12,
      itemHeight: 8,
      textStyle: { color: theme.secondary, fontSize: 11, fontFamily: css("--font-sans") }
    };
  }

  function buildSeriesOption(chart, theme) {
    var type = chart.type;
    var combo = type === "combo";
    var stacked = type === "stacked_bar";
    var series = asArray(chart.series).map(function (item, index) {
      var seriesType = combo ? item.type : (type === "line" ? "line" : "bar");
      var color = theme.palette[index % theme.palette.length];
      var result = {
        name: text(item.name),
        type: seriesType,
        data: item.data,
        yAxisIndex: combo ? Number(item.axis || 0) : 0,
        itemStyle: { color: color },
        emphasis: { focus: "series" }
      };
      if (seriesType === "line") {
        result.smooth = false;
        result.symbol = "circle";
        result.symbolSize = 7;
        result.lineStyle = { width: 2, color: color };
        result.label = {
          show: true,
          position: "top",
          color: theme.secondary,
          fontSize: 10,
          formatter: function (params) {
            return params.dataIndex === item.data.length - 1 ? params.value + text(item.unit || chart.unit) : "";
          }
        };
      } else {
        result.barMaxWidth = 34;
        if (stacked || item.stack) result.stack = text(item.stack, "total");
      }
      return result;
    });
    var yAxes = [{
      type: "value",
      name: text(chart.y_name),
      min: chart.min === undefined ? null : chart.min,
      max: chart.max === undefined ? null : chart.max,
      axisLabel: { color: theme.muted, fontSize: 11, formatter: "{value}" + text(chart.unit) },
      axisLine: baseAxis(theme).axisLine,
      axisTick: baseAxis(theme).axisTick,
      splitLine: baseAxis(theme).splitLine,
      nameTextStyle: baseAxis(theme).nameTextStyle
    }];
    if (combo && asArray(chart.series).some(function (item) { return Number(item.axis || 0) === 1; })) {
      yAxes.push({
        type: "value",
        name: text(chart.y2_name),
        axisLabel: { color: theme.muted, fontSize: 11 },
        axisLine: baseAxis(theme).axisLine,
        axisTick: baseAxis(theme).axisTick,
        splitLine: { show: false },
        nameTextStyle: baseAxis(theme).nameTextStyle
      });
    }
    return {
      animationDuration: 320,
      color: theme.palette,
      grid: { left: 54, right: yAxes.length > 1 ? 58 : 24, top: 48, bottom: 44, containLabel: true },
      tooltip: tooltip(theme, "axis"),
      legend: legend(theme),
      xAxis: Object.assign({ type: "category", data: chart.categories, name: text(chart.x_name), boundaryGap: type !== "line" }, baseAxis(theme)),
      yAxis: yAxes,
      series: series
    };
  }

  function buildHeatmapOption(chart, theme) {
    var values = asArray(chart.data).map(function (item) { return Number(item[2]); }).filter(Number.isFinite);
    var min = chart.min === undefined ? (values.length ? Math.min.apply(null, values) : 0) : Number(chart.min);
    var max = chart.max === undefined ? (values.length ? Math.max.apply(null, values) : 100) : Number(chart.max);
    return {
      animationDuration: 280,
      grid: { left: 72, right: 24, top: 24, bottom: 82, containLabel: true },
      tooltip: tooltip(theme, "item"),
      xAxis: Object.assign({ type: "category", data: chart.x_categories, name: text(chart.x_name) }, baseAxis(theme)),
      yAxis: Object.assign({ type: "category", data: chart.y_categories, name: text(chart.y_name) }, baseAxis(theme)),
      visualMap: {
        min: min,
        max: max === min ? min + 1 : max,
        calculable: true,
        orient: "horizontal",
        left: "center",
        bottom: 4,
        textStyle: { color: theme.muted, fontSize: 10 },
        inRange: { color: theme.heat }
      },
      series: [{
        type: "heatmap",
        data: chart.data,
        label: { show: true, color: theme.ink, fontSize: 10 },
        itemStyle: { borderColor: theme.surface, borderWidth: 2 },
        emphasis: { itemStyle: { borderColor: theme.ink, borderWidth: 1 } }
      }]
    };
  }

  function buildScatterOption(chart, theme) {
    return {
      animationDuration: 320,
      color: theme.palette,
      grid: { left: 56, right: 28, top: 48, bottom: 46, containLabel: true },
      tooltip: tooltip(theme, "item"),
      legend: legend(theme),
      xAxis: Object.assign({ type: "value", name: text(chart.x_name) }, baseAxis(theme)),
      yAxis: Object.assign({ type: "value", name: text(chart.y_name) }, baseAxis(theme)),
      series: asArray(chart.series).map(function (item, index) {
        return {
          name: text(item.name),
          type: "scatter",
          data: item.data,
          symbolSize: 10,
          itemStyle: { color: theme.palette[index % theme.palette.length], opacity: 0.78 },
          emphasis: { focus: "series", scale: 1.3 }
        };
      })
    };
  }

  function buildChartOption(chart) {
    var theme = chartTheme();
    if (chart.type === "heatmap") return buildHeatmapOption(chart, theme);
    if (chart.type === "scatter") return buildScatterOption(chart, theme);
    return buildSeriesOption(chart, theme);
  }

  function initializeChart(node, chart) {
    if (!window.echarts) {
      clear(node);
      node.appendChild(createElement("div", "empty-state", "图表运行时未加载，请确认 echarts-6.1.0.min.js 与报告位于同一目录。"));
      return;
    }
    var instance = window.echarts.init(node);
    instance.setOption(buildChartOption(chart), true);
    chartRecords.push({ instance: instance, spec: chart });
  }

  function disposeCharts() {
    chartRecords.forEach(function (record) {
      if (record.instance && !record.instance.isDisposed()) record.instance.dispose();
    });
    chartRecords = [];
  }

  function refreshChartsForTheme() {
    chartRecords.forEach(function (record) {
      record.instance.setOption(buildChartOption(record.spec), true);
    });
  }

  function renderMeta(report) {
    var node = getNode("report-meta");
    clear(node);
    var scope = isObject(report.scope) ? report.scope : {};
    var items = [
      report.data_cutoff ? "数据截止：" + report.data_cutoff : "",
      scope.period ? "周期：" + scope.period : "",
      scope.market ? "范围：" + scope.market : "",
      scope.grain ? "颗粒度：" + scope.grain : ""
    ].filter(Boolean);
    asArray(scope.filters).forEach(function (item) { items.push(text(item)); });
    items.forEach(function (item) { node.appendChild(createElement("li", "", item)); });
    node.hidden = !items.length;
  }

  function renderKpis(kpis) {
    var node = getNode("kpi-grid");
    clear(node);
    asArray(kpis).forEach(function (item) {
      var card = createElement("article", "kpi");
      card.dataset.tone = ALLOWED_TONES.indexOf(item.tone) !== -1 ? item.tone : "neutral";
      card.appendChild(createElement("div", "kpi-label", item.label));
      var valueRow = createElement("div", "kpi-value-row");
      valueRow.appendChild(createElement("div", "kpi-value", item.value));
      if (text(item.unit)) valueRow.appendChild(createElement("div", "kpi-unit", item.unit));
      card.appendChild(valueRow);
      if (text(item.hint)) card.appendChild(createElement("div", "kpi-hint", item.hint));
      node.appendChild(card);
    });
    node.hidden = !asArray(kpis).length;
  }

  function renderInsights(insights) {
    var section = getNode("insights");
    var node = getNode("insight-list");
    clear(node);
    asArray(insights).forEach(function (item, index) {
      var card = createElement("article", "insight");
      card.dataset.tone = ALLOWED_TONES.indexOf(item.tone) !== -1 ? item.tone : "neutral";
      card.appendChild(createElement("p", "insight-kicker", text(item.kicker, "发现 " + String(index + 1).padStart(2, "0"))));
      card.appendChild(createElement("h3", "insight-title", item.title));
      card.appendChild(createElement("p", "insight-summary", item.summary));
      node.appendChild(card);
    });
    section.hidden = !asArray(insights).length;
  }

  function renderBlockState(body, block) {
    body.appendChild(createElement("div", "empty-state", text(block.reason, block.status === "pending" ? "该区块尚未生成。" : "已验证当前范围无可用数据。")));
  }

  function renderChartBlock(body, block) {
    var chartNode = createElement("div", "chart");
    chartNode.id = "chart-" + block.id;
    chartNode.setAttribute("role", "img");
    chartNode.setAttribute("aria-label", block.title);
    body.appendChild(chartNode);
    initializeChart(chartNode, block.chart);
  }

  function renderSmallMultiples(body, block) {
    var grid = createElement("div", "small-multiples");
    body.appendChild(grid);
    asArray(block.items).forEach(function (item, index) {
      var card = createElement("section", "small-multiple");
      card.appendChild(createElement("h3", "small-multiple-title", item.title));
      card.appendChild(createElement("p", "small-multiple-note", text(item.note)));
      var chartNode = createElement("div", "small-chart");
      chartNode.id = "chart-" + block.id + "-" + index;
      chartNode.setAttribute("role", "img");
      chartNode.setAttribute("aria-label", block.title + "：" + item.title);
      card.appendChild(chartNode);
      grid.appendChild(card);
      initializeChart(chartNode, item.chart);
    });
  }

  function formatTableValue(value, column) {
    if (value === null || value === undefined || value === "") return "—";
    var format = column.format || "text";
    var number = Number(value);
    if (format === "integer" && Number.isFinite(number)) return Math.round(number).toLocaleString("zh-CN") + text(column.unit);
    if (format === "decimal" && Number.isFinite(number)) return number.toLocaleString("zh-CN", { maximumFractionDigits: 2 }) + text(column.unit);
    if (format === "percent" && Number.isFinite(number)) return number.toLocaleString("zh-CN", { maximumFractionDigits: 2 }) + "%";
    if (format === "delta" && Number.isFinite(number)) return (number > 0 ? "+" : "") + number.toLocaleString("zh-CN", { maximumFractionDigits: 2 }) + text(column.unit);
    return text(value) + text(column.unit);
  }

  function renderTable(body, block) {
    var scroller = createElement("div", "table-scroll");
    var table = createElement("table", "data-table");
    var thead = document.createElement("thead");
    var headRow = document.createElement("tr");
    asArray(block.columns).forEach(function (column) {
      var th = createElement("th", "", column.label);
      th.dataset.align = text(column.align, column.format && column.format !== "text" ? "right" : "left");
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    var tbody = document.createElement("tbody");
    asArray(block.rows).forEach(function (row) {
      var tr = document.createElement("tr");
      asArray(block.columns).forEach(function (column) {
        var td = createElement("td", "", formatTableValue(row && row[column.key], column));
        td.dataset.align = text(column.align, column.format && column.format !== "text" ? "right" : "left");
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    scroller.appendChild(table);
    body.appendChild(scroller);
  }

  function renderBlocks(blocks) {
    var node = getNode("blocks");
    var items = asArray(blocks);
    clear(node);
    node.hidden = !items.length;
    if (!items.length) return;
    items.forEach(function (block) {
      var card = createElement("section", "report-card");
      card.id = "block-" + block.id;
      var head = createElement("header", "card-head");
      head.appendChild(createElement("h2", "card-title", block.title));
      if (text(block.subtitle)) head.appendChild(createElement("p", "card-subtitle", block.subtitle));
      card.appendChild(head);
      var body = createElement("div", "card-body");
      card.appendChild(body);
      node.appendChild(card);
      if (block.status !== "ready") renderBlockState(body, block);
      else if (block.kind === "chart") renderChartBlock(body, block);
      else if (block.kind === "small_multiples") renderSmallMultiples(body, block);
      else if (block.kind === "table") renderTable(body, block);
    });
  }

  function renderMethodology(methodology) {
    var section = getNode("methodology");
    var node = getNode("method-list");
    clear(node);
    asArray(methodology).forEach(function (item) {
      var li = createElement("li", "method-item");
      li.appendChild(createElement("strong", "", item.title + "："));
      li.appendChild(document.createTextNode(text(item.description)));
      node.appendChild(li);
    });
    section.hidden = !asArray(methodology).length;
  }

  function renderStatus(payload, errors) {
    var node = getNode("status-banner");
    var quality = isObject(payload.quality) ? payload.quality : {};
    var warnings = asArray(quality.warnings).map(String);
    node.classList.toggle("is-error", errors.length > 0);
    if (errors.length) {
      node.textContent = "报告 Payload 未通过校验：" + errors.join("；");
      node.hidden = false;
      return;
    }
    if (quality.status === "draft") {
      node.textContent = "模板待生成：请完成查询后一次性替换 #report-payload。";
      node.hidden = false;
      return;
    }
    if (warnings.length) {
      node.textContent = "数据提示：" + warnings.join("；");
      node.hidden = false;
      return;
    }
    node.hidden = true;
  }

  function renderReport(payload) {
    disposeCharts();
    var errors = validatePayload(payload);
    var report = isObject(payload.report) ? payload.report : {};
    document.title = text(report.title, "产品配置专题分析报告模板");
    getNode("report-title").textContent = text(report.title, "产品配置专题分析报告模板");
    getNode("report-subtitle").textContent = text(report.subtitle, "围绕一个具体问题生成轻量、可追溯的可视化专题报告");
    getNode("report-subtitle").hidden = !text(report.subtitle) && report.title;
    var summary = getNode("report-summary");
    summary.textContent = text(report.summary);
    summary.hidden = !text(report.summary);
    renderMeta(report);
    renderKpis(errors.length ? [] : payload.kpis);
    renderInsights(errors.length ? [] : payload.insights);
    renderBlocks(errors.length ? [] : payload.blocks);
    renderMethodology(errors.length ? [] : payload.methodology);
    getNode("footer-date").textContent = report.report_date ? "生成日期：" + report.report_date : "";
    renderStatus(payload, errors);
    document.documentElement.dataset.reportStatus = errors.length ? "invalid" : text(payload.quality && payload.quality.status, "draft");
    return errors;
  }

  function preferredTheme() {
    try {
      var stored = localStorage.getItem(THEME_STORAGE_KEY);
      if (stored === "light" || stored === "dark") return stored;
    } catch (_error) {}
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function applyTheme(theme, persist) {
    var resolved = theme === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", resolved);
    var button = getNode("theme-toggle");
    if (button) {
      button.textContent = resolved === "dark" ? "切换浅色" : "切换深色";
      button.setAttribute("aria-pressed", resolved === "dark" ? "true" : "false");
    }
    if (persist) {
      try { localStorage.setItem(THEME_STORAGE_KEY, resolved); } catch (_error) {}
    }
    refreshChartsForTheme();
  }

  function initialize() {
    applyTheme(preferredTheme(), false);
    var payload;
    try {
      payload = parseEmbeddedPayload();
      renderReport(payload);
    } catch (error) {
      var banner = getNode("status-banner");
      banner.hidden = false;
      banner.classList.add("is-error");
      banner.textContent = "无法读取报告 Payload：" + error.message;
      document.documentElement.dataset.reportStatus = "invalid";
    }
    getNode("theme-toggle").addEventListener("click", function () {
      var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      applyTheme(next, true);
    });
    window.addEventListener("resize", function () {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(function () {
        chartRecords.forEach(function (record) { record.instance.resize(); });
      }, 100);
    });
  }

  window.TopicProductConfigReport = {
    validate: validatePayload,
    render: renderReport,
    buildChartOption: buildChartOption,
    applyTheme: applyTheme
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize);
  else initialize();
})();
