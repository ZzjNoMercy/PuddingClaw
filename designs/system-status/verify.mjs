import { chromium } from 'playwright';

const url = process.argv[2] || 'http://localhost:57130/system-status/index.html';
const screenshotPath = process.argv[3] || 'designs/system-status/screenshot.png';

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

const errors = [];
page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));
page.on('console', (msg) => {
  if (msg.type() === 'error') errors.push(`console.error: ${msg.text()}`);
});

await page.goto(url, { waitUntil: 'networkidle' });
await page.waitForTimeout(1500);

await page.screenshot({ path: screenshotPath, fullPage: false });

await browser.close();

if (errors.length) {
  console.error('ERRORS:');
  errors.forEach((e) => console.error(e));
  process.exit(1);
} else {
  console.log(`OK: ${url}`);
  console.log(`Screenshot: ${screenshotPath}`);
}
