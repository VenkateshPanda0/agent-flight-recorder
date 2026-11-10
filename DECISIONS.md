# Decisions

Non-obvious choices, so they can be explained and challenged.

1. **Money is integer minor units.** Floats are rejected by canonical JSON.
   `paypal.format_amount` converts to PayPal's decimal strings with integer
   maths and refuses currencies whose exponent is not listed.
2. **Guard totals come from line items**, never the agent's claimed total.
3. **Log before PayPal.** Intent and verdict are appended before any PayPal
   call; a denied purchase makes no network call at all (tested).
   The orchestrator verifies the full chain before changing mandates, approvals or
   payment state. A broken chain stops the operation before it can use damaged budget
   or revocation history. This costs one linear scan and provides integrity within the
   current ledger. Detecting a fully rebuilt history requires an independent witness.
4. **Replayed intents are idempotent.** The intent id is the hash of the
   intent. Re-submitting returns the earlier result and creates no second
   order. A hash of this id and the signed mandate is sent as `PayPal-Request-Id`. If the earlier attempt
   failed at PayPal (`ORDER_FAILED`), the same intent may be retried; the shared
   request id deduplicates only within PayPal's retention window; ambiguous remote outcomes require reconciliation.
5. **Approval re-runs the guard.** A human "yes" cannot override a revoked
   mandate, an expired one, or a blown cap.
6. **Budget and an order slot are reserved before calling PayPal.** `ORDER_REQUESTED`
   survives a process interruption. A definitive rejection releases the reservation.
   An unknown outcome retains it and blocks automatic retry. A confirmed order takes
   over the reservation until a local void or a completed refund releases the applicable limit.
   Capture attempts are logged before sending. Pending or uncertain captures block void and
   repeat capture until reconciled. Refunds release budget only after PayPal confirms the
   requested amount and currency and supplies a refund ID.
   A recorded capture attempt can be reconciled with a read-only lookup of its order.
   Missing or mismatched capture details leave the reservation in place.
7. **Void is a ledger release, not a PayPal call.** An approved-but-unpaid
   CREATED order has no void endpoint; it lapses on PayPal's side.
8. **The log head hash is written into each PayPal order's description**
   (and `log_head_at_creation`). That puts a copy of the chain state outside
   our database. It covers history before order creation, not later events or a removed witness event unless its order id was retained independently.
9. **Evidence bundles carry the full chain**, not just one mandate's events,
   because a hash chain can only be verified end to end. The cost is that a
   bundle may include other mandates' events. Fine for a single-user demo; a
   multi-tenant deployment would need per-tenant chains.
10. **`verify_bundle` regenerates the summary from the events**, so the
    plain-language summary cannot be edited independently of the record.
11. **Tamper-evident, not tamper-proof.** Someone who rebuilds the whole chain
    and the bundle together needs an independently retained head or external witness to expose it.
    A trusted public key authenticates the mandate signer only, not the event history.
12. **Sandbox only.** `PayPalClient` refuses any base URL except the sandbox.
    The HTTP transport rejects redirects and bounds response bodies. Resource IDs
    must fit in one path segment before OAuth or any API call can run.
13. **The server demo is rate limited; GitHub Pages has simulated PayPal.** Demo actions are
    limited per visitor and overall; dispute submission from the dashboard is off
    unless `ALLOW_DISPUTE_SUBMIT=1`; each visitor gets an isolated in-memory demo. A server configured with sandbox credentials can create sandbox orders for visitors.
14. **Timestamp anchoring instead of our own "publish the hash" scheme.** An RFC 3161
    authority needs no account, is independent of us, and its tokens verify offline
    with `openssl`. The request is built in pure Python (checked byte-for-byte against
    `openssl ts -query`); verification shells out to `openssl` and reports "not
    verified" if it is missing. A token is verified before it is logged. Anchoring is
    off in tests and in simulated mode so nothing touches the network by accident.
15. **Bounded verification work.** Uploaded bundles can contain many timestamp tokens and each check
    launches `openssl`, so only the newest 3 are verified (each pins everything before it) and
    `/api/verify-bundle` is rate limited. Unchecked anchors are reported as "not checked", not "ok".
16. **A real local model as the demo agent.** No API key or paid service was available, so the live agent is a
    small open model (Qwen2.5-1.5B-Instruct, Apache-2.0) run through llama.cpp. A small model is also a
    realistic worst case: it follows injected instructions often (15 of 24 runs strayed), which is exactly what the
    guard exists for. The live scenario is hidden when no model is configured, so the public deploy never claims
    it. Calls are serialised with a lock because a loaded model is not thread-safe.
17. **MCP server instead of PayPal's remote MCP server.** PayPal's hosted MCP endpoint sits behind a browser OAuth
    login, so the sandbox client credentials do not work against it. Rather than depend on it, the project ships its
    own MCP server that wraps the guard: agents get four tools, and the only way to move money is `propose_purchase`.
    There is intentionally no approval tool, so an agent can never approve itself.
18. **PayPal as a witness.** The log head hash is already written into each order's description, and equals the
    `prev_hash` of the order's ORDER_CREATED event. Fetching the order back therefore gives an independent copy of the
    chain's state that an attacker cannot rewrite along with the database. Verified against the real sandbox, with a
    test where the whole chain is rebuilt consistently and only PayPal's copy exposes it.
19. **`PayPal-Request-Id` includes the mandate.** It used to be the intent id alone, so two demo sessions buying the same
    thing (same fixed demo mandate id) shared a request id and PayPal returned the first session's order, with its stale
    log head. Found by running the witness check against the real sandbox. The id is now a hash of the intent and the
    signed mandate: stable for a replay under the same mandate, different for every mandate.

20. **Dispute notes, not JSON attachments.** PayPal accepts image/PDF attachments, not JSON. Submission sends notes with the summary, head and bundle digest; retain the returned full bundle separately. This remains fake-transport tested only.
21. **Shared ledger lock.** Orchestrators, webhook processors and timestamp recording share the Ledger object's reentrant operation lock. Do not share a SQLite file across processes or separate Ledger connections without external coordination. Webhooks record receipt separately from successful processing, so failed dispute lookups can be retried without duplicating the receipt. Pending refunds keep budget reserved. Webhooks do not reconcile capture/refund state, and a local void cannot cancel a remotely approved order.

22. **Approval belongs to an attempt.** A failed order can be resubmitted for fresh approval. Only the latest attempt controls retry eligibility; an old failure cannot reopen a later rejection. Replaying a pending intent preserves its pending status.
