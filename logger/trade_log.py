"""
logger/trade_log.py — 거래 로그 기록

형식: JSON Lines (.jsonl) — 매 주기 한 줄씩 추가
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import config

logger = logging.getLogger(__name__)

_LOG_DIR = Path(config.LOG_DIR)


def _log_path(ticker: str) -> Path:
    """종목별 로그 파일 경로를 반환한다. (예: logs/trades_005930.jsonl)"""
    base = Path(config.LOG_FILE).stem  # 'trades'
    ext = Path(config.LOG_FILE).suffix  # '.jsonl'
    return _LOG_DIR / f"{base}_{ticker}{ext}"


def _ensure_log_dir() -> None:
    """로그 디렉터리가 없으면 생성한다."""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def write_cycle_log(
    ticker: str,
    current_price: int,
    gate_passed: bool,
    gate_result: Optional[dict] = None,
    arbiter_passed: bool = False,
    arbiter_direction: Optional[str] = None,
    arbiter_reason: Optional[str] = None,
    action: str = "SKIP",  # BUY / SELL / HOLD / SKIP
    order_price: Optional[int] = None,
    order_qty: Optional[int] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    result: Optional[float] = None,  # 청산 후 수익률
    vwap: Optional[float] = None,
    price_vs_vwap: Optional[float] = None,
    momentary_amt: Optional[int] = None,
    tick_weighted_imbalance: Optional[float] = None,
    extra: Optional[dict] = None,
) -> None:
    """
    매 주기 실행 결과를 JSON Lines 형식으로 기록한다.

    Args:
        ticker: 종목 코드
        current_price: 현재가
        gate_passed: Gate 통과 여부
        gate_result: Gate 상세 결과
        arbiter_passed: Arbiter 통과 여부
        arbiter_direction: Arbiter 판단 방향
        arbiter_reason: Arbiter 판단 근거
        action: 실행 액션
        order_price: 주문 가격
        order_qty: 주문 수량
        stop_loss: 손절가
        take_profit: 익절가
        result: 청산 후 수익률 (None이면 미청산)
        vwap: 당일 VWAP (가중평균가)
        price_vs_vwap: 현재가와 VWAP 이격률 (%)
        momentary_amt: 최근 사이클 간 순간 체결대금 (원)
        tick_weighted_imbalance: 호가 틱 가중 잔량 비율
        extra: 추가 정보
    """
    _ensure_log_dir()

    record: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "ticker": ticker,
        "current_price": current_price,
        "gate_passed": gate_passed,
        "arbiter_passed": arbiter_passed,
        "arbiter_direction": arbiter_direction,
        "arbiter_reason": arbiter_reason,
        "action": action,
        "order_price": order_price,
        "order_qty": order_qty,
        "stop_loss": round(stop_loss, 0) if stop_loss is not None else None,
        "take_profit": round(take_profit, 0) if take_profit is not None else None,
        "result": result,
        "vwap": vwap,
        "price_vs_vwap": price_vs_vwap,
        "momentary_amt": momentary_amt,
        "tick_weighted_imbalance": tick_weighted_imbalance,
    }

    if gate_result:
        record["gate_detail"] = gate_result.get("checks", [])

    if extra:
        record.update(extra)

    try:
        log_file = _log_path(ticker)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.error("[TradeLog] 로그 기록 실패: %s", e)


def write_close_log(
    ticker: str,
    exit_price: int,
    qty: int,
    entry_price: int,
    pnl: float,
    close_reason: str,
    entry_time: Optional[datetime] = None,
) -> None:
    """포지션 청산 이벤트를 기록한다."""
    _ensure_log_dir()

    pnl_ratio = pnl / (entry_price * qty) if qty > 0 and entry_price > 0 else 0.0
    record: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "event": "CLOSE",
        "ticker": ticker,
        "exit_price": exit_price,
        "entry_price": entry_price,
        "entry_time": entry_time.isoformat(timespec="seconds") if entry_time else None,
        "qty": qty,
        "pnl": round(pnl, 0),
        "pnl_ratio": round(pnl_ratio, 6),
        "close_reason": close_reason,
    }
    try:
        log_file = _log_path(ticker)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.error("[TradeLog] 청산 로그 기록 실패: %s", e)


def get_last_buy_time(ticker: str) -> Optional[datetime]:
    """
    로그 파일에서 해당 종목의 가장 최근 BUY 진입 시간을 찾아 반환한다.
    로그가 없거나 기록이 없으면 None을 반환한다.
    """
    log_file = _log_path(ticker)
    if not log_file.exists():
        return None

    last_time = None
    try:
        with log_file.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    # 사이클 로그 중 실행 액션이 BUY인 내역 탐색
                    if record.get("ticker") == ticker and record.get("action") == "BUY":
                        ts_str = record.get("timestamp")
                        if ts_str:
                            last_time = datetime.fromisoformat(ts_str)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.error("[TradeLog] 로그 읽기 실패: %s", e)

    return last_time


def print_session_summary(trade_history: list) -> None:
    """
    세션 거래 요약을 콘솔(stdout)과 app.log에 출력한다.
    프로그램 종료 직전에 호출한다.

    Args:
        trade_history: RiskManager.trade_history (list[TradeRecord])
    """
    sep_thick = "=" * 68
    sep_thin = "-" * 68

    lines: list[str] = [
        "",
        sep_thick,
        "  📊  세션 거래 요약",
        sep_thick,
    ]

    if not trade_history:
        lines.append("  ※ 이번 세션에서 완결된 거래가 없습니다.")
        lines.append(sep_thick)
        _print_and_log(lines)
        return

    total_gross = 0.0
    total_comm = 0.0
    total_net = 0.0
    win_count = 0
    loss_count = 0

    for idx, rec in enumerate(trade_history, start=1):
        holding_secs = int((rec.exit_time - rec.entry_time).total_seconds())
        holding_min = holding_secs // 60
        holding_sec = holding_secs % 60
        direction_kr = "매수→매도" if rec.direction == "BUY" else "매도→매수"
        reason_map = {
            "STOP_LOSS": "손절",
            "TRAILING_STOP": "트레일링 스탑",
            "FORCE_CLOSE_MARKET": "장 마감 강제 청산",
            "FORCE_CLOSE_DAILY_LOSS": "일일 손실 한도 초과",
            "USER_EXIT": "사용자 종료",
            "UNKNOWN": "기타",
        }
        reason_kr = reason_map.get(rec.close_reason, rec.close_reason)
        pnl_sign = "▲" if rec.net_pnl >= 0 else "▼"

        lines += [
            "",
            f"  [{idx:02d}] {rec.ticker}  {direction_kr}  ({reason_kr})",
            f"       진입: {rec.entry_time.strftime('%Y-%m-%d %H:%M:%S')}  "
            f"→  청산: {rec.exit_time.strftime('%H:%M:%S')}  "
            f"(보유 {holding_min}분 {holding_sec:02d}초)",
            f"       진입가: {rec.entry_price:>10,}원  ×  {rec.qty:>5,}주",
            f"       청산가: {rec.exit_price:>10,}원",
            f"       수익(Gross): {rec.gross_pnl:>+12,.0f}원",
            f"       수수료:      {rec.commission:>12,.0f}원" f"  (브로커×2 + 거래세)",
            f"       순손익(Net): {rec.net_pnl:>+12,.0f}원  "
            f"({rec.net_pnl_ratio:>+.2%})  {pnl_sign}",
            sep_thin,
        ]

        total_gross += rec.gross_pnl
        total_comm += rec.commission
        total_net += rec.net_pnl
        if rec.net_pnl >= 0:
            win_count += 1
        else:
            loss_count += 1

    total_count = len(trade_history)
    win_rate = win_count / total_count * 100 if total_count > 0 else 0.0
    net_sign = "▲" if total_net >= 0 else "▼"

    lines += [
        "",
        f"  총 거래 횟수: {total_count:>3}건  (수익 {win_count}건 / 손실 {loss_count}건 / 승률 {win_rate:.1f}%)",
        f"  총 수익(Gross):  {total_gross:>+14,.0f}원",
        f"  총 수수료:       {total_comm:>14,.0f}원",
        f"  총 순손익(Net):  {total_net:>+14,.0f}원  {net_sign}",
        sep_thick,
    ]

    _print_and_log(lines)


def _print_and_log(lines: list[str]) -> None:
    """콘솔과 로거 양쪽에 출력한다."""
    import sys
    output = "\n".join(lines)
    try:
        print(output)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        safe_output = output.encode(encoding, errors="replace").decode(encoding)
        print(safe_output)
    # app.log에도 기록 (이미 핸들러가 붙어있으면 logging 사용)
    for line in lines:
        if line.strip():
            logging.getLogger(__name__).info(line)


class _ColorFormatter(logging.Formatter):
    """콘솔 전용 색상 포매터.

    레벨별로 ANSI 색상을 적용한다. 파일 핸들러에는 사용하지 않는다.
    """

    _RESET = "\033[0m"
    _LEVEL_COLORS: dict[int, str] = {
        logging.DEBUG: "\033[36m",  # 청록 (Cyan)
        logging.INFO: "\033[32m",  # 초록 (Green)
        logging.WARNING: "\033[33m",  # 노랑 (Yellow)
        logging.ERROR: "\033[31m",  # 빨강 (Red)
        logging.CRITICAL: "\033[1;31m",  # 굵은 빨강 (Bold Red)
    }

    def format(self, record: logging.LogRecord) -> str:
        color = self._LEVEL_COLORS.get(record.levelno, "")
        # levelname에만 색상 적용 (메시지 전체에 색상이 번지지 않도록)
        original_levelname = record.levelname
        record.levelname = f"{color}{record.levelname}{self._RESET}"
        result = super().format(record)
        record.levelname = original_levelname  # 원상복구 (다른 핸들러 영향 방지)
        return result


def setup_logging(ticker: str, level: int = logging.INFO) -> None:
    """루트 로거를 콘솔 + 파일 핸들러로 설정한다.

    - 콘솔(StreamHandler): 레벨별 ANSI 색상 적용
    - 파일(FileHandler): 종목별 일반 텍스트 (ANSI 코드 없음)
    """
    _ensure_log_dir()
    log_format = "%(asctime)s [%(levelname)s]: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    # 콘솔 핸들러 — 색상 포매터 적용
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(_ColorFormatter(log_format, datefmt=datefmt))

    # 파일 핸들러 — 종목별 일반 텍스트 포매터 (색상 코드 없음)
    file_handler = logging.FileHandler(
        _LOG_DIR / f"app_{ticker}.log",
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(log_format, datefmt=datefmt))

    logging.basicConfig(level=level, handlers=[console_handler, file_handler])
