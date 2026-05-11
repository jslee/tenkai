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
