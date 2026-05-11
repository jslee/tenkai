"""
strategy/indicators.py — 기술 지표 계산
계산 대상: RSI, MACD, EMA, 볼린저밴드, ATR
모든 함수는 동기(sync) — I/O 없음
"""

from typing import Any
import math
import logging
import config

logger = logging.getLogger(__name__)


def _ema(prices: list[float], period: int) -> list[float]:
    """지수이동평균(EMA)을 계산한다. prices는 오래된 것부터 최신 순.

    시딩 방식: prices[0]을 초기값으로 사용 (TradingView / 대부분 증권사 차트 표준).
    SMA 시딩은 period개 데이터를 소비해 Signal 수렴이 늦어지는 문제가 있어,
    첫 가격 시딩으로 변경하면 제한된 캔들 수(400~500개) 내에서도 더 빠르게 수렴한다.
    """
    if not prices:
        return []
    k = 2 / (period + 1)
    result: list[float] = [prices[0]]
    for price in prices[1:]:
        result.append(price * k + result[-1] * (1 - k))
    return result


def _sma(prices: list[float], period: int) -> list[float]:
    """단순이동평균(SMA) 시계열을 계산한다."""
    result = []
    for i in range(len(prices)):
        if i + 1 >= period:
            result.append(sum(prices[i - period + 1 : i + 1]) / period)
        else:
            result.append(sum(prices[: i + 1]) / (i + 1))
    return result


def calc_rsi(closes: list[float], period: int = config.RSI_PERIOD) -> dict[str, float]:
    """
    RSI를 계산한다.

    Args:
        closes: 종가 리스트 (오래된 것부터 최신 순)
        period: RSI 기간

    Returns:
        {"rsi": float, "rsi_prev": float}
    """
    default = {"rsi": 50.0, "rsi_prev": 50.0}
    # [개선] Wilder's smoothing의 안정성을 위해 충분한 데이터(최소 period * 2) 확보 확인
    if len(closes) < period * 2:
        return default

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    # 초기 평균 계산 (SMA 방식)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    rsi_series = []
    if avg_loss == 0:
        rsi_series.append(100.0 if avg_gain > 0 else 50.0)
    else:
        rsi_series.append(100 - (100 / (1 + avg_gain / avg_loss)))

    # Wilder's smoothing 적용
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi_series.append(100.0 if avg_gain > 0 else 50.0)
        else:
            rsi_series.append(100 - (100 / (1 + avg_gain / avg_loss)))

    return {
        "rsi": rsi_series[-1] if rsi_series else 50.0,
        "rsi_prev": rsi_series[-2] if len(rsi_series) >= 2 else 50.0,
    }


def calc_atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> dict[str, float]:
    """
    ATR(Average True Range)을 계산한다.
    ATR은 시장의 변동성을 측정하는 기술적 지표로,
    특정 기간(보통 14일) 동안의 고가, 저가, 전일 종가를 고려한 TR(True Range)의 평균값.
    이는 가격의 방향이 아닌 변동 폭(위험도)을 나타내며, 손절매 및 포지션 규모 설정에 활용된다.

    해석:
        - ATR 상승: 시장의 변동성이 커짐 (추세 형성 또는 급등락).
        - ATR 하락: 시장의 변동성이 줄어듦 (횡보 또는 숨 고르기).
        - 높은 ATR: 바닥권에서 투매 발생 시나 시장 공황 상황.

    ATR 계산법 (3단계)
    1. TR(True Range) 계산: 아래 세 가지 값 중 가장 큰 값(절대값)을 선택
        - 당일 고가 - 당일 저가
        - |당일 고가 - 전일 종가|
        - |당일 저가 - 전일 종가|
    2. ATR 계산 (첫 번째 값): 보통 14일치 TR의 단순평균
    3. ATR 계산 (이후 값): 이동평균 방식 적용 (전일 ATR x 13 + 당일 TR) / 14

    ATR 활용 및 특징
    - 변동성 측정: ATR 값이 상승하면 변동성이 증가, 하락하면 감소.
    - 매매 활용: 변동성이 클 때(ATR 상승)는 적게 매수, 작을 때(ATR 하락)는 많이 매수하여 위험 관리.
    - 주의점: 방향성이 없어 추세 지표(MACD 등)와 함께 사용해야 하며, 단독으로 매수/매도 신호로 활용 불가.

    Returns:
        {"atr": float, "atr_prev": float}
    """
    if len(closes) < period + 1:
        return {"atr": 0.0, "atr_prev": 0.0}

    tr_list: list[float] = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],  # 당일 고가 - 당일 저가
            abs(highs[i] - closes[i - 1]),  # |당일 고가 - 전일 종가|
            abs(lows[i] - closes[i - 1]),  # |당일 저가 - 전일 종가|
        )
        tr_list.append(tr)

    atr_series: list[float] = []
    current_atr = sum(tr_list[:period]) / period  # 보통 14일치 단순평균
    atr_series.append(current_atr)

    k = 1 / period
    for i in range(period, len(tr_list)):
        current_atr = (tr_list[i] * k) + (current_atr * (1 - k))
        atr_series.append(current_atr)

    return {
        "atr": atr_series[-1] if atr_series else 0.0,
        "atr_prev": atr_series[-2] if len(atr_series) >= 2 else 0.0,
    }


def calc_macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict[str, float]:
    """
    MACD, MACD Signal, MACD Histogram을 계산한다.

    Args:
        closes: 종가 리스트 (오래된 것부터 최신 순)

    계산 방식:
        - _ema()가 prices[0] 시딩이므로 fast/slow 모두 len(closes) 길이를 반환
        - 따라서 별도 정렬(alignment) 없이 바로 zip 가능
        - Signal은 macd_series 전체에 EMA를 적용 (prices[0] 시딩으로 빠른 수렴)

    Returns:
        {
            "macd": float,         # 현재 MACD
            "signal": float,       # 현재 Signal
            "histogram": float,    # MACD - Signal
            "macd_prev": float,    # 직전 MACD
            "signal_prev": float,  # 직전 Signal
        }
    """
    default = {
        "macd": 0.0,
        "signal": 0.0,
        "histogram": 0.0,
        "macd_prev": 0.0,
        "signal_prev": 0.0,
        "macd_prev2": 0.0,  # 2봉 전 MACD (closed bar 2)
        "signal_prev2": 0.0,  # 2봉 전 Signal (closed bar 2)
        "macd_prev3": 0.0,  # 3봉 전 MACD (closed bar 3)
        "signal_prev3": 0.0,  # 3봉 전 Signal (closed bar 3)
    }
    if len(closes) < slow + signal:
        return default

    ema_fast_series = _ema(closes, fast)
    ema_slow_series = _ema(closes, slow)

    if not ema_fast_series or not ema_slow_series:
        return default

    # 두 EMA 시리즈 모두 len(closes) 길이이므로 그대로 zip
    macd_series = [f - s for f, s in zip(ema_fast_series, ema_slow_series)]

    if len(macd_series) < 2:
        return default

    signal_series = _ema(macd_series, signal)
    if len(signal_series) < 2:
        return default

    return {
        "macd": macd_series[-1],
        "signal": signal_series[-1],
        "histogram": macd_series[-1] - signal_series[-1],
        "macd_prev": macd_series[-2],  # closed bar 1 (직전 확정 봉)
        "signal_prev": signal_series[-2],  # closed bar 1
        "macd_prev2": (
            macd_series[-3] if len(macd_series) >= 3 else macd_series[-2]
        ),  # closed bar 2
        "signal_prev2": (
            signal_series[-3] if len(signal_series) >= 3 else signal_series[-2]
        ),  # closed bar 2
        "macd_prev3": (
            macd_series[-4] if len(macd_series) >= 4 else macd_series[-2]
        ),  # closed bar 3
        "signal_prev3": (
            signal_series[-4] if len(signal_series) >= 4 else signal_series[-2]
        ),  # closed bar 3
    }


def calc_smacd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict[str, float]:
    """
    데이터가 부족한 환경에서도 오차가 나지 않는 '단순이동평균 기반 지연없는 특수 MACD'
    """
    default = {
        "macd": 0.0,
        "signal": 0.0,
        "histogram": 0.0,
        "macd_prev": 0.0,
        "signal_prev": 0.0,
    }
    if len(closes) < slow:
        return default

    sma_fast_series = _sma(closes, fast)
    sma_slow_series = _sma(closes, slow)
    macd_series = [f - s for f, s in zip(sma_fast_series, sma_slow_series)]

    if not macd_series:
        return default

    signal_series = _sma(macd_series, signal)
    if not signal_series:
        return default

    return {
        "macd": macd_series[-1],
        "signal": signal_series[-1],
        "histogram": macd_series[-1] - signal_series[-1],
        "macd_prev": macd_series[-2] if len(macd_series) >= 2 else macd_series[-1],
        "signal_prev": (
            signal_series[-2] if len(signal_series) >= 2 else signal_series[-1]
        ),
    }


def calc_ema_pair(
    closes: list[float],
    short: int = config.EMA_SHORT,
    long_: int = config.EMA_LONG,
) -> dict[str, float]:
    """단기/장기 EMA를 계산한다."""
    ema_s = _ema(closes, short)
    ema_l = _ema(closes, long_)
    return {
        "ema_short": ema_s[-1] if ema_s else 0.0,
        "ema_long": ema_l[-1] if ema_l else 0.0,
    }


def calc_sma_pair(
    closes: list[float],
    short: int = config.EMA_SHORT,
    long_: int = config.EMA_LONG,
) -> dict[str, float]:
    """단기/장기 SMA를 계산하되 호환성을 위해 ema_short, ema_long 키로 반환한다."""
    sma_s = _sma(closes, short)
    sma_l = _sma(closes, long_)
    return {
        "ema_short": sma_s[-1] if sma_s else 0.0,
        "ema_long": sma_l[-1] if sma_l else 0.0,
    }


def calc_bollinger_bands(
    closes: list[float],
    period: int = config.BB_PERIOD if hasattr(config, "BB_PERIOD") else 20,
    num_std: float = 2.0,
) -> dict[str, float]:
    """볼린저밴드를 계산한다."""
    # Use fallback for period from config or default
    p = period

    default = {
        "bb_upper": 0.0,
        "bb_mid": 0.0,
        "bb_lower": 0.0,
        "bb_upper_prev": 0.0,
        "bb_lower_prev": 0.0,
        "bb_width_ratio": 0.0,
        "bb_width_ratio_prev": 0.0,
    }
    if len(closes) < p:
        return default

    window = closes[-p:]
    mid = sum(window) / p
    variance = sum((x - mid) ** 2 for x in window) / p
    std = math.sqrt(variance)

    prev_upper, prev_lower = 0.0, 0.0
    prev_mid = 0.0
    if len(closes) >= p + 1:
        prev_window = closes[-(p + 1) : -1]
        prev_mid = sum(prev_window) / p
        prev_variance = sum((x - prev_mid) ** 2 for x in prev_window) / p
        prev_std = math.sqrt(prev_variance)
        prev_upper = prev_mid + num_std * prev_std
        prev_lower = prev_mid - num_std * prev_std

    bb_upper = mid + num_std * std
    bb_lower = mid - num_std * std
    bb_width_ratio = ((bb_upper - bb_lower) / mid) if mid > 0 else 0.0
    bb_width_ratio_prev = (
        ((prev_upper - prev_lower) / prev_mid)
        if prev_mid > 0 and prev_upper > 0 and prev_lower > 0
        else 0.0
    )

    return {
        "bb_upper": bb_upper,
        "bb_mid": mid,
        "bb_lower": bb_lower,
        "bb_upper_prev": prev_upper,
        "bb_lower_prev": prev_lower,
        "bb_width_ratio": bb_width_ratio,
        "bb_width_ratio_prev": bb_width_ratio_prev,
    }


def calc_median_volume(candles: list[dict[str, Any]], period: int = 20) -> float:
    """최근 N봉의 중앙값 거래량을 계산한다."""
    volumes = sorted(c["volume"] for c in candles[:period] if "volume" in c)
    if not volumes:
        return 0.0
    n = len(volumes)
    if n % 2 == 1:
        return float(volumes[n // 2])
    mid_idx = n // 2
    return (volumes[mid_idx - 1] + volumes[mid_idx]) / 2.0


def resample_candles(
    candles_asc: list[dict[str, Any]], interval: int
) -> list[dict[str, Any]]:
    """
    1분봉을 N분봉으로 리샘플링한다.
    """
    if not candles_asc or interval <= 1:
        return candles_asc

    def _aggregate_chunk(chunk: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "timestamp": chunk[-1].get("timestamp", ""),
            "open": chunk[0]["open"],
            "high": max(x["high"] for x in chunk),
            "low": min(x["low"] for x in chunk),
            "close": chunk[-1]["close"],
            "volume": sum(x["volume"] for x in chunk),
        }

    resampled: list[dict[str, Any]] = []
    current_chunk = []
    current_chunk_id: tuple[str, int] | None = None

    for c in candles_asc:
        ts = c.get("timestamp", "")
        try:
            if len(ts) >= 14:
                day_key = ts[:8]
                hh, mm = int(ts[8:10]), int(ts[10:12])
                total_minutes = hh * 60 + mm
                chunk_id = (day_key, total_minutes // interval)
            elif len(ts) >= 4:
                day_key = "same-day"
                hh, mm = int(ts[:2]), int(ts[2:4])
                total_minutes = hh * 60 + mm
                chunk_id = (day_key, total_minutes // interval)
            else:
                chunk_id = (
                    current_chunk_id
                    if current_chunk_id is not None
                    else ("same-day", 0)
                )
        except (ValueError, IndexError):
            chunk_id = (
                current_chunk_id if current_chunk_id is not None else ("same-day", 0)
            )

        if chunk_id != current_chunk_id:
            if current_chunk:
                resampled.append(_aggregate_chunk(current_chunk))
            current_chunk = []
            current_chunk_id = chunk_id
        current_chunk.append(c)

    if current_chunk:
        resampled.append(_aggregate_chunk(current_chunk))
    return resampled


def compute_all_indicators(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """
    캔들 데이터로부터 모든 기술 지표를 한 번에 계산해 반환한다.

    입력 candles는 이미 분석 프레임(1분/3분/5분 등)으로 정렬된 캔들이라고 가정한다.
    따라서 기본 지표(RSI, MACD, EMA, BB, ATR)는 입력 프레임 그대로 계산하고,
    상위 프레임(HTF)만 추가로 리샘플링한다.

    HTF(상위 프레임) EMA:
        - 현재 분석 프레임 캔들을 상위 프레임으로 리샘플링
        - 기본: 1분봉 × 5 = 5분봉, 3분봉 × 5 = 15분봉, 5분봉 × 5 = 25분봉
        - 해당 봉의 EMA_LONG을 htf_ema_long으로 반환
    """
    ordered = list(reversed(candles))  # 오래된 것부터 최신 순 (ascending)

    closes = [float(c["close"]) for c in ordered]
    highs = [float(c["high"]) for c in ordered]
    lows = [float(c["low"]) for c in ordered]

    rsi_data = calc_rsi(closes)
    macd_data = calc_macd(closes)
    ema_data = calc_ema_pair(closes)
    bb_data = calc_bollinger_bands(closes)
    atr_data = calc_atr(highs, lows, closes)
    median_vol = calc_median_volume(candles)

    # ── HTF EMA (상위 프레임 추세) ──────────────────────────────────────────
    # 현재 분석 프레임 캔들을 HTF 단위로 리샘플링 후 EMA_LONG 계산
    # gate2의 HTF_EMA 상향/하향 점수(+1)가 실제로 동작하기 위해 필요
    htf_interval = config.CANDLE_INTERVAL * config.HTF_MULTIPLIER
    htf_candles = resample_candles(ordered, htf_interval)
    htf_closes = [float(c["close"]) for c in htf_candles]
    htf_ema_series = _ema(htf_closes, config.EMA_LONG)
    htf_ema_long = htf_ema_series[-1] if htf_ema_series else 0.0

    if htf_ema_long == 0.0:
        logger.debug(
            "[지표] HTF EMA 계산 불가 (HTF 캔들 %d봉, EMA_LONG=%d 미만)",
            len(htf_candles),
            config.EMA_LONG,
        )

    return {
        "rsi": rsi_data["rsi"],
        "rsi_prev": rsi_data["rsi_prev"],
        "macd": macd_data["macd"],
        "macd_signal": macd_data["signal"],
        "macd_histogram": macd_data["histogram"],
        "macd_prev": macd_data["macd_prev"],
        "macd_signal_prev": macd_data["signal_prev"],
        "macd_prev2": macd_data["macd_prev2"],  # closed bar 2 MACD
        "macd_signal_prev2": macd_data["signal_prev2"],  # closed bar 2 Signal
        "macd_prev3": macd_data["macd_prev3"],  # closed bar 3 MACD
        "macd_signal_prev3": macd_data["signal_prev3"],  # closed bar 3 Signal
        "ema_short": ema_data["ema_short"],
        "ema_long": ema_data["ema_long"],
        "bb_upper": bb_data["bb_upper"],
        "bb_mid": bb_data["bb_mid"],
        "bb_lower": bb_data["bb_lower"],
        "bb_upper_prev": bb_data["bb_upper_prev"],
        "bb_lower_prev": bb_data["bb_lower_prev"],
        "bb_width_ratio": bb_data["bb_width_ratio"],
        "bb_width_ratio_prev": bb_data["bb_width_ratio_prev"],
        "atr": atr_data["atr"],
        "median_vol": median_vol,
        "htf_ema_long": htf_ema_long,  # HTF EMA_LONG (gate2 HTF 점수용)
    }
