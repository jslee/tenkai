"""
tests/test_filters.py — 각 Gate 필터 단위 테스트

실행:
python -m pytest tests/ -v
python -m pytest tests/test_filters.py -k test_stop_loss_price -s --log-cli-level=INFO
"""

from datetime import datetime
import pytest
import config
from filters import gate_market_filter


def test_gate_market_filter_passed(monkeypatch):
    monkeypatch.setattr(config, "MARKET_DROP_THRESHOLD", -2.0, raising=False)
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_RATIO", 0.05)

    market_data = {
        "market_change": -0.5,
        "current_volume": 10000,
        "circuit_breaker": False,
        "daily_loss_ratio": 0.01,
    }

    passed, result = gate_market_filter(market_data)
    assert passed is True
    assert result["passed"] is True
    assert result["halt_trading_today"] is False


@pytest.mark.skip(
    reason="Market drop check is disabled by user in config and gate_market"
)
def test_gate_market_filter_market_drop(monkeypatch):
    monkeypatch.setattr(config, "MARKET_DROP_THRESHOLD", -2.0, raising=False)

    market_data = {
        "market_change": -2.5,
        "current_volume": 10000,
        "circuit_breaker": False,
        "daily_loss_ratio": 0.01,
    }

    passed, result = gate_market_filter(market_data)
    assert passed is False
    assert result["passed"] is False
    assert any(
        "폭락" in check["detail"] for check in result["checks"] if not check["passed"]
    )


def test_gate_market_filter_zero_volume():
    market_data = {
        "market_change": 0.0,
        "current_volume": 0,
        "circuit_breaker": False,
        "daily_loss_ratio": 0.0,
    }

    passed, result = gate_market_filter(market_data)
    assert passed is False
    assert result["passed"] is False
    assert any(
        "거래량 없음" in check["detail"]
        for check in result["checks"]
        if not check["passed"]
    )


def test_gate_market_filter_circuit_breaker():
    market_data = {
        "market_change": 0.0,
        "current_volume": 100,
        "circuit_breaker": True,
        "daily_loss_ratio": 0.0,
    }

    passed, result = gate_market_filter(market_data)
    assert passed is False
    assert result["passed"] is False
    assert any(
        "서킷브레이커 발동" in check["detail"]
        for check in result["checks"]
        if not check["passed"]
    )


def test_gate_market_filter_daily_loss_limit(monkeypatch):
    monkeypatch.setattr(config, "MAX_DAILY_LOSS_RATIO", 0.05)

    market_data = {
        "market_change": 0.0,
        "current_volume": 100,
        "circuit_breaker": False,
        "daily_loss_ratio": 0.06,
    }

    passed, result = gate_market_filter(market_data)
    assert passed is False
    assert result["passed"] is False
    assert result["halt_trading_today"] is True
    assert any(
        "손실 한도 초과" in check["detail"]
        for check in result["checks"]
        if not check["passed"]
    )


class TestRiskManager:
    """strategy/risk.py 리스크 관리 테스트"""

    def setup_method(self):
        from strategy.risk import RiskManager

        self.rm = RiskManager()
        self.rm.sync_daily_stats(total_assets=10_000_000)

    def test_can_enter_initial(self):
        can, reason = self.rm.can_enter(70000, 10_000_000)
        assert can is True

    def test_cannot_enter_when_position_exists(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_POSITION_RATIO", 0.5)
        params = self.rm.calc_order_params(70000, 10_000_000)
        # 10_000_000 * 0.5 = 5_000_000 is MAX_POSITION_RATIO limit.
        # Add a position of 80 shares (80 * 70,000 = 5,600,000), which exceeds the limit.
        self.rm.add_position(
            "005930",
            "BUY",
            70000,
            80,
            params["stop_loss"],
            params["take_profit"],
        )
        can, reason = self.rm.can_enter(70000, 10_000_000)
        assert can is False
        assert "한도" in reason

    def test_calc_order_qty(self, monkeypatch):
        monkeypatch.setattr(config, "SINGLE_TRADE_RATIO", 0.1)
        params = self.rm.calc_order_params(current_price=70000, total_assets=10_000_000)
        expected_qty = int((10_000_000 * 0.1) / 70000)  # floor(1000000/70000) = 14
        assert params["qty"] == expected_qty

    def test_stop_loss_price(self, monkeypatch):
        monkeypatch.setattr(config, "STOP_LOSS_RATIO", 0.02)
        params = self.rm.calc_order_params(
            current_price=70000, total_assets=10_000_000, atr=300.0
        )
        assert abs(params["stop_loss"] - 70000 * 0.98) < 1

    def test_take_profit_price(self, monkeypatch):
        monkeypatch.setattr(config, "TAKE_PROFIT_RATIO", 0.04)
        params = self.rm.calc_order_params(current_price=70000, total_assets=10_000_000)
        assert abs(params["take_profit"] - 70000 * 1.04) < 1

    def test_stop_loss_trigger(self, monkeypatch):
        monkeypatch.setattr(config, "STOP_LOSS_RATIO", 0.02)
        monkeypatch.setattr(config, "TAKE_PROFIT_RATIO", 0.04)
        params = self.rm.calc_order_params(70000, 10_000_000)
        self.rm.add_position(
            "005930",
            "BUY",
            70000,
            int(params["qty"]),
            params["stop_loss"],
            params["take_profit"],
        )
        trading_time = datetime(2026, 3, 28, 10, 0, 0)
        check = self.rm.check_position(
            current_price=68550, current_time=trading_time
        )  # 2% 이하 (68600 이하)
        assert check["action"] == "STOP_LOSS"

    def test_trailing_stop_activation(self, monkeypatch):
        monkeypatch.setattr(config, "STOP_LOSS_RATIO", 0.02)
        monkeypatch.setattr(config, "TAKE_PROFIT_RATIO", 0.04)
        params = self.rm.calc_order_params(70000, 10_000_000)
        self.rm.add_position(
            "005930",
            "BUY",
            70000,
            int(params["qty"]),
            params["stop_loss"],
            params["take_profit"],
        )
        trading_time = datetime(2026, 3, 28, 10, 0, 0)
        # 익절가(72800) 도달
        check = self.rm.check_position(current_price=72800, current_time=trading_time)
        assert check["action"] == "HOLD"
        assert self.rm.position.trailing_active is True  # type: ignore

    def test_trailing_stop_trigger(self, monkeypatch):
        monkeypatch.setattr(config, "STOP_LOSS_RATIO", 0.02)
        monkeypatch.setattr(config, "TAKE_PROFIT_RATIO", 0.04)
        monkeypatch.setattr(config, "TRAILING_STOP_RATIO", 0.015)
        params = self.rm.calc_order_params(70000, 10_000_000)
        self.rm.add_position(
            "005930",
            "BUY",
            70000,
            int(params["qty"]),
            params["stop_loss"],
            params["take_profit"],
        )
        trading_time = datetime(2026, 3, 28, 10, 0, 0)
        # 익절가 도달하여 트레일링 활성화
        self.rm.check_position(current_price=73000, current_time=trading_time)
        # 트레일링 스톱 = 73000 * (1 - 0.015) = 71905
        check = self.rm.check_position(current_price=71900, current_time=trading_time)
        assert check["action"] == "TRAILING_STOP"

    def test_force_close_at_market_close(self, monkeypatch):
        monkeypatch.setattr(config, "HOLD_OVERNIGHT", False)
        params = self.rm.calc_order_params(70000, 10_000_000)
        self.rm.add_position(
            "005930",
            "BUY",
            70000,
            int(params["qty"]),
            params["stop_loss"],
            params["take_profit"],
        )
        close_time = datetime(2026, 3, 28, 16, 0, 0)
        check = self.rm.check_position(current_price=70500, current_time=close_time)
        assert check["action"] == "FORCE_CLOSE_MARKET"

    def test_close_position_pnl_buy(self, monkeypatch):
        params = self.rm.calc_order_params(70000, 10_000_000)
        qty = int(params["qty"])
        self.rm.add_position(
            "005930", "BUY", 70000, qty, params["stop_loss"], params["take_profit"]
        )
        pnl = self.rm.close_position(exit_price=71000)
        assert pnl == (71000 - 70000) * qty
        assert self.rm.has_position is False

    def test_halt_after_daily_loss_limit(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_DAILY_LOSS_RATIO", 0.05)
        params = self.rm.calc_order_params(70000, 10_000_000)
        qty = int(params["qty"])
        self.rm.add_position(
            "005930", "BUY", 70000, qty, params["stop_loss"], params["take_profit"]
        )
        # 큰 손실 발생 (초기 자산 10_000_000의 5% = 500_000 이상)
        self.rm.close_position(exit_price=30000)
        assert self.rm.halt_today is True
        can, _ = self.rm.can_enter(70000, 10_000_000)
        assert can is False

    def test_max_trades_per_day(self, monkeypatch):
        monkeypatch.setattr(config, "MAX_TRADES_PER_DAY", 2)
        params = self.rm.calc_order_params(70000, 10_000_000)
        qty = int(params["qty"])
        for _ in range(2):
            self.rm.add_position(
                "005930", "BUY", 70000, qty, params["stop_loss"], params["take_profit"]
            )
            self.rm.close_position(exit_price=70500)
        can, reason = self.rm.can_enter(70000, 10_000_000)
        assert can is False
        assert "횟수" in reason
