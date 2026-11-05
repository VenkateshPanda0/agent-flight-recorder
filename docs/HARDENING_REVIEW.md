# Hardening review — 6 November 2026

This review improves the sandbox prototype; it is not a production security certification.

## Reproduced and fixed

New regression tests were run failing before the corresponding fixes (the file-validation edge cases were added after the MIME regression fix).

| Location | Reproduction / regression | Fix |
|---|---|---|
| `backend/flightrecorder/orchestrator.py:205` | `test_voided_order_cannot_be_captured`: void, then capture | Reject capture before network access after a local void/refund. |
| `backend/flightrecorder/orchestrator.py:228` | `test_captured_order_cannot_be_voided`: capture, then void | Require refund for captured orders; preserve budget. |
| `backend/flightrecorder/orchestrator.py:240` | `test_refund_replay_is_rejected_before_network`, `test_pending_refund_keeps_budget_reserved` | Reject completed-refund replay; release budget only for COMPLETED capture/refund responses. Pending refunds require reconciliation/retry. |
| `backend/flightrecorder/ledger.py:251` | `test_duplicate_refund_cannot_release_another_orders_budget`: two orders, repeated refund of first | Deduplicate refund IDs and cap releases per order. |
| `backend/flightrecorder/evidence.py:44` | `test_unhashable_proposal_still_exports_verifiable_evidence`: submit float total, export bundle | Keep rejected input in the chain without treating it as a structured purchase. |
| `backend/flightrecorder/orchestrator.py:269` | `test_different_signers_do_not_share_payment_request_ids`: identical mandate payload signed by different keys | Include the full signed mandate digest in the payment request ID. |
| `backend/flightrecorder/webhooks.py:45` | `test_concurrent_webhooks_are_logged_once`: overlap duplicate deliveries before append | Serialize check-and-record inside a processor. |
| `backend/flightrecorder/server.py` | `test_http_reader_rejects_invalid_lengths_and_non_objects`, `test_rate_limiter_rejected_visitors_do_not_grow_memory` | Reject negative/invalid lengths, unsupported transfer encoding, non-object/deep JSON; do not retain rejected visitor IDs. Negative read lengths could previously wait for EOF, tying up request threads. |
| `backend/flightrecorder/mcp_server.py` | `test_non_string_tool_name_returns_protocol_error`: list as tool name | Return a protocol error instead of terminating stdio processing. |
| `backend/flightrecorder/disputes.py` | `test_disputes_submit_notes_without_unsupported_json_attachment` | Send notes containing summary/head/digest; return the full bundle separately. JSON attachments are unsupported. |
| `backend/flightrecorder/paypal.py:274` | Existing multipart test now asserts PDF MIME; additional unsafe/unsupported/oversized-file test | Correct image/PDF MIME, reject unsafe filenames and files >=10 MB. |
| `backend/flightrecorder/paypal.py:302` | Existing create-dispute assertion test now rejects fabricated contact fields | Remove hard-coded contact date and unsubstantiated merchant-contact history. |
| `backend/flightrecorder/anchor.py:85`, `tests/test_mcp.py` | Existing baseline tests failed on Windows | Honor explicit CA environment settings; retain Windows system environment needed by cryptographic subprocesses. |

## Validation

- Full suite: **179 passed**, Python 3.12 on Windows; the original baseline was 164 passed / 2 failed due to CA configuration and the subprocess environment.
- Red team: **27/27** attacks blocked, two in-scope cases reported separately.
- Bundle tampering: **209/209** detected.
- Seeded guard/oracle comparison: **5,000/5,000**; 1,527 ALLOW, 306 NEEDS_APPROVAL, 3,167 DENY.
- All 15 recorded out-of-scope model proposals were denied again. The original 24 live model runs were **not rerun**; no GGUF model was configured.
- Local static-mode story: Chromium and Firefox at 1200px; WebKit at 390px. Completed without page exceptions or horizontal overflow.
- Public GitHub Pages story: Firefox and WebKit at 390px, completed without page exceptions or overflow, before this push. WebKit on Windows is not the Safari application. The browser check recorded page exceptions, not every console message; static mode deliberately probes an absent `/api/state` endpoint.
- axe-core 4.10.3: zero violations in light/1200, dark/1200, light/390 on the patched local static demo.
- Fresh source clone plus patch: 178 tests and both red-team/tamper reports passed before the final upload-validation test was added. Dependencies were installed into a new workspace virtual environment; the clean source clone reused that interpreter. The final working-tree suite passed 179 tests.
- OpenSSL fixture verified offline with `SSL_CERT_FILE` pointing to the installed Git for Windows CA bundle. No test contacted PayPal or the TSA; HTTP tests used loopback only.

## API documentation review

Official sources checked on 6 November 2026:

- [Dispute evidence](https://developer.paypal.com/api/customer-disputes/v1/disputes-provide-evidence) and [file restrictions](https://developer.paypal.com/platforms/disputes/reference/supported-file-types-sizes/): notes-only submission avoids unsupported JSON attachments. The generic upload helper now uses supported MIME types and a conservative per-file limit. Notes remain capped at 2,000 characters.
- [Evidence schema](https://developer.paypal.com/api/customer-disputes/v1/definitions/evidence): OTHER remains the supplemental record type; it does not substitute for specifically requested delivery/refund evidence.
- [Require evidence](https://developer.paypal.com/api/customer-disputes/v1/disputes-require-evidence) and [adjudicate](https://developer.paypal.com/api/customer-disputes/v1/disputes-adjudicate): current action/outcome names match; availability depends on dispute state and returned links.
- [Webhook verification](https://developer.paypal.com/api/webhooks/v1/verify-webhook-signature-post): current request fields and SUCCESS handling match.
- [Orders](https://developer.paypal.com/api/orders/v2) and [refund](https://developer.paypal.com/api/payments/v2/definitions/refund/): pending is not completed, so budget cannot be released merely because HTTP succeeded.
- [PayPal's published disputes specification](https://github.com/paypal/paypal-rest-api-specifications/blob/main/openapi/customer_disputes_v1.json): create-dispute is limited release. Current public rendered reference does not establish that this app has buyer-assertion access. The unsigned assertion/payload remains fake-tested and unverified live; sandbox credentials alone may not suffice.

## Claim corrections to mirror on Devpost

- “Live demo uses a real model” → hosted demo replays real recorded proposals; live inference is optional/local.
- “Full bundle attached to PayPal” → summary/head/digest submitted as notes; retain the JSON separately.
- “Intent ID is PayPal-Request-Id; retries cannot duplicate orders” → hash includes signed mandate; remote idempotency is time-limited.
- “Trusted public key catches rebuilt history” → key authenticates the mandate signer; event history needs an independent head/witness.
- “Public demo cannot use credentials” → static demo is simulated; a configured server can create sandbox orders.
- Test count 166 → 179. Existing screenshot remains explicitly historical.

## Remaining risks, ranked

1. **High before any production use:** no durable reconciliation for ambiguous remote outcomes, captures/refunds from outside this process, or expired PayPal idempotency retention. Retrying an order after a remote timeout can also make the witness head differ from the retry's local event. Local void is not remote cancellation. The changed request-ID derivation must not be used to blindly retry an outstanding order from an older release: reconcile it first.
2. **High before multi-user production use:** merchant/category labels are not bound to verified PayPal payee identities. Locks are per orchestrator/processor, not cross-process transactions. Use one owner per ledger; multiple SQLite writers are not supported by this review.
3. **Medium:** webhook dispute handling logs receipt before fetching the dispute and swallows a failed fetch, so replay does not retry that processing. Server visitor ledgers are separate from the webhook owner's ledger. Webhooks do not reconcile financial lifecycle state.
4. **Medium:** stdlib threaded HTTP server has no connection/concurrency ceiling or read timeout. Body/token limits and request rate limits do not replace a reverse proxy, authentication, or network abuse protection. GET verification can repeat timestamp work. The process remains a demonstration server.
5. **Medium:** live dispute creation/evidence submission, capture/refund, registered webhooks, Render, Anthropic, and Claude Desktop were not exercised. No credentials or accounts were used. Recorded sandbox claims remain historical, not newly verified.
6. **Low:** monetary prose in evidence assumes two decimal places even though the payment client also supports JPY. Browser CDN availability remains a dependency. There is no full manual accessibility or Safari-on-Apple-device audit.

No Devpost edits were made. No new runtime dependencies or large binary files were added to Git.
