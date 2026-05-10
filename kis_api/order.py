"""
kis_api/order.py — 주문 실행

사용 API:
- 매수: TTTC0802U (실전) / VTTC0802U (모의)
- 매도: TTTC0801U (실전) / VTTC0801U (모의)
"""

import logging
from typing import Any

import aiohttp

import config
from .auth import KISAuth

logger = logging.getLogger(__name__)


class KISOrder:
    """KIS 주문 실행 클래스"""

    def __init__(self, auth: KISAuth) -> None:
        self._auth = auth

    # ── 매수 주문 ──────────────────────────────────────────────────────────

    async def buy_market(self, ticker: str, qty: int) -> dict[str, Any]:
        """
        시장가 매수 주문을 실행한다.

        Args:
            ticker: 종목 코드
            qty: 주문 수량

        Returns:
            {"order_no": str, "order_time": str}
        """
        if qty <= 0:
            raise ValueError(f"주문 수량은 1 이상이어야 합니다. (qty={qty})")

        tr_id = "VTTC0802U" if self._auth.is_paper else "TTTC0802U"
        return await self._send_order(
            tr_id=tr_id,
            ticker=ticker,
            qty=qty,
            price=0,           # 시장가
            order_type="01",   # 01: 시장가
        )

    async def buy_limit(self, ticker: str, qty: int, price: int) -> dict[str, Any]:
        """
        지정가 매수 주문을 실행한다.

        Args:
            ticker: 종목 코드
            qty: 주문 수량
            price: 지정가

        Returns:
            {"order_no": str, "order_time": str}
        """
        if qty <= 0:
            raise ValueError(f"주문 수량은 1 이상이어야 합니다. (qty={qty})")
        if price <= 0:
            raise ValueError(f"지정가는 0 초과여야 합니다. (price={price})")

        tr_id = "VTTC0802U" if self._auth.is_paper else "TTTC0802U"
        return await self._send_order(
            tr_id=tr_id,
            ticker=ticker,
            qty=qty,
            price=price,
            order_type="00",   # 00: 지정가
        )

    # ── 매도 주문 ──────────────────────────────────────────────────────────

    async def sell_market(self, ticker: str, qty: int) -> dict[str, Any]:
        """
        시장가 매도 주문을 실행한다.

        Args:
            ticker: 종목 코드
            qty: 주문 수량

        Returns:
            {"order_no": str, "order_time": str}
        """
        if qty <= 0:
            raise ValueError(f"주문 수량은 1 이상이어야 합니다. (qty={qty})")

        tr_id = "VTTC0801U" if self._auth.is_paper else "TTTC0801U"
        return await self._send_order(
            tr_id=tr_id,
            ticker=ticker,
            qty=qty,
            price=0,
            order_type="01",
        )

    async def sell_limit(self, ticker: str, qty: int, price: int) -> dict[str, Any]:
        """
        지정가 매도 주문을 실행한다.

        Args:
            ticker: 종목 코드
            qty: 주문 수량
            price: 지정가

        Returns:
            {"order_no": str, "order_time": str}
        """
        if qty <= 0:
            raise ValueError(f"주문 수량은 1 이상이어야 합니다. (qty={qty})")
        if price <= 0:
            raise ValueError(f"지정가는 0 초과여야 합니다. (price={price})")

        tr_id = "VTTC0801U" if self._auth.is_paper else "TTTC0801U"
        return await self._send_order(
            tr_id=tr_id,
            ticker=ticker,
            qty=qty,
            price=price,
            order_type="00",
        )

    # ── 내부 공통 주문 ─────────────────────────────────────────────────────

    async def _send_order(
        self,
        tr_id: str,
        ticker: str,
        qty: int,
        price: int,
        order_type: str,
    ) -> dict[str, Any]:
        """KIS 주문 API를 호출한다."""
        url = f"{self._auth.base_url}/uapi/domestic-stock/v1/trading/order-cash"
        acc_no = self._auth.account_no
        payload = {
            "CANO": acc_no[:8],
            "ACNT_PRDT_CD": acc_no[8:10] if len(acc_no) >= 10 else "01",
            "PDNO": ticker,
            "ORD_DVSN": order_type,
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
        }
        try:
            await self._auth.get_token()
            headers = self._auth.get_headers(tr_id, {"hashkey": ""})
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            if data.get("rt_cd") != "0":
                msg = data.get("msg1", "알 수 없는 오류")
                logger.error("[주문] KIS 오류 응답 (tr_id=%s, msg=%s)", tr_id, msg)
                raise RuntimeError(f"주문 실패: {msg}")

            output = data.get("output", {})
            logger.critical(
                "[주문] 완료 (tr_id=%s, ticker=%s, qty=%d, price=%d)",
                tr_id, ticker, qty, price,
            )
            return {
                "order_no": output.get("ODNO", ""),
                "order_time": output.get("ORD_TMD", ""),
            }

        except aiohttp.ClientError as e:
            logger.error("[주문] API 오류 (tr_id=%s, ticker=%s): %s", tr_id, ticker, e)
            raise
        except RuntimeError:
            raise
        except Exception as e:
            logger.error("[주문] 예상치 못한 오류 (tr_id=%s): %s", tr_id, e)
            raise
