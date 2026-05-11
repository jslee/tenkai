"""LM Studio 기반 자동매매 진입점.

기능:
1. LM Studio(OpenAI 호환 API)에 차트 PNG와 호가창 텍스트를 전달한다.
2. AI의 BUY/SELL/HOLD 판단을 받아 KIS API 주문에 연결한다.

실행 예:
    python main_delphi.py --ticker 005930
    python main_delphi.py --ticker 005930 --once --real
    python main_delphi.py --ticker 005930 --once --decision-time 202605091030
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
import holidays
import numpy as np

try:
    import yaml  # type: ignore[import-not-found]
except Exception:  # pragma: no cover
    yaml = None

import config
from filters import gate_market_filter
from kis_api import KISAuth, KISMarket, KISOrder
from logger.trade_log import (
    get_last_buy_time,
    print_session_summary,
    setup_logging,
    write_close_log,
    write_cycle_log,
)
from make_charts import (
    _build_indicator_frame,
    _prepare_interval_candles,
    _render_interval_chart,
)
from strategy import RiskManager, check_fee_viability, compute_all_indicators

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """Follow these instructions strictly:
- Do NOT output thinking or reasoning steps
- Provide direct, concise answers only
- Skip any internal monologue or thought process
- Return only the final answer requested by the user
"""

DECISION_INTERVALS = (1, 3, 5)
DECISION_LOOP_INTERVAL_SEC = 60


def _load_chart_defaults() -> dict[str, Any]:
    config_path = Path(__file__).with_name("chart_config.yaml")
    defaults = {"plot_count": 120, "output_dir": "charts"}
    if yaml is None:
        return defaults
    if not config_path.exists():
        return defaults
    try:
        with config_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        section = data.get("defaults", {}) if isinstance(data, dict) else {}
        if isinstance(section, dict):
            defaults["plot_count"] = int(
                section.get("plot_count", defaults["plot_count"])
            )
            defaults["output_dir"] = str(
                section.get("output_dir", defaults["output_dir"])
            )
    except Exception:
        pass
    return defaults


_CHART_DEFAULTS = _load_chart_defaults()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI 기반 KIS 자동매매")
    parser.add_argument(
        "--ticker", type=str, default=config.TICKER, help="거래 종목 코드"
    )
    parser.add_argument("--real", action="store_true", help="실투자 모드")
    parser.add_argument("--debug", action="store_true", help="DEBUG 로그 출력")
    parser.add_argument("--once", action="store_true", help="1회만 실행 후 종료")
    parser.add_argument(
        "--decision-time",
        type=str,
        help="테스트용 판단 시점. YYYYMMDDHHMM[SS], YYYYMMDDHHMM, HHMM[SS] 지원",
    )
    parser.add_argument(
        "--plot-count",
        type=int,
        default=int(_CHART_DEFAULTS["plot_count"]),
        help="AI에 보낼 차트에 표시할 봉 수",
    )
    parser.add_argument(
        "--output-dir",
        default=str(_CHART_DEFAULTS["output_dir"]),
        help="생성 차트 저장 디렉터리",
    )
    return parser.parse_args()


def set_title(ticker: str, stock_name: str, mode: str) -> None:
    import platform

    title_str = f"{stock_name}({ticker})-LMStudio-1m3m5m-{mode}"
    try:
        if sys.stdout.isatty():
            system = platform.system()
            if system == "Windows":
                os.system(f"title {title_str}")
                try:
                    import ctypes

                    ctypes.windll.kernel32.SetConsoleTitleW(title_str)
                except Exception:
                    pass
            elif system in ("Linux", "Darwin"):
                sys.stdout.write(f"\033]0;{title_str}\007")
                sys.stdout.flush()
    except Exception:
        pass


def _encode_image_to_data_url(image_path: Path) -> str:
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _build_orderbook_str(prices: list[int], volumes: list[int]) -> str:
    parts = [f"{price:,}({volume:,})" for price, volume in zip(prices, volumes)]
    return " / ".join(parts)


def _extract_json(text: str) -> dict:
    text = re.sub(r"```(?:json)?", "", text).strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("JSON 블록을 찾을 수 없습니다.")
    return json.loads(text[start:end])


def _normalize_candle_timestamp(timestamp: str) -> str:
    text = str(timestamp or "").strip()
    return text[-14:] if len(text) >= 14 else text


def _format_decision_timestamp(timestamp: str | None) -> str | None:
    if not timestamp:
        return None
    try:
        return datetime.strptime(timestamp, "%Y%m%d%H%M%S").strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except ValueError:
        return timestamp


def _resolve_decision_timestamp(
    raw_value: str | None, candles_desc: list[dict[str, Any]]
) -> str | None:
    if not raw_value:
        return None

    value = str(raw_value).strip()
    if not value:
        return None

    latest_ts = _normalize_candle_timestamp(candles_desc[0].get("timestamp", ""))
    latest_date = latest_ts[:8] if len(latest_ts) >= 8 else ""

    if len(value) == 4:
        value = f"{latest_date}{value}00"
    elif len(value) == 6:
        value = f"{latest_date}{value}"
    elif len(value) == 12:
        value = f"{value}00"
    elif len(value) != 14:
        raise ValueError(
            "decision-time 형식이 올바르지 않습니다. YYYYMMDDHHMM[SS] 또는 HHMM[SS]를 사용하세요."
        )

    try:
        datetime.strptime(value, "%Y%m%d%H%M%S")
    except ValueError as exc:
        raise ValueError(
            "decision-time 형식이 올바르지 않습니다. YYYYMMDDHHMM[SS] 또는 HHMM[SS]를 사용하세요."
        ) from exc
    return value


def _slice_candles_for_decision_time(
    candles_desc: list[dict[str, Any]], decision_timestamp: str | None
) -> list[dict[str, Any]]:
    if decision_timestamp is None:
        return candles_desc

    filtered = [
        candle
        for candle in candles_desc
        if _normalize_candle_timestamp(candle.get("timestamp", ""))
        <= decision_timestamp
    ]
    if not filtered:
        raise ValueError(f"지정 시점 {decision_timestamp} 이전 캔들이 없습니다.")
    return filtered


def _price_data_from_candle(candle: dict[str, Any]) -> dict[str, Any]:
    return {
        "current_price": int(candle["close"]),
        "change_rate": 0.0,
        "volume": int(candle["volume"]),
        "open_price": int(candle["open"]),
        "high_price": int(candle["high"]),
        "low_price": int(candle["low"]),
    }


def _normalize_action(raw_action: Any) -> str:
    text = str(raw_action or "HOLD").strip().upper()
    mapping = {
        "BUY": "BUY",
        "LONG": "BUY",
        "매수": "BUY",
        "SELL": "SELL",
        "CLOSE": "SELL",
        "EXIT": "SELL",
        "청산": "SELL",
        "매도": "SELL",
        "HOLD": "HOLD",
        "WAIT": "HOLD",
        "KEEP": "HOLD",
        "관망": "HOLD",
    }
    return mapping.get(text, "HOLD")


def _coerce_lm_message_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
        return "\n".join(part.strip() for part in parts if part).strip()
    return ""


def _extract_lm_response_text(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("LM Studio 응답에 choices가 없습니다.")

    message = choices[0].get("message", {})
    if not isinstance(message, dict):
        raise ValueError("LM Studio 응답에 message가 없습니다.")

    content_text = _coerce_lm_message_text(message.get("content"))
    if content_text:
        return content_text

    reasoning_text = _coerce_lm_message_text(message.get("reasoning_content"))
    if reasoning_text:
        return reasoning_text

    raise ValueError("LM Studio 응답에 content/reasoning_content가 비어 있습니다.")


class DelphiTrader:
    def __init__(
        self,
        ticker: str,
        plot_count: int,
        output_dir: Path,
        decision_time: str | None = None,
    ) -> None:
        self.ticker = ticker
        self.intervals = DECISION_INTERVALS
        self.plot_count = plot_count
        self.output_dir = output_dir
        self.decision_time_input = decision_time
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.auth_data = KISAuth(
            app_key=config.KIS_REAL_APP_KEY,
            app_secret=config.KIS_REAL_APP_SECRET,
            account_no=config.KIS_REAL_ACCOUNT_NO,
            base_url=config.BASE_URL_REAL,
            is_paper=False,
        )
        self.auth_trade = (
            KISAuth(
                app_key=config.KIS_PAPER_APP_KEY,
                app_secret=config.KIS_PAPER_APP_SECRET,
                account_no=config.KIS_PAPER_ACCOUNT_NO,
                base_url=config.BASE_URL_PAPER,
                is_paper=True,
            )
            if config.KIS_IS_PAPER
            else self.auth_data
        )
        self.market = KISMarket(auth_data=self.auth_data, auth_trade=self.auth_trade)
        self.order = KISOrder(self.auth_trade)
        self.risk = RiskManager()
        self.stock_name = self.market.get_stock_name(ticker)

    async def initialize(self) -> None:
        if self.market.is_etf(self.ticker):
            config.TRANSACTION_TAX_RATE = 0.0
            logger.info("[Init] ETF/ETN 감지 — 거래세 0%% 적용")

        await self.auth_data.get_token()
        if config.KIS_IS_PAPER:
            await self.auth_trade.get_token()

        await self._restore_position()

    async def _restore_position(self) -> None:
        try:
            balance = await self.market.get_balance()
            for pos_data in balance.get("positions", []):
                if pos_data.get("ticker") != self.ticker or pos_data.get("qty", 0) <= 0:
                    continue

                avg_price = int(pos_data["avg_price"])
                last_buy_time = get_last_buy_time(self.ticker)
                self.risk.restore_position(
                    ticker=self.ticker,
                    direction="BUY",
                    entry_price=avg_price,
                    qty=int(pos_data["qty"]),
                    stop_loss=avg_price * (1 - config.STOP_LOSS_RATIO),
                    take_profit=avg_price * (1 + config.TAKE_PROFIT_RATIO),
                    entry_time=last_buy_time,
                )
                logger.info(
                    "[Init] 기존 포지션 복구: %s %d주", self.ticker, pos_data["qty"]
                )
                break
        except Exception as exc:
            logger.error("[Init] 기존 포지션 복구 실패: %s", exc)

    async def _keyboard_monitor_loop(self) -> None:
        """Windows 콘솔에서 긴급 청산 단축키를 감시한다."""
        if sys.platform != "win32":
            return

        import msvcrt

        logger.info(
            "[Monitor] 긴급 청산 단축키 활성화: 콘솔에서 's' 또는 'S' 키를 누르면 즉시 시장가 매도합니다."
        )

        while True:
            await asyncio.sleep(0.2)

            while msvcrt.kbhit():
                try:
                    char = msvcrt.getch()
                    if char not in (b"s", b"S", b"\x13"):
                        continue

                    if not self.risk.has_position:
                        logger.info(
                            "[Monitor] 긴급 청산 요청이 들어왔지만 보유 포지션이 없어 무시합니다."
                        )
                        continue

                    pos = self.risk.position
                    if pos is None:
                        continue

                    logger.critical(
                        "[🚨 긴급 청산 발동] 사용자 수동 개입! 미청산 포지션 %s %d주 — 시장가 매도 시작!",
                        pos.ticker,
                        pos.qty,
                    )
                    try:
                        price_data = await self.market.get_current_price(pos.ticker)
                        current_price = int(price_data["current_price"])
                        await self.order.sell_market(pos.ticker, pos.qty)
                        pnl = self.risk.close_position(
                            current_price, close_reason="PANIC_SELL_MANUAL"
                        )
                        write_close_log(
                            ticker=pos.ticker,
                            exit_price=current_price,
                            qty=pos.qty,
                            entry_price=pos.entry_price,
                            pnl=pnl,
                            close_reason="PANIC_SELL_MANUAL",
                            entry_time=pos.entry_time,
                        )
                        logger.info("[Monitor] 긴급 청산 완료.")
                    except Exception as exc:
                        logger.error("[Monitor] 긴급 청산 실패: %s", exc)
                except Exception as exc:
                    logger.error("[Monitor] 키보드 입력 처리 중 오류: %s", exc)

    async def _position_monitor_loop(self) -> None:
        next_interval = max(config.POSITION_CHECK_SEC, 1)

        while True:
            await asyncio.sleep(next_interval)

            if not self.risk.has_position:
                next_interval = max(config.POSITION_CHECK_SEC, 1)
                continue

            pos = self.risk.position
            if pos is None:
                next_interval = max(config.POSITION_CHECK_SEC, 1)
                continue

            try:
                price_data = await self.market.get_current_price(pos.ticker)
                current_price = int(price_data["current_price"])
            except Exception as exc:
                logger.error("[Monitor] 현재가 조회 실패: %s", exc)
                next_interval = max(config.POSITION_CHECK_SEC, 1)
                continue

            self.risk.record_price_tick(current_price)
            next_interval = self.risk.get_monitor_interval(current_price)
            check = self.risk.check_position(current_price)
            if check.get("action") == "HOLD":
                continue

            entry_time = pos.entry_time
            entry_price = pos.entry_price
            qty = pos.qty
            close_reason = str(check.get("action") or "RISK_EXIT")
            try:
                await self.order.sell_market(pos.ticker, qty)
                pnl = self.risk.close_position(current_price, close_reason=close_reason)
                write_close_log(
                    ticker=pos.ticker,
                    exit_price=current_price,
                    qty=qty,
                    entry_price=entry_price,
                    pnl=pnl,
                    close_reason=close_reason,
                    entry_time=entry_time,
                )
                logger.info(
                    "[Monitor] 리스크 청산 완료: %s %d주 @ %d",
                    pos.ticker,
                    qty,
                    current_price,
                )
                next_interval = max(config.POSITION_CHECK_SEC, 1)
            except Exception as exc:
                logger.error("[Monitor] 리스크 청산 실패: %s", exc)

    async def _collect_snapshot(self) -> dict[str, Any]:
        candles_desc_raw = await self.market.get_minute_candles(
            self.ticker, count=config.CANDLE_COUNT
        )
        decision_timestamp = _resolve_decision_timestamp(
            self.decision_time_input, candles_desc_raw
        )
        candles_desc = _slice_candles_for_decision_time(
            candles_desc_raw, decision_timestamp
        )

        if decision_timestamp is None:
            price_data = await self.market.get_current_price(self.ticker)
            orderbook = await self.market.get_orderbook(self.ticker)
            market_change = await self.market.get_market_index_change()
            trade_strength = await self.market.get_trade_strength(self.ticker)
        else:
            latest_visible_candle = candles_desc[0]
            price_data = _price_data_from_candle(latest_visible_candle)
            orderbook = {
                "ask_prices": [],
                "ask_volumes": [],
                "bid_prices": [],
                "bid_volumes": [],
                "buy_ratio": 0.5,
            }
            market_change = 0.0
            trade_strength = 0.0

        balance = await self.market.get_balance()

        interval_snapshots: dict[int, dict[str, Any]] = {}
        for interval in self.intervals:
            candles_asc = _prepare_interval_candles(candles_desc, interval)
            analysis_candles = list(reversed(candles_asc))
            indicators = (
                compute_all_indicators(analysis_candles) if analysis_candles else {}
            )

            df = _build_indicator_frame(candles_asc) if candles_asc else None
            if df is not None and self.plot_count > 0:
                df = df.tail(self.plot_count).reset_index(drop=True)
                df["x"] = np.arange(len(df), dtype=float)

            interval_snapshots[interval] = {
                "candles_asc": candles_asc,
                "analysis_candles": analysis_candles,
                "indicators": indicators,
                "chart_frame": df,
            }

        base_snapshot = interval_snapshots[self.intervals[0]]
        self.risk.update_candles(base_snapshot["analysis_candles"])
        self.risk.update_atr(float(base_snapshot["indicators"].get("atr", 0.0)))

        return {
            "price_data": price_data,
            "candles_desc": candles_desc,
            "orderbook": orderbook,
            "market_change": market_change,
            "trade_strength": trade_strength,
            "balance": balance,
            "interval_snapshots": interval_snapshots,
            "analysis_candles": base_snapshot["analysis_candles"],
            "indicators": base_snapshot["indicators"],
            "decision_timestamp": decision_timestamp,
            "decision_timestamp_label": _format_decision_timestamp(decision_timestamp),
            "decision_is_historical": decision_timestamp is not None,
            "decision_current_time": (
                datetime.strptime(decision_timestamp, "%Y%m%d%H%M%S")
                if decision_timestamp is not None
                else None
            ),
        }

    def _build_prompt_text(
        self, snapshot: dict[str, Any], chart_paths: dict[int, Path]
    ) -> str:
        price_data = snapshot["price_data"]
        orderbook = snapshot["orderbook"]
        interval_snapshots = snapshot["interval_snapshots"]
        decision_timestamp_label = snapshot.get("decision_timestamp_label")
        decision_is_historical = bool(snapshot.get("decision_is_historical"))

        interval_summaries = []
        for interval in self.intervals:
            indicators = interval_snapshots[interval]["indicators"]
            interval_summaries.append(
                f"- {interval}분봉 ({chart_paths[interval].name}): RSI {float(indicators.get('rsi', 0.0)):.1f}, "
                f"MACD {float(indicators.get('macd', 0.0)):.1f}, "
                f"Signal {float(indicators.get('macd_signal', 0.0)):.1f}, "
                f"Hist {float(indicators.get('macd_histogram', 0.0)):.1f}, "
                f"ATR {float(indicators.get('atr', 0.0)):.1f}"
            )
        interval_summary_text = "\n".join(interval_summaries)
        decision_context_text = (
            f"판단 시점: {decision_timestamp_label}\n"
            if decision_timestamp_label
            else ""
        )
        historical_mode_text = ""
        if decision_is_historical:
            historical_mode_text = (
                "- 이 테스트는 지정 시점 재현입니다. 차트의 마지막 봉을 해당 시점으로 보고 판단하세요.\n"
                "- 호가창, 체결강도, 시장지수는 지정 시점의 실제 값이 아니므로 판단 근거에서 제외하세요.\n"
            )
        market_context_text = (
            "시장 지수 변동률: 테스트 모드에서는 사용 안 함\n"
            "체결강도: 테스트 모드에서는 사용 안 함\n"
            "매도호가(상위5): 테스트 모드에서는 사용 안 함\n"
            "매수호가(상위5): 테스트 모드에서는 사용 안 함\n"
            "호가 잔량비율: 테스트 모드에서는 사용 안 함"
            if decision_is_historical
            else (
                f"시장 지수 변동률: {snapshot['market_change']:.2f}%\n"
                f"체결강도: {snapshot['trade_strength']:.1f}%\n"
                f"매도호가(상위5): {_build_orderbook_str(orderbook.get('ask_prices', []), orderbook.get('ask_volumes', []))}\n"
                f"매수호가(상위5): {_build_orderbook_str(orderbook.get('bid_prices', []), orderbook.get('bid_volumes', []))}\n"
                f"호가 잔량비율: {orderbook.get('buy_ratio', 0.5):.1%}"
            )
        )

        return f"""
당신은 한국 주식 단기매매 보조 AI입니다.

입력으로 받는 내용:
- 최근 차트 이미지 3장 (1분봉, 3분봉, 5분봉 순서)
- 호가창 텍스트
- 현재가/체결강도/시장지수 정보

차트에는 최소한 다음 정보가 포함되어 있습니다.
- 볼린저밴드
- RSI
- 거래량
- MACD
- EMA(5,20,60)

판단 원칙:
- 지수이동평균선이 첨부 이미지 순서는 1분봉, 3분봉, 5분봉입니다.
- 차트에 포함된 볼린저밴드, RSI, 거래량, MACD를 직접 확인하세요.
- 호가창과 체결강도는 차트 해석을 보강하거나 반박하는 근거로 사용하세요.
- 반드시 현재 시점 기준으로 BUY, SELL, HOLD 중 하나만 선택하세요.
- BUY는 상승/반등 진입이 유리하다고 판단될 때 선택하세요.
- SELL은 하락 전환 또는 청산이 유리하다고 판단될 때 선택하세요.
- 불확실하면 HOLD를 선택하세요.

종목코드: {self.ticker}
종목명: {self.stock_name}
{decision_context_text}{historical_mode_text}현재가: {price_data['current_price']:,}원
시가/고가/저가: {price_data['open_price']:,}/{price_data['high_price']:,}/{price_data['low_price']:,}
{market_context_text}
분봉별 지표 요약:
{interval_summary_text}

위 차트와 호가 정보를 기준으로 지금 시점의 단일 액션을 판단하세요.
- 1분봉, 3분봉, 5분봉 차트를 함께 보고 마지막 시점의 방향성을 종합 판단하세요.
- reason에는 핵심 근거만 짧게 설명하세요.

반드시 아래 JSON 하나만 반환하세요.
{{
    "action": "BUY" | "SELL" | "HOLD",
    "reason": "판단 이유를 짧게 설명"
}}
""".strip()

    async def _ask_lm_studio(
        self, prompt_text: str, chart_paths: dict[int, Path]
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
        for interval in self.intervals:
            content.append(
                {"type": "text", "text": f"다음 이미지는 {interval}분봉 차트입니다."}
            )
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": _encode_image_to_data_url(chart_paths[interval])
                    },
                }
            )

        payload = {
            "model": config.LM_STUDIO_MODEL,
            "max_tokens": config.LM_STUDIO_MAX_TOKENS,
            "temperature": config.LM_STUDIO_TEMPERATURE,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": content,
                },
            ],
        }

        timeout = aiohttp.ClientTimeout(total=config.LM_STUDIO_TIMEOUT_SEC)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    config.LM_STUDIO_BASE_URL, json=payload
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
        except aiohttp.ClientError as exc:
            logger.error("[LM Studio] HTTP 오류: %s", exc)
            return {
                "action": "HOLD",
                "reason": str(exc),
            }
        except Exception as exc:
            logger.error("[LM Studio] 호출 실패: %s", exc)
            return {
                "action": "HOLD",
                "reason": str(exc),
            }

        try:
            raw_text = _extract_lm_response_text(data)
            parsed = _extract_json(raw_text)
        except Exception as exc:
            logger.error("[LM Studio] 응답 파싱 실패: %s | raw=%s", exc, data)
            return {
                "action": "HOLD",
                "reason": f"응답 파싱 실패: {exc}",
            }

        parsed["action"] = _normalize_action(parsed.get("action"))
        parsed.setdefault("reason", "사유 없음")
        return parsed

    async def _render_charts(
        self, interval_snapshots: dict[int, dict[str, Any]]
    ) -> dict[int, Path]:
        chart_paths: dict[int, Path] = {}
        now = datetime.now()
        timestamp = now.strftime("%Y%m%d%H%M%S")
        for interval in self.intervals:
            frame = interval_snapshots[interval]["chart_frame"]
            _render_interval_chart(
                frame,
                self.ticker,
                self.stock_name,
                interval,
                self.output_dir,
                timestamp=timestamp,
            )
            # 실제 경로 구성: charts/YYYYMMDD/HH/HHMM_1m.png
            day = now.strftime("%Y%m%d")
            hour = now.strftime("%H")
            hhmm = now.strftime("%H%M")
            actual_path = self.output_dir / day / hour / f"{hhmm}_{interval}m.png"
            chart_paths[interval] = actual_path
        return chart_paths

    async def run_cycle(self) -> None:
        now = datetime.now()
        try:
            snapshot = await self._collect_snapshot()
        except Exception as exc:
            logger.error("[Cycle] 데이터 수집 실패: %s", exc)
            write_cycle_log(
                ticker=self.ticker,
                current_price=0,
                gate_passed=False,
                action="SKIP",
                extra={"error": str(exc), "decision_source": "LM_STUDIO"},
            )
            return

        price_data = snapshot["price_data"]
        current_price = int(price_data["current_price"])
        total_assets = float(snapshot["balance"]["total_eval_amount"])
        self.risk.sync_daily_stats(total_assets)

        market_env = {
            "current_time": snapshot.get("decision_current_time") or now,
            "market_change": snapshot["market_change"],
            "current_volume": int(price_data["volume"]),
            "circuit_breaker": False,
            "daily_loss_ratio": self.risk.daily_stats.daily_loss_ratio,
        }
        gate_passed, gate_result = gate_market_filter(market_env)
        if gate_result.get("halt_trading_today"):
            self.risk.set_halt_today(True)

        if not gate_passed:
            write_cycle_log(
                ticker=self.ticker,
                current_price=current_price,
                gate_passed=False,
                gate_result=gate_result,
                action="SKIP",
                extra={"decision_source": "LM_STUDIO"},
            )
            return

        interval_snapshots = snapshot["interval_snapshots"]
        min_candles = max(config.BB_PERIOD, 30)
        invalid_intervals = []
        for interval in self.intervals:
            frame = interval_snapshots[interval]["chart_frame"]
            analysis_candles = interval_snapshots[interval]["analysis_candles"]
            if frame is None or frame.empty or len(analysis_candles) < min_candles:
                invalid_intervals.append(interval)

        if invalid_intervals:
            logger.warning(
                "[Cycle] 차트/지표 계산용 캔들 부족: %s분봉",
                ", ".join(str(interval) for interval in invalid_intervals),
            )
            write_cycle_log(
                ticker=self.ticker,
                current_price=current_price,
                gate_passed=True,
                gate_result=gate_result,
                action="SKIP",
                extra={
                    "reason": "캔들 부족",
                    "decision_source": "LM_STUDIO",
                    "intervals": list(self.intervals),
                    "missing_intervals": invalid_intervals,
                    "decision_timestamp": snapshot.get("decision_timestamp"),
                },
            )
            return

        chart_paths = await self._render_charts(interval_snapshots)
        prompt_text = self._build_prompt_text(snapshot, chart_paths)
        decision = await self._ask_lm_studio(prompt_text, chart_paths)
        ai_action = _normalize_action(decision.get("action"))
        ai_reason = str(decision.get("reason", ""))

        executed_action = "HOLD"
        order_price: int | None = None
        order_qty: int | None = None
        stop_loss: float | None = None
        take_profit: float | None = None

        if ai_action == "HOLD":
            logger.info(
                "[Cycle] AI 보류: action=%s reason=%s",
                ai_action,
                ai_reason,
            )
        elif ai_action == "BUY":
            can_enter, enter_reason = self.risk.can_enter()
            if not can_enter:
                logger.info("[Cycle] BUY 진입 불가: %s", enter_reason)
            else:
                indicators = snapshot["indicators"]
                fee_viable, fee_reason = check_fee_viability(
                    current_price=float(current_price),
                    atr=float(indicators.get("atr", 0.0)),
                )
                if not fee_viable:
                    logger.info("[Cycle] BUY 수수료 필터 차단: %s", fee_reason)
                else:
                    order_params = self.risk.calc_order_params(
                        current_price=current_price,
                        total_assets=total_assets,
                        atr=float(indicators.get("atr", 0.0)),
                    )
                    qty = int(order_params["qty"])
                    if qty < 1:
                        logger.info("[Cycle] BUY 수량 부족")
                    else:
                        await self.order.buy_market(self.ticker, qty)
                        self.risk.open_position(
                            ticker=self.ticker,
                            direction="BUY",
                            entry_price=current_price,
                            qty=qty,
                            stop_loss=order_params["stop_loss"],
                            take_profit=order_params["take_profit"],
                        )
                        executed_action = "BUY"
                        order_price = current_price
                        order_qty = qty
                        stop_loss = float(order_params["stop_loss"])
                        take_profit = float(order_params["take_profit"])
                        logger.info(
                            "[Cycle] BUY 실행: %s %d주 @ %d",
                            self.ticker,
                            qty,
                            current_price,
                        )
        elif ai_action == "SELL":
            pos = self.risk.position
            if pos is None:
                logger.info("[Cycle] SELL 권고 무시 — 보유 포지션 없음")
            else:
                entry_time = pos.entry_time
                entry_price = pos.entry_price
                qty = pos.qty
                await self.order.sell_market(self.ticker, qty)
                pnl = self.risk.close_position(
                    current_price, close_reason="LM_STUDIO_SELL"
                )
                write_close_log(
                    ticker=self.ticker,
                    exit_price=current_price,
                    qty=qty,
                    entry_price=entry_price,
                    pnl=pnl,
                    close_reason="LM_STUDIO_SELL",
                    entry_time=entry_time,
                )
                executed_action = "SELL"
                order_price = current_price
                order_qty = qty
                logger.info(
                    "[Cycle] SELL 실행: %s %d주 @ %d", self.ticker, qty, current_price
                )

        write_cycle_log(
            ticker=self.ticker,
            current_price=current_price,
            gate_passed=True,
            gate_result=gate_result,
            arbiter_passed=ai_action in ("BUY", "SELL"),
            arbiter_direction=ai_action,
            arbiter_reason=ai_reason,
            action=executed_action if executed_action in ("BUY", "SELL") else "HOLD",
            order_price=order_price,
            order_qty=order_qty,
            stop_loss=stop_loss,
            take_profit=take_profit,
            extra={
                "decision_source": "LM_STUDIO",
                "intervals": list(self.intervals),
                "decision_timestamp": snapshot.get("decision_timestamp"),
                "chart_paths": {
                    str(interval): str(chart_paths[interval])
                    for interval in self.intervals
                },
                "ai_action": ai_action,
            },
        )

    async def run(self, once: bool = False) -> None:
        mode = "모의" if config.KIS_IS_PAPER else "실전"
        set_title(self.ticker, self.stock_name, mode=mode)

        monitor_task = asyncio.create_task(self._position_monitor_loop())
        keyboard_task = asyncio.create_task(self._keyboard_monitor_loop())
        kr_holidays = holidays.KR()

        try:
            while True:
                now = datetime.now()
                if self.decision_time_input:
                    await self.run_cycle()
                elif now.weekday() >= 5 or now.date() in kr_holidays:
                    logger.debug("[Main] 주말/공휴일 — 사이클 생략")
                else:
                    await self.run_cycle()

                if once:
                    break

                await asyncio.sleep(DECISION_LOOP_INTERVAL_SEC)
        finally:
            monitor_task.cancel()
            keyboard_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
            try:
                await keyboard_task
            except asyncio.CancelledError:
                pass

    async def safe_exit_cleanup(self) -> None:
        pos = self.risk.position
        if pos is not None and config.FORCE_CLOSE_ON_EXIT:
            try:
                price_data = await self.market.get_current_price(pos.ticker)
                exit_price = int(price_data["current_price"])
                await self.order.sell_market(pos.ticker, pos.qty)
                pnl = self.risk.close_position(exit_price, close_reason="USER_EXIT")
                write_close_log(
                    ticker=pos.ticker,
                    exit_price=exit_price,
                    qty=pos.qty,
                    entry_price=pos.entry_price,
                    pnl=pnl,
                    close_reason="USER_EXIT",
                    entry_time=pos.entry_time,
                )
                logger.info("[Main] 종료 청산 완료")
            except Exception as exc:
                logger.error("[Main] 종료 청산 실패: %s", exc)

        print_session_summary(self.risk.trade_history)


async def _async_main(args: argparse.Namespace) -> None:
    if args.real:
        config.KIS_IS_PAPER = False
        logger.warning("실투자 모드로 실행합니다.")

    trader = DelphiTrader(
        ticker=args.ticker,
        plot_count=args.plot_count,
        output_dir=Path(args.output_dir),
        decision_time=args.decision_time,
    )
    await trader.initialize()
    try:
        await trader.run(once=args.once)
    finally:
        await trader.safe_exit_cleanup()


def main() -> None:
    args = parse_args()
    setup_logging(
        ticker=args.ticker, level=logging.DEBUG if args.debug else logging.INFO
    )
    try:
        asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        logger.info("사용자 종료 요청")


if __name__ == "__main__":
    main()
