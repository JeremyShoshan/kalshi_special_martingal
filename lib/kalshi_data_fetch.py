import threading
import time
from datetime import datetime, timezone

import requests

from lib.kalshi_env import rest_base_url
from lib.kalshi_market import (
    beat_ts_to_market_ticker,
    series_ticker_for,
    token_id_for_side,
)


class KalshiDataFetcher:
    """Market data fetcher for Kalshi crypto up/down beat markets."""

    def __init__(self, asset: str = "BTC", beat_duration_min: int = 5):
        self.asset = asset.upper()
        self.beat_duration_min = beat_duration_min
        self.series_ticker = series_ticker_for(self.asset, beat_duration_min)
        self.base_url = rest_base_url()
        self.session = requests.Session()
        self._token_cache: dict[str, dict[str, str]] = {}
        self._event_ticker_cache: dict[str, str] = {}
        self._lock = threading.Lock()

        self._updater_thread = threading.Thread(target=self._run_token_updater, daemon=True)
        self._updater_thread.start()

    def _run_token_updater(self):
        while True:
            try:
                self._get_token_ids()
            except Exception as exc:
                print(f"         [KALSHI BACKGROUND UPDATE ERROR] {exc}")
            time.sleep(5)

    def _get_current_slug(self) -> str:
        now = datetime.now(timezone.utc)
        minute = (now.minute // self.beat_duration_min) * self.beat_duration_min
        rounded = now.replace(minute=minute, second=0, microsecond=0)
        return beat_ts_to_market_ticker(self.series_ticker, int(rounded.timestamp()), self.beat_duration_min)

    def resolve_market_ticker(self, beat_start_ts: int) -> str:
        """Resolve the official Kalshi market ticker for a beat start timestamp."""
        candidate = beat_ts_to_market_ticker(self.series_ticker, beat_start_ts, self.beat_duration_min)
        try:
            resp = self.session.get(
                f"{self.base_url}/markets/{candidate}",
                timeout=5,
            )
            if resp.status_code == 200:
                return candidate
        except Exception:
            pass

        close_ts = beat_start_ts + self.beat_duration_min * 60
        try:
            resp = self.session.get(
                f"{self.base_url}/markets",
                params={
                    "series_ticker": self.series_ticker,
                    "min_close_ts": close_ts - 60,
                    "max_close_ts": close_ts + 60,
                    "limit": 5,
                },
                timeout=5,
            )
            if resp.status_code == 200:
                markets = resp.json().get("markets", [])
                if markets:
                    return markets[0]["ticker"]
        except Exception:
            pass

        return candidate

    def get_token_ids_for_beat(self, beat_start_ts: int, force_refresh: bool = False) -> dict[str, str]:
        slug = beat_ts_to_market_ticker(self.series_ticker, beat_start_ts, self.beat_duration_min)
        if not force_refresh:
            with self._lock:
                if slug in self._token_cache:
                    return self._token_cache[slug]

        market_ticker = self.resolve_market_ticker(beat_start_ts)
        if not market_ticker:
            raise RuntimeError(f"No Kalshi market found for beat {beat_start_ts} ({slug})")

        result = {
            "Up": token_id_for_side(market_ticker, "Up"),
            "Down": token_id_for_side(market_ticker, "Down"),
        }

        with self._lock:
            if len(self._token_cache) > 10:
                self._token_cache.clear()
                self._event_ticker_cache.clear()
            self._token_cache[slug] = result
            self._event_ticker_cache[slug] = market_ticker
            return result

    def invalidate_token_cache(self, beat_start_ts: int):
        """Invalidate cached token IDs for a beat to force fresh resolution on retry."""
        slug = beat_ts_to_market_ticker(self.series_ticker, beat_start_ts, self.beat_duration_min)
        with self._lock:
            self._token_cache.pop(slug, None)
            self._event_ticker_cache.pop(slug, None)

    def _get_token_ids(self) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        minute = (now.minute // self.beat_duration_min) * self.beat_duration_min
        rounded = now.replace(minute=minute, second=0, microsecond=0)
        return self.get_token_ids_for_beat(int(rounded.timestamp()))

    def _get_book(self, market_ticker: str) -> dict:
        r = self.session.get(
            f"{self.base_url}/markets/{market_ticker}/orderbook",
            timeout=3,
        )
        return r.json()

    @staticmethod
    def _book_levels_for_side(book: dict, side: str) -> tuple[list[dict], list[dict]]:
        """Return (bids, asks) for the given Up/Down side with full implied ask depth."""
        ob = book.get("orderbook_fp", book.get("orderbook", {}))
        yes_levels = ob.get("yes_dollars", [])
        no_levels = ob.get("no_dollars", [])

        def to_bids(levels: list, reverse: bool = False) -> list[dict]:
            parsed = [{"price": lvl[0], "size": lvl[1]} for lvl in levels if len(lvl) >= 2]
            return sorted(parsed, key=lambda x: float(x["price"]), reverse=reverse)

        if side == "Up":
            bids = to_bids(yes_levels, reverse=True)
            asks = []
            for lvl in no_levels:
                if len(lvl) >= 2:
                    p_no = float(lvl[0])
                    asks.append({"price": f"{1.0 - p_no:.4f}", "size": lvl[1]})
            asks = sorted(asks, key=lambda x: float(x["price"]))
        else:
            bids = to_bids(no_levels, reverse=True)
            asks = []
            for lvl in yes_levels:
                if len(lvl) >= 2:
                    p_yes = float(lvl[0])
                    asks.append({"price": f"{1.0 - p_yes:.4f}", "size": lvl[1]})
            asks = sorted(asks, key=lambda x: float(x["price"]))

        return bids, asks

    def get_book_data(self, side: str) -> dict:
        token_map = self._get_token_ids()
        if side not in token_map:
            raise RuntimeError(f"Unknown outcome side '{side}'. Available: {list(token_map)}")

        token_id = token_map[side]
        market_ticker = token_id.split("|")[0]
        book = self._get_book(market_ticker)
        bids, asks = self._book_levels_for_side(book, side)
        return {"token_id": token_id, "asks": asks, "bids": bids}

    def get_current_condition_id(self) -> str | None:
        slug = self._get_current_slug()
        with self._lock:
            return self._event_ticker_cache.get(slug)

    def get_market_snapshot(self, market_ticker: str) -> dict | None:
        try:
            resp = self.session.get(
                f"{self.base_url}/markets/{market_ticker}",
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.json().get("market")
        except Exception:
            pass
        return None

    def get_current_values(self) -> dict | None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        now_utc = datetime.now(timezone.utc)
        beat_minute = (now_utc.minute // self.beat_duration_min) * self.beat_duration_min
        beat_ts = int(now_utc.replace(minute=beat_minute, second=0, microsecond=0).timestamp())

        try:
            token_map = self.get_token_ids_for_beat(beat_ts)
        except Exception as exc:
            raise RuntimeError(f"Kalshi market lookup error: {exc}")

        market_ticker = token_map["Up"].split("|")[0]
        market = self.get_market_snapshot(market_ticker)
        question = market.get("yes_sub_title", market_ticker) if market else market_ticker

        def _fetch(token_id: str, outcome: str) -> dict:
            try:
                book = self._get_book(market_ticker)
                bids, asks = self._book_levels_for_side(book, outcome)
                buy_price = bids[0]["price"] if bids else None
                sell_price = asks[0]["price"] if asks else None
                return {
                    "token_id": token_id,
                    "buy": buy_price,
                    "sell": sell_price,
                    "bids": bids,
                    "asks": asks,
                }
            except Exception as exc:
                return {"token_id": token_id, "error": str(exc)}

        results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(_fetch, tid, outcome): outcome
                for outcome, tid in token_map.items()
            }
            for future in as_completed(futures):
                outcome = futures[future]
                results[outcome] = future.result()

        data_list = []
        for outcome in ("Up", "Down"):
            data = results.get(outcome, {})
            data_list.append({
                "outcome": outcome,
                "token_id": token_map[outcome],
                "buy": data.get("buy"),
                "sell": data.get("sell"),
                "bids": data.get("bids"),
                "asks": data.get("asks"),
            })

        return {
            "question": question,
            "beat_start_ts": beat_ts,
            "server_ts": None,
            "local_ts": int(now_utc.timestamp()),
            "data": data_list,
        }

    @staticmethod
    def best_prices_from_orderbook(book: dict, side: str) -> tuple[float | None, float | None]:
        """Return (best_bid, best_ask) for Up or Down from a raw Kalshi orderbook response."""
        ob = book.get("orderbook_fp", book.get("orderbook", {}))
        yes_levels = ob.get("yes_dollars", [])
        no_levels = ob.get("no_dollars", [])

        best_yes_bid = float(yes_levels[-1][0]) if yes_levels else None
        best_no_bid = float(no_levels[-1][0]) if no_levels else None

        if side == "Up":
            bid = best_yes_bid
            ask = (1.0 - best_no_bid) if best_no_bid is not None else None
        else:
            bid = best_no_bid
            ask = (1.0 - best_yes_bid) if best_yes_bid is not None else None
        return bid, ask

    @staticmethod
    def outcome_from_market(market: dict) -> str | None:
        result = str(market.get("result", "")).lower()
        if result == "yes":
            return "Up"
        if result == "no":
            return "Down"
        status = str(market.get("status", "")).lower()
        if status not in ("settled", "finalized", "determined"):
            return None
        yes_bid = market.get("yes_bid_dollars")
        if yes_bid is not None:
            try:
                if float(yes_bid) > 0.9:
                    return "Up"
                if float(yes_bid) < 0.1:
                    return "Down"
            except ValueError:
                pass
        return None
