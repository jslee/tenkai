import config
from strategy import indicators as indicator_module


def _make_candles_asc() -> list[dict]:
    return [
        {
            "timestamp": "20260508090000",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 1000,
        },
        {
            "timestamp": "20260508090300",
            "open": 101,
            "high": 102,
            "low": 100,
            "close": 101,
            "volume": 1100,
        },
        {
            "timestamp": "20260508090600",
            "open": 102,
            "high": 103,
            "low": 101,
            "close": 102,
            "volume": 1200,
        },
        {
            "timestamp": "20260508090900",
            "open": 103,
            "high": 104,
            "low": 102,
            "close": 103,
            "volume": 1300,
        },
    ]


def test_compute_all_indicators_skips_duplicate_base_resample(monkeypatch):
    monkeypatch.setattr(config, "CANDLE_INTERVAL", 3)
    monkeypatch.setattr(config, "HTF_MULTIPLIER", 5)

    candles_desc = list(reversed(_make_candles_asc()))
    calls: list[int] = []
    original_resample = indicator_module.resample_candles

    def tracking_resample(candles_asc, interval):
        calls.append(interval)
        return original_resample(candles_asc, interval)

    monkeypatch.setattr(indicator_module, "resample_candles", tracking_resample)

    indicator_module.compute_all_indicators(candles_desc)

    assert calls == [15]


def test_resample_candles_does_not_merge_different_days_same_slot():
    candles_asc = [
        {
            "timestamp": "20260508090000",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "volume": 1000,
        },
        {
            "timestamp": "20260508090100",
            "open": 101,
            "high": 102,
            "low": 100,
            "close": 101,
            "volume": 1100,
        },
        {
            "timestamp": "20260509090000",
            "open": 200,
            "high": 201,
            "low": 199,
            "close": 200,
            "volume": 1200,
        },
        {
            "timestamp": "20260509090100",
            "open": 201,
            "high": 202,
            "low": 200,
            "close": 201,
            "volume": 1300,
        },
    ]

    resampled = indicator_module.resample_candles(candles_asc, 3)

    assert len(resampled) == 2
    assert resampled[0]["timestamp"] == "20260508090100"
    assert resampled[1]["timestamp"] == "20260509090100"
    assert resampled[0]["volume"] == 2100
    assert resampled[1]["volume"] == 2500


def test_calc_tick_weighted_imbalance():
    from strategy.indicators import calc_tick_weighted_imbalance

    # Test case 1: 빈 데이터 또는 데이터 부족 시 0.0 반환
    assert calc_tick_weighted_imbalance({}) == 0.0
    assert calc_tick_weighted_imbalance({"ask_volumes": [10]*5, "bid_volumes": [10]*5}) == 0.0

    # Test case 2: 완벽한 균형 상태
    orderbook_balanced = {
        "ask_volumes": [100] * 10,
        "bid_volumes": [100] * 10,
    }
    assert calc_tick_weighted_imbalance(orderbook_balanced) == 0.0

    # Test case 3: 매수 호가 잔량 우세 (수급 양수)
    orderbook_bid_heavy = {
        "ask_volumes": [10] * 10,
        "bid_volumes": [100] * 10,
    }
    imbalance = calc_tick_weighted_imbalance(orderbook_bid_heavy)
    assert imbalance > 0.0
    # (100 * 55 - 10 * 55) / (110 * 55) = 90 / 110 = 0.8181...
    assert abs(imbalance - 90 / 110) < 1e-4

    # Test case 4: 호가 잔량 총합은 같으나 근접 호가의 잔량 차이가 있어 가중치로 인한 변별력 확보 검증
    # 매도: 1호가 100, 나머지 10 (총합 190) -> 가중합 = 100*10 + 10*45 = 1450
    # 매수: 10호가 100, 나머지 10 (총합 190) -> 가중합 = 10*54 + 100*1 = 640
    # 잔량 총합은 190으로 같지만 매도 잔량이 현재가에 훨씬 가깝게 배치되어 있으므로 수급 지표는 음수(매도 우세)여야 함
    orderbook_tick_weighted = {
        "ask_volumes": [100] + [10] * 9,
        "bid_volumes": [10] * 9 + [100],
    }
    imbalance_weighted = calc_tick_weighted_imbalance(orderbook_tick_weighted)
    assert imbalance_weighted < 0.0

