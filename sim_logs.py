import argparse
import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import config
from kis_api import KISAuth, KISMarket
from strategy.arbiter import Arbiter
from strategy.risk import RiskManager

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("sim_logs")


def create_mock_snapshot(record: dict) -> dict:
    """로그에 저장된 정보를 바탕으로 arbiter.build_prompt가 요구하는 snapshot 구조를 복원합니다."""
    # 1. 최신 로그에는 snapshot_data가 직접 저장되어 있으므로 우선 사용
    if "snapshot_data" in record:
        return record["snapshot_data"]

    current_price = record.get("current_price", 0)
    old_prompt_text = record.get("prompt_text", "")
    
    snapshot = {
        "price_data": {"current_price": current_price},
        "orderbook": {
            "buy_ratio": 0.5,
            "ask_prices": [],
            "ask_volumes": [],
            "bid_prices": [],
            "bid_volumes": [],
        },
        "market_change": 0.0,
        "trade_strength": 0.0,
        "indicators": {},
        "analysis_candles": [],
        "interval_snapshots": {3: {"analysis_candles": []}},
        "last_exit_info": None,
    }

    # 2. prompt_text가 있는 경우 (정규식 파싱)
    if old_prompt_text:
        m = re.search(r"시장 지수 변동률:\s*([-\d\.]+)%", old_prompt_text)
        if m:
            snapshot["market_change"] = float(m.group(1))

        m = re.search(r"체결강도:\s*([-\d\.]+)%", old_prompt_text)
        if m:
            snapshot["trade_strength"] = float(m.group(1))

        m = re.search(r"매수잔량비율:\s*([-\d\.]+)%", old_prompt_text)
        if m:
            snapshot["orderbook"]["buy_ratio"] = float(m.group(1)) / 100.0

        def parse_orderbook_str(label: str) -> tuple[list[int], list[int]]:
            pattern = rf"{label}:\s*(.*)"
            match = re.search(pattern, old_prompt_text)
            prices, volumes = [], []
            if match:
                items = match.group(1).strip().split(", ")
                for item in items:
                    m2 = re.search(r"(\d+)\((\d+)\)", item)
                    if m2:
                        prices.append(int(m2.group(1)))
                        volumes.append(int(m2.group(2)))
            return prices, volumes

        ask_prices, ask_volumes = parse_orderbook_str(r"매도호가 상위5\(잔량\)")
        bid_prices, bid_volumes = parse_orderbook_str(r"매수호가 상위5\(잔량\)")

        snapshot["orderbook"]["ask_prices"] = ask_prices
        snapshot["orderbook"]["ask_volumes"] = ask_volumes
        snapshot["orderbook"]["bid_prices"] = bid_prices
        snapshot["orderbook"]["bid_volumes"] = bid_volumes

        m_exit_time = re.search(r"매도 시간:\s*([0-9\-\s:]+)", old_prompt_text)
        m_exit_price = re.search(r"매도 가격:\s*([\d,]+)원", old_prompt_text)
        m_exit_res = re.search(r"결과:\s*([-\+\d\.]+)%\s*\(", old_prompt_text)

        if m_exit_time and m_exit_price and m_exit_res:
            snapshot["last_exit_info"] = {
                "exit_time": m_exit_time.group(1).replace(" ", "T"),
                "exit_price": int(m_exit_price.group(1).replace(",", "")),
                "net_pnl_ratio_pct": float(m_exit_res.group(1)),
            }
    # 3. prompt_text가 없는 경우 (gate_detail 및 analysis에서 추출)
    else:
        # market_change 추출
        gate_detail = record.get("gate_detail", [])
        for item in gate_detail:
            if item.get("check") == "market_change":
                m = re.search(r"([-\d\.]+)%", item.get("detail", ""))
                if m:
                    snapshot["market_change"] = float(m.group(1))
                break

        # trade_strength 추출 (analysis 텍스트 분석)
        analysis = record.get("analysis", {})
        orderbook_str = analysis.get("orderbook_strength", "")
        m_ts = re.search(r"체결강도[^\d]*(\d+)%", orderbook_str)
        if m_ts:
            snapshot["trade_strength"] = float(m_ts.group(1))

        m_br = re.search(r"매수잔량비율[^\d]*(\d+)%", orderbook_str)
        if m_br:
            snapshot["orderbook"]["buy_ratio"] = float(m_br.group(1)) / 100.0

    return snapshot


async def simulate_today(ticker: str):
    log_dir = os.environ.get("LOG_DIR", "logs")
    log_file = Path(log_dir) / f"trades_{ticker}.jsonl"
    if not log_file.exists():
        logger.error(f"로그 파일이 존재하지 않습니다: {log_file}")
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    cycles = []

    with log_file.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if record.get("event", "CYCLE") == "CYCLE" and record.get(
                    "gate_passed"
                ):
                    if record.get("timestamp", "").startswith(today_str):
                        if "chart_paths" in record:
                            cycles.append(record)
            except json.JSONDecodeError:
                pass

    if not cycles:
        logger.warning(
            "시뮬레이션을 실행할 수 있는 유효한 오늘자 사이클 로그(chart_paths 포함)가 없습니다."
        )
        return

    logger.info(
        f"총 {len(cycles)}개의 사이클 로그를 불러왔습니다. 시뮬레이션을 시작합니다..."
    )

    virtual_total_assets = 10_000_000
    risk = RiskManager()
    risk.sync_daily_stats(virtual_total_assets)
    arbiter = Arbiter(
        ticker=ticker,
        stock_name="Simulated",
        intervals=(1, 3, 5),
        risk=risk,
        output_dir=Path("charts"),
    )

    last_price = 0

    for record in cycles:
        ts = record["timestamp"]
        current_price = record.get("current_price", 0)
        last_price = current_price

        # 새 프롬프트 생성 (로그에서 추출한 데이터를 바탕으로 build_prompt 호출)
        mock_snapshot = create_mock_snapshot(record)
        new_prompt_text = arbiter.build_prompt(mock_snapshot)
        logger.info(f"\n{new_prompt_text}\n")

        chart_paths_str = record.get("chart_paths", {})
        chart_paths = {}
        for k, v in chart_paths_str.items():
            path = Path(v)
            if path.exists():
                chart_paths[int(k)] = path

        if not chart_paths:
            logger.warning(
                f"[{ts}] 차트 이미지가 존재하지 않아 이 사이클은 스킵합니다."
            )
            continue

        # 로그에 기록된 차트 인터벌에 맞춰 Arbiter 설정 동적 업데이트
        arbiter.intervals = tuple(sorted(chart_paths.keys()))

        logger.info(f"[{ts}] 현재가: {current_price:,}원 | LM Studio에 분석 요청 중...")

        decision = await arbiter.ask(new_prompt_text, chart_paths)
        from strategy import normalize_action

        ai_action = normalize_action(decision.get("action"))
        ai_confidence = int(decision.get("confidence", 0))
        ai_reason = decision.get("reason", "")

        if (
            ai_action in ("BUY", "SELL")
            and ai_confidence < config.ARBITER_MIN_CONFIDENCE
        ):
            ai_action = "HOLD"

        logger.info(
            f"[{ts}] AI 결정: {ai_action} (신뢰도: {ai_confidence}) | {ai_reason}"
        )

        if risk.has_position:
            risk.record_price_tick(current_price)
            check = risk.check_position(current_price)
            if check.get("action") in ("STOP_LOSS", "TAKE_PROFIT", "TRAILING_STOP"):
                pnl = risk.close_position(
                    current_price, close_reason=check.get("action")
                )
                logger.info(
                    f"[{ts}] ⚡ 가상 청산 (리스크 관리: {check.get('action')}) -> 수익금: {pnl:,.0f}원"
                )

        if ai_action == "BUY" and not risk.has_position:
            can_enter, enter_reason = risk.can_enter(
                current_price, virtual_total_assets
            )
            if can_enter:
                order_params = risk.calc_order_params(
                    current_price, virtual_total_assets, 0.0
                )
                qty = int(order_params["qty"])
                if qty > 0:
                    risk.add_position(
                        ticker=ticker,
                        direction="BUY",
                        entry_price=current_price,
                        qty=qty,
                        stop_loss=order_params["stop_loss"],
                        take_profit=order_params["take_profit"],
                    )
                    logger.info(
                        f"[{ts}] 🟢 가상 진입 (매수): {qty}주 @ {current_price:,}원"
                    )
            else:
                logger.info(f"[{ts}] ⚠️ BUY 결정이나 진입 불가: {enter_reason}")

        elif ai_action == "SELL" and risk.has_position:
            pnl = risk.close_position(current_price, close_reason="ARBITER_SELL")
            logger.info(f"[{ts}] 🔴 가상 청산 (매도) -> 수익금: {pnl:,.0f}원")

    if risk.has_position and last_price > 0:
        pnl = risk.close_position(last_price, close_reason="SIM_END_FORCE_CLOSE")
        logger.info(f"[종료] 장 마감 가상 강제 청산 -> 수익금: {pnl:,.0f}원")

    total_net = sum(r.net_pnl for r in risk.trade_history)
    wins = sum(1 for r in risk.trade_history if r.net_pnl >= 0)
    losses = sum(1 for r in risk.trade_history if r.net_pnl < 0)

    print("\n" + "=" * 55)
    print(" 📊 시뮬레이션 결과 요약")
    print("=" * 55)
    print(f" 종목명      : {ticker}")
    print(f" 시뮬레이션수: {len(cycles)} 회")
    print(f" 매매 횟수   : {len(risk.trade_history)} 번")
    print(f" 승패 기록   : {wins}승 {losses}패")
    print(f" 최종 수익금 : {total_net:+,.0f} 원")
    print("=" * 55 + "\n")


def main():
    parser = argparse.ArgumentParser(description="오늘자 로그 기반 매매 시뮬레이션")
    parser.add_argument("--ticker", type=str, default=config.TICKER, help="종목 코드")
    parser.add_argument("--name", type=str, help="종목 이름")
    args = parser.parse_args()

    ticker = args.ticker
    if args.name:
        auth = KISAuth(
            app_key=config.KIS_REAL_APP_KEY,
            app_secret=config.KIS_REAL_APP_SECRET,
            account_no=config.KIS_REAL_ACCOUNT_NO,
            base_url=config.BASE_URL_REAL,
            is_paper=False,
        )
        market = KISMarket(auth_data=auth, auth_trade=auth)
        matches = market.find_ticker_by_name(args.name)
        if matches:
            ticker = matches[0][0]
            print(f"'{args.name}' 검색 결과 -> {ticker} ({matches[0][1]})")
        else:
            print(f"'{args.name}'에 해당하는 종목을 찾을 수 없습니다.")
            return

    try:
        asyncio.run(simulate_today(ticker))
    except KeyboardInterrupt:
        print("\n시뮬레이션을 강제 종료합니다.")


if __name__ == "__main__":
    main()
