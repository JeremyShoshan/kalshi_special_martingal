"""
Order-book replay engine for Kalshi KXBTC15M 15-minute beat markets.

Replays the *exact* execution path used by bot_live.py (marketable limit mode) against the
recorded order book + trade tape in collect_data/combined_dataset/BTC_250:

  1. At beat start (+ placement latency) a GTC limit BUY is sent at `max_maker_price`.
  2. If the best ask on our side is <= limit, the order executes immediately as TAKER at the ask.
  3. Otherwise the order rests in the book as MAKER at the limit price and fills only when the
     recorded tape proves a seller was present at/below our price.
  4. At `stop_maker_sec` the unfilled remainder is cancelled (optional IOC taker fallback when
     `taker_limit_price` is configured).
  5. Partial fills count as a real position. Kalshi fees are charged per order
     (ceil to the cent of rate * C * P * (1-P)).

Dataset facts this engine relies on (verified in BTC_250):
  * orderbook CSV = top-of-book only (best bid / best ask per outcome), no sizes, only written on change
    -> state at time t is the last snapshot <= t (forward fill).
  * trades CSV = every fill (price + size). Each Kalshi trade is written as two mirrored rows
    (Up @ p, Down @ 1-p). The BUY/SELL label is NOT a reliable aggressor flag (it is always
    Up/SELL or Down/BUY), so it is ignored. Rows are canonicalised to (ts, yes_price, size).
  * The combined dataset only keeps the first N seconds + last 60s of every beat; coverage is
    detected per beat and reported.

Maker fill models for trades printing *exactly* at our limit (queue position is unknown because
the book has no sizes):
  * "strict" : only trades strictly THROUGH our price fill us (price-priority guarantees we would
               have been matched first).  -> conservative, recommended.
  * "queue"  : trades at our price fill us after `queue_ahead` contracts already resting ahead of us.
  * "touch"  : every trade at our price fills us (optimistic: we are first in the queue).
  * "ideal"  : legacy assumption - 100% filled at the limit price, no book (for comparison only).
"""
from __future__ import annotations

import bisect
import csv
import glob
import math
import os
import pickle
from dataclasses import dataclass, field
from datetime import datetime, timezone

BEAT_SEC = 900
CACHE_VERSION = 3


# ───────────────────────────────────────────────────────────────────────────────
# Fees
# ───────────────────────────────────────────────────────────────────────────────
def kalshi_fee(contracts: float, price: float, rate: float) -> float:
    """Kalshi fee per order: ceil_to_cent(rate * C * P * (1 - P))."""
    if contracts <= 0 or rate <= 0:
        return 0.0
    raw = rate * contracts * price * (1.0 - price)
    return math.ceil(round(raw * 100.0, 6)) / 100.0


def adjust_shares_to_minimum(shares: float, price: float) -> float:
    """Mirror of bot_live.adjust_shares_to_minimum (Kalshi $2 / 1 contract minimum)."""
    min_shares = max(1.0, 2.0 / price) if price > 0 else 1.0
    return min_shares if shares < min_shares else shares


# ───────────────────────────────────────────────────────────────────────────────
# Data loading
# ───────────────────────────────────────────────────────────────────────────────
def _parse_iso(s: str) -> float:
    s = s.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def _parse_wall(s: str) -> float:
    return datetime.strptime(s.strip()[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


@dataclass
class Beat:
    slug: str
    start_ts: float
    outcome: str  # "Up" | "Down"
    coin_start: float
    coin_end: float
    date: str

    @property
    def body(self) -> float:
        return abs(self.coin_end - self.coin_start)


def load_markets(data_dir: str, start_date: str | None = None, end_date: str | None = None) -> list[Beat]:
    files = sorted(glob.glob(os.path.join(data_dir, "**", "btc_15m_markets_*.csv"), recursive=True))
    if not files:
        raise FileNotFoundError(f"No btc_15m_markets_*.csv files found under {data_dir}")
    beats: dict[str, Beat] = {}
    for mf in files:
        date = os.path.basename(mf).rsplit("_", 1)[-1][:-4]
        if start_date and date < start_date:
            continue
        if end_date and date > end_date:
            continue
        with open(mf, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                res = str(row.get("result", "")).strip().lower()
                if res in ("yes", "up"):
                    outcome = "Up"
                elif res in ("no", "down"):
                    outcome = "Down"
                else:
                    continue
                slug = row["market_ticker"]
                try:
                    start_ts = _parse_iso(row["open_time"])
                except Exception:
                    continue
                beats[slug] = Beat(
                    slug=slug,
                    start_ts=start_ts,
                    outcome=outcome,
                    coin_start=float(row.get("coin_price_start") or 0),
                    coin_end=float(row.get("coin_price_end") or 0),
                    date=date,
                )
    return sorted(beats.values(), key=lambda b: b.start_ts)


@dataclass
class BeatTape:
    """Order book + trade tape of a single beat."""
    # side -> sorted list of (ts, best_bid, best_ask)
    book: dict[str, list[tuple[float, float, float]]] = field(default_factory=lambda: {"Up": [], "Down": []})
    # sorted list of (ts, yes_price, size)
    trades: list[tuple[float, float, float]] = field(default_factory=list)

    def book_at(self, side: str, ts: float, grace: float = 0.0):
        """Last snapshot <= ts. If none exists, the first snapshot within `grace` seconds after ts."""
        snaps = self.book.get(side) or []
        if not snaps:
            return None
        i = bisect.bisect_right(snaps, (ts, 9.0, 9.0)) - 1
        if i >= 0:
            return snaps[i]
        if snaps[0][0] <= ts + grace:
            return snaps[0]
        return None


class TapeStore:
    """Lazily parses daily orderbook/trade CSVs, caching a compact pickle per day."""

    def __init__(self, data_dir: str, cache_dir: str | None = None, verbose: bool = True):
        self.data_dir = data_dir
        self.cache_dir = cache_dir
        self.verbose = verbose
        self._day_cache: dict[str, dict[str, BeatTape]] = {}
        self._day_files: dict[str, tuple[str, str]] = {}
        for mf in glob.glob(os.path.join(data_dir, "**", "btc_15m_markets_*.csv"), recursive=True):
            date = os.path.basename(mf).rsplit("_", 1)[-1][:-4]
            d = os.path.dirname(mf)
            self._day_files[date] = (
                os.path.join(d, f"btc_15m_orderbook_{date}.csv"),
                os.path.join(d, f"btc_15m_trades_{date}.csv"),
            )
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    # -- public --------------------------------------------------------------
    def tape(self, beat: Beat) -> BeatTape | None:
        """Tape for a beat. Trades of a beat can live in the file of its start date or the previous one."""
        day = self._load_day(beat.date)
        t = day.get(beat.slug)
        return t

    # -- internals -----------------------------------------------------------
    def _src_sig(self, date: str) -> tuple:
        ob, tr = self._day_files.get(date, ("", ""))
        sig = []
        for p in (ob, tr):
            if os.path.exists(p):
                st = os.stat(p)
                sig.append((st.st_size, int(st.st_mtime)))
            else:
                sig.append(None)
        return (CACHE_VERSION, tuple(sig))

    def _load_day(self, date: str) -> dict[str, BeatTape]:
        if date in self._day_cache:
            return self._day_cache[date]
        sig = self._src_sig(date)
        cache_path = os.path.join(self.cache_dir, f"{date}.pkl") if self.cache_dir else None
        if cache_path and os.path.exists(cache_path):
            try:
                with open(cache_path, "rb") as f:
                    payload = pickle.load(f)
                if payload.get("sig") == sig:
                    self._day_cache[date] = payload["tapes"]
                    return payload["tapes"]
            except Exception:
                pass
        if self.verbose:
            print(f"  [tape] parsing {date} ...", flush=True)
        tapes = self._parse_day(date)
        if cache_path:
            with open(cache_path, "wb") as f:
                pickle.dump({"sig": sig, "tapes": tapes}, f, protocol=pickle.HIGHEST_PROTOCOL)
        # keep memory bounded: only hold a few days in RAM
        if len(self._day_cache) > 4:
            self._day_cache.pop(next(iter(self._day_cache)))
        self._day_cache[date] = tapes
        return tapes

    def _parse_day(self, date: str) -> dict[str, BeatTape]:
        ob_path, tr_path = self._day_files.get(date, ("", ""))
        tapes: dict[str, BeatTape] = {}

        # Order book: rows of the same poll share poll_timestamp; collect bid/ask per side.
        if os.path.exists(ob_path):
            pending: dict[tuple[str, str, float], list[float | None]] = {}
            with open(ob_path, "r", encoding="utf-8", buffering=1 << 20) as f:
                rd = csv.reader(f)
                next(rd, None)
                for r in rd:
                    if len(r) < 8:
                        continue
                    try:
                        ts = float(r[0])
                        px = float(r[7])
                    except ValueError:
                        continue
                    key = (r[3], r[5], ts)
                    slot = pending.get(key)
                    if slot is None:
                        slot = [None, None]
                        pending[key] = slot
                    if r[6] == "Bid":
                        slot[0] = px
                    elif r[6] == "Ask":
                        slot[1] = px
            for (slug, side, ts), (bid, ask) in pending.items():
                if side not in ("Up", "Down"):
                    continue
                t = tapes.setdefault(slug, BeatTape())
                t.book[side].append((ts, bid if bid is not None else 0.0, ask if ask is not None else 1.0))
            for t in tapes.values():
                for side in ("Up", "Down"):
                    t.book[side].sort()

        # Trades: canonicalise to yes-price and de-duplicate the mirrored Up/Down rows.
        if os.path.exists(tr_path) and os.path.getsize(tr_path) > 120:
            seen: dict[str, set] = {}
            with open(tr_path, "r", encoding="utf-8", buffering=1 << 22) as f:
                rd = csv.reader(f)
                next(rd, None)
                for r in rd:
                    if len(r) < 9:
                        continue
                    try:
                        ts = float(r[0])
                        px = float(r[7])
                        sz = float(r[8]) if r[8] else 0.0
                    except ValueError:
                        continue
                    if sz <= 0:
                        continue
                    yes_px = px if r[5] == "Up" else round(1.0 - px, 4)
                    slug = r[3]
                    key = (round(ts, 3), round(yes_px, 4), sz)
                    s = seen.setdefault(slug, set())
                    if key in s:
                        continue
                    s.add(key)
                    tapes.setdefault(slug, BeatTape()).trades.append((ts, yes_px, sz))
            for t in tapes.values():
                t.trades.sort()
        return tapes


# ───────────────────────────────────────────────────────────────────────────────
# Execution
# ───────────────────────────────────────────────────────────────────────────────
@dataclass
class ExecConfig:
    limit_price: float = 0.48
    stop_maker_sec: float = 240.0
    place_latency_sec: float = 1.0
    book_grace_sec: float = 5.0
    fill_model: str = "strict"  # strict | queue | touch | ideal
    queue_ahead: float = 0.0  # contracts ahead of us at our price (queue model)
    book_cross_fills: bool = True  # resting ask on our side <= limit proves a seller crossed us
    taker_fee_rate: float = 0.07
    maker_fee_rate: float = 0.0
    taker_limit_price: float | None = None  # IOC fallback at stop_maker_sec


@dataclass
class Fill:
    status: str  # FILLED | PARTIAL | UNFILLED | NO_DATA
    shares: float = 0.0
    cost: float = 0.0
    fees: float = 0.0
    taker_shares: float = 0.0
    maker_shares: float = 0.0
    ioc_shares: float = 0.0
    open_ask: float | None = None
    first_fill_sec: float | None = None
    source: str = ""
    trades_covered: bool = True

    @property
    def avg_price(self) -> float:
        return self.cost / self.shares if self.shares > 0 else 0.0


def simulate_order(beat: Beat, tape: BeatTape | None, side: str, stake: float, cfg: ExecConfig) -> Fill:
    limit = cfg.limit_price
    target = adjust_shares_to_minimum(round(stake / limit, 4), limit)

    if cfg.fill_model == "ideal":
        return Fill("FILLED", target, target * limit, kalshi_fee(target, limit, cfg.maker_fee_rate),
                    maker_shares=target, source="ideal")

    if tape is None or not tape.book.get(side):
        return Fill("NO_DATA", trades_covered=False, source="no orderbook")

    place_ts = beat.start_ts + cfg.place_latency_sec
    cancel_ts = beat.start_ts + cfg.stop_maker_sec
    snap = tape.book_at(side, place_ts, cfg.book_grace_sec)
    if snap is None:
        return Fill("NO_DATA", trades_covered=False, source="no book at open")

    fill = Fill("UNFILLED")
    fill.open_ask = snap[2]
    remaining = target
    # The order rests from placement time. If the first recorded snapshot arrives a few seconds later it
    # is only used as a proxy for the opening book; tape prints before it still count for the maker part.
    eff_place_ts = place_ts

    # 1) Marketable part: crosses the spread immediately as taker at the best ask.
    if snap[2] <= limit + 1e-9:
        px = snap[2]
        fill.taker_shares = remaining
        fill.cost += remaining * px
        fill.fees += kalshi_fee(remaining, px, cfg.taker_fee_rate)
        fill.first_fill_sec = eff_place_ts - beat.start_ts
        remaining = 0.0
        fill.source = "taker@open"

    # 2) Resting maker part.
    if remaining > 1e-9:
        maker = 0.0
        queue_left = cfg.queue_ahead
        sources = []
        trades = tape.trades
        fill.trades_covered = bool(trades) and trades[-1][0] >= beat.start_ts and any(
            beat.start_ts <= t[0] <= beat.start_ts + 300 for t in trades)
        events: list[tuple[float, str, float, float]] = []  # (ts, kind, side_px, size)
        lo = bisect.bisect_right(trades, (eff_place_ts, 9.0, 1e18))
        for ts, yes_px, sz in trades[lo:]:
            if ts > cancel_ts:
                break
            side_px = yes_px if side == "Up" else round(1.0 - yes_px, 4)
            events.append((ts, "trade", side_px, sz))
        if cfg.book_cross_fills:
            snaps = tape.book[side]
            j = bisect.bisect_right(snaps, (eff_place_ts, 9.0, 9.0))
            for ts, _bid, ask in snaps[j:]:
                if ts > cancel_ts:
                    break
                if ask <= limit - 1e-9:
                    events.append((ts, "cross", ask, float("inf")))
                    break  # one proof is enough: a seller sat below our bid
        events.sort(key=lambda e: e[0])

        for ts, kind, px, sz in events:
            if remaining <= 1e-9:
                break
            take = 0.0
            if kind == "cross":
                take = remaining
            elif px < limit - 1e-9:
                take = min(remaining, sz)  # through our price: price priority -> we are hit first
            elif abs(px - limit) <= 1e-9:
                if cfg.fill_model == "touch":
                    take = min(remaining, sz)
                elif cfg.fill_model == "queue":
                    eat = min(queue_left, sz)
                    queue_left -= eat
                    take = min(remaining, sz - eat)
            if take > 0:
                if fill.first_fill_sec is None:
                    fill.first_fill_sec = ts - beat.start_ts
                maker += take
                remaining -= take
                sources.append(kind)
        if maker > 0:
            fill.maker_shares = maker
            fill.cost += maker * limit
            fill.fees += kalshi_fee(maker, limit, cfg.maker_fee_rate)
            tag = "maker(cross)" if "cross" in sources else "maker(tape)"
            fill.source = f"{fill.source}+{tag}" if fill.source else tag

    # 3) Optional IOC taker fallback at the maker timeout.
    if remaining > 1e-9 and cfg.taker_limit_price:
        s2 = tape.book_at(side, cancel_ts)
        if s2 is not None and s2[2] <= cfg.taker_limit_price + 1e-9:
            px = s2[2]
            stake_left = remaining * limit
            ioc = adjust_shares_to_minimum(round(stake_left / cfg.taker_limit_price, 4), cfg.taker_limit_price)
            fill.ioc_shares = ioc
            fill.cost += ioc * px
            fill.fees += kalshi_fee(ioc, px, cfg.taker_fee_rate)
            if fill.first_fill_sec is None:
                fill.first_fill_sec = cfg.stop_maker_sec
            remaining = 0.0
            fill.source = f"{fill.source}+ioc" if fill.source else "ioc"

    fill.shares = fill.taker_shares + fill.maker_shares + fill.ioc_shares
    if fill.shares <= 1e-9:
        # No trade tape for this beat -> a resting order's fate is unknowable, don't count it as unfilled.
        fill.status = "UNFILLED" if fill.trades_covered else "NO_DATA"
        if not fill.trades_covered:
            fill.source = "no trade tape"
    elif remaining > 1e-9:
        fill.status = "PARTIAL"
    else:
        fill.status = "FILLED"
    return fill


# ───────────────────────────────────────────────────────────────────────────────
# Live-bot decision emulation (decisive close 15s before beat end)
# ───────────────────────────────────────────────────────────────────────────────
def decided_close_side(beat: Beat, tape: BeatTape | None, seconds_before_close: int = 15,
                       decisive_up: float = 0.90, decisive_down: float = 0.10) -> tuple[str, bool]:
    """
    Reproduces bot_live's decision loop: from (900 - seconds_before_close) to 898s it polls the
    Up best bid each second; > decisive_up -> Up, < decisive_down -> Down, else falls back to
    bid >= 0.50. Returns (side, has_book_data). Without book data it returns the settled result.
    """
    if tape is None or not tape.book.get("Up"):
        return beat.outcome, False
    t0 = beat.start_ts + BEAT_SEC - seconds_before_close
    t_end = beat.start_ts + BEAT_SEC - 2
    snap = tape.book_at("Up", t0)
    if snap is None or snap[0] < t0 - 120:  # no recent book coverage near the close
        return beat.outcome, False
    t = t0
    last_bid = snap[1]
    while t < t_end:
        s = tape.book_at("Up", t)
        if s is not None:
            last_bid = s[1]
            if last_bid > decisive_up:
                return "Up", True
            if last_bid < decisive_down:
                return "Down", True
        t += 1.0
    return ("Up" if last_bid >= 0.50 else "Down"), True
