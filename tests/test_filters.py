"""
tests/test_filters.py — 각 Gate 필터 단위 테스트

실행: python -m pytest tests/ -v
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch

import config

# ─────────────────────────────────────────────────────────────────────────────
# gate 테스트
# ─────────────────────────────────────────────────────────────────────────────


            indicators,
        )

        assert passed is False
        assert direction == "NEUTRAL"
        assert score == 1
        assert any("MACD-GX" in signal for signal in signals)

    def test_setup_adds_macd_dead_cross_bonus(self):
        from filters.gate2_signal import gate2_setup_filter

        indicators = self._make_setup_indicators(
            macd_prev=2.0,
            macd_signal_prev=3.0,
            macd_prev2=3.0,
            macd_signal_prev2=3.0,
            htf_ema_long=70000.0,
        )

        passed, score, direction, signals = gate2_setup_filter(
            self._make_closed_candles(
                latest_close=70000,
                latest_open=70000,
                latest_volume=500_000,
            ),
            indicators,
        )

        assert passed is False
        assert direction == "NEUTRAL"
        assert score == 1
        assert any("MACD-DX" in signal for signal in signals)

    def test_5m_window_counts_recent_rsi_hook_from_previous_loop(self, monkeypatch):
        from filters.gate2_signal import gate2_setup_filter

        monkeypatch.setattr(config, "CANDLE_INTERVAL", 5)

        first_indicators = self._make_setup_indicators(
            rsi=31.0,
            rsi_prev=28.0,
            htf_ema_long=70000.0,
        )
        first_candles = [
            {
                "open": 70000,
                "high": 70200,
                "low": 69900,
                "close": 70100,
                "volume": 500_000,
                "timestamp": "20260509095000",
            },
            {
                "open": 69900,
                "high": 70050,
                "low": 69800,
                "close": 69950,
                "volume": 450_000,
                "timestamp": "20260509094500",
            },
        ]

        gate2_setup_filter(first_candles, first_indicators)

        second_indicators = self._make_setup_indicators(htf_ema_long=70150.0)
        second_candles = [
            {
                "open": 70100,
                "high": 70250,
                "low": 70050,
                "close": 70150,
                "volume": 480_000,
                "timestamp": "20260509095500",
            },
            {
                "open": 70000,
                "high": 70100,
                "low": 69900,
                "close": 70100,
                "volume": 500_000,
                "timestamp": "20260509095000",
            },
        ]

        passed, score, direction, signals = gate2_setup_filter(
            second_candles, second_indicators
        )

        assert passed is False
        assert direction == "NEUTRAL"
        assert score == 3
        assert any("RSI:↑(095000)" in signal for signal in signals)

    def test_5m_window_rejects_signal_outside_timestamp_window(self, monkeypatch):
        from filters.gate2_signal import gate2_setup_filter

        monkeypatch.setattr(config, "CANDLE_INTERVAL", 5)

        first_indicators = self._make_setup_indicators(
            rsi=31.0,
            rsi_prev=28.0,
            htf_ema_long=70000.0,
        )
        first_candles = [
            {
                "open": 70000,
                "high": 70100,
                "low": 69900,
                "close": 70100,
                "volume": 500_000,
                "timestamp": "20260509095000",
            },
            {
                "open": 70000,
                "high": 70100,
                "low": 69900,
                "close": 70000,
                "volume": 500_000,
                "timestamp": "20260509094500",
            },
        ]

        gate2_setup_filter(first_candles, first_indicators)

        second_indicators = self._make_setup_indicators(htf_ema_long=70000.0)
        second_candles = [
            {
                "open": 70000,
                "high": 70100,
                "low": 69900,
                "close": 70000,
                "volume": 500_000,
                "timestamp": "20260509100500",
            },
            {
                "open": 70000,
                "high": 70100,
                "low": 69900,
                "close": 70000,
                "volume": 500_000,
                "timestamp": "20260509100000",
            },
        ]

        passed, score, direction, signals = gate2_setup_filter(
            second_candles, second_indicators
        )

        assert passed is False
        assert direction == "NEUTRAL"
        assert score == 0
        assert not any("RSI반등" in signal for signal in signals)

    def test_wrapper_blocks_buy_setup_when_live_bar_reverses(self):
        from filters.gate2_signal import gate2_signal_filter

        setup_indicators = self._make_setup_indicators(
            rsi=31.0,
            rsi_prev=28.0,
            macd_prev=-4.0,
            macd_signal_prev=-5.0,
            macd_prev2=-6.0,
            macd_signal_prev2=-5.0,
            ema_short=71000.0,
            ema_long=70000.0,
        )
        passed, score, direction, signals = gate2_signal_filter(
            self._make_closed_candles(),
            setup_indicators,
            live_candles=self._make_live_candles(
                current_open=70100,
                current_close=69700,
                prev_close=70000,
            ),
            live_indicators=self._make_setup_indicators(),
        )
        assert direction == "BUY"
        assert score >= 6
        assert any("TRIGGER역행음봉" in signal for signal in signals)

    def test_wrapper_allows_buy_setup_when_live_bar_confirms(self):
        from filters.gate2_signal import gate2_signal_filter

        setup_indicators = self._make_setup_indicators(
            rsi=31.0,
            rsi_prev=28.0,
            macd_prev=-4.0,
            macd_signal_prev=-5.0,
            macd_prev2=-6.0,
            macd_signal_prev2=-5.0,
            ema_short=71000.0,
            ema_long=70000.0,
        )
        live_indicators = self._make_setup_indicators(median_vol=1_000_000.0)

        passed, score, direction, signals = gate2_signal_filter(
            self._make_closed_candles(),
            setup_indicators,
            live_candles=self._make_live_candles(
                current_open=70020,
                current_close=70080,
                prev_close=70000,
                current_volume=900_000,
            ),
            live_indicators=live_indicators,
        )

        assert passed is True
        assert direction == "BUY"
        assert score >= 6
        assert any("TRIGGER중립" in signal for signal in signals)

    def test_wrapper_keeps_buy_setup_on_neutral_live_bar(self):
        from filters.gate2_signal import gate2_signal_filter

        setup_indicators = self._make_setup_indicators(
            rsi=31.0,
            rsi_prev=28.0,
            macd_prev=-4.0,
            macd_signal_prev=-5.0,
            macd_prev2=-6.0,
            macd_signal_prev2=-5.0,
            ema_short=71000.0,
            ema_long=70000.0,
        )

        passed, score, direction, signals = gate2_signal_filter(
            self._make_closed_candles(),
            setup_indicators,
            live_candles=self._make_live_candles(
                current_open=70000,
                current_close=70000,
                prev_close=70020,
                current_volume=200_000,
            ),
            live_indicators=self._make_setup_indicators(median_vol=1_000_000.0),
        )

        assert passed is True
        assert direction == "BUY"
        assert score >= 6
        assert any("TRIGGER중립(차단없음)" in signal for signal in signals)


# ─────────────────────────────────────────────────────────────────────────────
# RiskManager 테스트
# ─────────────────────────────────────────────────────────────────────────────


class TestRiskManager:
    """strategy/risk.py 리스크 관리 테스트"""

    def setup_method(self):
        from strategy.risk import RiskManager

        self.rm = RiskManager()
        self.rm.sync_daily_stats(total_assets=10_000_000)

    def test_can_enter_initial(self):
        can, reason = self.rm.can_enter()
        assert can is True

    def test_cannot_enter_when_position_exists(self):
        params = self.rm.calc_order_params(70000, 10_000_000)
        self.rm.open_position(
            "005930",
            "BUY",
            70000,
            int(params["qty"]),
            params["stop_loss"],
            params["take_profit"],
        )
        can, reason = self.rm.can_enter()
        assert can is False
        assert "포지션" in reason

    def test_calc_order_qty(self):
        params = self.rm.calc_order_params(current_price=70000, total_assets=10_000_000)
        expected_qty = int((10_000_000 * 0.1) / 70000)  # floor(1000000/70000) = 14
        assert params["qty"] == expected_qty

    def test_stop_loss_price(self):
        params = self.rm.calc_order_params(current_price=70000, total_assets=10_000_000)
        assert abs(params["stop_loss"] - 70000 * 0.98) < 1

    def test_take_profit_price(self):
        params = self.rm.calc_order_params(current_price=70000, total_assets=10_000_000)
        assert abs(params["take_profit"] - 70000 * 1.04) < 1

    def test_stop_loss_trigger(self):
        params = self.rm.calc_order_params(70000, 10_000_000)
        self.rm.open_position(
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
        )  # 2% 이하
        assert check["action"] == "STOP_LOSS"

    def test_trailing_stop_activation(self):
        params = self.rm.calc_order_params(70000, 10_000_000)
        self.rm.open_position(
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

    def test_trailing_stop_trigger(self):
        params = self.rm.calc_order_params(70000, 10_000_000)
        self.rm.open_position(
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

    def test_force_close_at_market_close(self):
        params = self.rm.calc_order_params(70000, 10_000_000)
        self.rm.open_position(
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

    def test_close_position_pnl_buy(self):
        params = self.rm.calc_order_params(70000, 10_000_000)
        qty = int(params["qty"])
        self.rm.open_position(
            "005930", "BUY", 70000, qty, params["stop_loss"], params["take_profit"]
        )
        pnl = self.rm.close_position(exit_price=71000)
        assert pnl == (71000 - 70000) * qty
        assert self.rm.has_position is False

    def test_halt_after_daily_loss_limit(self):
        params = self.rm.calc_order_params(70000, 10_000_000)
        qty = int(params["qty"])
        self.rm.open_position(
            "005930", "BUY", 70000, qty, params["stop_loss"], params["take_profit"]
        )
        # 큰 손실 발생 (초기 자산 10_000_000의 5% = 500_000 이상)
        self.rm.close_position(exit_price=30000)
        assert self.rm.halt_today is True
        can, _ = self.rm.can_enter()
        assert can is False

    def test_max_trades_per_day(self):
        import config as _cfg

        _cfg.MAX_TRADES_PER_DAY = 2
        params = self.rm.calc_order_params(70000, 10_000_000)
        qty = int(params["qty"])
        for _ in range(2):
            self.rm.open_position(
                "005930", "BUY", 70000, qty, params["stop_loss"], params["take_profit"]
            )
            self.rm.close_position(exit_price=70500)
        can, reason = self.rm.can_enter()
        assert can is False
        assert "횟수" in reason
        _cfg.MAX_TRADES_PER_DAY = 5  # 원복
