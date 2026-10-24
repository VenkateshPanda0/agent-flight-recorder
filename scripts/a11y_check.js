// Accessibility audit (axe-core, WCAG 2.0/2.1 A+AA + best-practice) in light, dark and phone width.
//   npm i playwright axe-core && node scripts/a11y_check.js http://localhost:8000/
const { chromium } = require('playwright');
const axe = require('axe-core');
(async () => {
  const b = await chromium.launch({ ...(process.env.PLAYWRIGHT_BROWSERS_PATH ? { executablePath: process.env.PLAYWRIGHT_BROWSERS_PATH + '/chromium' } : {}) });
  for (const [scheme, w] of [['light', 1200], ['dark', 1200], ['light', 390]]) {
    const ctx = await b.newContext({ colorScheme: scheme, viewport: { width: w, height: 900 } });
    const p = await ctx.newPage();
    await p.goto((process.argv[2] || 'http://localhost:8000/') + '');
    await p.getByRole('button', { name: 'Buy an in-scope pair' }).click(); await p.waitForTimeout(600);
    await p.getByRole('button', { name: 'Injected listing tries to hijack the agent' }).click(); await p.waitForTimeout(600);
    await p.getByRole('button', { name: 'Verify chain' }).click(); await p.waitForTimeout(400);
    await p.addScriptTag({ content: axe.source });
    const r = await p.evaluate(() => axe.run(document, { runOnly: ['wcag2a', 'wcag2aa', 'wcag21aa', 'best-practice'] }));
    const overflow = await p.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    console.log(scheme, w, 'violations:', r.violations.length, 'horizontal overflow:', overflow);
    r.violations.forEach(v => console.log('  -', v.id, v.impact, v.nodes.length, 'nodes:', v.nodes.slice(0, 2).map(n => n.target.join(' ')).join(' | ')));
    if (w === 390) await p.screenshot({ path: 'mobile.png', fullPage: true });
    await ctx.close();
  }
  await b.close();
})();
