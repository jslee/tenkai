"""
kis_api/market.py — 시세/호가 조회

사용 API:
- FHKST01010100: 현재가·등락률·거래량
- FHKST03010230: 1분봉 OHLCV 전체 fetch (주식일별분봉조회, 날짜 지정, 최대 120봉/페이지)
- FHKST03010200: 1분봉 최신 페이지 증분 갱신 (당일분봉, 최대 30봉/페이지)
- FHKST01010200: 매수·매도 10호가 잔량
"""

import logging
import asyncio
from pathlib import Path
from typing import Any

import aiohttp

import config
from .auth import KISAuth

logger = logging.getLogger(__name__)


class KISMarket:
    """KIS 시세/호가 조회 클래스"""

    def __init__(self, auth_data: KISAuth, auth_trade: KISAuth) -> None:
        self._auth_data = auth_data
        self._auth_trade = auth_trade
        # 1분봉 캐시: ticker -> {timestamp: candle_dict}
        # 미확정 봉도 timestamp가 같으면 자동으로 덮어써지므로 별도 처리 불필요
        self._candle_cache: dict[str, dict[str, dict]] = {}

    @staticmethod
    def _page_before_time(time_str: str) -> str:
        """분봉 페이지 경계 중복을 피하기 위해 기준 시각보다 1초 앞당긴다."""
        try:
            raw = time_str[-6:]
            hour = int(raw[0:2])
            minute = int(raw[2:4])
            second = int(raw[4:6])
            total_seconds = (hour * 3600) + (minute * 60) + second
            if total_seconds <= 0:
                return "000000"
            total_seconds -= 1
            new_hour = total_seconds // 3600
            remain = total_seconds % 3600
            new_minute = remain // 60
            new_second = remain % 60
            return f"{new_hour:02d}{new_minute:02d}{new_second:02d}"
        except (ValueError, IndexError):
            return time_str

    def get_stock_name(self, ticker: str) -> str:
        """
        kospi_code.mst / kosdaq_code.mst 파일에서 종목명을 조회한다.

        파일 포맷 (cp949):
        - 0-5:   단축코드 (6자리)
        - 6-20:  표준코드 (사용 안 함)
        - 21-:   한글명

        Args:
            ticker: 6자리 종목 코드

        Returns:
            종목명 (찾지 못하면 ticker 반환)
        """
        import config as _cfg

        base_dir = getattr(_cfg, "STOCK_CODE_DIR", ".")
        for filename in ["kospi_code.mst", "kosdaq_code.mst"]:
            path = Path(base_dir) / filename
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="cp949") as f:
                    for line in f:
                        r = line[0 : len(line) - 228]
                        code = r[0:9].rstrip()
                        # fcode = r[9:21].rstrip()
                        name = r[21:].strip()

                        if code == ticker:
                            return name
            except Exception as e:
                logger.warning("[종목명 조회] 파일 읽기 실패 (%s): %s", path, e)
        logger.warning("[종목명 조회] 종목을 찾지 못함: %s", ticker)
        return ticker

    def find_ticker_by_name(self, name: str) -> list[tuple[str, str]]:
        """종목명(부분 일치)으로 종목코드를 역조회한다.

        Args:
            name: 검색할 종목명 키워드 (대소문자/공백 무관 부분 일치)

        Returns:
            [(코드, 종목명), ...] — 일치하는 종목 목록 (빈 리스트 = 미발견)
        """
        import config as _cfg

        keyword = name.strip().lower()
        results: list[tuple[str, str]] = []
        base_dir = getattr(_cfg, "STOCK_CODE_DIR", ".")
        for filename in ["kospi_code.mst", "kosdaq_code.mst"]:
            path = Path(base_dir) / filename
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="cp949") as f:
                    for line in f:
                        r = line[0 : len(line) - 228]
                        code = r[0:9].rstrip()
                        stock_name = r[21:].strip()
                        if keyword in stock_name.lower():
                            results.append((code, stock_name))
            except Exception as e:
                logger.warning("[종목명 역조회] 파일 읽기 실패 (%s): %s", path, e)
        return results

    def is_etf(self, ticker: str) -> bool:
        """
        kospi_code.mst / kosdaq_code.mst 파일에서 종목의 ETF/ETN 여부를 조회한다.
        마스터 파일의 61~62바이트 '증권그룹코드'가 'EF'(ETF) 또는 'EN'(ETN)인지 판별한다.
        """
        import config as _cfg

        base_dir = getattr(_cfg, "STOCK_CODE_DIR", ".")
        for filename in ["kospi_code.mst", "kosdaq_code.mst"]:
            path = Path(base_dir) / filename
            if not path.exists():
                continue
            try:
                with path.open("rb") as f:
                    for line in f:
                        if line.startswith(ticker.encode("cp949")):
                            try:
                                group_code = line[61:63].decode("cp949")
                                return group_code in ("EF", "EN")
                            except Exception:
                                pass
            except Exception as e:
                logger.warning("[ETF 확인] 파일 읽기 실패 (%s): %s", path, e)
        return False

    # ── 현재가 조회 ─────────────────────────────────────────────────────────

    async def get_current_price(self, ticker: str) -> dict[str, Any]:
        """
        현재가, 등락률, 거래량 등 기본 시세를 조회한다.

        Returns:
            {
                "current_price": int,
                "change_rate": float,        # 등락률 (%)
                "volume": int,               # 당일 누적 거래량
                "open_price": int,
                "high_price": int,
                "low_price": int,
            }
        """
        url = f"{self._auth_data.base_url}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": ticker,
        }
        try:
            await self._auth_data.get_token()
            headers = self._auth_data.get_headers("FHKST01010100")
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            output = data["output"]
            return {
                "current_price": int(output["stck_prpr"]),
                "change_rate": float(output["prdy_ctrt"]),
                "volume": int(output["acml_vol"]),
                "open_price": int(output["stck_oprc"]),
                "high_price": int(output["stck_hgpr"]),
                "low_price": int(output["stck_lwpr"]),
            }
        except aiohttp.ClientError as e:
            logger.error("[현재가 조회] API 오류 (ticker=%s): %s", ticker, e)
            raise
        except (KeyError, ValueError) as e:
            logger.error("[현재가 조회] 응답 파싱 오류 (ticker=%s): %s", ticker, e)
            raise

    # ── 체결강도 조회 ─────────────────────────────────────────────────

    async def get_trade_strength(self, ticker: str) -> float:
        """
        당일 체결강도(매수체결량/매도체결량 × 100)를 반환한다.
        FHKST01010300 (주식현재가 체결) API의 tday_rltv 필드 사용.

        Returns:
            체결강도 (%). 100 초과=매수 우세, 100 미만=매도 우세.
            오류 시 0.0 반환.
        """
        url = (
            f"{self._auth_data.base_url}/uapi/domestic-stock/v1/quotations/inquire-ccnl"
        )
        params = {
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": ticker,
        }
        try:
            await self._auth_data.get_token()
            headers = self._auth_data.get_headers("FHKST01010300")
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            output = data.get("output", [])
            if output and isinstance(output, list):
                return float(output[0].get("tday_rltv", 0.0))
            return 0.0

        except (aiohttp.ClientError, KeyError, ValueError, TypeError) as e:
            logger.warning("[체결강도 조회] 오류 (기본값 0.0 반환): %s", e)
            return 0.0

    # ── 일/주/월봉 조회 (기간별시세) ──────────────────────────────────────

    async def _fetch_period_candles(
        self, ticker: str, period_code: str, count: int
    ) -> list[dict[str, Any]]:
        """일/주/월봉 공통 페이지네이션 조회 헬퍼. (FHKST03010100)

        KIS API는 최신→과거 순으로 output2를 반환하며 한 페이지당 최대 약 100건을
        내려준다. 필요한 봉 수(count)를 채울 때까지 FID_INPUT_DATE_2를 이전 페이지의
        가장 오래된 날짜 하루 전으로 당기며 반복 호출한다.

        Args:
            ticker:      종목코드
            period_code: "D" (일봉) | "W" (주봉) | "M" (월봉)
            count:       최대 반환 봉 수
        """
        from datetime import datetime, timedelta

        url = f"{self._auth_data.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        # start_date는 충분히 과거로 고정 (KIS는 이 값을 하한으로만 사용)
        start_date = (datetime.now() - timedelta(days=count * 4)).strftime("%Y%m%d")
        end_date = datetime.now().strftime("%Y%m%d")

        candles: list[dict[str, Any]] = []
        seen_dates: set[str] = set()

        try:
            await self._auth_data.get_token()
            headers = self._auth_data.get_headers("FHKST03010100")
            async with aiohttp.ClientSession() as session:
                while len(candles) < count:
                    params = {
                        "FID_COND_MRKT_DIV_CODE": "J",
                        "FID_INPUT_ISCD": ticker,
                        "FID_INPUT_DATE_1": start_date,
                        "FID_INPUT_DATE_2": end_date,
                        "FID_PERIOD_DIV_CODE": period_code,
                        "FID_ORG_ADJ_PRC": "0",
                    }
                    async with session.get(
                        url,
                        headers=headers,
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        resp.raise_for_status()
                        data = await resp.json()

                    raw = data.get("output2")
                    if not isinstance(raw, list) or not raw:
                        break  # 더 이상 데이터 없음

                    oldest_date: str = ""
                    page_added = 0
                    for c in raw:
                        date_str = c.get("stck_bsop_date", "")
                        if not date_str or date_str in seen_dates:
                            continue
                        vol = int(c.get("acml_vol", 0))
                        if vol <= 0:
                            oldest_date = date_str
                            continue
                        seen_dates.add(date_str)
                        oldest_date = date_str
                        candles.append(
                            {
                                "date": date_str,
                                "open": int(c.get("stck_oprc", 0)),
                                "high": int(c.get("stck_hgpr", 0)),
                                "low": int(c.get("stck_lwpr", 0)),
                                "close": int(c.get("stck_clpr", 0)),
                                "volume": vol,
                            }
                        )
                        page_added += 1
                        if len(candles) >= count:
                            break

                    if not oldest_date or page_added == 0:
                        break  # 무한 루프 방지

                    # 다음 페이지: 이번 페이지 가장 오래된 날짜 하루 전까지 조회
                    next_end = datetime.strptime(oldest_date, "%Y%m%d") - timedelta(
                        days=1
                    )
                    if next_end.strftime("%Y%m%d") < start_date:
                        break
                    end_date = next_end.strftime("%Y%m%d")

                    # KIS OpenAPI 호출 제한 배려
                    await asyncio.sleep(0.3)

        except aiohttp.ClientError as e:
            logger.error(
                "[기간봉 조회] API 오류 (ticker=%s, period=%s): %s",
                ticker,
                period_code,
                e,
            )
            raise
        except (KeyError, ValueError, TypeError) as e:
            logger.error(
                "[기간봉 조회] 응답 파싱 오류 (ticker=%s, period=%s): %s: %s",
                ticker,
                period_code,
                type(e).__name__,
                e,
            )
            raise

        logger.debug(
            "[기간봉 조회] %s %s: %d봉 수집 (요청 %d)",
            ticker,
            period_code,
            len(candles),
            count,
        )
        return candles

    async def get_daily_candles(
        self, ticker: str, days: int = 200
    ) -> list[dict[str, Any]]:
        """
        최근 N 거래일의 일봉 OHLCV를 조회한다.

        기본값 200봉: RSI/MACD/BB 계산 후 EMA(60) 안정화까지 충분한 여유(약 10개월치).

        Returns:
            [{"date": "YYYYMMDD", "open": int, "high": int, "low": int,
              "close": int, "volume": int}, ...]  # 최신 날짜가 index 0
        """
        # 페이지네이션으로 필요한 만큼 가져오므로 lookback 계산 불필요.
        return await self._fetch_period_candles(ticker, "D", days)

    async def get_weekly_candles(
        self, ticker: str, weeks: int = 200
    ) -> list[dict[str, Any]]:
        """
        최근 N 주의 주봉 OHLCV를 조회한다.

        기본값 200봉: 약 4년치.
        date 필드는 해당 주의 마지막 거래일(금요일) 기준이다.

        Returns:
            [{"date": "YYYYMMDD", "open": int, "high": int, "low": int,
              "close": int, "volume": int}, ...]  # 최신 주가 index 0
        """
        # 페이지네이션으로 필요한 만큼 가져오므로 lookback 계산 불필요.
        return await self._fetch_period_candles(ticker, "W", weeks)

    async def get_monthly_candles(
        self, ticker: str, months: int = 200
    ) -> list[dict[str, Any]]:
        """
        최근 N 개월의 월봉 OHLCV를 조회한다.

        기본값 200봉: 시도하지만 200개 모두를 가져오지 못한다. EMA(60) 미완성
        date 필드는 해당 월의 마지막 거래일 기준이다.

        Returns:
            [{"date": "YYYYMMDD", "open": int, "high": int, "low": int,
              "close": int, "volume": int}, ...]  # 최신 월이 index 0
        """
        # 페이지네이션으로 필요한 만큼 가져오므로 lookback 계산 불필요.
        return await self._fetch_period_candles(ticker, "M", months)

    # ── 1분봉 조회 ─────────────────────────────────────────────────────────

    @staticmethod
    def _parse_raw_candle_page(
        raw_candles: list[dict],
        current_date_str: str = "",
        prev_time_val: int = -1,
    ) -> tuple[list[dict[str, Any]], str, int]:
        """KIS 1분봉 raw 응답(output2) 한 페이지를 파싱하고 날짜 복사 버그를 보정한다.

        KIS API는 최신→과거 방향으로 데이터를 내려주는데, 날짜가 바뀌는 경계(예:
        09:00:00 다음에 15:30:00)에서 stck_bsop_date를 오늘 날짜로 복사하는 버그가
        있다.  시간값이 40000(≈4시간) 이상 급증할 때 전일로 전환하고, API가 실제 과거
        날짜를 제공하면 그것을 채택한다.

        Args:
            raw_candles:      API output2 리스트 (최신→과거 순)
            current_date_str: 이전 페이지에서 이어받은 날짜 상태. 첫 페이지면 ""
            prev_time_val:    이전 페이지 마지막 시간 정수값. 첫 페이지면 -1

        Returns:
            (candles, current_date_str, prev_time_val) — 호출자가 다음 페이지에
            current_date_str와 prev_time_val을 그대로 넘겨 상태를 이어갈 수 있다.
        """
        candles: list[dict[str, Any]] = []
        for c in raw_candles:
            time_str = c.get("stck_cntg_hour", "")
            if not time_str:
                continue

            curr_time_val = int(time_str)
            api_date_str = c.get("stck_bsop_date", "")

            if not current_date_str:
                current_date_str = api_date_str
            elif prev_time_val != -1 and curr_time_val > prev_time_val + 40000:
                # 날짜 경계: 시간값이 급증 → 전일로 전환
                if api_date_str and api_date_str < current_date_str:
                    current_date_str = api_date_str

            prev_time_val = curr_time_val

            # open==0: FHKST03010200 전일 소급 시 채워지는 가짜 보정 봉 제거
            if int(c.get("stck_oprc", 0)) == 0:
                continue

            candles.append(
                {
                    "timestamp": f"{current_date_str}{time_str}",
                    "open": int(c.get("stck_oprc", 0)),
                    "high": int(c.get("stck_hgpr", 0)),
                    "low": int(c.get("stck_lwpr", 0)),
                    "close": int(c.get("stck_prpr", 0)),
                    "volume": int(c.get("cntg_vol", 0)),
                }
            )
        return candles, current_date_str, prev_time_val

    @staticmethod
    def _parse_daily_chart_page(
        raw_candles: list[dict],
    ) -> list[dict[str, Any]]:
        """FHKST03010230 output2 한 페이지를 파싱한다.

        FHKST03010230은 날짜별 역사적 분봉 API로 stck_bsop_date가 각 봉에 정확히
        제공되므로 FHKST03010200의 날짜 복사 버그 보정 로직이 불필요하다.

        Args:
            raw_candles: API output2 리스트 (최신→과거 순)

        Returns:
            파싱된 봉 리스트
        """
        candles: list[dict[str, Any]] = []
        for c in raw_candles:
            time_str = c.get("stck_cntg_hour", "")
            date_str = c.get("stck_bsop_date", "")
            if not time_str or not date_str:
                continue
            open_price = int(c.get("stck_oprc", 0))
            if open_price == 0:  # 허봉 방어
                continue
            candles.append(
                {
                    "timestamp": f"{date_str}{time_str}",
                    "open": open_price,
                    "high": int(c.get("stck_hgpr", 0)),
                    "low": int(c.get("stck_lwpr", 0)),
                    "close": int(c.get("stck_prpr", 0)),
                    "volume": int(c.get("cntg_vol", 0)),
                }
            )
        return candles

    async def _get_minute_page_raw(
        self,
        session: aiohttp.ClientSession,
        headers: dict,
        ticker: str,
        hour_str: str,
    ) -> list[dict] | None:
        """1분봉 단일 페이지 HTTP 요청. 500 에러 시 None(중단 신호), 데이터 없으면 []."""
        url = f"{self._auth_data.base_url}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
        params = {
            "fid_etc_cls_code": "",
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": ticker,
            "fid_input_hour_1": hour_str,
            "fid_pw_data_incu_yn": "Y",
        }
        try:
            async with session.get(
                url,
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 500:
                    logger.warning(
                        "[1분봉 조회] KIS 서버 에러(HTTP %d), 조회 중단", resp.status
                    )
                    return None
                resp.raise_for_status()
                data = await resp.json()
        except aiohttp.ClientResponseError:
            raise

        raw = data.get("output2")
        return raw if isinstance(raw, list) else []

    async def _get_daily_chart_page_raw(
        self,
        session: aiohttp.ClientSession,
        headers: dict,
        ticker: str,
        date_str: str,
        hour_str: str,
    ) -> list[dict] | None:
        """FHKST03010230 단일 페이지 HTTP 요청.

        date_str 날짜의 hour_str 시각 기준으로 과거 방향으로 최대 120봉을 반환한다.
        500 에러 시 None(중단 신호), 데이터 없으면 [].
        """
        url = f"{self._auth_data.base_url}/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
        params = {
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": ticker,
            "fid_input_hour_1": hour_str,
            "fid_input_date_1": date_str,
            "fid_pw_data_incu_yn": "Y",
            "fid_fake_tick_incu_yn": "",
        }
        try:
            async with session.get(
                url,
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 500:
                    logger.warning(
                        "[일별분봉 조회] KIS 서버 에러(HTTP %d), 조회 중단", resp.status
                    )
                    return None
                resp.raise_for_status()
                data = await resp.json()
        except aiohttp.ClientResponseError:
            raise

        raw = data.get("output2")
        return raw if isinstance(raw, list) else []

    async def _fetch_all_minute_candles(
        self, ticker: str, count: int
    ) -> list[dict[str, Any]]:
        """1분봉 전체 페이지네이션 fetch — FHKST03010230(주식일별분봉조회) 사용.

        FHKST03010200(당일분봉)과 달리 날짜 지정 조회를 지원하며 페이지당 최대 120봉,
        최대 1년치 실제 분봉을 반환한다. 전일 소급 시 0으로 채우는 문제 없음.
        """
        from datetime import datetime, timedelta

        all_candles: list[dict[str, Any]] = []
        seen_ts: set[str] = set()
        next_date = datetime.now().strftime("%Y%m%d")
        next_hour = "000000"
        empty_streak = 0  # 연속 빈 응답 카운터 (공휴일·주말 스킵용)

        try:
            await self._auth_data.get_token()
            headers = self._auth_data.get_headers("FHKST03010230")
            async with aiohttp.ClientSession() as session:
                while len(all_candles) < count:
                    raw_candles = await self._get_daily_chart_page_raw(
                        session, headers, ticker, next_date, next_hour
                    )
                    if raw_candles is None:  # 500 서버 에러 — 중단
                        break

                    if not raw_candles:
                        # 빈 응답 — 공휴일·주말이거나 해당 날짜 데이터 끝
                        empty_streak += 1
                        if empty_streak >= 10:
                            break
                        prev_day = datetime.strptime(next_date, "%Y%m%d") - timedelta(
                            days=1
                        )
                        next_date = prev_day.strftime("%Y%m%d")
                        next_hour = "160000"  # 전일 장 마감 후 시각으로 재시도
                        continue

                    empty_streak = 0
                    page_candles = self._parse_daily_chart_page(raw_candles)
                    for candle in page_candles:
                        ts = candle["timestamp"]
                        if ts in seen_ts:
                            continue
                        seen_ts.add(ts)
                        all_candles.append(candle)
                        if len(all_candles) >= count:
                            break

                    if len(all_candles) >= count:
                        break

                    # 다음 페이지 커서: 가장 오래된 봉의 날짜+시간 기준으로 이동
                    oldest = raw_candles[-1]
                    oldest_date = oldest.get("stck_bsop_date", next_date)
                    oldest_time = oldest.get("stck_cntg_hour", "")
                    if not oldest_time:
                        break

                    next_page_hour = self._page_before_time(oldest_time)
                    if next_page_hour == oldest_time:
                        # 시간이 더 이상 줄어들지 않음 — 무한 루프 방지
                        break

                    # 장 시작(09:00) 이전으로 가면 전일 마감 시간으로 이동
                    if int(next_page_hour) < 90000:
                        prev_day = datetime.strptime(oldest_date, "%Y%m%d") - timedelta(
                            days=1
                        )
                        next_date = prev_day.strftime("%Y%m%d")
                        next_hour = "160000"
                    else:
                        next_date = oldest_date
                        next_hour = next_page_hour

                    # KIS OpenAPI 초당 호출 제한 배려
                    await asyncio.sleep(0.3)

            logger.debug(
                "[일별분봉 조회] %s: %d봉 수집 (요청 %d)",
                ticker,
                len(all_candles),
                count,
            )
            return all_candles

        except aiohttp.ClientError as e:
            logger.error("[일별분봉 조회] API 오류 (ticker=%s): %s", ticker, e)
            raise
        except (KeyError, ValueError, TypeError) as e:
            logger.error(
                "[일별분봉 조회] 응답 파싱 오류 (ticker=%s): %s: %s",
                ticker,
                type(e).__name__,
                e,
            )
            raise

    async def _fetch_latest_minute_page(self, ticker: str) -> list[dict[str, Any]]:
        """최신 1페이지(KIS API 기준 약 30봉)만 fetch한다. 증분 캐시 갱신에 사용."""
        try:
            await self._auth_data.get_token()
            headers = self._auth_data.get_headers("FHKST03010200")
            async with aiohttp.ClientSession() as session:
                raw_candles = await self._get_minute_page_raw(
                    session, headers, ticker, "000000"
                )
            if not raw_candles:
                return []
            result, _, _ = self._parse_raw_candle_page(raw_candles)
            return result
        except Exception as e:
            logger.warning("[1분봉 증분 조회] 실패, 캐시 유지: %s", e)
            return []

    def _merge_into_cache(self, ticker: str, new_page: list[dict]) -> None:
        """new_page(최신순)를 캐시에 upsert한다.

        같은 timestamp면 덮어쓰므로 미확정(열린) 봉이 자동으로 갱신된다.
        """
        cache = self._candle_cache.setdefault(ticker, {})
        for candle in new_page:
            cache[candle["timestamp"]] = candle
        # 캐시 상한 유지: 오래된 봉부터 제거
        if len(cache) > config.CANDLE_COUNT:
            keep = sorted(cache.keys(), reverse=True)[: config.CANDLE_COUNT]
            self._candle_cache[ticker] = {k: cache[k] for k in keep}

    async def get_minute_candles(
        self, ticker: str, count: int = config.CANDLE_COUNT
    ) -> list[dict[str, Any]]:
        """
        1분봉 OHLCV를 최신순으로 반환한다.

        첫 호출 또는 캐시 부족 시 전체 페이지네이션 fetch를 수행하고,
        이후에는 최신 1페이지만 가져와 캐시를 증분 갱신한다.

        Returns:
            [
                {
                    "timestamp": "YYYYMMDDHHmmss",
                    "open": int,
                    "high": int,
                    "low": int,
                    "close": int,
                    "volume": int,
                },
                ...
            ]  # 최신 봉이 index 0
        """
        from datetime import datetime as _dt

        cache = self._candle_cache.get(ticker, {})

        need_full_fetch = len(cache) < count
        if not need_full_fetch:
            # 최신 봉 timestamp 기준 gap이 20분 초과면 증분으로 메울 수 없으므로 전체 re-fetch
            try:
                newest_ts = max(cache.keys())
                gap_minutes = (
                    _dt.now() - _dt.strptime(newest_ts, "%Y%m%d%H%M%S")
                ).total_seconds() / 60
                if gap_minutes > 20:
                    logger.info(
                        "[1분봉] 캐시 gap %.0f분 — 전체 re-fetch (ticker=%s)",
                        gap_minutes,
                        ticker,
                    )
                    need_full_fetch = True
            except (ValueError, KeyError):
                need_full_fetch = True

        if need_full_fetch:
            logger.debug("[1분봉] 캐시 미스/만료 (ticker=%s) — 전체 fetch 시작", ticker)
            candles = await self._fetch_all_minute_candles(ticker, count)
            self._candle_cache[ticker] = {c["timestamp"]: c for c in candles}
        else:
            new_page = await self._fetch_latest_minute_page(ticker)
            if new_page:
                self._merge_into_cache(ticker, new_page)

        cache = self._candle_cache.get(ticker, {})
        return sorted(cache.values(), key=lambda c: c["timestamp"], reverse=True)[
            :count
        ]

    # ── 호가 조회 ──────────────────────────────────────────────────────────

    async def get_orderbook(self, ticker: str) -> dict[str, Any]:
        """
        매수·매도 10호가 잔량을 조회한다.

        Returns:
            {
                "ask_prices": [int, ...],    # 매도 1~5호가 (낮은 순)
                "ask_volumes": [int, ...],
                "bid_prices": [int, ...],    # 매수 1~5호가 (높은 순)
                "bid_volumes": [int, ...],
                "total_ask_vol": int,
                "total_bid_vol": int,
                "buy_ratio": float,          # 체결강도 (매수/(매수+매도))
            }
        """
        url = f"{self._auth_data.base_url}/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
        params = {
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": ticker,
        }
        try:
            await self._auth_data.get_token()
            headers = self._auth_data.get_headers("FHKST01010200")
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            output1 = data.get("output1", {})

            ask_prices, ask_volumes, bid_prices, bid_volumes = [], [], [], []
            for i in range(1, 6):
                ask_prices.append(int(output1.get(f"askp{i}", 0)))
                ask_volumes.append(int(output1.get(f"askp_rsqn{i}", 0)))
                bid_prices.append(int(output1.get(f"bidp{i}", 0)))
                bid_volumes.append(int(output1.get(f"bidp_rsqn{i}", 0)))

            total_ask = sum(ask_volumes)
            total_bid = sum(bid_volumes)
            buy_ratio = (
                total_bid / (total_ask + total_bid)
                if (total_ask + total_bid) > 0
                else 0.5
            )

            return {
                "ask_prices": ask_prices,
                "ask_volumes": ask_volumes,
                "bid_prices": bid_prices,
                "bid_volumes": bid_volumes,
                "total_ask_vol": total_ask,
                "total_bid_vol": total_bid,
                "buy_ratio": buy_ratio,
            }

        except aiohttp.ClientError as e:
            logger.error("[호가 조회] API 오류 (ticker=%s): %s", ticker, e)
            raise
        except (KeyError, ValueError, TypeError) as e:
            logger.error(
                "[호가 조회] 응답 파싱 오류 (ticker=%s): %s: %s",
                ticker,
                type(e).__name__,
                e,
            )
            raise

    # ── 시장 지수 조회 ───────────────────────────────────────────────────

    async def get_market_index_change(self) -> float:
        """현재 타겟 시장(KOSPI/KOSDAQ)의 지수 등락률(%)을 반환한다."""
        url = f"{self._auth_data.base_url}/uapi/domestic-stock/v1/quotations/inquire-index-price"
        market_code = "1001" if config.MARKET.upper() == "KOSDAQ" else "0001"
        params = {
            "fid_cond_mrkt_div_code": "U",
            "fid_input_iscd": market_code,
        }
        try:
            await self._auth_data.get_token()
            headers = self._auth_data.get_headers("FHPUP02100000")
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        err_text = await resp.text()
                        logger.error(
                            "[시장 지수 조회] HTTP %d: %s", resp.status, err_text
                        )
                    resp.raise_for_status()
                    data = await resp.json()

            output = data.get("output", {})
            return float(output.get("bstp_nmix_prdy_ctrt", 0.0))

        except (aiohttp.ClientError, KeyError, ValueError, TypeError) as e:
            logger.warning(
                "[시장 지수 조회] 오류 (기본값 0.0 반환): %s: %s", type(e).__name__, e
            )
            return 0.0

    # ── 잔고 조회 ──────────────────────────────────────────────────────────

    async def get_balance(self) -> dict[str, Any]:
        """
        계좌 잔고를 조회한다.

        Returns:
            {
                "total_eval_amount": int,   # 총 평가금액
                "cash_balance": int,        # 예수금
                "positions": [              # 보유 종목 목록
                    {"ticker": str, "qty": int, "avg_price": int, "eval_amount": int},
                    ...
                ],
            }
        """
        url = f"{self._auth_trade.base_url}/uapi/domestic-stock/v1/trading/inquire-balance"
        tr_id = "VTTC8434R" if self._auth_trade.is_paper else "TTTC8434R"
        acc_no = self._auth_trade.account_no
        params = {
            "CANO": acc_no[:8],
            "ACNT_PRDT_CD": acc_no[8:10] if len(acc_no) >= 10 else "01",
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "01",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        try:
            await self._auth_trade.get_token()
            headers = self._auth_trade.get_headers(tr_id)
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            output1: list[dict] = data.get("output1", [])
            output2: dict = data.get("output2", [{}])[0] if data.get("output2") else {}

            positions = []
            for item in output1:
                qty = int(item.get("hldg_qty", 0))
                if qty > 0:
                    avg_price = int(float(item.get("pchs_avg_pric", 0)))
                    current_price = int(item.get("prpr", 0))
                    eval_amount = int(item.get("evlu_amt", 0))
                    pnl_amount = int(item.get("evlu_pfls_amt", 0))
                    pnl_ratio = float(item.get("evlu_pfls_rt", 0))
                    positions.append(
                        {
                            "ticker": item.get("pdno", ""),
                            "qty": qty,
                            "avg_price": avg_price,
                            "current_price": current_price,
                            "eval_amount": eval_amount,
                            "pnl_amount": pnl_amount,
                            "pnl_ratio": pnl_ratio,
                        }
                    )

            # 순자산금액(nass_amt)을 기준으로 총자산을 잡고, 현금은 역산합니다.
            # KIS API에서 예수금이나 정산금액은 주식 매수 시 즉각 반영되지 않는 경우가 많으나,
            # 순자산은 평가액 변동을 포함하여 실시간 계좌 가치를 가장 잘 반영합니다.
            nass_amt = int(output2.get("nass_amt", 0))
            tot_evlu_amt = int(output2.get("tot_evlu_amt", 0))
            stock_eval = int(output2.get("scts_evlu_amt", 0))

            total_assets = nass_amt if nass_amt > 0 else tot_evlu_amt
            # 현금 = 총자산 - 주식평가액 (이렇게 해야 주식 매수 시 현금이 즉시 줄어듬)
            cash_balance = total_assets - stock_eval

            return {
                "total_eval_amount": total_assets,
                "stock_eval_amount": stock_eval,
                "cash_balance": cash_balance,
                "positions": positions,
            }

        except aiohttp.ClientError as e:
            logger.error("[잔고 조회] API 오류: %s", e)
            raise
        except (KeyError, ValueError) as e:
            logger.error("[잔고 조회] 응답 파싱 오류: %s", e)
            raise
