from datetime import datetime, timedelta

import config
from strategy.risk import RiskManager


def test_monitor_interval_stays_default_when_far_from_exit_levels(monkeypatch):
    monkeypatch.setattr(config, "POSITION_CHECK_SEC", 10)
    monkeypatch.setattr(config, "POSITION_CHECK_FAST_SEC", 3)
    monkeypatch.setattr(config, "POSITION_CHECK_FAST_ATR_BUFFER", 1.0)

    risk = RiskManager()
    risk.update_atr(100.0)
    risk.open_position(
        ticker="005930",
        direction="BUY",
        entry_price=1000,
        qty=1,
        stop_loss=900,
        take_profit=1200,
    )

    assert risk.get_monitor_interval(1050) == 10


def test_monitor_interval_switches_to_fast_near_exit_levels(monkeypatch):
    monkeypatch.setattr(config, "POSITION_CHECK_SEC", 10)
    monkeypatch.setattr(config, "POSITION_CHECK_FAST_SEC", 3)
    monkeypatch.setattr(config, "POSITION_CHECK_FAST_ATR_BUFFER", 1.0)

    risk = RiskManager()
    risk.update_atr(100.0)
    risk.open_position(
        ticker="005930",
        direction="BUY",
        entry_price=1000,
        qty=1,
        stop_loss=900,
        take_profit=1200,
    )

    assert risk.get_monitor_interval(970) == 3

    pos = risk.position
    assert pos is not None
    pos.trailing_active = True
    pos.trailing_stop = 990

    assert risk.get_monitor_interval(1020) == 3


def test_velocity_stop_detects_drawdown_from_recent_peak(monkeypatch):
    monkeypatch.setattr(config, "VELOCITY_STOP_ENABLED", True)
    monkeypatch.setattr(config, "VELOCITY_STOP_WINDOW_SEC", 30)
    monkeypatch.setattr(config, "VELOCITY_STOP_ATR_MULT", 3.0)
    monkeypatch.setattr(config, "VELOCITY_STOP_MIN_TICKS", 4)
    monkeypatch.setattr(config, "VELOCITY_STOP_DOWN_TICK_RATIO", 0.66)
    monkeypatch.setattr(config, "VELOCITY_STOP_CUTOFF_TIME", "23:59")

    risk = RiskManager()
    risk.update_atr(100.0)

    now = datetime.now()
    risk.record_price_tick(1000, now - timedelta(seconds=24))
    risk.record_price_tick(1100, now - timedelta(seconds=18))
    risk.record_price_tick(950, now - timedelta(seconds=12))
    risk.record_price_tick(780, now - timedelta(seconds=6))

    triggered, reason = risk._check_velocity_stop(780)

    assert triggered is True
    assert "고점대비급락" in reason
    assert "1,100" in reason


def test_velocity_stop_ignores_choppy_noise_without_downward_bias(monkeypatch):
    monkeypatch.setattr(config, "VELOCITY_STOP_ENABLED", True)
    monkeypatch.setattr(config, "VELOCITY_STOP_WINDOW_SEC", 30)
    monkeypatch.setattr(config, "VELOCITY_STOP_ATR_MULT", 3.0)
    monkeypatch.setattr(config, "VELOCITY_STOP_MIN_TICKS", 5)
    monkeypatch.setattr(config, "VELOCITY_STOP_DOWN_TICK_RATIO", 0.67)
    monkeypatch.setattr(config, "VELOCITY_STOP_CUTOFF_TIME", "23:59")

    risk = RiskManager()
    risk.update_atr(50.0)

    now = datetime.now()
    risk.record_price_tick(1000, now - timedelta(seconds=25))
    risk.record_price_tick(1200, now - timedelta(seconds=20))
    risk.record_price_tick(1040, now - timedelta(seconds=15))
    risk.record_price_tick(1120, now - timedelta(seconds=10))
    risk.record_price_tick(1040, now - timedelta(seconds=5))

    triggered, _ = risk._check_velocity_stop(1040)

    assert triggered is False
