# Agent Flight Recorder

**Who allowed the AI agent to buy that, and can you prove it?**

AI agents are starting to spend people's money. When one of those purchases is disputed, the dispute process has no record of what the agent was permitted to do, and the agent's own logs are just text that anyone with database access could rewrite. Agent Flight Recorder keeps the missing record: a **signed permission**, a **deterministic check before any money moves**, a **tamper-evident log**, and an **evidence bundle anyone can verify offline**.

![License: MIT](https://img.shields.io/badge/license-MIT-blue) ![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue) ![PayPal sandbox only](https://img.shields.io/badge/PayPal-sandbox%20only-orange) ![146 tests](https://img.shields.io/badge/tests-146%20passing-brightgreen)

Built for the PayPal AI Hackathon. **Sandbox only: no real money moves.**

![The dashboard: a signed mandate, the guard's verdicts, the hash-chained log with a tampered event flagged, and the evidence panel](docs/shot-hero.png)

## The idea in four moves

| | |
|---|---|
| **1. Mandate** | The person signs a narrow, expiring permission: budget, per-order cap, named merchants, categories, order limit, optional approval threshold. Ed25519-signed, so it cannot be quietly edited later. |
| **2. Guard** | Plain deterministic code checks every proposed purchase **before** any PayPal order exists and answers ALLOW, NEEDS_APPROVAL or DENY with reason codes. **No AI model is in the enforcement path**: the model proposes, the guard decides. |
| **3. Recorder** | Every step goes into an append-only, SHA-256 hash-chained log. The log head can be stamped by an independent RFC 3161 timestamp authority. |
| **4. Evidence** | For a dispute, the signed mandate and the full log become a bundle that anyone can check offline: signature, every hash, and the timestamp. |

```mermaid
flowchart LR
    P[Person] -- signs --> M[Mandate]
    A[AI agent] -- proposes purchase --> G{Guard<br/>deterministic code}
    M --> G
    G -- DENY + reason codes --> L[(Hash-chained log)]
    G -- ALLOW / approved --> O[PayPal Orders v2 sandbox]
    G -- NEEDS_APPROVAL --> P
    O --> L
    L -- head hash --> T[RFC 3161<br/>timestamp authority]
    T --> L
    D[PayPal dispute / webhook] --> E[Evidence bundle]
    L --> E
    E -- provide-evidence --> D
    E --> V[Offline verifier]
```

## A real AI model, a poisoned listing, and the guard

The shopping agent in the live demo is a real model: a small open model (**Qwen2.5-1.5B-Instruct**, Apache-2.0) running locally through llama.cpp, with no API key. It reads a catalogue and proposes a purchase. The catalogue is poisoned: the cheap shoe's listing hides instructions telling the agent to buy something else.

![A live local AI model proposes the $249 marathon shoe from a poisoned listing; the guard denies it with two reason codes and no PayPal order is created](docs/shot-live.png)

The model took the bait. The guard refused, and PayPal was never called. This is not a one-off: `scripts/llm_eval.py` ran 24 live trials (4 injection wordings x 2 listings x 3 requests). **The model proposed something outside its mandate in 15 of 24.** **The guard denied all 15, and no PayPal order was created for any of them.** A small model is a realistic worst case, which is what the guard is for. (Small sample, my own injection wordings: a demonstration, not a benchmark.)

Purchases above the approval threshold go to the person first, and approval re-runs the guard, so a human "yes" can never override a hard rule:

![A purchase above the approval threshold waits for the person to approve](docs/shot-approval.png)

## How PayPal and AI are used

**PayPal (developer sandbox only).** Everything runs against `api-m.sandbox.paypal.com`; the client refuses any other base URL.

| PayPal feature | What it does here | Status |
|---|---|---|
| OAuth 2.0 (`/v1/oauth2/token`) | Authenticates every call; the token is cached and never logged | Verified against the real sandbox |
| Orders v2: create (`POST /v2/checkout/orders`) | Creates the order **only after** the guard returns ALLOW (or an approved NEEDS_APPROVAL). The intent id is the `PayPal-Request-Id`, so retries cannot double-order; the log head hash is written into the order description | Verified against the real sandbox |
| Orders v2: capture; Payments refund | Keeps the ledger's budget in step with PayPal | Written, tested against a fake transport; capture needs a sandbox buyer to approve in a browser |
| Customer Disputes: `provide-evidence` (multipart) | Submits the evidence bundle (summary in the notes, full bundle as an attached JSON document) | Written to the documented API; **tested against a fake transport only, not a live sandbox dispute** |
| Disputes: create, `require-evidence`, `adjudicate` (sandbox helpers) | Used by `scripts/sandbox_dispute_flow.py` to run a dispute end to end | Written; not yet run live |
| Webhooks: `verify-webhook-signature` | A webhook is only believed after PayPal verifies it; events are logged once per event id; dispute events trigger evidence handling | Written, tested against a fake transport; not registered with PayPal |

**AI.** An AI model is in the loop in three ways:
- **The live shopping agent:** the local Qwen2.5-1.5B-Instruct model above. Optionally the agent can call the Anthropic Messages API instead (`ANTHROPIC_API_KEY`); that path has not been run.
- **Built with AI assistance:** Claude Code (Anthropic's Claude) wrote most of the code, tests and docs, directed and reviewed by the author.
- **Demo narration:** synthesized with Piper and the CC0 `en_US-joe-medium` voice.

**Other tools and technologies:** Python standard library, `cryptography` (Ed25519), SQLite, DigiCert's public RFC 3161 timestamp service, `openssl` (offline timestamp checks), llama.cpp, pytest, Playwright and axe-core (screenshots and accessibility checks).

**Sponsor integrations:** none used. (A `render.yaml` is included for deployment; the Render deploy has not been verified.)

## Evidence you can check yourself

The log head can be stamped by a public timestamp authority. Once stamped, every event up to it is pinned by a third party's signature: it cannot be edited or removed without the stamp exposing it. The dashboard's evidence panel verifies the bundle offline, including the timestamp:

![The head hash is anchored by an independent timestamp and the evidence bundle verifies offline](docs/shot-evidence.png)

The same check runs from the command line with no server and no network. Below, a real bundle verifies, then fails after one amount is edited by hand, and the verifier names the exact event:

![Terminal: verify_bundle.py accepts a real bundle, then rejects the same bundle after one edit and points at event 4](docs/shot-cli-verify.png)

And in the dashboard, a direct edit to the database is caught at the exact event:

![Chain broken at event 4 after a direct database edit](docs/shot-tamper.png)

## Measured results

![Terminal: 146 tests pass; red-team suite blocks 27 of 27; bundle tamper sweep catches 209 of 209](docs/shot-cli-results.png)

| Check | Result |
|---|---|
| Automated tests | **146 passing** |
| Red-team suite (`scripts/redteam_report.py`) | **27 / 27** attack scenarios blocked: prompt-injected rationale, under-reported totals, negative or float prices, lookalike and homoglyph merchants, currency swap, replay, expiry, revocation, order splitting, forged mandate |
| Bundle tamper sweep | **209 / 209** single-field edits, deletions and adjacent swaps on a real evidence bundle detected by the offline verifier |
| Guard vs. independent oracle | **5,000 / 5,000** seeded random cases matched on decision and exact reason codes (1,527 allow, 306 needs-approval, 3,167 deny) |
| Live AI model vs. poisoned catalogues | Model strayed in **15 / 24** runs; guard denied **15 / 15**; **0** PayPal orders for them |
| Accessibility (`scripts/a11y_check.js`) | axe-core: **0 violations** in light, dark and phone width |
| Real PayPal sandbox | Orders created end to end through the guard and ledger |

How far to trust these:
- The red-team scenarios and the oracle were written by the same author as the guard, so they are a regression suite and an implementation check, not independent testing.
- Two scenarios inside the mandate's own scope are **deliberately not blocked** (an overpriced but permitted item at an allowed merchant; injected text in an item name on an otherwise valid order). The mandate allowed those purchases, and they are reported separately.
- Automated accessibility checks cover only part of accessibility.

## Honest status and limits

| Area | State |
|---|---|
| Mandates, guard, log, orchestrator, evidence bundles, timestamp anchoring, dashboard | Done and tested |
| PayPal sandbox order creation | Verified against the real sandbox |
| **Dispute evidence via the Disputes API** | Built to PayPal's documented API and tested against a **fake transport only**. **Not yet run against a live sandbox dispute**, which needs a sandbox buyer to approve an order. `scripts/sandbox_dispute_flow.py` does the rest once that is done. |
| Webhooks | Done, tested with a fake transport; not registered with PayPal |
| Shopping agent | **Scripted** by default (and on any deploy without a model); **live** with a local open model (`requirements-llm.txt`). The UI labels which one produced each proposal. |

Design limits, stated plainly:
- **Tamper-evident, not tamper-proof.** Someone who rebuilds the entire chain and the bundle together is only caught by a head hash or public key obtained elsewhere; the verifier accepts both.
- **Timestamp anchoring pins history up to the latest stamp.** Events after it are not pinned (the verifier reports how many). If a bundle is truncated to before its stamp, the stamp is simply absent, so keep the anchored hash somewhere else; the dispute notes carry it.
- Capturing a payment needs a sandbox buyer to approve the order in a browser, so captured payments are not exercised automatically.

## Run it

Requires Python 3.10 or newer.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m pytest                                    # 146 tests
python scripts/redteam_report.py                    # red-team block rate + bundle tamper sweep

# the dashboard, with a simulated PayPal (needs no credentials)
DEMO_PAYPAL=simulated PYTHONPATH=backend python -m flightrecorder.server    # http://localhost:8000
```

On Windows PowerShell: `$env:DEMO_PAYPAL="simulated"; $env:PYTHONPATH="backend"; python -m flightrecorder.server`.

- **Real PayPal sandbox:** copy `.env.example` to `.env`, add your sandbox client ID and secret, and start the server without `DEMO_PAYPAL=simulated`. The badge in the page header shows which mode is active.
- **Live AI agent (optional):** follow `requirements-llm.txt` to install llama.cpp and a small GGUF model, set `AFR_LOCAL_MODEL` to its path, and a "live AI model shops" button appears. `AFR_LOCAL_MODEL=... python scripts/llm_eval.py` reruns the 24-trial evaluation.
- Dispute submission from the dashboard is off unless `ALLOW_DISPUTE_SUBMIT=1`, and demo actions are rate limited.

### Verify a bundle yourself

Download a bundle from the dashboard, then, with no server and no network:

```bash
python scripts/verify_bundle.py evidence-<mandate>.json --key <public key> --head <saved head hash>
```

It checks the signature, every hash and any timestamp anchors (the latter needs the `openssl` command). Without `--head`, removal of the newest events cannot be detected, and without `--key` the signer is not checked; the script says so when either is missing.

### Scripts

| Script | Purpose |
|---|---|
| `scripts/verify_bundle.py` | Offline evidence-bundle verifier |
| `scripts/redteam_report.py` | Red-team block rate and bundle tamper sweep |
| `scripts/llm_eval.py` | Live-model evaluation against poisoned catalogues (needs `AFR_LOCAL_MODEL`) |
| `scripts/sandbox_smoke.py` | One real sandbox order through the full path |
| `scripts/sandbox_dispute_flow.py start\|finish` | Real order, capture, dispute and evidence (needs a sandbox buyer approval in between) |
| `scripts/register_webhook.py <url>` | Register the deployed webhook with the sandbox |
| `scripts/a11y_check.js <url>` | axe-core accessibility audit |

Deploy on Render with `render.yaml` (defaults to the simulated PayPal; set the sandbox variables in the Render dashboard to use the real one).

## Repository layout

```
backend/flightrecorder/   mandate, guard, ledger, orchestrator, paypal, evidence, anchor,
                          disputes, webhooks, agent, redteam, demo, server
frontend/index.html       single-page dashboard
tests/                    146 tests, including fuzz and tamper sweeps
scripts/                  verifier, reports, evaluation and sandbox tools
docs/                     screenshots and submission notes
DECISIONS.md              non-obvious design choices and why
```

## AI assistance and licence

**This project was built with AI assistance (Claude Code).** The code is released under the MIT licence; see `LICENSE`.
