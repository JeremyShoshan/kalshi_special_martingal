import sys
sys.path.insert(0, r"c:\Users\Jeremy\Documents\projects\kalshi_special_martingal")
"""
Unit tests and self-verification suite for Special Martingale (Pattern C: Trend Transition Engine).
Tests:
1. Streak detection correctness.
2. Pattern C trigger progression:
   - Step 0 (Step 1/3): FADE streak (1x base stake, e.g. $10)
   - Step 1 (Step 2/3): FOLLOW streak (2x base stake, e.g. $20)
   - Step 2 (Step 3/3): FADE streak (4x base stake, e.g. $40)
3. Lifecycle state transitions (Wins, Losses, Hard Cap resets).
4. Skip handling (Unfilled maker orders, Volatility/Delta filter skips).
5. Historical state simulation (simulate_state_from_history).
"""

from bot_live import (
    get_consecutive_streak,
    check_special_martingale_trigger,
    TradeBot,
    simulate_state_from_history,
)


def test_streak_detection():
    print("Testing streak detection...")
    assert get_consecutive_streak(["Up", "Up"]) == (2, "Up")
    assert get_consecutive_streak(["Down", "Down", "Down"]) == (3, "Down")
    assert get_consecutive_streak(["Up", "Down", "Up"]) == (1, "Up")
    assert get_consecutive_streak([]) == (0, None)
    print("✓ test_streak_detection PASSED")


def test_special_martingale_trigger_rules():
    print("\nTesting Pattern C trigger rules (FADE-FOLLOW-FADE)...")
    # 1. Insufficient streak
    trig, side, act, stake, slen, sside = check_special_martingale_trigger(
        outcomes=["Up"], consecutive_streak_count=2, base_stake_usd=10.0
    )
    assert not trig and side is None

    # 2. Trigger on 2 Ups: Step 0 is FADE Down ($10)
    trig, side, act, stake, slen, sside = check_special_martingale_trigger(
        outcomes=["Up", "Up"], consecutive_streak_count=2, base_stake_usd=10.0
    )
    assert trig and side == "Down" and act == "FADE" and stake == 10.0 and slen == 2 and sside == "Up"

    # 3. Active campaign at Step 0 -> FADE Down ($10)
    trig, side, act, stake, slen, sside = check_special_martingale_trigger(
        outcomes=["Up", "Up"],
        in_campaign=True,
        campaign_step=0,
        campaign_trigger_dir="Up",
        base_stake_usd=10.0,
        stake_multipliers=[1.0, 2.0, 4.0],
    )
    assert trig and side == "Down" and act == "FADE" and stake == 10.0

    # 4. Active campaign at Step 1 (Step 2/3) -> FOLLOW Up ($20)
    trig, side, act, stake, slen, sside = check_special_martingale_trigger(
        outcomes=["Up", "Up", "Up"], # Step 0 lost because streak continued to 3
        in_campaign=True,
        campaign_step=1,
        campaign_trigger_dir="Up",
        base_stake_usd=10.0,
        stake_multipliers=[1.0, 2.0, 4.0],
    )
    assert trig and side == "Up" and act == "FOLLOW" and stake == 20.0

    # 5. Active campaign at Step 2 (Step 3/3) -> FADE Down ($40)
    trig, side, act, stake, slen, sside = check_special_martingale_trigger(
        outcomes=["Up", "Up", "Up", "Down"], # Step 1 lost because streak reversed at 4
        in_campaign=True,
        campaign_step=2,
        campaign_trigger_dir="Up",
        base_stake_usd=10.0,
        stake_multipliers=[1.0, 2.0, 4.0],
    )
    assert trig and side == "Down" and act == "FADE" and stake == 40.0
    print("✓ test_special_martingale_trigger_rules PASSED")


def test_campaign_transitions_and_settlement():
    print("\nTesting full campaign lifecycle and state transitions...")
    bot = TradeBot(base_stake=10.0, starting_balance=1000.0)

    # Case A: Win on Step 1 (Fade wins immediately)
    # Streak 2 Up detected -> Bet FADE Down ($10).
    # Actual outcome: Down (Win!)
    bot.record_trade({
        "signal_ts": 1000,
        "buy_local_ts": 1000,
        "sell_local_ts": 1900,
        "side": "Down",
        "entry_ask": 0.50,
        "exit_bid": 1.00,
        "cost": 10.0,
        "shares": 20.0,
        "pnl": 10.0,
        "won": True,
        "result": "WIN",
    })
    bot.record_campaign_result(True)
    assert bot.state.trade_num == 1
    assert bot.state.wins == 1
    assert bot.state.campaigns == 1
    assert bot.state.campaign_wins == 1
    assert bot.state.campaign_losses == 0
    assert bot.state.campaign_winrate == 100.0
    print("  ✓ Case A (Step 1 Fade Win -> Reset) Verified.")

    # Case B: Loss on Step 1, Win on Step 2 (Trend Follower Wins)
    # Step 1: Bet Fade Down ($10). Actual: Up (Loss)
    bot.record_trade({
        "signal_ts": 2000,
        "buy_local_ts": 2000,
        "sell_local_ts": 2900,
        "side": "Down",
        "entry_ask": 0.50,
        "exit_bid": 0.00,
        "cost": 10.0,
        "shares": 20.0,
        "pnl": -10.0,
        "won": False,
        "result": "LOSS",
    })
    # Step advances to Step 2 ($20 FOLLOW Up). Actual: Up (Win!)
    bot.record_trade({
        "signal_ts": 3000,
        "buy_local_ts": 3000,
        "sell_local_ts": 3900,
        "side": "Up",
        "entry_ask": 0.50,
        "exit_bid": 1.00,
        "cost": 20.0,
        "shares": 40.0,
        "pnl": 20.0,
        "won": True,
        "result": "WIN",
    })
    bot.record_campaign_result(True)
    assert bot.state.trade_num == 3
    assert bot.state.wins == 2
    assert bot.state.losses == 1
    assert bot.state.campaigns == 2
    assert bot.state.campaign_wins == 2
    assert bot.state.campaign_losses == 0
    assert bot.state.campaign_winrate == 100.0
    print("  ✓ Case B (Step 1 Loss, Step 2 Follow Win -> Reset) Verified.")

    # Case C: Hard Cap 3 Reached (Sequence Loss)
    # Step 1 Loss ($10), Step 2 Loss ($20), Step 3 Loss ($40) -> Total loss = -$70
    bot.record_trade({
        "signal_ts": 4000, "buy_local_ts": 4000, "sell_local_ts": 4900,
        "side": "Down", "entry_ask": 0.50, "exit_bid": 0.0, "cost": 10.0, "shares": 20.0,
        "pnl": -10.0, "won": False, "result": "LOSS"
    })
    bot.record_trade({
        "signal_ts": 5000, "buy_local_ts": 5000, "sell_local_ts": 5900,
        "side": "Up", "entry_ask": 0.50, "exit_bid": 0.0, "cost": 20.0, "shares": 40.0,
        "pnl": -20.0, "won": False, "result": "LOSS"
    })
    bot.record_trade({
        "signal_ts": 6000, "buy_local_ts": 6000, "sell_local_ts": 6900,
        "side": "Down", "entry_ask": 0.50, "exit_bid": 0.0, "cost": 40.0, "shares": 80.0,
        "pnl": -40.0, "won": False, "result": "LOSS"
    })
    bot.record_campaign_result(False)
    assert bot.state.trade_num == 6
    assert bot.state.wins == 2
    assert bot.state.losses == 4
    assert bot.state.campaigns == 3
    assert bot.state.campaign_wins == 2
    assert bot.state.campaign_losses == 1
    assert round(bot.state.campaign_winrate, 1) == 66.7
    print("  ✓ Case C (Hard Cap 3 Stop Loss -> Reset) Verified.")
    print("✓ test_campaign_transitions_and_settlement PASSED")


def test_skip_cases_and_unfilled():
    print("\nTesting skip cases and unfilled order mechanics...")
    bot = TradeBot(base_stake=10.0)

    # 1. Unfilled Maker Order (stop_maker_sec timeout)
    bot.record_skipped_trade(won=True)
    bot.record_skipped_trade(won=False)
    assert bot.state.skipped_count == 2
    assert bot.state.skipped_wins == 1
    assert bot.state.skipped_losses == 1
    assert bot.state.trade_num == 0, "Unfilled orders must NOT count as executed trades"
    assert bot.state.total_stake == 0.0, "Unfilled orders risk $0 capital"
    print("  ✓ Unfilled maker order tracking Verified ($0 capital spent).")

    # 2. Filter Skips (Delta filter body < $75)
    bot.record_filter_skipped_trade(won=True)
    bot.record_filter_skipped_trade(won=False)
    assert bot.state.filter_skipped_count == 2
    assert bot.state.filter_skipped_wins == 1
    assert bot.state.filter_skipped_losses == 1
    print("  ✓ Filter skip tracking Verified.")
    print("✓ test_skip_cases_and_unfilled PASSED")


def test_simulate_state_from_history():
    print("\nTesting simulate_state_from_history...")
    history = [
        (100, "Up"),
        (200, "Down"),
        (300, "Up"),
        (400, "Up"),  # streak of 2 Up!
    ]
    sim = simulate_state_from_history(history, history_len=10, consecutive_streak_count=2)
    assert sim["triggered"], "Should be triggered on 2 Ups"
    assert sim["bet_side"] == "Down", "Step 0 must FADE Down"
    assert sim["action_type"] == "FADE"
    assert sim["stake_usd"] == 10.0

    print("✓ test_simulate_state_from_history PASSED")


if __name__ == "__main__":
    print("================ RUNNING SPECIAL MARTINGALE SELF-VERIFICATION ================\n")
    test_streak_detection()
    test_special_martingale_trigger_rules()
    test_campaign_transitions_and_settlement()
    test_skip_cases_and_unfilled()
    test_simulate_state_from_history()
    print("\n================ ALL TESTS PASSED SUCCESSFULLY (100% CORRECT) ================")
