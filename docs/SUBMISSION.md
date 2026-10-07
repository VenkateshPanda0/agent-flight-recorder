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

### Challenges
- Making a hash chain verifiable without the database, which led to bundles that carry the whole chain.
- Being honest about limits: the log is tamper-evident, not tamper-proof. Removing the newest events is only detectable against a head hash saved elsewhere, so the head hash is written into each PayPal order.
- Sandbox constraints: the sandbox business account was not enabled for card payments, so capturing an order needs a sandbox buyer to approve it in a browser.

### Accomplishments (measured)
- 135 automated tests.
- Red-team suite: 27 of 27 attack scenarios blocked (prompt injection, inflated totals, lookalike merchants, replay, order splitting, forged mandates, and more). Two in-scope cases are intentionally not blocked and are reported. The scenarios were written by the same author as the guard, so this is a regression suite, not independent testing.
- 8 tamper cases against the offline verifier (edited event, reordered, removed, edited mandate, forged key, wrong key, truncated tail, edited summary).
- Real PayPal sandbox orders created end to end through the guard and ledger.

### What I learned
The interesting problem in agentic commerce is not getting an agent to pay; it is being able to show afterwards what it was allowed to do.

### What's next
Per-tenant chains, a published head-hash anchor, and mandate templates for common purchases.

### Built with
Python, cryptography (Ed25519), SQLite, PayPal REST APIs (Orders v2, Customer Disputes, Webhooks; sandbox), optionally the Anthropic API for the demo agent, Render.

### AI assistance
This project was built with AI assistance (Claude Code). Sandbox only; no real money moves.

## Video script (target 2:40, hard limit 3:00)
1. 0:00 Problem in one sentence: "When an AI agent buys something and it is disputed, nobody can show what it was allowed to do."
2. 0:15 Dashboard: the signed mandate. Point at budget, cap, merchants, signature.
3. 0:35 Click "Buy an in-scope pair": ALLOW, PayPal sandbox order id appears, log grows.
4. 1:05 Click "Injected listing": agent proposes five gift cards; DENY with four reason codes; "PayPal was never called".
5. 1:30 Dispute: build the evidence bundle; submit it to a real sandbox dispute (record only if the dispute is real; otherwise show the bundle and offline verification and say plainly it is not yet submitted).
6. 2:00 "Edit one event in the database", then Verify chain: it fails at the exact event. Say the head-hash limit in one sentence.
7. 2:30 Numbers: 135 tests, red-team 27/27, repo link.
Check the screen for secrets, emails and tokens before uploading. 1080p, public, "Not for Kids", upload at least a day early.

## Checklist
- [ ] Repo public, MIT licence present, README run steps tested on a clean checkout
- [ ] Live demo URL works (Render), seeded demo works from a clean browser
- [ ] Real sandbox dispute created and evidence accepted (or the description says it was not)
- [ ] Tools list matches what was really used (do not list APIMatic, Channel3 or AG Grid unless actually used)
- [ ] AI assistance disclosed
- [ ] Video under 3:00, public, Not for Kids, works logged out
- [ ] No secrets in history (scan before final push)
- [ ] Submitted by 9 Nov; confirmation page saved
