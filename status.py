"""
status.py - 계좌 현황 모니터링

실행:
    python status.py [--interval 10] [--paper]

기능:
    - 주기적으로 계좌 잔고 및 종목별 보유 현황 갱신 (기본 10초)
    - 화면을 덮어쓰며 갱신 (스크롤 없음)
    - border가 있는 테이블로 컬럼 폭 자동 맞춤
"""

import argparse
import asyncio
import glob
import json
import os
import sys
import unicodedata
from datetime import datetime

from dotenv import load_dotenv

from kis_api import KISAuth, KISMarket

load_dotenv()

KIS_REAL_APP_KEY = os.environ.get("KIS_REAL_APP_KEY", "")
KIS_REAL_APP_SECRET = os.environ.get("KIS_REAL_APP_SECRET", "")
KIS_REAL_ACCOUNT_NO = os.environ.get("KIS_REAL_ACCOUNT_NO", "")

KIS_PAPER_APP_KEY = os.environ.get(
    "KIS_PAPER_APP_KEY", os.environ.get("KIS_APP_KEY", "")
)
KIS_PAPER_APP_SECRET = os.environ.get(
    "KIS_PAPER_APP_SECRET", os.environ.get("KIS_APP_SECRET", "")
)
KIS_PAPER_ACCOUNT_NO = os.environ.get(
    "KIS_PAPER_ACCOUNT_NO", os.environ.get("KIS_ACCOUNT_NO", "")
)

BASE_URL_REAL = "https://openapi.koreainvestment.com:9443"
BASE_URL_PAPER = "https://openapivts.koreainvestment.com:29443"


# ── 유틸리티 ────────────────────────────────────────────────────────────


def _display_width(text: str) -> int:
    """한글 등 전각문자를 2칸으로 계산한 표시 폭을 반환한다."""
    w = 0
    for ch in text:
        eaw = unicodedata.east_asian_width(ch)
        w += 2 if eaw in ("W", "F") else 1
    return w


def _pad(text: str, width: int, align: str = "left") -> str:
    """표시 폭 기준으로 패딩한다. align: left / right / center."""
    dw = _display_width(text)
    pad = max(width - dw, 0)
    if align == "right":
        return " " * pad + text
    elif align == "center":
        lp = pad // 2
        return " " * lp + text + " " * (pad - lp)
    return text + " " * pad


def _build_table(
    headers: list[str],
    rows: list[list[str]],
    aligns: list[str],
    min_widths: list[int] | None = None,
) -> str:
    """border가 있는 테이블 문자열을 생성한다. 컬럼 폭은 내용에 맞춰 자동 결정."""
    ncols = len(headers)
    # 컬럼별 최대 표시폭 계산
    col_w = [_display_width(h) for h in headers]
    if min_widths:
        col_w = [max(c, m) for c, m in zip(col_w, min_widths)]
    for row in rows:
        for i, cell in enumerate(row):
            col_w[i] = max(col_w[i], _display_width(cell))

    def _row_line(cells: list[str], als: list[str]) -> str:
        parts = [_pad(c, col_w[i], als[i]) for i, c in enumerate(cells)]
        return "│ " + " │ ".join(parts) + " │"

    top = "┌─" + "─┬─".join("─" * w for w in col_w) + "─┐"
    mid = "├─" + "─┼─".join("─" * w for w in col_w) + "─┤"
    bot = "└─" + "─┴─".join("─" * w for w in col_w) + "─┘"

    header_aligns = ["center"] * ncols
    lines: list[str] = []
    lines.append(top)
    lines.append(_row_line(headers, header_aligns))
    lines.append(mid)
    for row in rows:
        lines.append(_row_line(row, als=aligns))
    lines.append(bot)
    return "\n".join(lines)


def _clear_screen() -> None:
    """터미널 화면을 지운다."""
    if sys.platform == "win32":
        os.system("cls")
    else:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


# ── 메인 로직 ───────────────────────────────────────────────────────────


class KISMarketExt(KISMarket):
    def __init__(self, auth: KISAuth):
        super().__init__(auth_data=auth, auth_trade=auth)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="계좌 현황 모니터링")
    parser.add_argument(
        "--interval", type=int, default=10, help="갱신 주기 (초, 기본 10)"
    )
    parser.add_argument("--paper", action="store_true", help="모의투자 모드")
    return parser.parse_args()


def get_auth(is_paper: bool) -> KISAuth:
    if is_paper:
        return KISAuth(
            app_key=KIS_PAPER_APP_KEY,
            app_secret=KIS_PAPER_APP_SECRET,
            account_no=KIS_PAPER_ACCOUNT_NO,
            base_url=BASE_URL_PAPER,
            is_paper=True,
        )
    return KISAuth(
        app_key=KIS_REAL_APP_KEY,
        app_secret=KIS_REAL_APP_SECRET,
        account_no=KIS_REAL_ACCOUNT_NO,
        base_url=BASE_URL_REAL,
        is_paper=False,
    )


def get_recent_trades(limit: int = 20) -> list[dict]:
    """로그 파일에서 최신 완료된 거래(CLOSE 이벤트)를 추출한다."""
    log_dir = os.environ.get("LOG_DIR", "logs")
    log_pattern = os.path.join(log_dir, "trades_*.jsonl")
    log_files = glob.glob(log_pattern)

    trades = []
    for file_path in log_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        if record.get("event") == "CLOSE":
                            trades.append(record)
                    except json.JSONDecodeError:
                        continue
        except Exception:
            continue

    # 타임스탬프 기준 내림차순 정렬
    trades.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return trades[:limit]


def get_daily_summary() -> dict[str, dict]:
    """오늘 발생한 모든 종목별 거래 요약을 집계한다."""
    log_dir = os.environ.get("LOG_DIR", "logs")
    log_pattern = os.path.join(log_dir, "trades_*.jsonl")
    log_files = glob.glob(log_pattern)

    today_str = datetime.now().strftime("%Y-%m-%d")
    summary = {}

    for file_path in log_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        if record.get("event") == "CLOSE":
                            ts = record.get("timestamp", "")
                            if ts.startswith(today_str):
                                ticker = record.get("ticker", "UNKNOWN")
                                if ticker not in summary:
                                    summary[ticker] = {
                                        "trades": 0,
                                        "pnl": 0.0,
                                        "pnl_ratio": 0.0,
                                        "wins": 0,
                                        "losses": 0,
                                    }
                                s = summary[ticker]
                                s["trades"] += 1
                                s["pnl"] += record.get("pnl", 0.0)
                                s["pnl_ratio"] += record.get("pnl_ratio", 0.0)
                                if record.get("pnl", 0.0) >= 0:
                                    s["wins"] += 1
                                else:
                                    s["losses"] += 1
                    except json.JSONDecodeError:
                        continue
        except Exception:
            continue

    # 평균 수익률 계산
    for ticker in summary:
        s = summary[ticker]
        if s["trades"] > 0:
            s["pnl_ratio"] /= s["trades"]

    return summary


async def render_status(market: KISMarket, is_paper: bool) -> None:
    """잔고를 조회하고 화면을 덮어쓴다."""
    try:
        balance = await market.get_balance()
    except Exception as e:
        _clear_screen()
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 잔고 조회 실패: {e}")
        return

    total_eval = balance["total_eval_amount"]
    stock_eval = balance.get("stock_eval_amount", 0)
    cash = balance["cash_balance"]
    positions = balance["positions"]
    mode_str = "모의투자" if is_paper else "실투자"

    # -- 요약 정보 --
    summary_lines = [
        f"  계좌현황 - {mode_str}",
        f"  갱신시간 : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"  현금     : {cash:>14,} 원",
        f"  주식평가 : {stock_eval:>14,} 원",
        f"  총자산   : {total_eval:>14,} 원",
        "",
    ]

    # -- 종목 테이블 --
    headers = [
        "종목코드",
        "종목명",
        "수량",
        "매입가",
        "현재가",
        "평가액",
        "손익",
        "수익률",
    ]
    aligns = ["left", "left", "right", "right", "right", "right", "right", "right"]
    min_widths = [8, 10, 6, 10, 10, 12, 12, 8]

    if not positions:
        rows = [["", "보유 종목 없음", "", "", "", "", "", ""]]
    else:
        rows = []
        for p in positions:
            ticker = p["ticker"]
            name = market.get_stock_name(ticker)
            qty = p["qty"]
            avg_price = p["avg_price"]
            cur_price = p.get("current_price", 0)
            eval_amt = p.get("eval_amount", 0)
            pnl = p.get("pnl_amount", 0)
            pnl_ratio = p.get("pnl_ratio", 0.0)

            if cur_price == 0 and qty > 0:
                cur_price = eval_amt // qty
            if eval_amt == 0 and cur_price > 0:
                eval_amt = cur_price * qty
            if pnl == 0 and avg_price > 0 and cur_price != avg_price:
                pnl = (cur_price - avg_price) * qty
                pnl_ratio = (cur_price - avg_price) / avg_price * 100

            rows.append(
                [
                    ticker,
                    name,
                    f"{qty:,}",
                    f"{avg_price:,}",
                    f"{cur_price:,}",
                    f"{eval_amt:,}",
                    f"{int(pnl):+,}",
                    f"{pnl_ratio:+.2f}%",
                ]
            )

    table_str = _build_table(headers, rows, aligns, min_widths)

    # -- 최근 거래 내역 --
    recent_trades = get_recent_trades(20)
    trade_headers = [
        "매수시간",
        "매도시간",
        "종목",
        "수량",
        "진입가",
        "청산가",
        "수익금",
        "수익률",
        "보유시간",
    ]
    trade_aligns = [
        "center",
        "center",
        "left",
        "right",
        "right",
        "right",
        "right",
        "right",
        "right",
    ]
    trade_min_widths = [19, 19, 14, 6, 10, 10, 10, 8, 10]

    if not recent_trades:
        trade_rows = [["", "", "거래 내역 없음", "", "", "", "", "", ""]]
    else:
        trade_rows = []
        for t in recent_trades:
            # ISO timestamp에서 년월일 시간 추출
            ts_exit_raw = t.get("timestamp", "")
            ts_exit = ts_exit_raw.replace("T", " ")[:19] if ts_exit_raw else ""

            ts_entry_raw = t.get("entry_time", "")
            ts_entry = ts_entry_raw.replace("T", " ")[:19] if ts_entry_raw else ""

            ticker = t.get("ticker", "")
            name = market.get_stock_name(ticker)
            qty = t.get("qty", 0)
            entry = t.get("entry_price", 0)
            exit = t.get("exit_price", 0)
            pnl = t.get("pnl", 0)
            pnl_ratio = t.get("pnl_ratio", 0.0) * 100

            # 보유 시간 계산
            holding_time = "-"
            if ts_exit_raw and ts_entry_raw:
                try:
                    dt_exit = datetime.fromisoformat(ts_exit_raw)
                    dt_entry = datetime.fromisoformat(ts_entry_raw)
                    diff = dt_exit - dt_entry
                    h_secs = int(diff.total_seconds())
                    if h_secs < 0:
                        holding_time = "0초"
                    else:
                        h_min = h_secs // 60
                        h_sec = h_secs % 60
                        holding_time = (
                            f"{h_min}분 {h_sec}초" if h_min > 0 else f"{h_sec}초"
                        )
                except Exception:
                    pass

            trade_rows.append(
                [
                    ts_entry,
                    ts_exit,
                    f"{name}({ticker})",
                    f"{qty:,}",
                    f"{entry:,}",
                    f"{exit:,}",
                    f"{int(pnl):+,}",
                    f"{pnl_ratio:+.2f}%",
                    holding_time,
                ]
            )

    trade_table_str = _build_table(
        trade_headers, trade_rows, trade_aligns, trade_min_widths
    )

    # -- 오늘 거래 요약 (집계) --
    daily_summary = get_daily_summary()
    summary_headers = ["종목", "거래", "승/패", "수익금", "평균익률"]
    summary_aligns = ["left", "right", "center", "right", "right"]
    summary_min_widths = [14, 6, 8, 12, 10]

    if not daily_summary:
        summary_rows = [["", "오늘 거래 없음", "", "", ""]]
    else:
        summary_rows = []
        # 수익금 기준 내림차순 정렬
        sorted_tickers = sorted(
            daily_summary.keys(), key=lambda x: daily_summary[x]["pnl"], reverse=True
        )
        for ticker in sorted_tickers:
            s = daily_summary[ticker]
            name = market.get_stock_name(ticker)
            summary_rows.append(
                [
                    f"{name}({ticker})",
                    f"{s['trades']}회",
                    f"{s['wins']}/{s['losses']}",
                    f"{int(s['pnl']):+,}",
                    f"{s['pnl_ratio']*100:+.2f}%",
                ]
            )

    summary_table_str = _build_table(
        summary_headers, summary_rows, summary_aligns, summary_min_widths
    )

    # -- 화면 출력 --
    _clear_screen()
    print("\n".join(summary_lines))
    print(table_str)

    if positions:
        total_pnl = sum(
            p.get("pnl_amount", 0)
            or ((p.get("current_price", 0) - p["avg_price"]) * p["qty"])
            for p in positions
        )
        print(f"\n  보유 종목 실시간 손익 : {int(total_pnl):+,} 원")

    print(f"\n  오늘 종목별 누적 성과 (전체 로그 집계)")
    print(summary_table_str)

    print(f"\n  최근 거래 내역 (20건)")
    print(trade_table_str)


async def main() -> None:
    args = parse_args()
    auth = get_auth(args.paper)
    market = KISMarketExt(auth)

    while True:
        await render_status(market, args.paper)
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n종료되었습니다.")
