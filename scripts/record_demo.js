// Records a captioned screen video of the dashboard (no narration).
//   npm i playwright && node scripts/record_demo.js http://localhost:8000 out_dir
// Uses PLAYWRIGHT_BROWSERS_PATH chromium if present. Runs against whatever the server uses
// (real PayPal sandbox if the server has credentials, simulated otherwise): the badge in the
// page header shows which, so the video cannot misstate it.
const { chromium } = require('playwright');
const base = process.argv[2] || 'http://localhost:8000';
const outDir = process.argv[3] || 'demo_video';
const wait = ms => new Promise(r => setTimeout(r, ms));

const CSS = `
#cap{position:fixed;left:0;right:0;bottom:0;z-index:99999;background:rgba(10,14,22,.92);color:#fff;
 font:600 24px/1.35 system-ui,sans-serif;padding:16px 28px;text-align:center;min-height:84px;display:flex;align-items:center;justify-content:center}
#card{position:fixed;inset:0;z-index:100000;background:#0b1220;color:#fff;display:none;flex-direction:column;
 align-items:center;justify-content:center;text-align:center;padding:60px;font-family:system-ui,sans-serif}
#card h1{font-size:54px;margin:0 0 18px} #card p{font-size:28px;max-width:1000px;margin:6px 0;color:#c9d3e3;line-height:1.4}`;

(async () => {
  const exe = process.env.PLAYWRIGHT_BROWSERS_PATH ? process.env.PLAYWRIGHT_BROWSERS_PATH + '/chromium' : undefined;
  const browser = await chromium.launch(exe ? { executablePath: exe } : {});
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 }, recordVideo: { dir: outDir, size: { width: 1280, height: 720 } } });
  const page = await ctx.newPage();
  await page.goto(base);
  await page.addStyleTag({ content: CSS });
  await page.evaluate(() => {
    const c = document.createElement('div'); c.id = 'cap'; document.body.append(c);
    const k = document.createElement('div'); k.id = 'card'; document.body.append(k);
    document.body.style.paddingBottom = '90px';
  });
  const caption = async (t, ms) => { await page.evaluate(t => { document.getElementById('cap').textContent = t; }, t); await wait(ms); };
  const card = async (h, lines, ms) => {
    await page.evaluate(([h, lines]) => { const k = document.getElementById('card'); k.replaceChildren();
      const e = document.createElement('h1'); e.textContent = h; k.append(e);
      lines.forEach(l => { const p = document.createElement('p'); p.textContent = l; k.append(p); });
      k.style.display = 'flex'; }, [h, lines]);
    await wait(ms);
    await page.evaluate(() => { document.getElementById('card').style.display = 'none'; });
  };
  const click = async name => { await page.getByRole('button', { name }).click(); };
  const scroll = y => page.evaluate(y => window.scrollTo({ top: y, behavior: 'smooth' }), y);

  await card('Agent Flight Recorder', ['When an AI agent buys something and the purchase is disputed,', 'nobody can show what the agent was allowed to do.', 'This is that record.'], 9000);
  await caption('1 · A person signs a mandate: budget, per-order cap, named merchants, expiry. It is Ed25519-signed.', 7000);
  await caption('2 · The agent proposes a purchase. Plain code (no AI) checks it before any PayPal order exists.', 4000);
  await click('Buy an in-scope pair'); await wait(3500);
  await caption('Allowed. A PayPal order is created and the step is written to the hash-chained log.', 6000);
  await click('Injected listing tries to hijack the agent'); await wait(1500);
  await caption('A product listing tells the agent to buy five gift cards and ignore limits. The guard refuses, with reason codes.', 8000);
  await caption('Denied before PayPal: no order was created and PayPal was never called.', 5000);
  await click('Over the per-order cap'); await wait(1500);
  await caption('Over the cap: denied, with the exact rule that failed.', 5000);
  await click('Pricey pair, needs approval'); await wait(1500);
  await caption('Above the approval threshold: the person is asked. Approval re-runs the guard; it can never override a hard rule.', 7000);
  await page.getByRole('button', { name: 'Approve as the person' }).click(); await wait(3000);
  await scroll(500);
  await caption('3 · Every step is in an append-only log. Each event contains the hash of the one before it.', 7000);
  await click('Anchor head hash (independent timestamp)'); await wait(5000);
  await caption('The log head is stamped by an independent timestamp authority (RFC 3161), so edits before the stamp are caught by a third party.', 8000);
  await scroll(1000);
  await caption('4 · The evidence bundle holds the signed mandate and the full log. Anyone can verify it offline.', 6000);
  await click('Verify bundle offline'); await wait(1500); await scroll(99999); await wait(2500);
  await caption('Signature valid, every hash recomputed, timestamp checked.', 5500);
  await scroll(450); await wait(1500);
  await click('Edit one event in the database (demo)'); await wait(1500);
  await caption('Now someone edits one event directly in the database.', 4500);
  await click('Verify chain'); await wait(1500);
  await caption('Verification fails and points at the exact event that was changed.', 7000);
  await caption('Tamper-evident, not tamper-proof: events after the latest timestamp are not independently pinned.', 7000);
  await card('Measured, not claimed', [
    'Red-team suite: 27 of 27 attack scenarios blocked (2 in-scope cases allowed by design).',
    '209 of 209 single edits to an evidence bundle detected offline.',
    '5,000 random cases: guard matched an independent oracle every time.',
    'Dispute submission is built to PayPal\'s Disputes API but tested only against a fake: not yet run on a live sandbox dispute.',
    'PayPal sandbox only. Built with AI assistance (Claude Code).',
    'github.com/VenkateshPanda0/agent-flight-recorder'], 16000);
  await ctx.close(); await browser.close();
})();
