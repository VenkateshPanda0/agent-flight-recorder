# Submission kit (draft; every claim here is true today and must be re-checked on submission day)

## Devpost text

### Inspiration
AI agents are starting to buy things on people's behalf. When one of those purchases is disputed, the dispute process has no record of what the agent was actually permitted to do. I wanted to build that record.

### What it does
Agent Flight Recorder sits between an AI shopping agent and PayPal.
1. **Mandate.** The person signs a narrow, expiring permission: budget, per-order cap, named merchants, allowed categories, order limit, optional approval threshold.
2. **Guard.** Plain deterministic code checks every proposed purchase before any PayPal order exists. The answer is ALLOW, NEEDS_APPROVAL or DENY, with reason codes. No AI model is in the enforcement path.
3. **Recorder.** Every step is written to an append-only SHA-256 hash-chained log.
4. **Evidence.** For a dispute, the signed mandate and the full log are packaged into a bundle that anyone can verify offline, and sent through PayPal's Customer Disputes API.

### How I built it
Python standard library plus `cryptography` (Ed25519). PayPal Orders v2 and OAuth against the sandbox, Customer Disputes `provide-evidence` (multipart), and webhook signature verification. Money is integer minor units, never floats. A single-page dashboard shows the mandate, the guard's verdicts and the hash chain, with a Verify button.

### Anchoring
The log head hash can be stamped by an independent RFC 3161 timestamp authority, so edits to anything before the stamp are caught by a third party's signature, and the offline verifier reports how many newer events are not yet pinned.

### Challenges
- Making a hash chain verifiable without the database, which led to bundles that carry the whole chain.
- Being honest about limits: the log is tamper-evident, not tamper-proof. Removing the newest events is only detectable against a head hash saved elsewhere, so the head hash is written into each PayPal order.
- Sandbox constraints: the sandbox business account was not enabled for card payments, so capturing an order needs a sandbox buyer to approve it in a browser.

### Accomplishments (measured)
- 145 automated tests.
- Red-team suite: 27 of 27 attack scenarios blocked (prompt injection, inflated totals, lookalike merchants, replay, order splitting, forged mandates, and more). Two in-scope cases are intentionally not blocked and are reported. The scenarios were written by the same author as the guard, so this is a regression suite, not independent testing.
- Tamper sweep: every single-field edit, deletion and swap on a real bundle (209 edits) is caught by the offline verifier, plus named cases for a forged key, wrong key and truncated tail.
- 5,000 seeded random cases: the guard's decision and reason codes matched an independent oracle every time.
- Real PayPal sandbox orders created end to end through the guard and ledger.
- A real AI model in the loop: a small open model (Qwen2.5-1.5B-Instruct, local) reading catalogues with hidden injections proposed something outside the mandate in 15 of 24 runs; the guard denied all 15 and made 0 PayPal order calls. Small sample, my own injection wordings.
- Dispute evidence submission is implemented against PayPal's documented Disputes API but has only been tested against a fake transport, not a live sandbox dispute. Say this plainly in the final text unless a real dispute is run first.

### What I learned
The interesting problem in agentic commerce is not getting an agent to pay; it is being able to show afterwards what it was allowed to do.

### What's next
Per-tenant chains, a published head-hash anchor, and mandate templates for common purchases.

### Built with
Python, cryptography (Ed25519), SQLite, PayPal REST APIs (Orders v2, Customer Disputes, Webhooks; sandbox), llama.cpp with Qwen2.5-1.5B-Instruct (Apache-2.0) as the live demo agent.

### AI assistance
This project was built with AI assistance (Claude Code). Sandbox only; no real money moves.

## Video script (target 2:40, hard limit 3:00)
1. 0:00 Problem in one sentence: "When an AI agent buys something and it is disputed, nobody can show what it was allowed to do."
2. 0:15 Dashboard: the signed mandate. Point at budget, cap, merchants, signature.
3. 0:35 Click "Buy an in-scope pair": ALLOW, PayPal sandbox order id appears, log grows.
4. 1:05 Click "Injected listing": agent proposes five gift cards; DENY with four reason codes; "PayPal was never called".
5. 1:30 Dispute: build the evidence bundle; submit it to a real sandbox dispute (record only if the dispute is real; otherwise show the bundle and offline verification and say plainly it is not yet submitted).
6. 2:00 "Edit one event in the database", then Verify chain: it fails at the exact event. Press "Anchor head hash" before the tamper step to show the independent timestamp, then say the limit in one sentence: events after the latest anchor are not pinned.
7. 2:30 Numbers: 145 tests, red-team 27/27, repo link.
Check the screen for secrets, emails and tokens before uploading. 1080p, public, "Not for Kids", upload at least a day early.

## Voiceover
`docs/narration.json` is the spoken script (about 1:53 of speech). The voice is Piper's `en_US-joe-medium`, licensed CC0, synthesized locally with `scripts/make_voiceover.py`; say "synthesized voice" in the Devpost description if asked. To re-record: start the server, run `node scripts/record_demo.js <url> <dir> narration_timed.json`, then `python scripts/make_voiceover.py docs/narration.json voice.onnx <dir> <dir>/video.webm out.mp4`.

## Checklist
- [ ] Repo public, MIT licence present, README run steps tested on a clean checkout
- [ ] Live demo URL works (Render), seeded demo works from a clean browser
- [ ] Real sandbox dispute created and evidence accepted (or the description says it was not)
- [ ] Tools list matches what was really used (do not list APIMatic, Channel3 or AG Grid unless actually used)
- [ ] AI assistance disclosed
- [ ] Video under 3:00, public, Not for Kids, works logged out
- [ ] No secrets in history (scan before final push)
- [ ] Submitted by 9 Nov; confirmation page saved

## Devpost "main technology requirement" answer (paste this)
PayPal: the project uses the PayPal developer platform's free sandbox. OAuth 2.0 to authenticate; Orders v2 (create order) to place the purchases an AI agent is allowed to make, after a deterministic guard approves them; the Customer Disputes API (provide-evidence, multipart) to submit the evidence bundle for a disputed purchase (implemented to the documented API and tested against a fake transport; not yet run on a live sandbox dispute); and Webhooks signature verification (verify-webhook-signature) for dispute and payment events (implemented and tested against a fake; not registered).
AI: the shopping agent is an AI model. In the live demo a real open model, Qwen2.5-1.5B-Instruct (Apache-2.0), runs locally through llama.cpp, reads a product catalogue and proposes a purchase. The product's whole purpose is to govern what AI agents are allowed to buy: the model proposes, deterministic code (no AI) decides. In 24 live runs against catalogues with hidden prompt-injection text, the model proposed an out-of-mandate purchase 15 times, and the guard denied all 15 before any PayPal order existed. The project was also built with AI assistance (Claude Code).
