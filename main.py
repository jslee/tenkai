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
from datetime import datetime, time
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
    ) -> None:
        self.ticker = ticker
        self.intervals = DECISION_INTERVALS
        self.plot_count = plot_count
        self.output_dir = output_dir
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
        candles_desc = candles_desc_raw
        price_data = await self.market.get_current_price(self.ticker)
        orderbook = await self.market.get_orderbook(self.ticker)
        market_change = await self.market.get_market_index_change()
        trade_strength = await self.market.get_trade_strength(self.ticker)

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
        }

    def _build_prompt_text(self, snapshot: dict[str, Any]) -> str:
        orderbook = snapshot["orderbook"]
        current_price = snapshot["price_data"]["current_price"]

        pos = self.risk.position
        if pos:
            pnl_ratio = (current_price - pos.entry_price) / pos.entry_price * 100
            if pos.direction == "SELL":
                pnl_ratio = -pnl_ratio
            position_text = f"[{pos.direction}] 진입가 {pos.entry_price:,}원 (현재 수익률 {pnl_ratio:+.2f}%)"
        else:
            position_text = "없음 (미보유 상태)"

        market_context_text = (
            f"시장 지수 변동률: {snapshot['market_change']:.2f}%\n"
            f"체결강도: {snapshot['trade_strength']:.1f}%\n"
            f"매도호가 상위5(잔량): {_build_orderbook_str(orderbook.get('ask_prices', []), orderbook.get('ask_volumes', []))}\n"
            f"매수호가 상위5(잔량): {_build_orderbook_str(orderbook.get('bid_prices', []), orderbook.get('bid_volumes', []))}\n"
            f"호가 잔량비율: {orderbook.get('buy_ratio', 0.5):.1%}"
        )

        return f"""
당신은 한국 주식 초단기 트레이딩을 수행하는 전문적인 '퀀트 트레이딩 에이전트'입니다.
목표: 제공된 멀티 타임프레임 차트와 호가 데이터를 분석하여 즉각적인 매매 의사결정을 내립니다.

[입력 데이터 구성]
- 최근 차트 이미지 3장 (1분봉, 3분봉, 5분봉 순서)
  * 차트 포함 지표: 캔들, 볼린저밴드, RSI, 거래량, MACD, EMA(5, 20, 60)
  * 세 차트 모두 오른쪽 마지막 캔들이 현재 시점입니다.
- 시장지수/체결강도 및 호가창 텍스트
- 현재 보유 포지션

[현재 시장/호가 데이터]
{market_context_text}

[현재 보유 포지션]
{position_text}

[분석 및 판단 원칙 - 반드시 준수할 것]
1. 상위 타임프레임 우선의 원칙 (Top-Down Approach):
   - 5분봉의 추세(Trend)를 '전략적 방향'으로 삼고, 1분/3분봉은 '진입 타이밍'을 결정하는 용도로 사용한다.
   - 만약 5분봉의 방향과 1분봉의 신호가 충돌할 경우, 반드시 'HOLD'를 선택하여 리스크를 관리한다.
2. 데이터 교차 검증 (Cross-Verification):
   - 차트의 기술적 지표(EMA, MACD, 볼린저밴드 등)와 호가창의 수급(매도/매수 잔량비율, 체결강도)이 일치할 때만 강력한 신호로 간주한다.
   - 예: 가격은 상승 중이나 매도호가 잔량이 압도적으로 많고 체결강도가 급락 중이라면 'SELL' 혹은 'HOLD'를 고려한다.
3. 액션(BUY/SELL/HOLD) 판단 기준 및 엄격성:
   - BUY: 상승/반등 진입이 유리하다고 확신할 때만 선택
   - SELL: 하락 전환 또는 보유 포지션 청산이 유리하다고 확신할 때 선택
   - HOLD: 불확실하거나 방향성이 모호하면 무조건 관망(HOLD)한다.
4. 보유 정보를 제공하는 목적은 '매수/매도 전략'을 정교화하기 위함이지, 손실 중인 종목을 무조건 유지하라는 뜻이 아닙니다. 차트의 기술적 신호가 파괴되었다면, 매수가와 관계없이 냉정하게 SELL을 결정하세요.

[출력 형식]
반드시 아래 구조의 JSON 객체 하나만 반환하십시오. 다른 설명은 생략합니다.

{{
    "action": "BUY" | "SELL" | "HOLD",
    "confidence": 0~100, // 판단에 대한 확신도를 숫자로 표현 (80 이상이면 강력한 신호)
    "analysis": {{
        "trend": "5분봉 기준의 현재 추세 상태 (상승/하락/횡보)",
        "momentum": "MACD 및 RSI를 통한 에너지 상태 (강화/약화/중립)",
        "orderbook": "호가창과 체결강도가 차트 신호를 뒷받침하는지 여부"
    }},
    "reason": "최종 결정을 내린 핵심적인 근거 한 문장"
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
        _open = time(*map(int, config.MARKET_OPEN_TIME.split(":")))
        _close = time(*map(int, config.MARKET_CLOSE_TIME.split(":")))
        if not (_open <= now.time() <= _close):
            return

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
        prompt_text = self._build_prompt_text(snapshot)

        decision = await self._ask_lm_studio(prompt_text, chart_paths)
        ai_action = _normalize_action(decision.get("action"))
        ai_reason = str(decision.get("reason", ""))

        executed_action = "HOLD"
        order_price: int | None = None
        order_qty: int | None = None
        stop_loss: float | None = None
        take_profit: float | None = None

        analysis = decision.get("analysis", {})
        analysis_str = (
            (
                f"\n  - Trend: {analysis.get('trend', 'N/A')}"
                f"\n  - Momentum: {analysis.get('momentum', 'N/A')}"
                f"\n  - Orderbook: {analysis.get('orderbook', 'N/A')}"
            )
            if analysis
            else ""
        )

        logger.info(
            "[Cycle] %s: %s\nConfidence: %s\n%s",
            ai_action,
            ai_reason,
            decision.get("confidence", 0),
            analysis_str,
        )

        if ai_action == "BUY":
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
                logger.info("[Cycle] SELL 진입 불가: 보유 포지션 없음")
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
                "confidence": decision.get("confidence", 0),
                "analysis": analysis,
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
                if now.weekday() >= 5 or now.date() in kr_holidays:
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
        ticker=args.ticker, plot_count=args.plot_count, output_dir=Path(args.output_dir)
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
