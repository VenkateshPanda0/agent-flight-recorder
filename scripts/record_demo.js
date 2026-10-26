// Records a captioned screen video of the dashboard. If a narration timing file is given,
// every scene lasts as long as its spoken line and the start time of each line is written
// to <out_dir>/timings.json so scripts/make_voiceover.py can align the audio.
//   node scripts/record_demo.js http://localhost:8000 out_dir [narration_timed.json]
// The page's PayPal badge shows whether the server used the real sandbox or a simulation,
// so the footage cannot misstate it.
const { chromium } = require('playwright');
const fs = require('fs');
const base = process.argv[2] || 'http://localhost:8000';
const outDir = process.argv[3] || 'demo_video';
const narr = process.argv[4] ? Object.fromEntries(JSON.parse(fs.readFileSync(process.argv[4])).map(s => [s.id, s.dur])) : {};
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
  const t0 = Date.now();
  const page = await ctx.newPage();
  const timings = {};
  await page.goto(base);
  await page.addStyleTag({ content: CSS });
  await page.evaluate(() => {
    const c = document.createElement('div'); c.id = 'cap'; document.body.append(c);
    const k = document.createElement('div'); k.id = 'card'; document.body.append(k);
    document.body.style.paddingBottom = '90px';
  });
  const setCaption = t => page.evaluate(t => { document.getElementById('cap').textContent = t; }, t);
  const showCard = (h, lines) => page.evaluate(([h, lines]) => { const k = document.getElementById('card'); k.replaceChildren();
    const e = document.createElement('h1'); e.textContent = h; k.append(e);
    lines.forEach(l => { const p = document.createElement('p'); p.textContent = l; k.append(p); });
    k.style.display = 'flex'; }, [h, lines]);
  const hideCard = () => page.evaluate(() => { document.getElementById('card').style.display = 'none'; });
  const click = name => page.getByRole('button', { name }).click();
  const scroll = y => page.evaluate(y => window.scrollTo({ top: y, behavior: 'smooth' }), y);

  // One scene: mark when the spoken line starts, run the actions, then hold until the line ends.
  const scene = async (id, caption, actions, fallbackMs = 6000, extraS = 0) => {
    const start = Date.now();
    timings[id] = (start - t0) / 1000;
    if (caption) await setCaption(caption);
    const total = ((narr[id] ?? fallbackMs / 1000) + 0.8 + extraS) * 1000;
    await actions({ at: async s => { const d = s * 1000 - (Date.now() - start); if (d > 0) await wait(d); } });
    const rest = total - (Date.now() - start);
    if (rest > 0) await wait(rest);
  };

  await showCard('Agent Flight Recorder', ['When an AI agent buys something and the purchase is disputed,', 'nobody can show what the agent was allowed to do.', 'This is that record.']);
  await scene('intro', '', async () => {});
  await hideCard();
  await scene('mandate', '1 · A person signs a mandate: budget, per-order cap, named merchants, expiry. It is Ed25519-signed.', async () => {});
  await scene('buy', '2 · The agent proposes a purchase. Plain code (no AI) checks it before any PayPal order exists.', async s => { await s.at(3); await click('Buy an in-scope pair'); });
  await scene('live', 'A real AI model (small open model, running locally) reads a poisoned listing. The guard decides.', async s => { await s.at(2); await click('A live AI model shops'); }, 6000, 4);
  await scene('cap', 'Over the cap: denied, with the exact rule that failed.', async () => { await click('Over the per-order cap'); });
  await scene('approve', 'Above the approval threshold the person is asked. Approval re-runs the guard.', async s => { await click('Pricey pair, needs approval'); await s.at(3.5); await page.getByRole('button', { name: 'Approve as the person' }).click(); });
  await scene('log', '3 · Append-only log: each event contains the hash of the one before it.', async () => { await scroll(500); });
  await scene('anchor', 'The log head is stamped by an independent timestamp authority (RFC 3161).', async () => { await click('Anchor head hash (independent timestamp)'); });
  await scene('evidence', '4 · Evidence bundle: signed mandate + full log, verifiable offline.', async s => { await scroll(1000); await s.at(2); await click('Verify bundle offline'); await s.at(3.5); await scroll(99999); });
  await scene('tamper', 'Someone edits one event in the database. Verification fails at that exact event.', async s => { await scroll(450); await s.at(1.5); await click('Edit one event in the database (demo)'); await s.at(6); await click('Verify chain'); });
  await showCard('Measured, not claimed', [
    'Red-team suite: 27 of 27 attack scenarios blocked (2 in-scope cases allowed by design).',
    '209 of 209 single edits to an evidence bundle detected offline.',
    'Real local AI model vs. poisoned catalogues: it strayed in 15 of 24 runs; the guard denied all 15, 0 PayPal orders.',
    'Dispute submission is built to PayPal\'s Disputes API but tested only against a fake: not yet run on a live sandbox dispute.',
    'PayPal sandbox only. Built with AI assistance (Claude Code).',
    'github.com/VenkateshPanda0/agent-flight-recorder']);
  await scene('outro', '', async () => {}, 16000);
  fs.writeFileSync(outDir + '/timings.json', JSON.stringify(timings, null, 1));
  await ctx.close(); await browser.close();
})();
