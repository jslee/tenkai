"""KIS API 분봉 데이터를 받아 기술지표 차트를 PNG로 저장한다.

생성 차트:
1. 볼린저밴드 + 캔들
2. RSI
3. 거래량
4. MACD

기본적으로 1분, 3분, 5분봉 차트를 각각 저장한다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import math
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

import matplotlib
from matplotlib import font_manager

try:
    import yaml  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    yaml = None

matplotlib.use("Agg")


def _load_chart_config() -> SimpleNamespace:
    config_path = Path(__file__).with_name("chart_config.yaml")
    fallback = {
        "image": {"width": 1920, "height": 1080, "dpi": 100},
        "defaults": {"output_dir": "charts", "intervals": [1, 3, 5], "plot_count": 120},
        "fonts": {
            "candidates": [
                "Malgun Gothic",
                "AppleGothic",
                "NanumGothic",
                "Noto Sans CJK KR",
                "Noto Sans KR",
                "DejaVu Sans",
            ]
        },
        "colors": {
            "background": "#ffffff",
            "grid": "#d9d9d9",
            "text": "#222222",
            "price_up": "#d60000",
            "price_down": "#0057d8",
            "bb_upper": "#7a3db8",
            "bb_mid": "#666666",
            "bb_lower": "#7a3db8",
            "rsi": "#ff8c00",
            "rsi_overbought": "#d60000",
            "rsi_oversold": "#0057d8",
            "volume_up": "#d60000",
            "volume_down": "#0057d8",
            "macd": "#111111",
            "macd_signal": "#ff8c00",
            "macd_hist_positive": "#d60000",
            "macd_hist_positive_weak": "#f2a4a4",
            "macd_hist_negative": "#0057d8",
            "macd_hist_negative_weak": "#9ec0ff",
            "ema_short": "#ff1493",
            "ema_long": "#ffbf00",
            "ema_trend": "#32cd32",
        },
    }

    data: dict = {}
    if yaml is not None and config_path.exists():
        try:
            with config_path.open("r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    image = data.get("image", {}) if isinstance(data.get("image"), dict) else {}
    defaults = (
        data.get("defaults", {}) if isinstance(data.get("defaults"), dict) else {}
    )
    fonts = data.get("fonts", {}) if isinstance(data.get("fonts"), dict) else {}
    colors = data.get("colors", {}) if isinstance(data.get("colors"), dict) else {}

    merged_colors = dict(fallback["colors"])
    for k, v in colors.items():
        merged_colors[str(k)] = str(v)

    return SimpleNamespace(
        IMAGE_WIDTH=int(image.get("width", fallback["image"]["width"])),
        IMAGE_HEIGHT=int(image.get("height", fallback["image"]["height"])),
        DPI=int(image.get("dpi", fallback["image"]["dpi"])),
        DEFAULT_OUTPUT_DIR=str(
            defaults.get("output_dir", fallback["defaults"]["output_dir"])
        ),
        DEFAULT_INTERVALS=tuple(
            int(x) for x in defaults.get("intervals", fallback["defaults"]["intervals"])
        ),
        DEFAULT_PLOT_COUNT=int(
            defaults.get("plot_count", fallback["defaults"]["plot_count"])
        ),
        FONT_CANDIDATES=tuple(
            str(x) for x in fonts.get("candidates", fallback["fonts"]["candidates"])
        ),
        COLORS=merged_colors,
    )


chart_config = _load_chart_config()


def _configure_matplotlib_fonts() -> None:
    available_fonts = {font.name for font in font_manager.fontManager.ttflist}
    chosen_font = next(
        (font for font in chart_config.FONT_CANDIDATES if font in available_fonts),
        "DejaVu Sans",
    )
    matplotlib.rcParams["font.family"] = chosen_font
    matplotlib.rcParams["axes.unicode_minus"] = False


_configure_matplotlib_fonts()

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import config
from kis_api import KISAuth, KISMarket
from strategy.indicators import _ema, resample_candles


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="기술지표 차트를 PNG로 저장")
    parser.add_argument("--ticker", default=config.TICKER, help="종목 코드")
    parser.add_argument(
        "--name",
        default=None,
        help="종목명으로 종목 지정. 예: 삼성전자  (--ticker 대신 사용 가능)",
    )
    parser.add_argument(
        "--output-dir",
        default=chart_config.DEFAULT_OUTPUT_DIR,
        help="PNG 저장 디렉터리",
    )
    parser.add_argument(
        "--intervals",
        default=None,
        help="생성할 분봉 간격 목록. 예: 1,3,5. 지정 안 하면 분봉 차트를 생성하지 않습니다.",
    )
    parser.add_argument(
        "--days",
        type=int,
        nargs="?",
        const=200,
        default=None,
        metavar="N",
        help="일봉 차트 생성. 값 생략 시 200봉 사용. 예: --days 또는 --days 100",
    )
    parser.add_argument(
        "--weeks",
        type=int,
        nargs="?",
        const=100,
        default=None,
        metavar="N",
        help="주봉 차트 생성. 값 생략 시 100봉 사용. 예: --weeks 또는 --weeks 50",
    )
    parser.add_argument(
        "--months",
        type=int,
        nargs="?",
        const=60,
        default=None,
        metavar="N",
        help="월봉 차트 생성. 값 생략 시 60봉 사용. 예: --months 또는 --months 24",
    )
    return parser.parse_args()


def _parse_intervals(raw: str) -> list[int]:
    intervals: list[int] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            value = int(item)
        except ValueError as exc:
            raise ValueError(f"잘못된 interval 값: {item}") from exc
        if value <= 0:
            raise ValueError(f"interval은 1 이상이어야 합니다: {value}")
        intervals.append(value)
    return sorted(set(intervals))


def _timestamp_to_label(timestamp: str) -> str:
    ts = timestamp[-14:] if len(timestamp) >= 14 else timestamp
    try:
        return datetime.strptime(ts, "%Y%m%d%H%M%S").strftime("%m-%d %H:%M")
    except ValueError:
        return ts


def _date_to_label(date_str: str, period: str) -> str:
    """YYYYMMDD를 period에 맞는 레이블로 변환한다."""
    try:
        dt = datetime.strptime(date_str, "%Y%m%d")
        if period == "M":
            return dt.strftime("%Y-%m")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return date_str


def _timestamp_to_storage_parts(timestamp: str | None) -> tuple[str, str, str]:
    """차트 저장 경로용 날짜/시각/파일명 조각을 만든다."""
    now = (
        datetime.now()
        if timestamp is None
        else datetime.strptime(
            timestamp[-14:] if len(timestamp) >= 14 else timestamp,
            "%Y%m%d%H%M%S",
        )
    )
    return now.strftime("%Y%m%d"), now.strftime("%H"), now.strftime("%H%M")


def _build_rsi_series(closes: list[float], period: int) -> pd.Series:
    values = [math.nan] * len(closes)
    if len(closes) < period * 2:
        return pd.Series(values, dtype=float)

    gains: list[float] = []
    losses: list[float] = []
    for idx in range(1, len(closes)):
        diff = closes[idx] - closes[idx - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def _rsi(gain: float, loss: float) -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        return 100.0 - (100.0 / (1.0 + gain / loss))

    values[period] = _rsi(avg_gain, avg_loss)
    for idx in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[idx]) / period
        avg_loss = (avg_loss * (period - 1) + losses[idx]) / period
        values[idx + 1] = _rsi(avg_gain, avg_loss)
    return pd.Series(values, dtype=float)


def _build_period_indicator_frame(candles_asc: list[dict], period: str) -> pd.DataFrame:
    """일/주/월봉 캔들로 지표 DataFrame을 생성한다.

    KIS 기간봉 캔들은 'date' 필드(YYYYMMDD)를 사용한다. 내부에서 'timestamp' 필드를
    생성해 기존 _build_indicator_frame을 재사용하고, label만 period에 맞게 재설정한다.
    """
    normalized = [{**c, "timestamp": c["date"] + "000000"} for c in candles_asc]
    df = _build_indicator_frame(normalized)
    df["label"] = df["timestamp"].map(lambda ts: _date_to_label(ts[:8], period))
    return df


def _build_indicator_frame(candles_asc: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles_asc).copy()
    df["label"] = df["timestamp"].map(_timestamp_to_label)

    closes = df["close"].astype(float).tolist()
    highs = df["high"].astype(float)
    lows = df["low"].astype(float)

    rolling_close = (
        df["close"]
        .astype(float)
        .rolling(window=config.BB_PERIOD, min_periods=config.BB_PERIOD)
    )
    df["bb_mid"] = rolling_close.mean()
    df["bb_std"] = rolling_close.std(ddof=0)
    df["bb_upper"] = df["bb_mid"] + (df["bb_std"] * 2.0)
    df["bb_lower"] = df["bb_mid"] - (df["bb_std"] * 2.0)

    df["rsi"] = _build_rsi_series(closes, config.RSI_PERIOD)

    ema_fast = pd.Series(_ema(closes, 12), dtype=float)
    ema_slow = pd.Series(_ema(closes, 26), dtype=float)
    df["ema_short"] = pd.Series(_ema(closes, config.EMA_SHORT), dtype=float)
    df["ema_long"] = pd.Series(_ema(closes, config.EMA_LONG), dtype=float)
    df["ema_trend"] = pd.Series(_ema(closes, config.EMA_TREND), dtype=float)
    df["macd"] = ema_fast - ema_slow
    df["macd_signal"] = pd.Series(_ema(df["macd"].fillna(0.0).tolist(), 9), dtype=float)
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    df["x"] = np.arange(len(df), dtype=float)
    df["is_up"] = df["close"] >= df["open"]
    df["close_pos"] = np.where(
        (highs - lows) > 0,
        (df["close"] - lows) / (highs - lows),
        np.nan,
    )
    return df


def _style_axis(ax: plt.Axes) -> None:
    colors = chart_config.COLORS
    ax.set_facecolor(colors["background"])
    ax.grid(True, color=colors["grid"], linewidth=0.6, alpha=0.8)
    ax.tick_params(colors=colors["text"], labelsize=9)
    for spine in ax.spines.values():
        spine.set_color(colors["grid"])


def _plot_candles_with_bb(ax: plt.Axes, df: pd.DataFrame) -> None:
    colors = chart_config.COLORS
    width = 0.62
    for row in df.itertuples(index=False):
        candle_color = colors["price_up"] if row.is_up else colors["price_down"]
        ax.vlines(row.x, row.low, row.high, color=candle_color, linewidth=1.0, zorder=2)
        body_bottom = min(row.open, row.close)
        body_height = abs(row.close - row.open)
        if body_height == 0:
            body_height = 0.5
        ax.bar(
            row.x,
            body_height,
            bottom=body_bottom,
            width=width,
            color=candle_color,
            edgecolor=candle_color,
            linewidth=0.8,
            zorder=3,
        )

    ax.fill_between(
        df["x"],
        df["bb_lower"],
        df["bb_upper"],
        color=colors["bb_upper"],
        alpha=0.06,
        zorder=1,
    )
    ax.plot(
        df["x"],
        df["bb_upper"],
        color=colors["bb_upper"],
        linewidth=1.3,
        label="BB Upper",
        zorder=4,
    )

    # 지수이동평균(EMA) 렌더링 (chart_config.yaml 색상 적용)
    ax.plot(
        df["x"],
        df["ema_short"],
        color=colors["ema_short"],
        linewidth=1.3,
        linestyle="-",
        label=f"EMA {config.EMA_SHORT}",
        zorder=5,
    )
    ax.plot(
        df["x"],
        df["ema_long"],
        color=colors["ema_long"],
        linewidth=1.5,
        linestyle="-",
        label=f"EMA {config.EMA_LONG}",
        zorder=5,
    )
    ax.plot(
        df["x"],
        df["ema_trend"],
        color=colors["ema_trend"],
        linewidth=1.8,
        linestyle="-",
        label=f"EMA {config.EMA_TREND}",
        zorder=5,
    )
    ax.plot(
        df["x"],
        df["bb_mid"],
        color=colors["bb_mid"],
        linewidth=1.0,
        linestyle="--",
        label="BB Mid",
        zorder=4,
    )
    ax.plot(
        df["x"],
        df["bb_lower"],
        color=colors["bb_lower"],
        linewidth=1.3,
        label="BB Lower",
        zorder=4,
    )
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    ax.set_ylabel("Price", color=colors["text"])


def _plot_rsi(ax: plt.Axes, df: pd.DataFrame) -> None:
    colors = chart_config.COLORS
    ax.plot(df["x"], df["rsi"], color=colors["rsi"], linewidth=1.4)
    ax.axhline(70, color=colors["rsi_overbought"], linestyle="--", linewidth=0.9)
    ax.axhline(30, color=colors["rsi_oversold"], linestyle="--", linewidth=0.9)
    ax.set_ylim(0, 100)
    ax.set_ylabel("RSI", color=colors["text"])


def _plot_volume(ax: plt.Axes, df: pd.DataFrame) -> None:
    colors = chart_config.COLORS
    bar_colors = np.where(df["is_up"], colors["volume_up"], colors["volume_down"])
    ax.bar(df["x"], df["volume"], color=bar_colors, width=0.62, alpha=0.85)
    ax.set_ylabel("Volume", color=colors["text"])


def _plot_macd(ax: plt.Axes, df: pd.DataFrame) -> None:
    colors = chart_config.COLORS
    hist = df["macd_hist"].astype(float)
    prev_hist = hist.shift(1)

    hist_colors = []
    for current, previous in zip(hist, prev_hist):
        if np.isnan(current):
            hist_colors.append(colors["macd_hist_positive_weak"])
            continue

        if current >= 0:
            if np.isnan(previous) or current >= previous:
                hist_colors.append(colors["macd_hist_positive"])
            else:
                hist_colors.append(colors["macd_hist_positive_weak"])
        else:
            if np.isnan(previous) or current <= previous:
                hist_colors.append(colors["macd_hist_negative"])
            else:
                hist_colors.append(colors["macd_hist_negative_weak"])

    ax.bar(df["x"], df["macd_hist"], color=hist_colors, width=0.62, alpha=0.65)
    ax.plot(df["x"], df["macd"], color=colors["macd"], linewidth=1.2, label="MACD")
    ax.plot(
        df["x"],
        df["macd_signal"],
        color=colors["macd_signal"],
        linewidth=1.2,
        label="Signal",
    )
    ax.axhline(0, color=colors["grid"], linewidth=0.8)
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    ax.set_ylabel("MACD", color=colors["text"])


def _apply_shared_x(ax: plt.Axes, df: pd.DataFrame) -> None:
    count = len(df)
    tick_count = min(8, count)
    tick_positions = np.linspace(0, count - 1, tick_count, dtype=int)
    labels = [df.iloc[pos]["label"] for pos in tick_positions]
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(labels, rotation=0, ha="center")


def _render_interval_chart(
    df: pd.DataFrame,
    ticker: str,
    name: str,
    interval: int,
    output_path: Path,
    timestamp: str | None = None,
) -> None:
    dpi = chart_config.DPI
    fig, axes = plt.subplots(
        4,
        1,
        figsize=(chart_config.IMAGE_WIDTH / dpi, chart_config.IMAGE_HEIGHT / dpi),
        dpi=dpi,
        sharex=True,
        gridspec_kw={"height_ratios": [4.5, 1.8, 1.8, 2.2]},
        constrained_layout=True,
    )

    fig.patch.set_facecolor(chart_config.COLORS["background"])
    for ax in axes:
        _style_axis(ax)

    _plot_candles_with_bb(axes[0], df)
    _plot_rsi(axes[1], df)
    _plot_volume(axes[2], df)
    _plot_macd(axes[3], df)
    _apply_shared_x(axes[3], df)

    latest_label = df.iloc[-1]["label"] if not df.empty else "-"
    axes[0].set_title(
        f"{ticker} {name} | {interval}분봉 | latest {latest_label}",
        fontsize=14,
        color=chart_config.COLORS["text"],
        loc="left",
    )

    if timestamp is not None:
        day_part, hour_part, file_part = _timestamp_to_storage_parts(timestamp)
        output_path = (
            output_path / day_part / hour_part / f"{file_part}_{interval}m.png"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)


def _render_period_chart(
    df: pd.DataFrame,
    ticker: str,
    name: str,
    period_code: str,
    period_label: str,
    output_dir: Path,
) -> Path:
    """일/주/월봉 차트 PNG를 렌더링하고 저장 경로를 반환한다."""
    dpi = chart_config.DPI
    fig, axes = plt.subplots(
        4,
        1,
        figsize=(chart_config.IMAGE_WIDTH / dpi, chart_config.IMAGE_HEIGHT / dpi),
        dpi=dpi,
        sharex=True,
        gridspec_kw={"height_ratios": [4.5, 1.8, 1.8, 2.2]},
        constrained_layout=True,
    )
    fig.patch.set_facecolor(chart_config.COLORS["background"])
    for ax in axes:
        _style_axis(ax)

    _plot_candles_with_bb(axes[0], df)
    _plot_rsi(axes[1], df)
    _plot_volume(axes[2], df)
    _plot_macd(axes[3], df)
    _apply_shared_x(axes[3], df)

    latest_label = df.iloc[-1]["label"] if not df.empty else "-"
    axes[0].set_title(
        f"{ticker} {name} | {period_label} | latest {latest_label}",
        fontsize=14,
        color=chart_config.COLORS["text"],
        loc="left",
    )

    filename_map = {"D": "daily", "W": "weekly", "M": "monthly"}
    today = datetime.now().strftime("%Y%m%d")
    output_path = (
        output_dir / today / f"{filename_map.get(period_code, period_code)}.png"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return output_path


def _create_market_client() -> KISMarket:
    auth_data = KISAuth(
        app_key=config.KIS_REAL_APP_KEY,
        app_secret=config.KIS_REAL_APP_SECRET,
        account_no=config.KIS_REAL_ACCOUNT_NO,
        base_url=config.BASE_URL_REAL,
        is_paper=False,
    )
    return KISMarket(auth_data=auth_data, auth_trade=auth_data)


def _prepare_interval_candles(candles_desc: list[dict], interval: int) -> list[dict]:
    candles_asc = list(reversed(candles_desc))
    if interval > 1:
        candles_asc = resample_candles(candles_asc, interval)
    return candles_asc


async def _run() -> None:
    args = _parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    market = _create_market_client()

    if args.name:
        matches = market.find_ticker_by_name(args.name)
        if not matches:
            print(
                f"오류: '{args.name}' 에 해당하는 종목을 찾을 수 없습니다.",
                file=sys.stderr,
            )
            return
        if len(matches) == 1:
            args.ticker = matches[0][0]
            print(f"  종목 확인: {matches[0][1]} ({matches[0][0]})")
        else:
            print(f"'{args.name}' 검색 결과 {len(matches)}건:")
            for i, (code, nm) in enumerate(matches, 1):
                print(f"  {i:>3}. {nm} ({code})")
            try:
                sel = int(input("선택 번호를 입력하세요: ").strip())
                if not 1 <= sel <= len(matches):
                    raise ValueError
            except (ValueError, EOFError):
                print("올바른 번호를 입력하세요.", file=sys.stderr)
                return
            args.ticker = matches[sel - 1][0]

    stock_name = market.get_stock_name(args.ticker)

    # ── 분봉 차트 ──────────────────────────────────────────────────────────
    intervals = _parse_intervals(args.intervals) if args.intervals is not None else []
    if intervals:
        candles_desc = await market.get_minute_candles(
            args.ticker, count=max(config.CANDLE_COUNT, 400)
        )
        if not candles_desc:
            print("warning: 분봉 데이터를 받지 못했습니다.")
        else:
            for interval in intervals:
                candles_asc = _prepare_interval_candles(candles_desc, interval)
                if not candles_asc:
                    continue

                df = _build_indicator_frame(candles_asc)
                plot_count = chart_config.DEFAULT_PLOT_COUNT
                if plot_count > 0:
                    df = df.tail(plot_count).reset_index(drop=True)
                    df["x"] = np.arange(len(df), dtype=float)

                latest_timestamp = (
                    candles_asc[-1].get("timestamp") if candles_asc else None
                )
                _render_interval_chart(
                    df,
                    args.ticker,
                    stock_name,
                    interval,
                    output_dir,
                    timestamp=latest_timestamp,
                )
                day_part, hour_part, file_part = _timestamp_to_storage_parts(
                    latest_timestamp
                )
                output_path = (
                    output_dir / day_part / hour_part / f"{file_part}_{interval}m.png"
                )
                print(f"saved: {output_path}")

    # ── 기간봉 차트 ────────────────────────────────────────────────────────
    _PERIOD_LABELS = {"D": "일봉", "W": "주봉", "M": "월봉"}
    period_tasks: list[str] = []
    if args.days is not None:
        period_tasks.append("D")
    if args.weeks is not None:
        period_tasks.append("W")
    if args.months is not None:
        period_tasks.append("M")

    for period_code in period_tasks:
        label = _PERIOD_LABELS[period_code]
        if period_code == "D":
            candles = await market.get_daily_candles(args.ticker)
        elif period_code == "W":
            candles = await market.get_weekly_candles(args.ticker)
        else:
            candles = await market.get_monthly_candles(args.ticker)

        if not candles:
            print(f"warning: {label} 데이터 없음")
            continue

        # KIS 기간봉은 최신→과거 순 반환 → 오름차순으로 변환
        candles_asc = list(reversed(candles))
        df = _build_period_indicator_frame(candles_asc, period_code)
        plot_count = chart_config.DEFAULT_PLOT_COUNT
        if plot_count > 0:
            df = df.tail(plot_count).reset_index(drop=True)
            df["x"] = np.arange(len(df), dtype=float)

        output_path = _render_period_chart(
            df, args.ticker, stock_name, period_code, label, output_dir
        )
        print(f"saved: {output_path}")


def main() -> None:
    """
    # 일봉만
    python make_charts.py --ticker 005930 --days

    # 분봉만
    python make_charts.py --ticker 005930 --intervals 1,3,5

    # 둘 다
    python make_charts.py --ticker 005930 --intervals 1,3,5 --days --weeks
    """
    asyncio.run(_run())


if __name__ == "__main__":
    main()
