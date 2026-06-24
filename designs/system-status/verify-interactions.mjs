import { chromium } from 'playwright';

const url = process.argv[2] || 'http://localhost:57130/system-status/index.html';

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

const errors = [];
page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));
page.on('console', (msg) => {
  if (msg.type() === 'error') errors.push(`console.error: ${msg.text()}`);
});

await page.goto(url, { waitUntil: 'networkidle' });
await page.waitForTimeout(800);

// Expand AI Gateway details
const gatewayCard = await page.locator('text=AI Gateway (Higress)').locator('..').locator('..').locator('..');
await page.click('text=AI Gateway (Higress)');
await page.waitForTimeout(400);

// Open tweaks panel
await page.click('text=调试面板');
await page.waitForTimeout(300);

// Toggle Milvus off then on
await page.locator('label:has-text("Milvus 向量库") input[type="checkbox"]').uncheck();
await page.waitForTimeout(300);
await page.locator('label:has-text("Milvus 向量库") input[type="checkbox"]').check();
await page.waitForTimeout(300);

// Click refresh
await page.click('text=立即刷新');
await page.waitForTimeout(1200);

await page.screenshot({ path: 'designs/system-status/screenshot-interactions.png', fullPage: false });

await browser.close();

if (errors.length) {
  console.error('ERRORS:');
  errors.forEach((e) => console.error(e));
  process.exit(1);
} else {
  console.log('Interactions OK');
}
