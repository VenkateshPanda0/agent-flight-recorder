# Decisions

Non-obvious choices, so they can be explained and challenged.

1. **Money is integer minor units.** Floats are rejected by canonical JSON.
   `paypal.format_amount` converts to PayPal's decimal strings with integer
   maths and refuses currencies whose exponent is not listed.
2. **Guard totals come from line items**, never the agent's claimed total.
3. **Log before PayPal.** Intent and verdict are appended before any PayPal
   call; a denied purchase makes no network call at all (tested).
4. **Replayed intents are idempotent.** The intent id is the hash of the
   intent. Re-submitting returns the earlier result and creates no second
   order. The id is also sent as `PayPal-Request-Id`.
5. **Approval re-runs the guard.** A human "yes" cannot override a revoked
   mandate, an expired one, or a blown cap.
6. **Budget is reserved at order creation** and released by void, refund or a
   failed PayPal call (a failed call logs `ORDER_FAILED` and creates no
   reservation).
7. **Void is a ledger release, not a PayPal call.** An approved-but-unpaid
   CREATED order has no void endpoint; it lapses on PayPal's side.
8. **The log head hash is written into each PayPal order's description**
   (and `log_head_at_creation`). That puts a copy of the chain state outside
   our database, which is what detects truncation of the newest events.
9. **Evidence bundles carry the full chain**, not just one mandate's events,
   because a hash chain can only be verified end to end. The cost is that a
   bundle may include other mandates' events. Fine for a single-user demo; a
   multi-tenant deployment would need per-tenant chains.
10. **`verify_bundle` regenerates the summary from the events**, so the
    plain-language summary cannot be edited independently of the record.
11. **Tamper-evident, not tamper-proof.** Someone who rebuilds the whole chain
    and the bundle together is only caught by a head hash or public key
    obtained elsewhere; `verify_bundle` takes both for that reason.
12. **Sandbox only.** `PayPalClient` refuses any base URL except the sandbox.
