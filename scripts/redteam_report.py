"""Print the red-team block rate with exact counts, including misses."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from flightrecorder.redteam import run_all

r = run_all()
print(f"Attack scenarios: {r['attacks']}   blocked (no PayPal order beyond the one legitimate order): {r['blocked']}")
print(f"Block rate: {r['blocked']}/{r['attacks']}")
for name in r["misses"]:
    print("MISS:", name)
print(f"Allowed by design (inside the mandate's scope): {len(r['in_scope_not_blocked'])}")
for name in r["in_scope_not_blocked"]:
    print("  -", name)
by_cat = {}
for x in r["results"]:
    if not x["in_scope"]:
        c = by_cat.setdefault(x["category"], [0, 0]); c[0] += x["blocked"]; c[1] += 1
for cat, (b, n) in sorted(by_cat.items()):
    print(f"  {cat}: {b}/{n}")
