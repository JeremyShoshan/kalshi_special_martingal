"""
Martingale PATTERN SEARCH on the real Kalshi order book replay (BTC_250).

Design (anti-overfitting):
  * Every candidate is executed with the same fill engine logic as run_simulation.py (strict maker
    fills: only tape prints THROUGH our limit or a resting ask below our bid fill us; taker at the
    best ask when ask <= limit; Kalshi taker fee ceil-to-cent; partial fills; bot skip rules;
    decisive-close trigger from the order book).
  * Data is split chronologically: TRAIN (selection) and TEST (out-of-sample, never used to choose).
  * Stage 1 : signal grid (trigger streak x pattern x multipliers x body filter x limit price)
              with the bot's default entry window (enter at t=0, cancel at 240s).
  * Stage 2 : execution grid (entry delay x maker cancel time x limit) for the stage-1 finalists.
  * Ranking uses TRAIN only. TEST results are reported next to it.

Actions inside a campaign (relative to the trigger streak / previous beat):
  FADE   : bet against the trigger streak direction
  FOLLOW : bet with the trigger streak direction
  LAST   : bet the same direction as the previous beat's settled result
  ANTI   : bet against the previous beat's settled result
(Step 1 only allows FADE/FOLLOW because the previous beat is the streak itself.)

Usage:
  uv run python research/martingale_pattern_search.py
  uv run python research/martingale_pattern_search.py --split 2026-09-21 --top 25
"""
from __future__ import annotations

import argparse
import bisect
import csv
import itertools
import math
import os
import pickle
import sys
import time
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lib.orderbook_replay import BEAT_SEC, TapeStore, decided_close_side, kalshi_fee, load_markets  # noqa: E402

DATA_DIR = r"c:\Users\Jeremy\Documents\projects\collect_data\combined_dataset\BTC_250"
CACHE_DIR = os.path.join(ROOT, ".sim_cache", "BTC_250")
FEAT_PATH = os.path.join(ROOT, ".sim_cache", "features_BTC_250_v3.pkl")

LATENCY = 1.0
CHECKPOINTS = (1.0, 15.0, 16.0, 30.0, 31.0, 60.0, 61.0, 120.0, 121.0, 180.0, 240.0)
DELAYS = (0, 15, 30, 60, 120)          # min_maker_sec (entry delay after beat start)
STOPS = (15, 30, 60, 120, 180, 240)    # stop_maker_sec (cancel time); stop<=delay -> taker only
LIMITS = (0.45, 0.48, 0.50, 0.52, 0.55)
BODIES = (0, 50, 75, 100, 150)


# ───────────────────────────────────────────────────────────────────────────────
# Feature extraction (one pass over the tape, cached)
# ───────────────────────────────────────────────────────────────────────────────
def build_features():
    if os.path.exists(FEAT_PATH):
        with open(FEAT_PATH, "rb") as f:
            return pickle.load(f)
    beats = load_markets(DATA_DIR)
    store = TapeStore(DATA_DIR, CACHE_DIR, verbose=True)
    feats = []
    t0 = time.time()
    for k, b in enumerate(beats):
        tape = store.tape(b)
        decided, _ = decided_close_side(b, tape)
        f = {
            "slug": b.slug, "date": b.date, "start": b.start_ts, "outcome": b.outcome,
            "body": b.body, "decided": decided,
            "snaps": {"Up": [], "Down": []}, "has_trades": False, "iv": None,
        }
        if tape is not None:
            for side in ("Up", "Down"):
                f["snaps"][side] = [(ts - b.start_ts, bid, ask) for ts, bid, ask in tape.book[side]
                                    if -5 <= ts - b.start_ts <= 260]
            win = [(ts - b.start_ts, int(round(yp * 1000)), sz) for ts, yp, sz in tape.trades
                   if 0 <= ts - b.start_ts <= 255]
            f["has_trades"] = bool(tape.trades) and any(0 <= ts - b.start_ts <= 300 for ts, _, _ in tape.trades)
            # trade volume bucketed by checkpoint interval, keyed by yes price in mills (0.1 cent)
            ivs = []
            j = 0
            for cp in CHECKPOINTS:
                agg: dict[int, float] = {}
                while j < len(win) and win[j][0] <= cp:
                    agg[win[j][1]] = agg.get(win[j][1], 0.0) + win[j][2]
                    j += 1
                ivs.append(sorted(agg.items()))
            f["iv"] = ivs
        feats.append(f)
        if (k + 1) % 500 == 0:
            print(f"  features {k + 1}/{len(beats)} ({time.time() - t0:.0f}s)", flush=True)
    with open(FEAT_PATH, "wb") as fh:
        pickle.dump(feats, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return feats


def snap_at(snaps, t, grace=5.0):
    if not snaps:
        return None
    i = bisect.bisect_right(snaps, (t, 9.0, 9.0)) - 1
    if i >= 0:
        return snaps[i]
    return snaps[0] if snaps[0][0] <= t + grace else None


def nearest_cp(t):
    best = CHECKPOINTS[0]
    for cp in CHECKPOINTS:
        if cp <= t + 1e-9:
            best = cp
    return best


_exec_cache: dict = {}


def exec_info(feats, idx, side, limit, delay, stop):
    """(kind, ask, through_volume, cross) for one beat/side/exec config. kind: T=taker, M=maker window, X=no data."""
    key = (idx, side, limit, delay, stop)
    r = _exec_cache.get(key)
    if r is not None:
        return r
    f = feats[idx]
    entry = delay + LATENCY
    s = snap_at(f["snaps"][side], entry)
    if s is None:
        r = ("X", None, 0.0, False)
    elif s[2] <= limit + 1e-9:
        r = ("T", s[2], 0.0, False)
    elif stop <= delay:
        r = ("U", s[2], 0.0, False)  # taker-only and not marketable
    else:
        Lm = int(round(limit * 1000))
        cross = any(entry < t <= stop and a <= limit - 1e-9 for t, _b, a in f["snaps"][side])
        if not f["has_trades"] or not f["iv"]:
            r = ("M", s[2], 0.0, cross) if cross else ("X", s[2], 0.0, False)
        else:
            ce, cs = nearest_cp(entry), nearest_cp(stop)
            vol = 0.0
            for k, cp in enumerate(CHECKPOINTS):
                if ce < cp <= cs:
                    for ym, v in f["iv"][k]:
                        if side == "Up":
                            if ym < Lm:
                                vol += v
                            else:
                                break  # sorted by price
                        elif 1000 - ym < Lm:
                            vol += v
            r = ("M", s[2], vol, cross)
    _exec_cache[key] = r
    return r


# ───────────────────────────────────────────────────────────────────────────────
# Strategy simulation
# ───────────────────────────────────────────────────────────────────────────────
def precompute_context(feats):
    """Per beat i: observed streak at decision of beat i-1 (settled history + decided side), gap flags."""
    ctx = []
    settled_len, settled_side = 0, None
    prev = None
    for i, f in enumerate(feats):
        gap = prev is None or abs(f["start"] - prev["start"] - BEAT_SEC) > 1
        if gap:
            ctx.append(None)
            settled_len, settled_side = 0, None
            prev = f
            continue
        d = prev["decided"]
        # streak of (settled history ... , decided prev)
        if settled_side == d:
            sl = settled_len + 1
        else:
            sl = 1
        ctx.append((sl, d))
        # update settled streak with prev's real outcome
        o = prev["outcome"]
        if o == settled_side:
            settled_len += 1
        else:
            settled_len, settled_side = 1, o
        prev = f
    return ctx


def simulate(feats, ctx, n_trig, pattern, mults, body, limit, delay, stop, base=10.0,
             lo=0, hi=None, taker_fee=0.07, keep_log=False):
    hi = len(feats) if hi is None else hi
    cap = len(pattern)
    stakes = [base * m for m in mults]
    in_camp = False
    step = 0
    tdir = None
    pnl = peak = dd = 0.0
    cw = cl = orders = fills = unf = nodata = taker_n = 0
    fees = 0.0
    step_w = [0] * cap
    day = defaultdict(float)
    worst_camp = 0.0
    camp_pnl = 0.0
    consec_l = max_consec_l = 0
    log = [] if keep_log else None
    for i in range(max(lo, 1), hi):
        c = ctx[i]
        if c is None:
            in_camp = False; step = 0; tdir = None; camp_pnl = 0.0
            continue
        sl, sside = c
        prev = feats[i - 1]
        if not in_camp:
            if sl != n_trig:
                continue
            if body > 0 and prev["body"] < body:
                continue
            in_camp = True; step = 0; tdir = sside; camp_pnl = 0.0
        act = pattern[step]
        if act == "FADE":
            side = "Down" if tdir == "Up" else "Up"
        elif act == "FOLLOW":
            side = tdir
        elif act == "LAST":
            side = prev["outcome"]
        else:  # ANTI
            side = "Down" if prev["outcome"] == "Up" else "Up"
        stake = stakes[step]
        target = max(round(stake / limit, 4), max(1.0, 2.0 / limit))
        kind, ask, vol, cross = exec_info(feats, i, side, limit, delay, stop)
        orders += 1
        f = feats[i]
        shares = cost = fee = 0.0
        if kind == "T":
            shares = target; cost = target * ask; fee = kalshi_fee(target, ask, taker_fee); taker_n += 1
        elif kind == "M":
            shares = target if cross else min(target, vol)
            cost = shares * limit
        if shares <= 1e-9:
            if kind == "X":
                nodata += 1
            else:
                unf += 1
            if step == 0 and f["outcome"] != tdir:
                in_camp = False; step = 0; tdir = None
            continue
        fills += 1
        fees += fee
        won = f["outcome"] == side
        tp = (shares if won else 0.0) - cost - fee
        pnl += tp
        camp_pnl += tp
        day[f["date"]] += tp
        if keep_log:
            log.append((f["slug"], step + 1, act, side, round(shares, 2), round(cost / shares, 4), kind, won, round(tp, 2)))
        if won:
            cw += 1; step_w[step] += 1
            in_camp = False; step = 0; tdir = None
            worst_camp = min(worst_camp, camp_pnl); consec_l = 0
        else:
            step += 1
            if step >= cap:
                cl += 1
                worst_camp = min(worst_camp, camp_pnl)
                consec_l += 1; max_consec_l = max(max_consec_l, consec_l)
                in_camp = False; step = 0; tdir = None
        if pnl > peak:
            peak = pnl
        if peak - pnl > dd:
            dd = peak - pnl
    camps = cw + cl
    res = {
        "pnl": pnl, "dd": dd, "rdd": pnl / dd if dd > 0 else (pnl if pnl > 0 else 0.0),
        "camps": camps, "cwr": cw / camps * 100 if camps else 0.0, "cw": cw, "cl": cl,
        "orders": orders, "fills": fills, "unf": unf, "nodata": nodata, "taker": taker_n,
        "fill_rate": fills / (orders - nodata) * 100 if orders - nodata > 0 else 0.0,
        "fees": fees, "step_w": step_w, "worst_camp": worst_camp, "max_consec_l": max_consec_l,
        "days": len(day), "lose_days": sum(1 for v in day.values() if v < 0),
        "risk_per_camp": sum(stakes),
    }
    if keep_log:
        res["log"] = log
    return res


def patterns_all():
    out = []
    for cap in (1, 2, 3):
        for first in ("FADE", "FOLLOW"):
            for rest in itertools.product(("FADE", "FOLLOW", "LAST", "ANTI"), repeat=cap - 1):
                p = (first,) + rest
                out.append(p)
    return out


MULTS = {
    1: [(1.0,)],
    2: [(1.0, 1.0), (1.0, 2.0), (1.0, 3.0)],
    3: [(1.0, 1.0, 1.0), (1.0, 1.5, 2.25), (1.0, 2.0, 3.0), (1.0, 2.0, 4.0)],
}


def fmt_p(p):
    return "-".join(a[:3] if a not in ("LAST", "ANTI") else a for a in p)


# ───────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="2026-09-21", help="first TEST date (TRAIN is before)")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--min-train-camps", type=int, default=60)
    ap.add_argument("--out", default=os.path.join(ROOT, "logs", "pattern_search"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    print("Building / loading features ...", flush=True)
    feats = build_features()
    ctx = precompute_context(feats)
    split_idx = next(i for i, f in enumerate(feats) if f["date"] >= args.split)
    N = len(feats)
    print(f"Beats {N:,} | TRAIN {feats[1]['date']}..{feats[split_idx - 1]['date']} ({split_idx:,}) | "
          f"TEST {feats[split_idx]['date']}..{feats[-1]['date']} ({N - split_idx:,})", flush=True)

    def evaluate(n, p, m, b, lim, d, s):
        tr = simulate(feats, ctx, n, p, m, b, lim, d, s, lo=0, hi=split_idx)
        te = simulate(feats, ctx, n, p, m, b, lim, d, s, lo=split_idx, hi=N)
        fu = simulate(feats, ctx, n, p, m, b, lim, d, s)
        return {"n": n, "pattern": fmt_p(p), "p": p, "mults": m, "body": b, "limit": lim, "delay": d, "stop": s,
                "tr": tr, "te": te, "full": fu}

    def train_score(r):
        t = r["tr"]
        if t["camps"] < args.min_train_camps or t["pnl"] <= 0:
            return -1e9
        # risk-normalised: return / drawdown, penalise thin samples
        return t["rdd"] * min(1.0, t["camps"] / 150.0)

    # ---------------- Stage 1: signal grid ----------------
    t0 = time.time()
    stage1 = []
    pats = patterns_all()
    total = sum(len(MULTS[len(p)]) for p in pats) * 4 * len(BODIES) * len(LIMITS)
    k = 0
    for n in (1, 2, 3, 4):
        for p in pats:
            for m in MULTS[len(p)]:
                for b in BODIES:
                    for lim in LIMITS:
                        k += 1
                        tr = simulate(feats, ctx, n, p, m, b, lim, 0, 240, lo=0, hi=split_idx)
                        stage1.append((n, p, m, b, lim, tr))
        print(f"  stage1 n={n} done ({k}/{total}) {time.time() - t0:.0f}s", flush=True)

    def s1score(t):
        if t["camps"] < args.min_train_camps or t["pnl"] <= 0:
            return -1e9
        return t["rdd"] * min(1.0, t["camps"] / 150.0)

    stage1.sort(key=lambda x: s1score(x[5]), reverse=True)
    pos = sum(1 for x in stage1 if x[5]["pnl"] > 0 and x[5]["camps"] >= args.min_train_camps)
    print(f"Stage 1: {len(stage1):,} combos, {pos:,} profitable on TRAIN with >= {args.min_train_camps} campaigns", flush=True)

    # keep diverse finalists: best per (n, pattern, mults)
    finalists, seen = [], set()
    for n, p, m, b, lim, tr in stage1:
        key = (n, p, m)
        if key in seen or s1score(tr) <= -1e9:
            continue
        seen.add(key)
        finalists.append((n, p, m, b))
        if len(finalists) >= 40:
            break

    # ---------------- Stage 2: execution grid ----------------
    results = []
    for (n, p, m, b) in finalists:
        for bb in sorted({0, b}):
            for lim in LIMITS:
                for d in DELAYS:
                    for s in STOPS:
                        if s < d:
                            continue
                        results.append(evaluate(n, p, m, bb, lim, d, s))
    print(f"Stage 2: {len(results):,} full evaluations ({time.time() - t0:.0f}s)", flush=True)
    results.sort(key=train_score, reverse=True)

    # ---------------- Report ----------------
    hdr = (f"{'#':>2} {'N':>1} {'PATTERN':<16} {'MULT':<13} {'BODY':>4} {'LIM':>4} {'DLY':>3} {'STOP':>4} | "
           f"{'TR PNL':>8} {'TR DD':>6} {'TR CWR':>6} {'CMP':>4} | {'TE PNL':>8} {'TE DD':>6} {'TE CWR':>6} {'CMP':>4} | "
           f"{'FULL PNL':>8} {'DD':>6} {'R/DD':>5} {'CWR':>5} {'FILL':>5} {'WORST':>7} {'LdD':>5}")
    lines = [hdr, "-" * len(hdr)]
    shown, seen2 = 0, set()
    for r in results:
        key = (r["n"], r["pattern"], r["mults"])
        if key in seen2:
            continue
        seen2.add(key)
        shown += 1
        tr, te, fu = r["tr"], r["te"], r["full"]
        lines.append(
            f"{shown:>2} {r['n']:>1} {r['pattern']:<16} {str(r['mults']):<13} {r['body']:>4} {r['limit']:>4.2f} {r['delay']:>3} {r['stop']:>4} | "
            f"{tr['pnl']:>+8.1f} {tr['dd']:>6.1f} {tr['cwr']:>5.1f}% {tr['camps']:>4} | "
            f"{te['pnl']:>+8.1f} {te['dd']:>6.1f} {te['cwr']:>5.1f}% {te['camps']:>4} | "
            f"{fu['pnl']:>+8.1f} {fu['dd']:>6.1f} {fu['rdd']:>5.2f} {fu['cwr']:>4.1f}% {fu['fill_rate']:>4.0f}% {fu['worst_camp']:>+7.1f} {fu['lose_days']:>2}/{fu['days']:<2}")
        if shown >= args.top:
            break
    report = "\n".join(lines)
    print("\n" + report)
    with open(os.path.join(args.out, "top_strategies.txt"), "w", encoding="utf-8") as f:
        f.write(report + "\n")

    # full CSV of stage-2
    with open(os.path.join(args.out, "stage2_all.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["n", "pattern", "mults", "body", "limit", "delay", "stop",
                    "tr_pnl", "tr_dd", "tr_cwr", "tr_camps", "te_pnl", "te_dd", "te_cwr", "te_camps",
                    "full_pnl", "full_dd", "full_rdd", "full_cwr", "full_camps", "fill_rate", "fees", "worst_camp",
                    "max_consec_camp_losses", "lose_days", "days"])
        for r in results:
            tr, te, fu = r["tr"], r["te"], r["full"]
            w.writerow([r["n"], r["pattern"], r["mults"], r["body"], r["limit"], r["delay"], r["stop"],
                        round(tr["pnl"], 2), round(tr["dd"], 2), round(tr["cwr"], 2), tr["camps"],
                        round(te["pnl"], 2), round(te["dd"], 2), round(te["cwr"], 2), te["camps"],
                        round(fu["pnl"], 2), round(fu["dd"], 2), round(fu["rdd"], 3), round(fu["cwr"], 2), fu["camps"],
                        round(fu["fill_rate"], 1), round(fu["fees"], 2), round(fu["worst_camp"], 2),
                        fu["max_consec_l"], fu["lose_days"], fu["days"]])

    # stage-1 CSV (signal grid, TRAIN only, default execution)
    with open(os.path.join(args.out, "stage1_signal_grid_train.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["n", "pattern", "mults", "body", "limit", "tr_pnl", "tr_dd", "tr_cwr", "tr_camps", "fill_rate"])
        for n, p, m, b, lim, tr in stage1:
            w.writerow([n, fmt_p(p), m, b, lim, round(tr["pnl"], 2), round(tr["dd"], 2), round(tr["cwr"], 2),
                        tr["camps"], round(tr["fill_rate"], 1)])

    # Sensitivity of the winner: body filter and maker window
    if results:
        best = results[0]
        n, p, m = best["n"], best["p"], best["mults"]
        sens = [f"\nWINNER: N={n} {best['pattern']} x{m} body>={best['body']} limit {best['limit']:.2f} delay {best['delay']}s stop {best['stop']}s",
                "\nMaker window sensitivity (full period, winner's limit/body/delay):",
                f"{'STOP':>5} {'PNL':>9} {'DD':>7} {'CWR':>6} {'CAMPS':>5} {'FILL%':>6} {'TAKER':>5}"]
        for s in STOPS:
            if s < best["delay"]:
                continue
            fu = simulate(feats, ctx, n, p, m, best["body"], best["limit"], best["delay"], s)
            sens.append(f"{s:>5} {fu['pnl']:>+9.2f} {fu['dd']:>7.2f} {fu['cwr']:>5.1f}% {fu['camps']:>5} {fu['fill_rate']:>5.1f}% {fu['taker']:>5}")
        sens += ["\nBody filter sensitivity (full period):", f"{'BODY':>5} {'PNL':>9} {'DD':>7} {'CWR':>6} {'CAMPS':>5}"]
        for b in BODIES:
            fu = simulate(feats, ctx, n, p, m, b, best["limit"], best["delay"], best["stop"])
            sens.append(f"{b:>5} {fu['pnl']:>+9.2f} {fu['dd']:>7.2f} {fu['cwr']:>5.1f}% {fu['camps']:>5}")
        sens += ["\nLimit price sensitivity (full period):", f"{'LIMIT':>5} {'PNL':>9} {'DD':>7} {'CWR':>6} {'CAMPS':>5} {'FILL%':>6}"]
        for lim in LIMITS:
            fu = simulate(feats, ctx, n, p, m, best["body"], lim, best["delay"], best["stop"])
            sens.append(f"{lim:>5.2f} {fu['pnl']:>+9.2f} {fu['dd']:>7.2f} {fu['cwr']:>5.1f}% {fu['camps']:>5} {fu['fill_rate']:>5.1f}%")
        txt = "\n".join(sens)
        print(txt)
        with open(os.path.join(args.out, "top_strategies.txt"), "a", encoding="utf-8") as f:
            f.write(txt + "\n")
    print(f"\nDone in {time.time() - t0:.0f}s. Outputs in {args.out}")


if __name__ == "__main__":
    main()
