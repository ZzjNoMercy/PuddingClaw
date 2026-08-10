import assert from "node:assert/strict";
import test from "node:test";

// @ts-ignore Node's native TypeScript runner requires the source suffix.
import { MAX_LIGHTWEIGHT_HTML_BYTES, extractLightweightHtmlTitle, parseLightweightHtmlDocument } from "./lightweightHtml.ts";

test("recognizes a complete HTML fenced document", () => {
  const result = parseLightweightHtmlDocument(
    "language-html",
    '<!DOCTYPE html><html><head><title>临时趋势图</title></head><body><canvas></canvas></body></html>\n',
  );

  assert.deepEqual(result, {
    html: '<!DOCTYPE html><html><head><title>临时趋势图</title></head><body><canvas></canvas></body></html>',
    title: "临时趋势图",
  });
});

test("accepts an html root and decodes a safe display title", () => {
  const html = '<html lang="zh"><head><title>A &amp; B &#x56FE;&#34920;</title></head><body></body></html>';
  assert.equal(parseLightweightHtmlDocument("foo language-HTML bar", html)?.title, "A & B 图表");
  assert.equal(extractLightweightHtmlTitle("<html></html>"), "临时 HTML 预览");
});

test("leaves fragments, unfinished streams, and other languages as code", () => {
  assert.equal(parseLightweightHtmlDocument("language-html", "<div>chart</div>"), null);
  assert.equal(parseLightweightHtmlDocument("language-html", "<!doctype html><html><body>loading"), null);
  assert.equal(parseLightweightHtmlDocument("language-js", "<html></html>"), null);
});

test("leaves oversized HTML as code", () => {
  const html = `<html><body>${"x".repeat(MAX_LIGHTWEIGHT_HTML_BYTES)}</body></html>`;
  assert.equal(parseLightweightHtmlDocument("language-html", html), null);
});
