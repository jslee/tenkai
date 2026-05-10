"""
kis_api/market_websocket.py — KIS WebSocket 실시간 틱 데이터 수신 및 지표 생성

WebSocket TR:
  - H0STCNT0: 국내주식 실시간 체결가
  - H0STASP0: 국내주식 실시간 호가

생성 지표:
  1. 오더플로우 델타 (Order Flow Delta)
  2. 체결강도/속도 (Trade Intensity & Velocity)
  3. 실시간 VWAP (Volume Weighted Average Price)
  4. 호가잔량변화 (Order Book Imbalance Change)
"""

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import aiohttp

import config

logger = logging.getLogger(__name__)

# ── WebSocket 엔드포인트 ────────────────────────────────────────
WS_URL_REAL = "ws://ops.koreainvestment.com:21000"
WS_URL_PAPER = "ws://ops.koreainvestment.com:31000"


# ── 실시간 체결 H0STCNT0 필드 인덱스 ────────────────────────────
# KIS 공식 규격서 기준 ('^' 구분자로 split 후 인덱스)
class TF:  # Trade Fields
    TICKER = 0  # 종목코드
    TIME = 1  # 체결시간 (HHMMSS)
    PRICE = 2  # 체결가
    SIGN = 3  # 전일대비부호
    CHANGE = 4  # 전일대비
    CHANGE_RATE = 5  # 전일대비율
    WAVG_PRICE = 6  # 가중평균가
    OPEN = 7  # 시가
    HIGH = 8  # 고가
    LOW = 9  # 저가
    ASK1 = 10  # 매도호가1
    BID1 = 11  # 매수호가1
    VOLUME = 12  # 체결량
    ACML_VOL = 13  # 누적거래량
    ACML_AMT = 14  # 누적거래대금
    SELL_COUNT = 15  # 매도체결건수
    BUY_COUNT = 16  # 매수체결건수
    NET_COUNT = 17  # 순매수체결건수
    STRENGTH = 18  # 체결강도
    SIDE = 19  # 체결구분 (1:매도, 5:매수 — KIS 실시간 기준)


# ── 실시간 호가 H0STASP0 필드 인덱스 ────────────────────────────
class OF:  # Orderbook Fields
    TICKER = 0
    TIME = 1
    HOUR_CLS = 2
    # 매도호가 1~10 (인덱스 3~12)
    ASKP_START = 3
    # 매수호가 1~10 (인덱스 13~22)
    BIDP_START = 13
    # 매도잔량 1~10 (인덱스 23~32)
    ASK_VOL_START = 23
    # 매수잔량 1~10 (인덱스 33~42)
    BID_VOL_START = 33
    # 총매도잔량(43), 총매수잔량(44)
    TOTAL_ASK = 43
    TOTAL_BID = 44


# ── 지표 데이터 구조 ────────────────────────────────────────────


@dataclass
class TickTrade:
    """개별 체결 틱"""

    timestamp: float  # epoch seconds
    time_str: str  # HHMMSS
    price: int  # 체결가
    volume: int  # 체결량
    side: str  # 'BUY' 또는 'SELL'
    acml_vol: int  # 누적거래량
    acml_amt: int  # 누적거래대금 (백만원 단위일 수 있음)
    strength: float  # 체결강도


@dataclass
class TickOrderbook:
    """호가 스냅샷"""

    timestamp: float
    ask_prices: list[int]  # 매도호가 1~10
    bid_prices: list[int]  # 매수호가 1~10
    ask_volumes: list[int]  # 매도잔량 1~10
    bid_volumes: list[int]  # 매수잔량 1~10
    total_ask: int
    total_bid: int


@dataclass
class RealtimeIndicators:
    """실시간 지표 스냅샷 — 외부 모듈이 조회하는 구조체"""

    # 1) 오더플로우 델타
    cumulative_delta: int = 0  # 당일 누적 델타 (매수체결량 - 매도체결량)
    rolling_delta_1m: int = 0  # 최근 1분 델타
    rolling_delta_5m: int = 0  # 최근 5분 델타
    delta_trend: str = "NEUTRAL"  # BULLISH / BEARISH / NEUTRAL

    # 2) 체결강도/속도
    tick_count_1m: int = 0  # 최근 1분 체결 건수
    tick_count_5m: int = 0  # 최근 5분 체결 건수
    buy_ratio_1m: float = 0.5  # 최근 1분 매수비율
    avg_tick_interval_ms: float = 0.0  # 평균 체결간격 (ms)
    trade_velocity: float = 0.0  # 체결속도 (건/초)
    kis_strength: float = 0.0  # KIS 제공 체결강도

    # 3) 실시간 VWAP
    vwap: float = 0.0  # 당일 VWAP
    vwap_upper: float = 0.0  # VWAP + 1σ
    vwap_lower: float = 0.0  # VWAP - 1σ
    price_vs_vwap: float = 0.0  # (현재가 - VWAP) / VWAP * 100

    # 4) 호가잔량변화
    bid_ask_ratio: float = 0.5  # 매수잔량/(매수+매도)
    bid_ask_ratio_prev: float = 0.5  # 직전 스냅샷의 비율
    imbalance_change: float = 0.0  # 비율 변화량
    spread_bps: float = 0.0  # 스프레드 (bps)

    last_price: int = 0
    last_update: float = 0.0


class KISMarketWebSocket:
    """
    KIS WebSocket을 통해 실시간 체결/호가 데이터를 수신하고
    4가지 틱 기반 지표를 계산하는 독립 모듈.

    사용법 (나중에 연동 시):
        ws = KISMarketWebSocket(ticker="005930")
        await ws.start()          # 백그라운드 태스크로 실행
        indicators = ws.get_indicators()  # 언제든 스냅샷 조회
        await ws.stop()
    """

    def __init__(
        self,
        ticker: str = "",
        max_history: int = 6000,  # 틱 보관 개수 (~1시간 분량)
        on_trade: Optional[Callable] = None,
        on_orderbook: Optional[Callable] = None,
    ) -> None:
        self._ticker = ticker or config.TICKER
        self._max_history = max_history
        self._on_trade = on_trade  # 외부 콜백 (옵션)
        self._on_orderbook = on_orderbook

        # 데이터 저장소
        self._trades: deque[TickTrade] = deque(maxlen=max_history)
        self._orderbooks: deque[TickOrderbook] = deque(maxlen=200)
        self._indicators = RealtimeIndicators()

        # VWAP 누적 변수 (당일 리셋)
        self._vwap_cum_pv: float = 0.0  # Σ(price × volume)
        self._vwap_cum_vol: int = 0  # Σ(volume)
        self._vwap_sq_cum: float = 0.0  # Σ(price² × volume) — σ 계산용

        # 오더플로우 누적 델타
        self._cum_buy_vol: int = 0
        self._cum_sell_vol: int = 0

        # WebSocket 상태
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._approval_key: Optional[str] = None

    # ── 공개 API ────────────────────────────────────────────────

    def get_indicators(self) -> RealtimeIndicators:
        """현재 지표 스냅샷을 반환한다 (스레드 세이프한 읽기 전용 복사)."""
        return RealtimeIndicators(**self._indicators.__dict__)

    async def start(self) -> None:
        """WebSocket 연결 및 수신 루프를 백그라운드 태스크로 시작한다."""
        if self._running:
            logger.warning("[WS] 이미 실행 중입니다.")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("[WS] 실시간 데이터 수신 시작 — 종목: %s", self._ticker)

    async def stop(self) -> None:
        """WebSocket 연결을 종료한다."""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[WS] 실시간 데이터 수신 종료")

    def reset_daily(self) -> None:
        """장 시작 시 당일 누적 지표를 초기화한다."""
        self._trades.clear()
        self._orderbooks.clear()
        self._vwap_cum_pv = 0.0
        self._vwap_cum_vol = 0
        self._vwap_sq_cum = 0.0
        self._cum_buy_vol = 0
        self._cum_sell_vol = 0
        self._indicators = RealtimeIndicators()
        logger.info("[WS] 당일 누적 지표 초기화 완료")

    # ── WebSocket 접속키 발급 ───────────────────────────────────

    async def _get_approval_key(self) -> str:
        """REST API로 WebSocket 접속용 approval_key를 발급받는다."""
        url = f"{config.BASE_URL_REAL}/oauth2/Approval"
        payload = {
            "grant_type": "client_credentials",
            "appkey": config.KIS_REAL_APP_KEY,
            "secretkey": config.KIS_REAL_APP_SECRET,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        key = data.get("approval_key", "")
        logger.info("[WS] approval_key 발급 완료")
        return key

    # ── 구독 요청 생성 ──────────────────────────────────────────

    def _make_subscribe_msg(
        self, tr_id: str, tr_key: str, subscribe: bool = True
    ) -> str:
        """WebSocket 구독/해제 JSON 메시지를 생성한다."""
        return json.dumps(
            {
                "header": {
                    "approval_key": self._approval_key,
                    "custtype": "P",
                    "tr_type": "1" if subscribe else "2",
                    "content-type": "utf-8",
                },
                "body": {
                    "input": {
                        "tr_id": tr_id,
                        "tr_key": tr_key,
                    }
                },
            }
        )

    # ── 메인 수신 루프 ──────────────────────────────────────────

    async def _run_loop(self) -> None:
        """재연결 로직을 포함한 메인 WebSocket 수신 루프."""
        retry_delay = 1.0
        max_retry_delay = 60.0

        while self._running:
            try:
                self._approval_key = await self._get_approval_key()
                ws_url = WS_URL_PAPER if config.KIS_IS_PAPER else WS_URL_REAL
                # 시세 데이터는 항상 실전 WebSocket 사용 (모의투자 WS에서도 시세는 실전)
                ws_url = WS_URL_REAL

                self._session = aiohttp.ClientSession()
                self._ws = await self._session.ws_connect(
                    f"{ws_url}/tryitout/websocket",
                    timeout=aiohttp.ClientTimeout(total=30),
                )
                logger.info("[WS] WebSocket 연결 성공: %s", ws_url)

                # 체결 & 호가 구독
                await self._ws.send_str(
                    self._make_subscribe_msg("H0STCNT0", self._ticker)
                )
                await self._ws.send_str(
                    self._make_subscribe_msg("H0STASP0", self._ticker)
                )
                logger.info("[WS] 체결(H0STCNT0) + 호가(H0STASP0) 구독 완료")

                retry_delay = 1.0  # 성공 시 재시도 간격 리셋

                # 메시지 수신 루프
                async for msg in self._ws:
                    if not self._running:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._handle_message(msg.data)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error("[WS] 에러: %s", self._ws.exception())
                        break
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                    ):
                        logger.warning("[WS] 연결 종료됨")
                        break

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[WS] 연결/수신 오류: %s — %s초 후 재연결", e, retry_delay)
            finally:
                if self._ws and not self._ws.closed:
                    await self._ws.close()
                if self._session and not self._session.closed:
                    await self._session.close()

            if self._running:
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay)

    # ── 메시지 라우팅 ───────────────────────────────────────────

    def _handle_message(self, raw: str) -> None:
        """수신 메시지를 파싱하여 적절한 핸들러로 라우팅한다."""
        # 구독 응답(JSON)인 경우
        if raw.startswith("{"):
            try:
                resp = json.loads(raw)
                header = resp.get("header", {})
                tr_id = header.get("tr_id", "")
                msg_cd = resp.get("body", {}).get("output", {}).get("msg_cd", "")
                logger.info("[WS] 구독 응답 — TR: %s, 코드: %s", tr_id, msg_cd)
            except json.JSONDecodeError:
                logger.warning("[WS] JSON 파싱 실패: %s", raw[:100])
            return

        # 실시간 데이터: "암호화여부|TR_ID|건수|데이터" 형식
        parts = raw.split("|")
        if len(parts) < 4:
            return

        encrypted = parts[0]
        tr_id = parts[1]
        count = int(parts[2])
        data_str = parts[3]

        # 암호화된 데이터는 현재 미지원 (실전투자에서만 발생)
        if encrypted == "1":
            logger.debug("[WS] 암호화 데이터 수신 (TR: %s) — 복호화 미구현", tr_id)
            return

        if tr_id == "H0STCNT0":
            self._on_trade_tick(data_str)
        elif tr_id == "H0STASP0":
            self._on_orderbook_tick(data_str)

    # ── 체결 틱 처리 ────────────────────────────────────────────

    def _on_trade_tick(self, data: str) -> None:
        """실시간 체결 데이터를 파싱하고 지표를 갱신한다."""
        fields = data.split("^")
        if len(fields) < 20:
            logger.debug("[WS] 체결 데이터 필드 부족: %d개", len(fields))
            return

        try:
            price = int(fields[TF.PRICE])
            volume = abs(int(fields[TF.VOLUME]))
            acml_vol = int(fields[TF.ACML_VOL])
            acml_amt = int(fields[TF.ACML_AMT])
            strength = float(fields[TF.STRENGTH]) if fields[TF.STRENGTH] else 0.0

            # 매도/매수 구분: KIS 실시간 기준 1=매도, 5=매수 (일부 버전 2=매수)
            side_code = fields[TF.SIDE].strip()
            side = "BUY" if side_code in ("5", "2") else "SELL"
        except (ValueError, IndexError) as e:
            logger.debug("[WS] 체결 파싱 오류: %s", e)
            return

        now = time.time()
        tick = TickTrade(
            timestamp=now,
            time_str=fields[TF.TIME],
            price=price,
            volume=volume,
            side=side,
            acml_vol=acml_vol,
            acml_amt=acml_amt,
            strength=strength,
        )
        self._trades.append(tick)

        # ── 누적 지표 업데이트 ──
        # 1) 오더플로우 델타
        if side == "BUY":
            self._cum_buy_vol += volume
        else:
            self._cum_sell_vol += volume

        # 2) VWAP 누적
        pv = price * volume
        self._vwap_cum_pv += pv
        self._vwap_cum_vol += volume
        self._vwap_sq_cum += price * pv  # price² × volume

        # 지표 재계산
        self._update_indicators(now, price)

        # 외부 콜백
        if self._on_trade:
            try:
                self._on_trade(tick)
            except Exception as e:
                logger.debug("[WS] 체결 콜백 오류: %s", e)

    # ── 호가 틱 처리 ────────────────────────────────────────────

    def _on_orderbook_tick(self, data: str) -> None:
        """실시간 호가 데이터를 파싱하고 호가잔량 지표를 갱신한다."""
        fields = data.split("^")
        if len(fields) < 45:
            logger.debug("[WS] 호가 데이터 필드 부족: %d개", len(fields))
            return

        try:
            ask_prices = [int(fields[OF.ASKP_START + i]) for i in range(10)]
            bid_prices = [int(fields[OF.BIDP_START + i]) for i in range(10)]
            ask_vols = [int(fields[OF.ASK_VOL_START + i]) for i in range(10)]
            bid_vols = [int(fields[OF.BID_VOL_START + i]) for i in range(10)]
            total_ask = int(fields[OF.TOTAL_ASK])
            total_bid = int(fields[OF.TOTAL_BID])
        except (ValueError, IndexError) as e:
            logger.debug("[WS] 호가 파싱 오류: %s", e)
            return

        now = time.time()
        ob = TickOrderbook(
            timestamp=now,
            ask_prices=ask_prices,
            bid_prices=bid_prices,
            ask_volumes=ask_vols,
            bid_volumes=bid_vols,
            total_ask=total_ask,
            total_bid=total_bid,
        )

        # 직전 호가의 비율 저장
        prev_ratio = self._indicators.bid_ask_ratio
        self._orderbooks.append(ob)

        # ── 호가잔량 지표 갱신 ──
        ind = self._indicators
        total = total_ask + total_bid
        ind.bid_ask_ratio_prev = prev_ratio
        ind.bid_ask_ratio = total_bid / total if total > 0 else 0.5
        ind.imbalance_change = ind.bid_ask_ratio - ind.bid_ask_ratio_prev

        # 스프레드 (basis points)
        if ask_prices[0] > 0 and bid_prices[0] > 0:
            mid = (ask_prices[0] + bid_prices[0]) / 2
            ind.spread_bps = (ask_prices[0] - bid_prices[0]) / mid * 10000

        ind.last_update = now

        # 외부 콜백
        if self._on_orderbook:
            try:
                self._on_orderbook(ob)
            except Exception as e:
                logger.debug("[WS] 호가 콜백 오류: %s", e)

    # ── 지표 재계산 ─────────────────────────────────────────────

    def _update_indicators(self, now: float, current_price: int) -> None:
        """체결 틱이 들어올 때마다 전체 지표를 갱신한다."""
        ind = self._indicators
        ind.last_price = current_price
        ind.last_update = now

        # ── 1) 오더플로우 델타 ──
        ind.cumulative_delta = self._cum_buy_vol - self._cum_sell_vol

        # 롤링 델타 (1분, 5분)
        t1m = now - 60
        t5m = now - 300
        buy_1m = sell_1m = buy_5m = sell_5m = 0
        for t in reversed(self._trades):
            if t.timestamp < t5m:
                break
            vol = t.volume
            if t.timestamp >= t1m:
                if t.side == "BUY":
                    buy_1m += vol
                else:
                    sell_1m += vol
            if t.side == "BUY":
                buy_5m += vol
            else:
                sell_5m += vol

        ind.rolling_delta_1m = buy_1m - sell_1m
        ind.rolling_delta_5m = buy_5m - sell_5m

        # 델타 추세 판단 (1분 델타 기준 방향성)
        if ind.rolling_delta_1m > 0 and ind.rolling_delta_5m > 0:
            ind.delta_trend = "BULLISH"
        elif ind.rolling_delta_1m < 0 and ind.rolling_delta_5m < 0:
            ind.delta_trend = "BEARISH"
        else:
            ind.delta_trend = "NEUTRAL"

        # ── 2) 체결강도/속도 ──
        ticks_1m = [t for t in self._trades if t.timestamp >= t1m]
        ticks_5m = [t for t in self._trades if t.timestamp >= t5m]
        ind.tick_count_1m = len(ticks_1m)
        ind.tick_count_5m = len(ticks_5m)

        if ticks_1m:
            buy_cnt = sum(1 for t in ticks_1m if t.side == "BUY")
            ind.buy_ratio_1m = buy_cnt / len(ticks_1m)

        # 체결 간격 (최근 20건 기준)
        recent = list(self._trades)[-20:]
        if len(recent) >= 2:
            intervals = [
                (recent[i].timestamp - recent[i - 1].timestamp) * 1000
                for i in range(1, len(recent))
            ]
            ind.avg_tick_interval_ms = sum(intervals) / len(intervals)

        # 체결 속도 (건/초) — 최근 1분
        elapsed = now - ticks_1m[0].timestamp if ticks_1m else 0
        ind.trade_velocity = len(ticks_1m) / elapsed if elapsed > 0 else 0.0

        # KIS 체결강도 (최신 틱 값)
        ind.kis_strength = self._trades[-1].strength if self._trades else 0.0

        # ── 3) 실시간 VWAP ──
        if self._vwap_cum_vol > 0:
            vwap = self._vwap_cum_pv / self._vwap_cum_vol
            ind.vwap = vwap
            # 표준편차: σ = sqrt(Σ(p²v)/Σv - vwap²)
            variance = (self._vwap_sq_cum / self._vwap_cum_vol) - (vwap**2)
            std = variance**0.5 if variance > 0 else 0.0
            ind.vwap_upper = vwap + std
            ind.vwap_lower = vwap - std
            ind.price_vs_vwap = (
                ((current_price - vwap) / vwap) * 100 if vwap > 0 else 0.0
            )

    # ── 진단/디버그용 요약 ──────────────────────────────────────

    def summary(self) -> str:
        """현재 지표 상태를 콘솔 출력용 문자열로 반환한다."""
        ind = self._indicators
        lines = [
            f"═══ 실시간 틱 지표 ({self._ticker}) ═══",
            f"현재가: {ind.last_price:,}원",
            "",
            "【오더플로우 델타】",
            f"  누적 델타: {ind.cumulative_delta:+,}",
            f"  1분 델타: {ind.rolling_delta_1m:+,}  |  5분 델타: {ind.rolling_delta_5m:+,}",
            f"  추세: {ind.delta_trend}",
            "",
            "【체결강도/속도】",
            f"  1분 체결: {ind.tick_count_1m}건  |  5분: {ind.tick_count_5m}건",
            f"  매수비율(1분): {ind.buy_ratio_1m:.1%}",
            f"  평균간격: {ind.avg_tick_interval_ms:.0f}ms  |  속도: {ind.trade_velocity:.1f}건/초",
            f"  KIS 체결강도: {ind.kis_strength:.1f}%",
            "",
            "【실시간 VWAP】",
            f"  VWAP: {ind.vwap:,.0f}  ({ind.price_vs_vwap:+.2f}%)",
            f"  상단(+1σ): {ind.vwap_upper:,.0f}  |  하단(-1σ): {ind.vwap_lower:,.0f}",
            "",
            "【호가잔량】",
            f"  매수비율: {ind.bid_ask_ratio:.1%} (변화: {ind.imbalance_change:+.1%})",
            f"  스프레드: {ind.spread_bps:.1f}bps",
        ]
        return "\n".join(lines)
