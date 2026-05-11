"""
kis_api/market.py — 시세/호가 조회

사용 API:
- FHKST01010100: 현재가·등락률·거래량
- FHKST03010200: 1분봉 OHLCV
- FHKST01010200: 매수·매도 10호가 잔량
"""

import logging
import asyncio
from pathlib import Path
from typing import Any
from datetime import datetime

import aiohttp

import config
from .auth import KISAuth

logger = logging.getLogger(__name__)


class KISMarket:
    """KIS 시세/호가 조회 클래스"""

    def __init__(self, auth_data: KISAuth, auth_trade: KISAuth) -> None:
        self._auth_data = auth_data
        self._auth_trade = auth_trade

    @staticmethod
    def _estimate_latest_minute_elapsed(timestamp: str) -> float:
        """
        최신 1분봉의 경과 시간을 분 단위로 추정한다.
        - 닫힌 봉 계산만 할 거면 필요 없다.
        - 하지만 3분봉·5분봉 부분봉(닫히지 않은)의 거래량 속도를 보고 싶으면 필요하다.
        - live veto 또는 live volume trigger의 정확도를 위해 필요하다.
        """
        try:
            time_str = timestamp[-6:]
            hour = int(time_str[0:2])
            minute = int(time_str[2:4])
        except (ValueError, IndexError):
            return 1.0

        now = datetime.now()
        candle_start_minutes = hour * 60 + minute
        now_minutes = now.hour * 60 + now.minute + (now.second / 60)
        elapsed = now_minutes - candle_start_minutes
        return max(0.01, min(elapsed, 1.0))

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

    # ── 일봉 조회 (기간별시세) ─────────────────────────────────────────────

    async def get_daily_candles(
        self, ticker: str, days: int = 20
    ) -> list[dict[str, Any]]:
        """
        최근 N 거래일의 일봉 OHLCV를 조회한다. (FHKST03010100)

        Args:
            ticker: 종목코드
            days: 조회 일수 (기본 20)

        Returns:
            [
                {"date": "YYYYMMDD", "open": int, "high": int, "low": int,
                 "close": int, "volume": int},
                ...
            ]  # 최신 날짜가 index 0
        """
        from datetime import datetime, timedelta

        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")

        url = f"{self._auth_data.base_url}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
            "FID_INPUT_DATE_1": start_date,
            "FID_INPUT_DATE_2": end_date,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",
        }
        try:
            await self._auth_data.get_token()
            headers = self._auth_data.get_headers("FHKST03010100")
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            raw = data.get("output2")
            if not isinstance(raw, list):
                raw = []
            candles = []
            for c in raw[:days]:
                vol = int(c.get("acml_vol", 0))
                if vol <= 0:
                    continue
                candles.append(
                    {
                        "date": c.get("stck_bsop_date", ""),
                        "open": int(c.get("stck_oprc", 0)),
                        "high": int(c.get("stck_hgpr", 0)),
                        "low": int(c.get("stck_lwpr", 0)),
                        "close": int(c.get("stck_clpr", 0)),
                        "volume": vol,
                    }
                )
            return candles

        except aiohttp.ClientError as e:
            logger.error("[일봉 조회] API 오류 (ticker=%s): %s", ticker, e)
            raise
        except (KeyError, ValueError, TypeError) as e:
            logger.error(
                "[일봉 조회] 응답 파싱 오류 (ticker=%s): %s: %s",
                ticker,
                type(e).__name__,
                e,
            )
            raise

    # ── 1분봉 조회 ─────────────────────────────────────────────────────────

    async def get_minute_candles(
        self, ticker: str, count: int = config.CANDLE_COUNT
    ) -> list[dict[str, Any]]:
        """
        1분봉 OHLCV를 최신순으로 반환한다.

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
        url = f"{self._auth_data.base_url}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
        all_candles = []
        next_hour = "000000"

        current_date_str = ""
        prev_time_val = -1

        try:
            await self._auth_data.get_token()
            headers = self._auth_data.get_headers("FHKST03010200")
            async with aiohttp.ClientSession() as session:
                while len(all_candles) < count:
                    params = {
                        "fid_etc_cls_code": "",
                        "fid_cond_mrkt_div_code": "J",
                        "fid_input_iscd": ticker,
                        "fid_input_hour_1": next_hour,
                        "fid_pw_data_incu_yn": "Y",  # 핵심: 과거(어제) 데이터 포함 허용
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
                                    "[1분봉 조회] KIS 서버 에러(HTTP %d) 도달. 수집 한계점으로 판단하여 조회를 중단합니다. (누적: %d개)",
                                    resp.status,
                                    len(all_candles),
                                )
                                break
                            resp.raise_for_status()
                            data = await resp.json()
                    except aiohttp.ClientResponseError as e:
                        if e.status >= 500:
                            logger.warning(
                                "[1분봉 조회] KIS 서버 에러(HTTP %d) 도달. 조회를 중단합니다.",
                                e.status,
                            )
                            break
                        raise e

                    raw_candles = data.get("output2")
                    if not isinstance(raw_candles, list) or not raw_candles:
                        break

                    for c in raw_candles:
                        time_str = c.get("stck_cntg_hour", "")
                        if not time_str:
                            continue

                        # 가상 날짜 생성 로직 (KIS 서버의 '오늘 날짜 복사 버그' 회피)
                        # 최신->과거로 훑는 중, 090000에서 153000 등 갑자기 숫자가 커지면 어제로 역주행한 것!
                        curr_time_val = int(time_str)
                        api_date_str = c.get("stck_bsop_date", "")

                        if not current_date_str:
                            current_date_str = api_date_str
                        elif (
                            prev_time_val != -1
                            and curr_time_val > prev_time_val + 40000
                        ):
                            # 날짜가 바뀌는 시점 (예: 090000 -> 153000)
                            # KIS API가 이때는 정상적인 과거 날짜를 주지만, 페이지가 넘어가면
                            # 다시 오늘 날짜를 복사해버리는 버그가 있음.
                            # 따라서 경계선에서 제대로 된 과거 날짜가 오면 그것을 취하고, 그 외에는 락(lock)을 건다.
                            if api_date_str and api_date_str < current_date_str:
                                current_date_str = api_date_str

                        prev_time_val = curr_time_val

                        # 8자리 실제 날짜 + 6자리 시간을 합쳐 14자리 완벽 정렬 포맷 생성
                        timestamp = f"{current_date_str}{time_str}"

                        # KIS API 특성상 전송 페이징 경계선에서 같은 시간의 봉이 중복 전달될 수 있음
                        if all_candles and all_candles[-1]["timestamp"] == timestamp:
                            continue

                        all_candles.append(
                            {
                                "timestamp": timestamp,
                                "open": int(c.get("stck_oprc", 0)),
                                "high": int(c.get("stck_hgpr", 0)),
                                "low": int(c.get("stck_lwpr", 0)),
                                "close": int(c.get("stck_prpr", 0)),
                                "volume": int(c.get("cntg_vol", 0)),
                            }
                        )

                        if len(all_candles) >= count:
                            break

                    oldest_time = raw_candles[-1].get("stck_cntg_hour", "")
                    next_page_hour = self._page_before_time(oldest_time)
                    if not oldest_time or next_page_hour == next_hour:
                        # 더 이상 과거 데이터가 없거나 무한 루프 갇힘 방지
                        break
                    next_hour = next_page_hour

                    # KIS OpenAPI 초당 호출 제한(20건) 및 페이징 방화벽(500 에러)을 배려한 여유 있는 대기
                    # 초기 부팅 시 한 번만 실행되므로 0.3초(안전 마진)로 텀을 확실히 둡니다.
                    if len(all_candles) < count:
                        await asyncio.sleep(0.3)

            if all_candles:
                for candle in all_candles:
                    candle["elapsed_minutes"] = 1.0
                all_candles[0]["elapsed_minutes"] = (
                    self._estimate_latest_minute_elapsed(all_candles[0]["timestamp"])
                )

            return all_candles

        except aiohttp.ClientError as e:
            logger.error("[1분봉 조회] API 오류 (ticker=%s): %s", ticker, e)
            raise
        except (KeyError, ValueError, TypeError) as e:
            logger.error(
                "[1분봉 조회] 응답 파싱 오류 (ticker=%s): %s: %s",
                ticker,
                type(e).__name__,
                e,
            )
            raise

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
