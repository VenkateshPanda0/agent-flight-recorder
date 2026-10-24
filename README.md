# Agent Flight Recorder

A flight recorder for purchases made by AI agents.

When an AI agent buys something on a person's behalf and the person later
disputes it, nobody can show what the agent was actually allowed to do.
Agent Flight Recorder closes that gap:

1. **Mandate.** The person grants an agent a narrow, expiring permission:
   a budget, a per-order cap, named merchants, allowed categories and a
   maximum number of orders. The mandate is signed, so it cannot be edited
   afterwards.
2. **Guard.** Every purchase the agent proposes is checked against the
   mandate by plain, deterministic code before any PayPal order is created.
   The verdict is allow, deny, or ask the person first, with reason codes.
3. **Recorder.** Every step is written to an append-only, hash-chained log,
   so later edits to the record are detectable.
4. **Evidence.** If the buyer disputes the purchase, the mandate and the log
   are assembled into evidence and submitted through PayPal's Disputes API.

Built for the PayPal AI Hackathon. Sandbox only; no real money moves.

## How it works

```mermaid
flowchart LR
    P[Person] -- signs --> M[Mandate<br/>budget, cap, merchants, expiry]
    A[AI agent] -- proposes purchase --> G{Guard<br/>deterministic code}
    M --> G
    G -- DENY + reason codes --> L[(Hash-chained log)]
    G -- ALLOW / approved --> O[PayPal Orders v2 sandbox]
    G -- NEEDS_APPROVAL --> P
    O --> L
    D[PayPal dispute / webhook] --> E[Evidence bundle<br/>mandate + full log + head hash]
    L --> E
    E -- provide-evidence --> D
    E --> V[Offline verifier]
```

## Status

| Part | State |
|---|---|
| Signed mandates (Ed25519, canonical JSON) | Done, tested |
| Guard with reason codes | Done, tested |
| Hash-chained log, derived budget state | Done, tested |
| PayPal client and purchase orchestrator | Done, tested with a fake transport |
| Real PayPal sandbox order (OAuth + Orders v2 create) | Verified, including through the dashboard (`scripts/sandbox_smoke.py`) |
| Evidence bundle + offline verifier | Done, tested with 8 tamper cases |
| Dispute evidence via Disputes API (multipart) | Written to the documented API; tested against a fake transport only. **Not yet run against a real sandbox dispute** (needs a sandbox buyer action) |
| Webhooks (signature verified, idempotent) | Done, tested with a fake transport; not yet registered with PayPal |
| Red-team suite | 27 attack scenarios, 27 blocked (see below) |
| Dashboard and demo server | Done |
| Shopping agent | Scripted proposer always; live-model proposer written, needs `ANTHROPIC_API_KEY` |

`scripts/sandbox_dispute_flow.py start|finish` runs the real order, capture,
dispute and evidence chain once a sandbox buyer has approved the order.

Order capture needs a sandbox buyer to approve the order in a browser, so
captured orders and live disputes are not exercised automatically.

## Run it

Requires Python 3.10 or newer.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m pytest                       # all tests
python scripts/redteam_report.py       # red-team block rate
DEMO_PAYPAL=simulated PYTHONPATH=backend python -m flightrecorder.server   # http://localhost:8000
```

On Windows PowerShell use `$env:DEMO_PAYPAL="simulated"; $env:PYTHONPATH="backend"; python -m flightrecorder.server`.
Dispute submission from the dashboard is off unless `ALLOW_DISPUTE_SUBMIT=1`
(so a public deployment cannot spend your PayPal credentials); demo actions are
rate limited. Without `DEMO_PAYPAL=simulated` and with sandbox credentials in `.env`, the
demo creates real PayPal **sandbox** orders; the page shows which mode is active.

## Verify a bundle yourself

Download a bundle from the dashboard, then, with no server and no network:

```bash
python scripts/verify_bundle.py evidence-<mandate>.json --key <public key> --head <saved head hash>
```

Without `--head`, removal of the newest events cannot be detected; without
`--key`, the signer is not checked. The script says so when either is missing.

## Accessibility

`node scripts/a11y_check.js <url>` runs axe-core (WCAG 2.0/2.1 A and AA plus
best-practice rules) on the dashboard in light, dark and phone width after
exercising it. Last run: 0 violations in all three, no horizontal overflow.
Automated checks catch only part of accessibility; this is not a full audit.

## Red-team results

`python scripts/redteam_report.py` runs adversarial scenarios through the real
orchestrator and counts a scenario as blocked only if no PayPal order call was
made beyond a single legitimate one. Current result: **27 of 27 attack
scenarios blocked** (prompt-injected rationale, inflated or under-reported
totals, negative and float prices, lookalike and homoglyph merchants, currency
swap, replay, expiry, revocation, order splitting, forged mandate). Two
scenarios inside the mandate's scope are **not** blocked, on purpose: an
overpriced but permitted item at an allowed merchant, and injected text in an
item name on an otherwise valid order. The mandate allowed those purchases.

Two further measured checks, both in the test suite:

- **Bundle tamper sweep:** every single-field edit, event deletion and adjacent
  swap on a real evidence bundle (209 edits in total) is detected by the offline
  verifier: **209/209**.
- **Guard vs. an independent oracle:** 5,000 seeded random mandates, intents and
  states (1,527 allow, 306 needs-approval, 3,167 deny). The guard's decision and
  its exact reason codes matched the oracle in all 5,000. The oracle is a second
  statement of the rules by the same author, so it catches implementation slips,
  not a wrong reading of the rules.

Caveat: the scenarios were written by the same author as the guard, so this
is a regression suite, not independent testing.

## Configuration

Copy `.env.example` to `.env` and fill in your own PayPal sandbox client ID
and secret from the PayPal developer dashboard. `.env` is ignored by git.

## Design notes

- **No AI model in the enforcement path.** A model proposes a purchase; the
  guard decides. The same inputs always give the same verdict.
- **Money is integer minor units** (cents), never floats, so what was signed
  is exactly what is checked.
- **The guard uses the total computed from the line items**, not the total
  the agent claims.
- **Fail closed.** Anything unverifiable or out of scope is denied.
- **Independent timestamp anchoring (RFC 3161).** `Anchor head hash` asks a public
  timestamp authority (DigiCert's free service) to sign the log's head hash and
  records the token in the log. Anyone can check it offline with `openssl`
  (`scripts/verify_bundle.py` does). Events up to an anchor cannot be edited or
  removed without contradicting a third party's signed time. The limits: events
  after the latest anchor are not pinned (the verifier reports how many), and if
  someone truncates a bundle to before its anchor, the anchor is simply absent,
  so keep the anchored hash somewhere else (the dispute notes carry it).
- **Tamper-evident, not tamper-proof.** A hash chain shows edits to past
  events. Removing the newest events is only detectable against a head hash
  saved somewhere else, which is why the head hash is meant to be published
  outside the database.

## Tools used

Built with AI assistance (Claude Code). Python standard library, `cryptography`
(Ed25519), SQLite, pytest; PayPal Orders v2, OAuth, Customer Disputes and
Webhooks (sandbox); optional Anthropic Messages API for the demo agent.

## Licence

MIT. See `LICENSE`.
