"""
strategy/risk.py — 리스크 관리

원칙: 이 규칙은 어떤 상황에서도 우회되어서는 안 된다.

포지션 진입 시:
    주문 수량 = floor(총자산 × MAX_POSITION_RATIO / 현재가)
    손절가    = 진입가 × (1 - STOP_LOSS_RATIO)
    익절가    = 진입가 × (1 + TAKE_PROFIT_RATIO)

포지션 보유 중 (매 10초 체크):
    1. 현재가 <= 손절가          → 즉시 시장가 청산
    2. 현재가 >= 익절가          → 트레일링 스탑 전환
       트레일링 스탑 = max(현재가 × (1 - TRAILING_STOP_RATIO), 기존 트레일링)
    3. 당일 누적 손실 >= MAX_DAILY_LOSS_RATIO → 강제 청산 후 당일 거래 중단
    4. 장 마감 14:50            → 미청산 포지션 전량 청산

일일 거래 제한:
    - MAX_TRADES_PER_DAY 초과 시 신규 진입 차단 (청산만 허용)
    - 동일 종목 중복 포지션 금지
"""

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time, date, timedelta
from typing import Optional

import config
import config as _cfg  # 수수료 계산용 alias (동일 모듈, 두 이름으로 참조)

logger = logging.getLogger(__name__)

_MARKET_CLOSE_TIME = time(*map(int, config.MARKET_CLOSE_TIME.split(":")))


@dataclass
class TradeRecord:
    """
    완결된 거래 1건 기록 (진입 → 청산)
    세션 종료 시 요약 출력에 사용된다.
    """

    ticker: str
    direction: str  # 'BUY' | 'SELL'
    entry_time: datetime
    exit_time: datetime
    entry_price: int
    exit_price: int
    qty: int
    gross_pnl: float  # 수수료 차감 전 손익 (원)
    commission: float  # 총 수수료 (매수 + 매도 브로커 + 거래세)
    net_pnl: float  # 수수료 차감 후 순손익 (원)
    close_reason: (
        str  # 'STOP_LOSS' | 'TRAILING_STOP' | 'FORCE_CLOSE_MARKET' | 'USER_EXIT' 등
    )

    @property
    def net_pnl_ratio(self) -> float:
        """투자 원금 대비 순수익률"""
        cost = self.entry_price * self.qty
        return self.net_pnl / cost if cost > 0 else 0.0


@dataclass
class Position:
    """보유 포지션 정보"""

    ticker: str
    direction: str  # 'BUY' | 'SELL'
    entry_price: int
    qty: int
    stop_loss: float
    take_profit: float
    trailing_stop: Optional[float] = None
    trailing_active: bool = False
    entry_time: datetime = field(default_factory=datetime.now)


@dataclass
class DailyStats:
    """일별 거래 통계"""

    date: datetime = field(default_factory=datetime.today)
    trade_count: int = 0
    realized_pnl: float = 0.0  # 실현 손익 (원)
    initial_total_assets: float = 0.0  # 당일 시작 시 총자산

    def reset_if_new_day(self) -> None:
        today = date.today()
        if self.date != today:
            self.date = today
            self.trade_count = 0
            self.realized_pnl = 0.0
            self.initial_total_assets = 0.0

    @property
    def daily_loss_ratio(self) -> float:
        """당일 손실률 (양수=손실). 초기 자산이 0이면 0 반환."""
        if self.initial_total_assets <= 0:
            return 0.0
        pnl_ratio = self.realized_pnl / self.initial_total_assets
        # 손실이면 양수로 반환
        return max(-pnl_ratio, 0.0)


class RiskManager:
    """리스크 관리 클래스"""

    def __init__(self) -> None:
        self._position: Optional[Position] = None
        self._daily_stats = DailyStats()
        self._halt_today: bool = False  # 당일 거래 중단 플래그
        self._trade_history: list[TradeRecord] = []  # 세션 전체 거래 내역
        self._last_stop_loss_time: Optional[datetime] = (
            None  # 마지막 스톱로스 발생 시간
        )
        # 속도 기반 긴급 손절용 틱 이력 (최대 200틱)
        self._price_history: deque[tuple[datetime, int]] = deque(maxlen=200)
        # 연속 음봉 감지용 최신 캔들 (메인 사이클에서 업데이트)
        self._latest_candles: list[dict] = []
        # 속도 임계값 계산에 사용할 최신 ATR (메인 사이클에서 업데이트)
        self._latest_atr: float = 0.0

    # ── 틱 / 캔들 데이터 업데이트 ───────────────────────────────────────────

    def record_price_tick(self, price: int, ts: Optional[datetime] = None) -> None:
        """포지션 모니터 루프에서 매 틱마다 현재가를 기록한다 (속도 계산용)."""
        self._price_history.append((ts or datetime.now(), price))

    def update_candles(self, candles: list[dict]) -> None:
        """메인 사이클에서 최신 캔들 리스트를 업데이트한다 (연속 음봉 감지용)."""
        self._latest_candles = candles

    def update_atr(self, atr: float) -> None:
        """메인 사이클에서 최신 ATR을 업데이트한다 (속도 임계값 계산용)."""
        if atr > 0:
            self._latest_atr = atr

    def get_monitor_interval(self, current_price: Optional[int] = None) -> int:
        """현재가와 청산 기준선 간 거리에 따라 다음 모니터링 주기를 결정한다."""
        default_interval = max(int(config.POSITION_CHECK_SEC), 1)
        fast_interval = max(
            1, min(int(config.POSITION_CHECK_FAST_SEC), default_interval)
        )

        pos = self._position
        if pos is None or current_price is None or current_price <= 0:
            return default_interval

        if self._latest_atr <= 0 or config.POSITION_CHECK_FAST_ATR_BUFFER <= 0:
            return default_interval

        reference_prices = [pos.stop_loss]
        if pos.trailing_active and pos.trailing_stop:
            reference_prices.append(pos.trailing_stop)
        elif pos.take_profit > 0:
            reference_prices.append(pos.take_profit)

        valid_prices = [price for price in reference_prices if price > 0]
        if not valid_prices:
            return default_interval

        nearest_gap = min(abs(current_price - price) for price in valid_prices)
        fast_buffer = self._latest_atr * config.POSITION_CHECK_FAST_ATR_BUFFER

        if nearest_gap <= fast_buffer:
            return fast_interval
        return default_interval

    # ── 일일 초기화 ────────────────────────────────────────────────────────

    def sync_daily_stats(self, total_assets: float) -> None:
        """매 사이클 호출 — 새 날이면 일별 통계를 초기화하고, 초기 자산을 기록한다."""
        was_new_day = self._daily_stats.date != date.today()
        self._daily_stats.reset_if_new_day()
        if was_new_day:
            self._halt_today = False
            self._last_stop_loss_time = None  # 새 날에는 쿨다운 리셋
            logger.info(
                "[RiskManager] 일별 초기화 완료. 초기 자산: %s원",
                f"{total_assets:,.0f}",
            )
        if self._daily_stats.initial_total_assets == 0.0:
            self._daily_stats.initial_total_assets = total_assets

    # ── 포지션 진입 검증 ───────────────────────────────────────────────────

    def can_enter(self, current_price: int, total_assets: float) -> tuple[bool, str]:
        """
        신규/추가 진입 가능 여부를 반환한다.

        Returns:
            (can_enter: bool, reason: str)
        """
        self._daily_stats.reset_if_new_day()

        if self._halt_today:
            return False, "당일 거래 중단 상태 (손실 한도 초과)"

        if self._position is not None:
            current_invested = self._position.qty * self._position.entry_price
            max_invest = total_assets * config.MAX_POSITION_RATIO
            if current_invested >= max_invest * 0.99:
                return (
                    False,
                    f"최대 보유 한도 도달 ({current_invested:,.0f}원 >= {max_invest:,.0f}원)",
                )
            if max_invest - current_invested < current_price:
                return (
                    False,
                    f"남은 한도가 1주 매수 금액보다 작음 (잔여: {max_invest - current_invested:,.0f}원)",
                )
        if self._daily_stats.trade_count >= config.MAX_TRADES_PER_DAY:
            return False, f"일일 최대 거래 횟수 초과 ({config.MAX_TRADES_PER_DAY}회)"

        # 스톱로스 후 쿨다운 체크
        if self._last_stop_loss_time is not None:
            elapsed_minutes = (
                datetime.now() - self._last_stop_loss_time
            ).total_seconds() / 60
            if elapsed_minutes < config.STOP_LOSS_COOLDOWN_MINUTES:
                remaining = config.STOP_LOSS_COOLDOWN_MINUTES - elapsed_minutes
                return False, f"스톱로스 후 쿨다운 ({remaining:.1f}분 남음)"

        return True, "진입 가능"

    # ── 주문 수량 및 손절/익절가 계산 ──────────────────────────────────────

    def calc_order_params(
        self,
        current_price: int,
        total_assets: float,
        atr: float = 0.0,
    ) -> dict[str, float]:
        """
        주문 수량, 손절가, 익절가를 계산한다.

        Args:
            current_price: 현재가
            total_assets: 총 평가 자산 (원)
            atr: 지표에서 계산된 ATR 값 (기본값 0.0)

        Returns:
            {
                "qty": int,
                "stop_loss": float,
                "take_profit": float,
                "invest_amount": float,
            }
        """
        base_invest_amount = total_assets * config.SINGLE_TRADE_RATIO

        current_invested = 0.0
        if self._position is not None:
            current_invested = self._position.qty * self._position.entry_price

        max_invest = total_assets * config.MAX_POSITION_RATIO
        remaining_allowance = max_invest - current_invested

        invest_amount = min(base_invest_amount, remaining_allowance)

        qty = math.floor(invest_amount / current_price)
        if qty < 1:
            qty = 0

        # 비율 기반 최대 허용 구간 (가드레일 상한)
        max_sl_limit = current_price * (1 - config.STOP_LOSS_RATIO)
        max_tp_limit = current_price * (1 + config.TAKE_PROFIT_RATIO)

        stop_loss = max_sl_limit  # cp * (1 - 0.05) = cp * 0.95
        take_profit = max_tp_limit  # cp * (1 + 0.08) = cp * 1.08

        # ATR 변동성 기반 동적 손/익절가 적용
        if config.USE_ATR_STOP and atr > 0:
            atr_sl = current_price - (atr * config.ATR_SL_MULTIPLIER)
            atr_tp = current_price + (atr * config.ATR_TP_MULTIPLIER)

            # 가드레일 하한: 지나치게 좁은 손절 폭 방지 (노이즈 손절 방지: 최소 MIN_SL_RATIO)
            min_sl_limit = current_price * (1 - config.MIN_SL_RATIO)
            # 가드레일 하한: 지나치게 좁은 익절 폭 방지 (노이즈 청산 방지: 최소 MIN_TP_RATIO)
            min_tp_limit = current_price * (1 + config.MIN_TP_RATIO)

            # stop_loss를 [max_sl_limit, min_sl_limit] 범위로 제한
            # 최대 손실(max_sl_limit)보다는 커야(손실폭이 작아야) 하고, 최소 손절(min_sl_limit)보다는 작아야(손실폭이 커야) 함
            stop_loss = max(atr_sl, max_sl_limit)
            stop_loss = min(stop_loss, min_sl_limit)

            # take_profit을 [min_tp_limit, max_tp_limit] 이내로 제한
            # 최소 익절폭(min_tp_limit)보다는 커야하고 최대 익절폭(max_tp_limit)보다는 작아야 함
            take_profit = max(atr_tp, min_tp_limit)
            take_profit = min(take_profit, max_tp_limit)

        logger.info(
            "[RiskManager] 주문: %d주, 손절가=%.0f(-%.1f%%), 익절가=%.0f(+%.1f%%), 투자금=%.0f원",
            qty,
            stop_loss,
            (current_price - stop_loss) / current_price * 100,
            take_profit,
            (take_profit - current_price) / current_price * 100,
            invest_amount,
        )
        return {
            "qty": qty,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "invest_amount": invest_amount,
        }

    # ── 포지션 등록 ────────────────────────────────────────────────────────

    def add_position(
        self,
        ticker: str,
        direction: str,
        entry_price: int,
        qty: int,
        stop_loss: float,
        take_profit: float,
    ) -> None:
        """포지션을 등록하거나 기존 포지션에 추가한다(물타기/불타기). 평단가를 갱신하고 SL/TP를 재조정한다."""
        if self._position is None:
            self._position = Position(
                ticker=ticker,
                direction=direction,
                entry_price=entry_price,
                qty=qty,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            self._daily_stats.trade_count += 1
            logger.info(
                "[RiskManager] 신규 포지션 오픈: %s %s %d주 @ %d원 (손절=%.0f, 익절=%.0f)",
                ticker,
                direction,
                qty,
                entry_price,
                stop_loss,
                take_profit,
            )
        else:
            old_qty = self._position.qty
            old_entry = self._position.entry_price

            new_qty = old_qty + qty
            # 평단가 가중평균
            new_entry_price = int(
                round((old_entry * old_qty + entry_price * qty) / new_qty)
            )

            # 새로 진입할 때의 '현재가(entry_price)'와 전달받은 'stop_loss'의 괴리율(%)을 구해서 새 평단가에 동일하게 적용.
            sl_ratio = (entry_price - stop_loss) / entry_price
            tp_ratio = (take_profit - entry_price) / entry_price

            new_stop_loss = new_entry_price * (1 - sl_ratio)
            new_take_profit = new_entry_price * (1 + tp_ratio)

            self._position.qty = new_qty
            self._position.entry_price = new_entry_price
            self._position.stop_loss = new_stop_loss
            self._position.take_profit = new_take_profit
            # 트레일링 스탑 초기화 (평단가가 높아졌으므로 다시 익절가 도달할 때까지 대기)
            self._position.trailing_active = False
            self._position.trailing_stop = None

            self._daily_stats.trade_count += 1
            logger.info(
                "[RiskManager] 포지션 추가(분할진입): %s %s 추가 %d주 @ %d원 -> 총 %d주 평단 %d원 (새손절=%.0f, 새익절=%.0f)",
                ticker,
                direction,
                qty,
                entry_price,
                new_qty,
                new_entry_price,
                new_stop_loss,
                new_take_profit,
            )

    def restore_position(
        self,
        ticker: str,
        direction: str,
        entry_price: int,
        qty: int,
        stop_loss: float,
        take_profit: float,
        entry_time: Optional[datetime] = None,
    ) -> None:
        """기존 잔고에서 보유 포지션을 복구한다 (신규 진입으로 간주하지 않음)."""
        if self._position is not None:
            logger.warning("[RiskManager] 이미 포지션이 존재하여 복구를 생략합니다.")
            return

        real_entry_time = entry_time or datetime.now()

        self._position = Position(
            ticker=ticker,
            direction=direction,
            entry_price=entry_price,
            qty=qty,
            stop_loss=stop_loss,
            take_profit=take_profit,
            entry_time=real_entry_time,
        )
        logger.info(
            "[RiskManager] 기존 보유 포지션 복구 완료: %s %s %d주 @ %d원 (손절=%.0f, 익절=%.0f, 진입시간=%s)",
            ticker,
            direction,
            qty,
            entry_price,
            stop_loss,
            take_profit,
            real_entry_time.strftime("%Y-%m-%d %H:%M:%S"),
        )

    # ── 포지션 모니터링 ────────────────────────────────────────────────────

    def _check_velocity_stop(self, current_price: int) -> tuple[bool, str]:
        """최근 VELOCITY_STOP_WINDOW_SEC 초 내 가격 하락 속도를 확인한다.

        포지션 모니터 루프에서 record_price_tick()으로 쌓인 틱 이력을 사용한다.
        최근 윈도우의 최고가 대비 현재가 낙폭을 사용해 계단식 하락을 더 잘 잡는다.
        또한 하락 틱 비율을 함께 확인해 단순 노이즈나 V자 반등 오발동을 줄인다.

        Returns:
            (triggered: bool, reason: str)
        """
        if not config.VELOCITY_STOP_ENABLED or len(self._price_history) < 2:
            return False, ""

        # ATR이 아직 없으면 비활성
        if self._latest_atr <= 0:
            return False, ""

        # 정규장 마감 전 시간외 전환 구간 오발동 방지
        # 15:20 이후에는 API가 마지막 정규가를 유지하다가 한 번에 갱신하므로
        # 30초 윈도우 내 급락처럼 보이는 가격 점프가 발생할 수 있다.
        cutoff_h, cutoff_m = map(int, config.VELOCITY_STOP_CUTOFF_TIME.split(":"))
        if datetime.now().time() >= time(cutoff_h, cutoff_m):
            return False, ""

        window_sec = config.VELOCITY_STOP_WINDOW_SEC
        cutoff = datetime.now() - timedelta(seconds=window_sec)

        window_ticks = [
            (ts, price) for ts, price in self._price_history if ts >= cutoff
        ]
        min_ticks = max(config.VELOCITY_STOP_MIN_TICKS, 2)
        if len(window_ticks) < min_ticks:
            return False, ""

        peak_price = max(price for _, price in window_ticks)
        if peak_price <= 0:
            return False, ""

        total_moves = len(window_ticks) - 1
        down_moves = sum(
            1
            for idx in range(1, len(window_ticks))
            if window_ticks[idx][1] < window_ticks[idx - 1][1]
        )
        down_ratio = down_moves / total_moves if total_moves > 0 else 0.0

        drop = peak_price - current_price  # 양수 = 하락
        threshold = self._latest_atr * config.VELOCITY_STOP_ATR_MULT

        if drop >= threshold and down_ratio >= config.VELOCITY_STOP_DOWN_TICK_RATIO:
            change_pct = drop / peak_price * 100
            return True, (
                f"고점대비급락: -{change_pct:.2f}% ({drop:.0f}원) in {window_sec}s "
                f"> ATR×{config.VELOCITY_STOP_ATR_MULT}({threshold:.0f}원) "
                f"[고점={peak_price:,} → 현재={current_price:,}, 하락틱비율={down_ratio:.0%}]"
            )
        return False, ""

    def _check_consec_bearish_stop(self, pos: "Position") -> tuple[bool, str]:
        """최근 N봉이 연속 큰 음봉이면 강한 하락 추세로 판단해 선제 청산 신호를 반환한다.

        update_candles()로 업데이트된 캔들 이력을 사용한다.
        미완성 봉(candles[0])은 제외하고 닫힌 봉부터 확인한다.

        Returns:
            (triggered: bool, reason: str)
        """
        if not config.CONSEC_BEARISH_STOP_ENABLED:
            return False, ""

        needed = config.CONSEC_BEARISH_COUNT + 1  # 미완성 봉 1개 + 닫힌 봉 N개
        if len(self._latest_candles) < needed:
            return False, ""

        # 진입 직후에는 진입 이전 캔들이 음봉이어도 오판하지 않도록
        # 최소 CANDLE_INTERVAL분이 지난 뒤에만 적용
        hold_minutes = (datetime.now() - pos.entry_time).total_seconds() / 60
        if hold_minutes < config.CANDLE_INTERVAL:
            return False, ""

        # candles[0]: 미완성 봉(현재 봉), candles[1~N]: 닫힌 봉
        closed_candles = self._latest_candles[1 : config.CONSEC_BEARISH_COUNT + 1]

        count = 0
        for candle in closed_candles:
            o = float(candle.get("open", 0))
            c = float(candle.get("close", 0))
            h = float(candle.get("high", 0))
            ll = float(candle.get("low", 0))

            total_range = h - ll
            if total_range <= 0:
                break

            body_ratio = abs(c - o) / total_range

            # 음봉 + 몸통 비율 기준 충족
            if c < o and body_ratio >= config.CONSEC_BEARISH_BODY_RATIO:
                count += 1
            else:
                break

        if count >= config.CONSEC_BEARISH_COUNT:
            return True, (
                f"연속음봉감지: 최근 {count}봉 연속 음봉 "
                f"(몸통비율≥{config.CONSEC_BEARISH_BODY_RATIO:.0%})"
            )
        return False, ""

    def check_position(
        self,
        current_price: int,
        current_time: Optional[datetime] = None,
    ) -> dict[str, object]:
        """
        현재가를 기준으로 청산 여부를 판단한다.
        매 10초마다 호출한다.

        Returns:
            {
                "action": str, # HOLD | STOP_LOSS | TAKE_PROFIT | TRAILING_STOP | FORCE_CLOSE_MARKET | FORCE_CLOSE_DAILY_LOSS
                "reason": str,
                "position": Position | None,
            }
        """
        if self._position is None:
            return {"action": "HOLD", "reason": "포지션 없음", "position": None}

        pos = self._position
        now = current_time or datetime.now()

        # ── 1. 장 마감 강제 청산 ─────────────────────────────────────────
        # HOLD_OVERNIGHT가 False일 때만 장 마감 전 강제 청산을 수행한다.
        # HOLD_OVERNIGHT 체크 시 포지션 방향을 확인하지 않는 이유:
        # 1. 포지션 방향과 무관한 리스크 회피:
        #  - 포지션이 BUY든, SELL이든 상관없이 장 마감시 모든 포지션을 동일하게 청산
        # 2. 양방향 포지션 설계 지원
        #  - 현재 BUY 포지션만 거래하고 있지만, risk.py 내의 다른 메서드들은 SELL일 때의 숏 수익 계산식도 함께 구현됨.
        #  - 따라서 나중에 숏 포지션 기능이 활성화되도 장 마감 청산 로직이 정상적으로 작동할 수 있도록 방향 체크 없이 구현됨.
        if (
            not config.HOLD_OVERNIGHT
            and _MARKET_CLOSE_TIME is not None
            and now.time() >= _MARKET_CLOSE_TIME
        ):
            logger.warning("[RiskManager] 장 마감 강제 청산: %s", pos.ticker)
            return {
                "action": "FORCE_CLOSE_MARKET",
                "reason": f"장 마감 {config.MARKET_CLOSE_TIME}",
                "position": pos,
            }

        # ── 2. 속도 기반 긴급 손절 (Flash Crash 대응) ─────────────────────
        # BUY 포지션에서 단위 시간당 낙폭이 VELOCITY_STOP_PCT 이하이면 손절가 도달 전에 선제 청산한다.
        if pos.direction == "BUY":
            vel_triggered, vel_reason = self._check_velocity_stop(current_price)
            if vel_triggered:
                logger.warning("[RiskManager] 속도급락 선제 청산: %s", vel_reason)
                return {
                    "action": "VELOCITY_STOP",
                    "reason": vel_reason,
                    "position": pos,
                }

        # ── 3. 연속 음봉 선제 청산 (Band Walk 대응) ──────────────────────
        # BUY 포지션에서 최근 N봉이 연속 큰 음봉이면 강한 하락 추세로 판단해 선제 청산한다.
        if pos.direction == "BUY":
            bearish_triggered, bearish_reason = self._check_consec_bearish_stop(pos)
            if bearish_triggered:
                logger.warning("[RiskManager] 연속음봉 선제 청산: %s", bearish_reason)
                return {
                    "action": "CONSEC_BEARISH_STOP",
                    "reason": bearish_reason,
                    "position": pos,
                }

        # ── 4. 손절 ──────────────────────────────────────────────────────
        if pos.direction == "BUY" and current_price <= pos.stop_loss:
            logger.warning(
                "[RiskManager] 손절 발동: 현재가=%d <= 손절가=%.0f",
                current_price,
                pos.stop_loss,
            )
            return {
                "action": "STOP_LOSS",
                "reason": f"현재가({current_price}) <= 손절가({pos.stop_loss:.0f})",
                "position": pos,
            }

        # ── 5. 익절가 도달 → 트레일링 스탑 전환 ──────────────────────────
        if pos.direction == "BUY" and current_price >= pos.take_profit:
            new_trailing = current_price * (1 - config.TRAILING_STOP_RATIO)
            if not pos.trailing_active:
                pos.trailing_active = True
                pos.trailing_stop = new_trailing
                logger.info(
                    "[RiskManager] 트레일링 스탑 활성화: 현재가=%d, 트레일링=%.0f",
                    current_price,
                    new_trailing,
                )
            else:
                old_trailing = pos.trailing_stop or 0.0
                pos.trailing_stop = max(new_trailing, old_trailing)
                logger.debug(
                    "[RiskManager] 트레일링 스탑 갱신: %.0f → %.0f",
                    old_trailing,
                    pos.trailing_stop,
                )

        # ── 5.1 트레일링 스탑 발동 ─────────────────────────────────────────
        if (
            pos.direction == "BUY"
            and pos.trailing_active
            and pos.trailing_stop
            and current_price <= pos.trailing_stop
        ):
            logger.info(
                "[RiskManager] 트레일링 스탑 발동: 현재가=%d <= 트레일링=%.0f",
                current_price,
                pos.trailing_stop,
            )
            return {
                "action": "TRAILING_STOP",
                "reason": f"현재가({current_price}) <= 트레일링({pos.trailing_stop:.0f})",
                "position": pos,
            }

        return {"action": "HOLD", "reason": "정상 보유 중", "position": pos}

    # ── 포지션 청산 처리 ───────────────────────────────────────────────────

    def close_position(self, exit_price: int, close_reason: str = "UNKNOWN") -> float:
        """
        포지션을 청산하고 실현 손익(수수료 차감 전)을 반환한다.

        Args:
            exit_price: 청산 가격
            close_reason: 청산 사유 ('STOP_LOSS', 'TRAILING_STOP', 'FORCE_CLOSE_MARKET', ...)

        Returns:
            수수료 차감 전 gross 손익 (원). 손실이면 음수.
        """
        if self._position is None:
            logger.warning("[RiskManager] 청산할 포지션이 없습니다.")
            return 0.0

        pos = self._position
        now = datetime.now()

        # ── 수익 계산 (gross) ────────────────────────────────────────────────
        if pos.direction == "BUY":
            gross_pnl = (exit_price - pos.entry_price) * pos.qty
        else:
            gross_pnl = (pos.entry_price - exit_price) * pos.qty

        # ── 수수료 계산 ─────────────────────────────────────────────────────
        # 매수 수수료: 진입금액 × 브로커 수수료율
        buy_commission = pos.entry_price * pos.qty * _cfg.BROKER_FEE_RATE
        # 매도 수수료: 청산금액 × 브로커 수수료율
        sell_commission = exit_price * pos.qty * _cfg.BROKER_FEE_RATE
        # 거래세: 청산금액 × 거래세율 (매도 시에만 부과)
        transaction_tax = exit_price * pos.qty * _cfg.TRANSACTION_TAX_RATE
        total_commission = buy_commission + sell_commission + transaction_tax

        net_pnl = gross_pnl - total_commission

        # ── 일별 통계 반영 (gross 기준 — 기존 로직 유지) ────────────────────
        self._daily_stats.realized_pnl += gross_pnl

        # 스톱로스 발생 시 쿨다운 시간 기록
        if close_reason == "STOP_LOSS":
            self._last_stop_loss_time = datetime.now()
            logger.info(
                "[RiskManager] 스톱로스 발생, 쿨다운 시작: %d분간 재진입 차단",
                config.STOP_LOSS_COOLDOWN_MINUTES,
            )

        # 당일 손실 한도 확인
        if self._daily_stats.daily_loss_ratio >= config.MAX_DAILY_LOSS_RATIO:
            logger.warning(
                "[RiskManager] 당일 손실 한도 초과 (%.2f%% >= %.0f%%). 당일 거래 중단.",
                self._daily_stats.daily_loss_ratio * 100,
                config.MAX_DAILY_LOSS_RATIO * 100,
            )
            self._halt_today = True

        pnl_ratio = gross_pnl / (pos.entry_price * pos.qty) if pos.qty > 0 else 0.0
        logger.info(
            "[RiskManager] 포지션 청산: %s %d주 @ %d원 → %d원 "
            "(Gross=%+.0f원 / 수수료=%.0f원 / Net=%+.0f원, %.2f%%)",
            pos.ticker,
            pos.qty,
            pos.entry_price,
            exit_price,
            gross_pnl,
            total_commission,
            net_pnl,
            pnl_ratio * 100,
        )

        # ── 거래 내역 기록 ───────────────────────────────────────────────────
        record = TradeRecord(
            ticker=pos.ticker,
            direction=pos.direction,
            entry_time=pos.entry_time,
            exit_time=now,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            qty=pos.qty,
            gross_pnl=gross_pnl,
            commission=total_commission,
            net_pnl=net_pnl,
            close_reason=close_reason,
        )
        self._trade_history.append(record)

        self._position = None
        return gross_pnl

    # ── 상태 조회 ─────────────────────────────────────────────────────────

    @property
    def position(self) -> Optional[Position]:
        return self._position

    @property
    def has_position(self) -> bool:
        return self._position is not None

    @property
    def daily_stats(self) -> DailyStats:
        return self._daily_stats

    @property
    def halt_today(self) -> bool:
        return self._halt_today

    @property
    def trade_history(self) -> list[TradeRecord]:
        """세션 전체 거래 내역 (읽기 전용 뷰)."""
        return list(self._trade_history)

    def set_halt_today(self, value: bool) -> None:
        """외부(gate)에서 당일 거래 중단을 설정한다."""
        if value and not self._halt_today:
            logger.warning("[RiskManager] 외부 요청으로 당일 거래 중단 설정.")
        self._halt_today = value
