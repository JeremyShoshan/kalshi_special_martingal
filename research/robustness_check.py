"""
Robustness check for the martingale pattern search.

Splits the data into K chronological blocks and re-evaluates every stage-2 configuration per block.
A configuration is only "robust" if it is profitable in EVERY block and its parameter neighbourhood
(adjacent limit price / entry delay / cancel time) is also profitable on average.

Usage:  uv run python research/robustness_check.py [--blocks 4]
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import martingale_pattern_search as mps  # noqa: E402

ALL_ACTIONS = ("FADE", "FOLLOW", "LAST", "ANTI")


def parse_pattern(s: str) -> tuple:
    m = {"FAD": "FADE", "FOL": "FOLLOW", "LAST": "LAST", "ANTI": "ANTI"}
    return tuple(m[x] for x in s.split("-"))


def canonical(p: tuple) -> str:
    """After a LOST step: ANTI == repeat previous side, LAST == flip previous side."""
    out = [p[0]]
    for a in p[1:]:
        prev = out[-1]
        if a == "ANTI":
            out.append(prev)
        elif a == "LAST":
            out.append("FOLLOW" if prev == "FADE" else "FADE")
        else:
            out.append(a)
    return "-".join(x[:3] for x in out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=4)
    ap.add_argument("--stage2", default=os.path.join(mps.ROOT, "logs", "pattern_search", "stage2_all.csv"))
    ap.add_argument("--min-camps", type=int, default=150)
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    feats = mps.build_features()
    ctx = mps.precompute_context(feats)
    N = len(feats)
    dates = sorted({f["date"] for f in feats})
    per = len(dates) / args.blocks
    bounds = []
    for k in range(args.blocks):
        d0 = dates[int(round(k * per))]
        lo = next(i for i, f in enumerate(feats) if f["date"] >= d0)
        bounds.append(lo)
    bounds.append(N)
    blocks = [(bounds[k], bounds[k + 1]) for k in range(args.blocks)]
    print("Blocks:", " | ".join(f"{feats[a]['date']}..{feats[b - 1]['date']}" for a, b in blocks), flush=True)

    rows = list(csv.DictReader(open(args.stage2, encoding="utf-8")))
    t0 = time.time()
    res = []
    for r in rows:
        p = parse_pattern(r["pattern"])
        m = tuple(float(x) for x in r["mults"].strip("()").split(",") if x.strip())
        n, body, lim, d, s = int(r["n"]), int(r["body"]), float(r["limit"]), int(r["delay"]), int(r["stop"])
        bl = [mps.simulate(feats, ctx, n, p, m, body, lim, d, s, lo=a, hi=b) for a, b in blocks]
        full = mps.simulate(feats, ctx, n, p, m, body, lim, d, s)
        res.append({"n": n, "pattern": r["pattern"], "canon": canonical(p), "mults": m, "body": body,
                    "limit": lim, "delay": d, "stop": s, "blocks": bl, "full": full})
    print(f"Evaluated {len(res):,} configs x {args.blocks} blocks in {time.time() - t0:.0f}s", flush=True)

    # neighbourhood robustness
    idx = {(x["n"], x["pattern"], x["mults"], x["body"], x["limit"], x["delay"], x["stop"]): x for x in res}
    L, D, S = mps.LIMITS, mps.DELAYS, mps.STOPS

    def nb(vals, v):
        i = vals.index(v)
        return [vals[j] for j in (i - 1, i, i + 1) if 0 <= j < len(vals)]

    for x in res:
        neigh = []
        for lim in nb(L, x["limit"]):
            for d in nb(D, x["delay"]):
                for s in nb(S, x["stop"]):
                    y = idx.get((x["n"], x["pattern"], x["mults"], x["body"], lim, d, s))
                    if y is not None:
                        neigh.append(y["full"]["pnl"])
        x["nb_mean"] = sum(neigh) / len(neigh) if neigh else 0.0
        x["nb_pos"] = sum(1 for v in neigh if v > 0) / len(neigh) if neigh else 0.0
        x["worst_block"] = min(b["pnl"] for b in x["blocks"])
        x["pos_blocks"] = sum(1 for b in x["blocks"] if b["pnl"] > 0)

    robust = [x for x in res if x["pos_blocks"] == args.blocks and x["full"]["camps"] >= args.min_camps
              and x["nb_pos"] >= 0.75]
    # score: worst block return relative to full drawdown (consistency + low DD)
    for x in robust:
        x["score"] = x["worst_block"] / max(x["full"]["dd"], 1.0) + 0.25 * x["full"]["rdd"] / 10
    robust.sort(key=lambda x: x["score"], reverse=True)

    hdr = (f"{'#':>2} {'N':>1} {'PATTERN (effective)':<20} {'MULT':<16} {'BODY':>4} {'LIM':>4} {'DLY':>3} {'STOP':>4} | "
           + " ".join(f"{'B' + str(k + 1):>7}" for k in range(args.blocks))
           + f" | {'PNL':>8} {'MAXDD':>6} {'R/DD':>5} {'CWR':>5} {'CAMP':>4} {'FILL':>4} {'WORST':>6} {'LdD':>5} {'NBpos':>5}")
    out = [f"ROBUST CONFIGS (profitable in all {args.blocks} blocks, >= {args.min_camps} campaigns, >= 75% of parameter neighbours profitable)",
           hdr, "-" * len(hdr)]
    seen = set()
    k = 0
    for x in robust:
        key = (x["n"], x["canon"], x["mults"], x["body"])
        if key in seen:
            continue
        seen.add(key)
        k += 1
        f = x["full"]
        out.append(
            f"{k:>2} {x['n']:>1} {x['canon']:<20} {str(x['mults']):<16} {x['body']:>4} {x['limit']:>4.2f} {x['delay']:>3} {x['stop']:>4} | "
            + " ".join(f"{b['pnl']:>+7.1f}" for b in x["blocks"])
            + f" | {f['pnl']:>+8.1f} {f['dd']:>6.1f} {f['rdd']:>5.2f} {f['cwr']:>4.1f}% {f['camps']:>4} {f['fill_rate']:>3.0f}% {f['worst_camp']:>+6.1f} {f['lose_days']:>2}/{f['days']:<2} {x['nb_pos'] * 100:>4.0f}%")
        if k >= args.top:
            break
    txt = "\n".join(out)
    print("\n" + txt)
    print(f"\n{len(robust):,} of {len(res):,} configurations pass the robustness filter.")

    # family summary: how often each effective pattern family survives
    fam = defaultdict(lambda: [0, 0])
    for x in res:
        fam[(x["n"], x["canon"])][1] += 1
    for x in robust:
        fam[(x["n"], x["canon"])][0] += 1
    famtxt = ["\nFAMILY SURVIVAL (robust configs / tested configs):"]
    for (n, c), (a, b) in sorted(fam.items(), key=lambda kv: -kv[1][0] / max(kv[1][1], 1)):
        if b >= 20:
            famtxt.append(f"  N={n} {c:<20} {a:>4}/{b:<4} ({a / b * 100:5.1f}%)")
    print("\n".join(famtxt[:20]))
    with open(os.path.join(mps.ROOT, "logs", "pattern_search", "robust_strategies.txt"), "w", encoding="utf-8") as fh:
        fh.write(txt + "\n" + "\n".join(famtxt) + "\n")


if __name__ == "__main__":
    main()
