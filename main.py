"""LM Studio 기반 자동매매 진입점.

기능:
1. LM Studio(OpenAI 호환 API)에 차트 PNG와 호가창 텍스트를 전달한다.
2. AI의 BUY/SELL/HOLD 판단을 받아 KIS API 주문에 연결한다.

실행 예:
    python main_delphi.py --ticker 005930

    테스트로 한번만 실행한 후 주기를 끝내고 종료
    python main_delphi.py --ticker 005930 --once --real
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, time
from pathlib import Path
from typing import Any

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
)
from strategy import (
    Arbiter,
    RiskManager,
    compute_all_indicators,
    normalize_action,
)

logger = logging.getLogger(__name__)


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
    parser.add_argument(
        "--name",
        default=None,
        help="종목명으로 종목 지정. 예: 삼성전자  (--ticker 대신 사용 가능)",
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


def _price_data_from_candle(candle: dict[str, Any]) -> dict[str, Any]:
    return {
        "current_price": int(candle["close"]),
        "change_rate": 0.0,
        "volume": int(candle["volume"]),
        "open_price": int(candle["open"]),
        "high_price": int(candle["high"]),
        "low_price": int(candle["low"]),
    }


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
        self.arbiter = Arbiter(
            ticker=ticker,
            stock_name=self.stock_name,
            intervals=self.intervals,
            output_dir=self.output_dir,
            risk=self.risk,
        )

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

        chart_paths = await self.arbiter.render_charts(interval_snapshots)
        prompt_text = self.arbiter.build_prompt(snapshot)

        decision = await self.arbiter.ask(prompt_text, chart_paths)
        ai_action = normalize_action(decision.get("action"))
        ai_reason = str(decision.get("reason", ""))
        ai_confidence = int(decision.get("confidence", 0))
        if (
            ai_action in ("BUY", "SELL")
            and ai_confidence < config.ARBITER_MIN_CONFIDENCE
        ):
            logger.info(
                "[Cycle] %s → HOLD 강등 (confidence %d < %d)",
                ai_action,
                ai_confidence,
                config.ARBITER_MIN_CONFIDENCE,
            )
            ai_action = "HOLD"

        executed_action = "HOLD"
        order_price: int | None = None
        order_qty: int | None = None
        stop_loss: float | None = None
        take_profit: float | None = None

        analysis = decision.get("analysis", {})
        analysis_str = (
            (
                f"\n  - Trend: {analysis.get('trend_context', 'N/A')}"
                f"\n  - Entry Trigger: {analysis.get('entry_trigger', 'N/A')}"
                f"\n  - Orderbook Strength: {analysis.get('orderbook_strength', 'N/A')}"
            )
            if analysis
            else ""
        )

        logger.info(
            "[Cycle] %s: %s\nConfidence: %d%s",
            ai_action,
            ai_reason,
            ai_confidence,
            analysis_str,
        )

        if ai_action == "BUY":
            can_enter, enter_reason = self.risk.can_enter(current_price, total_assets)
            if not can_enter:
                logger.info("[Cycle] BUY 진입 불가: %s", enter_reason)
            else:
                indicators = snapshot["indicators"]
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
                    self.risk.add_position(
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
                    current_price, close_reason="ARBITER_SELL"
                )
                write_close_log(
                    ticker=self.ticker,
                    exit_price=current_price,
                    qty=qty,
                    entry_price=entry_price,
                    pnl=pnl,
                    close_reason="ARBITER_SELL",
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
                "confidence": ai_confidence,
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
    if args.name:
        _auth = KISAuth(
            app_key=config.KIS_REAL_APP_KEY,
            app_secret=config.KIS_REAL_APP_SECRET,
            account_no=config.KIS_REAL_ACCOUNT_NO,
            base_url=config.BASE_URL_REAL,
            is_paper=False,
        )
        _mkt = KISMarket(auth_data=_auth, auth_trade=_auth)
        matches = _mkt.find_ticker_by_name(args.name)
        if not matches:
            logger.error("'%s' 에 해당하는 종목을 찾을 수 없습니다.", args.name)
            return
        if len(matches) == 1:
            args.ticker = matches[0][0]
            logger.info("종목 확인: %s (%s)", matches[0][1], matches[0][0])
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
