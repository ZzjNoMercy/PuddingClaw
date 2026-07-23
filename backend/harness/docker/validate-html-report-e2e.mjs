#!/usr/bin/env node

import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { isAbsolute, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const input = process.argv[2];
if (!input || process.argv.length !== 3) {
  console.error("Usage: validate-html-report-e2e.mjs <report.html>");
  process.exit(2);
}

const reportPath = resolve(process.cwd(), input);
if (!reportPath.toLowerCase().endsWith(".html") && !reportPath.toLowerCase().endsWith(".htm")) {
  console.error("The validator operand must be an HTML file.");
  process.exit(2);
}

const sleep = (milliseconds) => new Promise((resolvePromise) => {
  setTimeout(resolvePromise, milliseconds);
});

async function waitForDevToolsPort(profileDir, child) {
  const portFile = resolve(profileDir, "DevToolsActivePort");
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (child.exitCode !== null) {
      throw new Error(`Chromium exited before DevTools became ready (${child.exitCode})`);
    }
    try {
      const [port] = (await readFile(portFile, "utf8")).trim().split(/\r?\n/u);
      if (port) return Number(port);
    } catch {
      // Chromium creates the file asynchronously.
    }
    await sleep(50);
  }
  throw new Error("Timed out waiting for Chromium DevTools port");
}

async function main() {
  const profileDir = await mkdtemp(resolve(tmpdir(), "puddingclaw-html-e2e-"));
  const chromium = process.env.PUPPETEER_EXECUTABLE_PATH || "/usr/bin/chromium";
  const child = spawn(
    chromium,
    [
      "--headless=new",
      "--no-sandbox",
      "--disable-gpu",
      "--disable-dev-shm-usage",
      "--allow-file-access-from-files",
      "--remote-debugging-address=127.0.0.1",
      "--remote-debugging-port=0",
      `--user-data-dir=${profileDir}`,
      "about:blank",
    ],
    { stdio: ["ignore", "ignore", "pipe"] },
  );
  let browserStderr = "";
  child.stderr.on("data", (chunk) => {
    browserStderr += String(chunk);
  });

  try {
    const port = await waitForDevToolsPort(profileDir, child);
    const targets = await fetch(`http://127.0.0.1:${port}/json/list`).then((response) => response.json());
    const pageTarget = targets.find((item) => item.type === "page");
    if (!pageTarget?.webSocketDebuggerUrl) {
      throw new Error("Chromium did not expose a page target");
    }

    const socket = new WebSocket(pageTarget.webSocketDebuggerUrl);
    await new Promise((resolvePromise, rejectPromise) => {
      socket.addEventListener("open", resolvePromise, { once: true });
      socket.addEventListener("error", rejectPromise, { once: true });
    });

    let nextId = 1;
    const pending = new Map();
    const runtimeErrors = [];
    const consoleErrors = [];
    const networkErrors = [];
    let loadResolved;
    const loaded = new Promise((resolvePromise) => {
      loadResolved = resolvePromise;
    });

    socket.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (message.id && pending.has(message.id)) {
        const { resolve: resolveCommand, reject } = pending.get(message.id);
        pending.delete(message.id);
        if (message.error) reject(new Error(message.error.message || "CDP command failed"));
        else resolveCommand(message.result || {});
        return;
      }
      if (message.method === "Page.loadEventFired") loadResolved();
      if (message.method === "Runtime.exceptionThrown") {
        runtimeErrors.push(
          message.params?.exceptionDetails?.exception?.description
          || message.params?.exceptionDetails?.text
          || "Runtime.exceptionThrown",
        );
      }
      if (
        message.method === "Runtime.consoleAPICalled"
        && message.params?.type === "error"
      ) {
        consoleErrors.push(
          (message.params.args || []).map((item) => item.value || item.description || "").join(" "),
        );
      }
      if (
        message.method === "Log.entryAdded"
        && message.params?.entry?.level === "error"
      ) {
        consoleErrors.push(message.params.entry.text || "Browser log error");
      }
      if (message.method === "Network.loadingFailed") {
        networkErrors.push(
          `${message.params?.type || "resource"}: ${message.params?.errorText || "loading failed"}`,
        );
      }
      if (
        message.method === "Network.responseReceived"
        && Number(message.params?.response?.status || 0) >= 400
      ) {
        networkErrors.push(
          `${message.params.response.status}: ${message.params.response.url}`,
        );
      }
    });

    const send = (method, params = {}) => new Promise((resolveCommand, reject) => {
      const id = nextId;
      nextId += 1;
      pending.set(id, { resolve: resolveCommand, reject });
      socket.send(JSON.stringify({ id, method, params }));
    });

    await Promise.all([
      send("Page.enable"),
      send("Runtime.enable"),
      send("Log.enable"),
      send("Network.enable"),
    ]);
    await send("Page.navigate", { url: pathToFileURL(reportPath).href });
    await Promise.race([
      loaded,
      sleep(10_000).then(() => {
        throw new Error("Timed out waiting for the HTML load event");
      }),
    ]);
    await sleep(1_500);

    const evaluation = await send("Runtime.evaluate", {
      expression: `(() => {
        const containers = Array.from(document.querySelectorAll('.echart'));
        const initialized = containers.filter((element) => {
          if (element.getAttribute('_echarts_instance_')) return true;
          return Boolean(
            window.echarts
            && typeof window.echarts.getInstanceByDom === 'function'
            && window.echarts.getInstanceByDom(element)
          );
        });
        const rendered = initialized.filter((element) => (
          Boolean(element.querySelector('canvas,svg'))
        ));
        const requiredYears = (document.body?.dataset.e2eRequiredYears || "")
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean);
        const requiredCutoff = (document.body?.dataset.e2eCutoff || "").trim();
        const selectorOptions = Object.fromEntries(
          Array.from(document.querySelectorAll("select[id]")).map((select) => [
            select.id,
            Array.from(select.options).map((option) => String(option.value || option.textContent || "").trim()),
          ]),
        );
        const cutoffText = String(
          document.querySelector("[data-cutoff],#cutoff")?.textContent || "",
        ).trim();
        return {
          title: document.title,
          readyState: document.readyState,
          bodyTextLength: (document.body?.innerText || '').trim().length,
          echartsAvailable: Boolean(window.echarts),
          chartContainerCount: containers.length,
          initializedChartCount: initialized.length,
          renderedChartCount: rendered.length,
          uninitializedChartIds: containers
            .filter((element) => !initialized.includes(element))
            .map((element) => element.id || '(missing-id)'),
          scriptSources: Array.from(document.scripts).map((script) => script.src || '(inline)'),
          requiredYears,
          requiredCutoff,
          selectorOptions,
          cutoffText,
        };
      })()`,
      returnByValue: true,
    });
    const page = evaluation.result?.value || {};
    const failures = [];
    if (page.readyState !== "complete") failures.push(`readyState=${page.readyState || "unknown"}`);
    if (!page.bodyTextLength) failures.push("document body has no visible text");
    if (page.chartContainerCount > 0 && !page.echartsAvailable) {
      failures.push("ECharts is unavailable");
    }
    if (page.initializedChartCount !== page.chartContainerCount) {
      failures.push(
        `${page.chartContainerCount - page.initializedChartCount} chart container(s) were not initialized`,
      );
    }
    if (page.renderedChartCount !== page.chartContainerCount) {
      failures.push(
        `${page.chartContainerCount - page.renderedChartCount} chart container(s) produced no canvas/svg surface`,
      );
    }
    if ((page.requiredYears || []).length > 0) {
      const selectors = Object.values(page.selectorOptions || {});
      if (!selectors.some((values) => (
        JSON.stringify(values) === JSON.stringify(page.requiredYears)
      ))) {
        failures.push(
          `no selector exactly matches required years ${JSON.stringify(page.requiredYears)}`,
        );
      }
    }
    if (page.requiredCutoff && page.cutoffText !== page.requiredCutoff) {
      failures.push(
        `cutoff=${page.cutoffText || "(missing)"}; expected ${page.requiredCutoff}`,
      );
    }
    failures.push(...runtimeErrors, ...consoleErrors, ...networkErrors);

    const artifactPaths = [reportPath];
    for (const source of page.scriptSources || []) {
      if (!String(source).startsWith("file:")) continue;
      const localPath = fileURLToPath(source);
      if (!artifactPaths.includes(localPath)) artifactPaths.push(localPath);
    }
    const artifactHashes = [];
    for (const artifactPath of artifactPaths) {
      const bytes = await readFile(artifactPath);
      artifactHashes.push({
        path: artifactPath,
        content_sha256: `sha256:${createHash("sha256").update(bytes).digest("hex")}`,
      });
    }

    const result = {
      passed: failures.length === 0,
      reportPath,
      page,
      runtimeErrors,
      consoleErrors,
      networkErrors,
      artifactHashes,
      failures,
    };
    console.log(JSON.stringify(result));
    socket.close();
    return result.passed ? 0 : 1;
  } finally {
    if (child.exitCode === null) {
      child.kill("SIGTERM");
      await Promise.race([
        new Promise((resolvePromise) => {
          child.once("exit", resolvePromise);
        }),
        sleep(2_000),
      ]);
    }
    await rm(profileDir, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
    if (child.exitCode !== null && child.exitCode !== 0 && browserStderr.trim()) {
      console.error(browserStderr.trim());
    }
  }
}

try {
  process.exitCode = await main();
} catch (error) {
  console.error(JSON.stringify({
    passed: false,
    reportPath: isAbsolute(reportPath) ? reportPath : resolve(reportPath),
    failures: [error instanceof Error ? error.message : String(error)],
  }));
  process.exitCode = 1;
}
