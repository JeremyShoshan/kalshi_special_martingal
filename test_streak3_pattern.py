import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
"""
Self-verification for the Streak-3 Double-Fade Breakout (FADE-FADE-FOLLOW) campaign.

1. Pattern helpers (normalize_pattern / get_campaign_bet / trigger) for the new pattern.
2. Backward compatibility: default pattern is still FADE-FOLLOW-FADE.
3. Cross-check against the real order-book replay: every order in
   logs/streak3_trades.csv (written by `run_simulation.py --config config.json
   --trades-out logs/streak3_trades.csv`) is re-derived with bot_live's state
   machine rules (win -> reset, loss -> next step, cap -> reset, unfilled Step 1
   + streak broke -> reset, otherwise keep step). Any step/side/stake mismatch fails.
"""

import csv
import json

from bot_live import (
    normalize_pattern,
    get_campaign_bet,
    check_special_martingale_trigger,
    simulate_state_from_history,
)

ROOT = os.path.dirname(os.path.abspath(__file__))
PATTERN = ["FADE", "FADE", "FOLLOW"]
MULTS = [1.0, 2.0, 3.0]


def test_normalize_pattern():
    assert normalize_pattern(None) == ["FADE", "FOLLOW", "FADE"]
    assert normalize_pattern("fade,fade,follow") == PATTERN
    assert normalize_pattern("FADE-FADE-FOLLOW") == PATTERN
    assert normalize_pattern(["Fade", "fade", "FOLLOW"]) == PATTERN
    try:
        normalize_pattern(["FADE", "FLIP"])
        raise AssertionError("invalid pattern accepted")
    except ValueError:
        pass
    print("OK test_normalize_pattern")


def test_get_campaign_bet_streak3():
    # Trigger dir Up (3 Ups): S1 FADE Down $10, S2 FADE Down $20, S3 FOLLOW Up $30
    assert get_campaign_bet(0, "Up", PATTERN, MULTS, 10.0, 3) == ("FADE", "Down", 10.0)
    assert get_campaign_bet(1, "Up", PATTERN, MULTS, 10.0, 3) == ("FADE", "Down", 20.0)
    assert get_campaign_bet(2, "Up", PATTERN, MULTS, 10.0, 3) == ("FOLLOW", "Up", 30.0)
    assert get_campaign_bet(2, "Down", PATTERN, MULTS, 10.0, 3) == ("FOLLOW", "Down", 30.0)
    print("OK test_get_campaign_bet_streak3")


def test_trigger_streak3():
    kw = dict(consecutive_streak_count=3, stake_multipliers=MULTS, base_stake_usd=10.0, pattern=PATTERN)
    assert not check_special_martingale_trigger(["Up", "Up"], **kw)[0]
    trig, side, act, stake, slen, sside = check_special_martingale_trigger(["Down", "Up", "Up", "Up"], **kw)
    assert (trig, side, act, stake, slen, sside) == (True, "Down", "FADE", 10.0, 3, "Up")
    # exactly-N rule: a 4-streak is not a fresh trigger
    assert not check_special_martingale_trigger(["Up", "Up", "Up", "Up"], **kw)[0]
    # in-campaign continuation
    r = check_special_martingale_trigger(["Up"] * 4, in_campaign=True, campaign_step=1, campaign_trigger_dir="Up", **kw)
    assert r[:4] == (True, "Down", "FADE", 20.0)
    r = check_special_martingale_trigger(["Up"] * 5, in_campaign=True, campaign_step=2, campaign_trigger_dir="Up", **kw)
    assert r[:4] == (True, "Up", "FOLLOW", 30.0)
    print("OK test_trigger_streak3")


def test_default_pattern_backward_compatible():
    kw = dict(in_campaign=True, campaign_trigger_dir="Up", stake_multipliers=[1.0, 2.0, 4.0], base_stake_usd=10.0)
    assert check_special_martingale_trigger(["Up"], campaign_step=0, **kw)[1:4] == ("Down", "FADE", 10.0)
    assert check_special_martingale_trigger(["Up"], campaign_step=1, **kw)[1:4] == ("Up", "FOLLOW", 20.0)
    assert check_special_martingale_trigger(["Up"], campaign_step=2, **kw)[1:4] == ("Down", "FADE", 40.0)
    print("OK test_default_pattern_backward_compatible")


def test_history_sim_streak3():
    hist = [(i, o) for i, o in enumerate(["Down", "Up", "Up", "Up", "Up", "Up"])]
    # trigger at 3rd Up -> S1 FADE lost (4th Up) -> S2 FADE lost (5th Up) -> now S3 FOLLOW Up $30
    sim = simulate_state_from_history(hist, history_len=10, consecutive_streak_count=3,
                                      max_cap=3, stake_multipliers=MULTS, pattern=PATTERN)
    assert sim["campaign_step"] == 2 and sim["bet_side"] == "Up" and sim["action_type"] == "FOLLOW"
    assert sim["stake_usd"] == 30.0
    print("OK test_history_sim_streak3")


def test_replay_log_matches_bot_state_machine():
    path = os.path.join(ROOT, "logs", "streak3_trades.csv")
    if not os.path.exists(path):
        print("SKIP test_replay_log_matches_bot_state_machine (run run_simulation.py --trades-out logs/streak3_trades.csv)")
        return
    with open(os.path.join(ROOT, "config.json")) as f:
        cfg = json.load(f)
    pattern = normalize_pattern(cfg["pattern"])
    mults = [float(x) for x in cfg["stake_multipliers"]]
    base = float(cfg["base_stake_usd"])
    max_cap = int(cfg["max_cap"])

    in_camp, step, tdir = False, 0, None
    checked = 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            r_step = int(row["step"]) - 1
            if in_camp:
                # The bot must be continuing this campaign with exactly this bet.
                act, side, stake = get_campaign_bet(step, tdir, pattern, mults, base, max_cap)
                assert r_step == step, f"{row['beat']}: step {r_step} != bot {step}"
                assert (row["action"], row["side"], float(row["stake"])) == (act, side, stake), f"{row['beat']}: {row} vs {(act, side, stake)}"
            else:
                # Fresh trigger: Step 1, side = fade of the streak.
                assert r_step == 0, f"{row['beat']}: campaign started at step {r_step + 1}"
                act, side, stake = get_campaign_bet(0, "Up", pattern, mults, base, max_cap)
                tdir = "Up" if row["side"] == side else "Down"
                assert get_campaign_bet(0, tdir, pattern, mults, base, max_cap) == (row["action"], row["side"], float(row["stake"]))
                in_camp, step = True, 0
            checked += 1

            outcome = row["outcome"]
            filled = float(row["shares"]) > 1e-9
            if not filled:
                # bot_live skipped-evaluation rule
                if step == 0 and outcome != tdir:
                    in_camp, step, tdir = False, 0, None
            elif outcome == row["side"]:
                in_camp, step, tdir = False, 0, None          # campaign won
            else:
                step += 1
                if step >= max_cap:
                    in_camp, step, tdir = False, 0, None      # hard cap
    assert checked > 100
    print(f"OK test_replay_log_matches_bot_state_machine ({checked} orders cross-checked)")


if __name__ == "__main__":
    test_normalize_pattern()
    test_get_campaign_bet_streak3()
    test_trigger_streak3()
    test_default_pattern_backward_compatible()
    test_history_sim_streak3()
    test_replay_log_matches_bot_state_machine()
    print("ALL STREAK-3 PATTERN TESTS PASSED")
