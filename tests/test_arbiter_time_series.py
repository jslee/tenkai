from pathlib import Path
import pandas as pd
import pytest
from strategy.arbiter import Arbiter
from strategy.risk import RiskManager


def test_build_time_series_in_charts():
    risk = RiskManager()
    arbiter = Arbiter(
        ticker="005930",
        stock_name="삼성전자",
        intervals=(3, 5),
        output_dir=Path("./charts"),
        risk=risk,
    )

    # Mock data for 3m chart
    df_3m = pd.DataFrame(
        [
            {
                "label": "06-20 09:30",
                "open": 50000.0,
                "high": 51000.0,
                "low": 49000.0,
                "close": 50500.0,
                "volume": 10000.0,
                "bb_upper": 51200.5,
                "bb_inner_upper": 50600.0,
                "bb_mid": 50000.0,
                "bb_inner_lower": 49400.0,
                "bb_lower": 48799.5,
                "ema_short": 50100.1,
                "ema_long": 49800.2,
                "ema_trend": 49500.3,
                "rsi": 55.5,
                "macd": 100.5,
                "macd_signal": 80.2,
                "macd_hist": 20.3,
            }
        ]
    )

    snapshot = {"interval_snapshots": {3: {"chart_frame": df_3m}}}

    result = arbiter.build_time_series_of_charts(snapshot)

    # Verify section title
    assert "### 3분봉 차트 시계열 데이터" in result
    # Verify headers
    assert (
        "시간 | 시가 | 고가 | 저가 | 종가 | 거래량 | BB상한(2.0) | BB상한(1.0) | BB기준 | BB하한(1.0) | BB하한(2.0)" in result
    )
    # Verify formatted row values
    assert "06-20 09:30" in result
    assert "50,000" in result
    assert "51,000" in result
    assert "49,000" in result
    assert "50,500" in result
    assert "10,000" in result
    assert "51,200.50" in result
    assert "50,600.00" in result
    assert "50,000.00" in result
    assert "49,400.00" in result
    assert "48,799.50" in result
    assert "50,100.10" in result
    assert "49,800.20" in result
    assert "49,500.30" in result
    assert "55.50" in result
    assert "100.50" in result
    assert "80.20" in result
    assert "20.30" in result
