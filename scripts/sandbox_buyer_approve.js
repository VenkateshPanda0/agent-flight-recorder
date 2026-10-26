// Approves a PayPal SANDBOX order as a sandbox buyer, in a headless browser.
//   SANDBOX_BUYER_EMAIL=... SANDBOX_BUYER_PASSWORD=... node scripts/sandbox_buyer_approve.js <approve_url>
// Sandbox accounts only (fake money). Credentials come from the environment, are never printed,
// and the script refuses any URL that is not on sandbox.paypal.com.
const { chromium } = require('playwright');
const url = process.argv[2];
const email = process.env.SANDBOX_BUYER_EMAIL, password = process.env.SANDBOX_BUYER_PASSWORD;
if (!url || !/^https:\/\/www\.sandbox\.paypal\.com\//.test(url)) { console.error('Need a https://www.sandbox.paypal.com/ approve URL.'); process.exit(2); }
const checkOnly = process.argv.includes('--check-only');   // load the page and report whether the login form is present
if (!checkOnly && (!email || !password)) { console.error('Set SANDBOX_BUYER_EMAIL and SANDBOX_BUYER_PASSWORD.'); process.exit(2); }

(async () => {
  const exe = process.env.PLAYWRIGHT_BROWSERS_PATH ? process.env.PLAYWRIGHT_BROWSERS_PATH + '/chromium' : undefined;
  const browser = await chromium.launch(exe ? { executablePath: exe } : {});
  const page = await (await browser.newContext({ viewport: { width: 1200, height: 900 } })).newPage();
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  const emailBox = page.locator('#email, input[name="login_email"]').first();
  await emailBox.waitFor({ timeout: 45000 }).catch(() => {});
  const hasLogin = await emailBox.isVisible().catch(() => false);
  if (checkOnly) { console.log(hasLogin ? 'login form present' : 'login form NOT found: ' + page.url()); await browser.close(); process.exit(hasLogin ? 0 : 1); }
  if (!hasLogin) { console.error('Login form not found (page: ' + page.url().split('?')[0] + ')'); await browser.close(); process.exit(1); }
  await emailBox.fill(email);
  const next = page.locator('#btnNext');
  if (await next.isVisible().catch(() => false)) await next.click();
  const pw = page.locator('#password, input[name="login_password"]').first();
  await pw.waitFor({ timeout: 30000 });
  await pw.fill(password);
  await page.locator('#btnLogin').click();
  // Review page: the final button is "Complete Purchase", "Pay Now" or "Continue", depending on the flow.
  const finish = page.getByRole('button', { name: /complete purchase|pay now|agree and pay|continue/i }).first();
  await finish.waitFor({ timeout: 60000 });
  await finish.click();
  await page.waitForTimeout(8000);
  console.log('approved (final page: ' + page.url().split('?')[0] + ')');
  await browser.close();
})().catch(e => { console.error('failed:', e.message.split('\n')[0]); process.exit(1); });
