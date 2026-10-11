"""Sensitivity sweeps for the selected N=3 FADE-FADE-FOLLOW candidates (full period + 4 blocks)."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import martingale_pattern_search as mps

feats = mps.build_features(); ctx = mps.precompute_context(feats)
N = len(feats)
dates = sorted({f["date"] for f in feats})
bnd = [next(i for i, f in enumerate(feats) if f["date"] >= dates[int(round(k * len(dates) / 4))]) for k in range(4)] + [N]
P = ("FADE", "FADE", "FOLLOW")
CANDS = {
    "A (max profit)":   dict(m=(1.0, 2.0, 3.0), body=50, lim=0.45, d=15, s=120),
    "B (low drawdown)": dict(m=(1.0, 1.5, 2.25), body=50, lim=0.55, d=60, s=120),
}


def row(label, m, body, lim, d, s):
    f = mps.simulate(feats, ctx, 3, P, m, body, lim, d, s)
    bl = [mps.simulate(feats, ctx, 3, P, m, body, lim, d, s, lo=bnd[k], hi=bnd[k + 1])["pnl"] for k in range(4)]
    return (f"{label:>8} | {f['pnl']:>+8.1f} {f['dd']:>6.1f} {f['rdd']:>5.2f} {f['cwr']:>5.1f}% {f['camps']:>4} "
            f"{f['fill_rate']:>4.0f}% {f['taker']:>4} | " + " ".join(f"{x:>+7.1f}" for x in bl)
            + f" | {sum(1 for x in bl if x > 0)}/4")


H = f"{'value':>8} | {'PNL':>8} {'MAXDD':>6} {'R/DD':>5} {'CWR':>6} {'CAMP':>4} {'FILL':>5} {'TKR':>4} | {'B1':>7} {'B2':>7} {'B3':>7} {'B4':>7} | pos"
for name, c in CANDS.items():
    print("=" * len(H)); print(f"CANDIDATE {name}: N=3 FADE-FADE-FOLLOW x{c['m']} body>={c['body']} limit {c['lim']} entry {c['d']}s cancel {c['s']}s"); print("=" * len(H))
    print("-- cancel time (stop_maker_sec)"); print(H)
    for s in (15, 30, 60, 120, 180, 240):
        if s >= c["d"]:
            print(row(f"{s}s" + ("*" if s == c["d"] else ""), c["m"], c["body"], c["lim"], c["d"], s))
    print("-- entry delay (min_maker_sec)"); print(H)
    for d in mps.DELAYS:
        if d <= c["s"]:
            print(row(f"{d}s", c["m"], c["body"], c["lim"], d, c["s"]))
    print("-- body filter"); print(H)
    for b in mps.BODIES:
        print(row(f">={b}", c["m"], b, c["lim"], c["d"], c["s"]))
    print("-- limit price"); print(H)
    for lim in mps.LIMITS:
        print(row(f"{lim:.2f}", c["m"], c["body"], lim, c["d"], c["s"]))
    print("-- stake multipliers"); print(H)
    for m in mps.MULTS[3]:
        print(row("/".join(f"{x:g}" for x in m), m, c["body"], c["lim"], c["d"], c["s"]))
print("(* = taker-only: cancel at entry)")
