"""LM Studio arbiter — 멀티차트 기반 BUY/SELL/HOLD 판정 모듈."""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import numpy as np

import config

if TYPE_CHECKING:
    from strategy.risk import RiskManager

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Follow these instructions strictly:
- Do NOT output thinking or reasoning steps
- Provide direct, concise answers only
- Skip any internal monologue or thought process
- Return only the final answer requested by the user
"""

_ACTION_MAP: dict[str, str] = {
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


# ── 순수 유틸 함수 ──────────────────────────────────────────────────────────


def normalize_action(raw_action: Any) -> str:
    """임의 문자열을 BUY/SELL/HOLD 중 하나로 정규화한다."""
    return _ACTION_MAP.get(str(raw_action or "HOLD").strip().upper(), "HOLD")


def _encode_image_to_data_url(image_path: Path) -> str:
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _build_orderbook_str(prices: list[int], volumes: list[int]) -> str:
    parts = [f"{p:,}({v:,})" for p, v in zip(prices, volumes)]
    return " / ".join(parts)


def _extract_json(text: str) -> dict:
    text = re.sub(r"```(?:json)?", "", text).strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("JSON 블록을 찾을 수 없습니다.")
    return json.loads(text[start:end])


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


# ── Arbiter 클래스 ──────────────────────────────────────────────────────────


class Arbiter:
    """LM Studio에 차트와 시장 데이터를 전달하고 BUY/SELL/HOLD를 판정한다."""

    def __init__(
        self,
        ticker: str,
        stock_name: str,
        intervals: tuple[int, ...],
        output_dir: Path,
        risk: RiskManager,
    ) -> None:
        self.ticker = ticker
        self.stock_name = stock_name
        self.intervals = intervals
        self.output_dir = output_dir
        self.risk = risk

    # ── 차트 렌더링 ─────────────────────────────────────────────────────────

    async def render_charts(
        self, interval_snapshots: dict[int, dict[str, Any]]
    ) -> dict[int, Path]:
        """각 interval 차트 PNG를 생성하고 경로를 반환한다."""
        from make_charts import _render_interval_chart  # 순환 임포트 방지

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
            day = now.strftime("%Y%m%d")
            hour = now.strftime("%H")
            hhmm = now.strftime("%H%M")
            chart_paths[interval] = (
                self.output_dir / day / hour / f"{hhmm}_{interval}m.png"
            )
        return chart_paths

    # ── 프롬프트 생성 ───────────────────────────────────────────────────────

    def build_prompt(self, snapshot: dict[str, Any]) -> str:
        """스냅샷으로부터 LM Studio 프롬프트 텍스트를 생성한다."""
        orderbook = snapshot["orderbook"]
        current_price = snapshot["price_data"]["current_price"]

        pos = self.risk.position
        if pos:
            pnl_ratio = (current_price - pos.entry_price) / pos.entry_price * 100
            if pos.direction == "SELL":
                pnl_ratio = -pnl_ratio
            position_text = (
                f"[{pos.direction}] 진입가 {pos.entry_price:,}원 "
                f"(현재 수익률 {pnl_ratio:+.2f}%)"
            )
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
1. 전략적 이원화 (Two-Way Strategy):
   - [전략 A: 추세 추종 (Trend Following)] 5분봉이 상승 중일 때, 1/3분봉의 일시적 눌림목(RSI 저점, 볼린저밴드 하단 터치)에서 BUY를 결정한다.
   - [전략 B: 역추세 반등 (Mean Reversion)] 5분봉이 하락 중이라도, 1분봉에서 강력한 수급 유입(거래량 급증 + MACD 골든크로스)과 함께 바닥권 신호가 포착되면 단기 반등을 노린 BUY를 결정한다.
2. 타임프레임의 역할 분담 (Context vs Trigger):
   - 5분봉은 '시장의 배경(Context)'이다. (상승장인가, 하락장인가?)
   - 1분/3분봉은 '실행 트리거(Trigger)'이다. 5분봉의 방향성과 일시적으로 반대되는 흐름이 나타날 때, 이를 '리스크'가 아닌 '진입 기회'로 포착하라. 
   - 단, 5분봉의 추세가 완전히 파괴된 상태에서의 무모한 물타기는 금지한다.
3. 데이터 교차 검증 (Cross-Verification):
   - 차트의 기술적 지표(EMA, MACD, 볼린저밴드 등)와 호가창의 수급(매도/매수 잔량비율, 체결강도)이 일치할 때만 강력한 신호로 간주한다.
   - 예: 가격은 상승 중이나 매도호가 잔량이 압도적으로 많고 체결강도가 급락 중이라면 'SELL' 혹은 'HOLD'를 고려한다.
4. 액션(BUY/SELL/HOLD) 판단 기준 및 엄격성:
   - BUY: 상승/반등 진입이 유리하다고 확신할 때만 선택
   - SELL: 하락 전환 또는 보유 포지션 청산이 유리하다고 확신할 때 선택
   - HOLD: 불확실하거나 방향성이 모호하면 무조건 관망(HOLD)한다.
5. 종목 보유 정보의 제공:
   - 종목의 보유 현황을 고려해 정교한 '매수/매도 전략'을 수립한다. 
   - 차트의 기술적 신호가 파괴되었다면, 매수가와 관계없이 냉정하게 SELL을 결정하라.

[출력 형식]
반드시 아래 구조의 JSON 객체 하나만 반환하십시오. 다른 설명은 생략합니다.

{{
    "action": "BUY" | "SELL" | "HOLD",
    "strategy_type": "Trend-Following" | "Mean-Reversion" | "None", 
    "confidence": 0~100,
    "analysis": {{
        "trend_context": "5분봉 기준 시장의 배경 (상승/하락/횡보)",
        "entry_trigger": "1/3분봉에서 발견된 진입 신호 (눌림목/돌파/반등 등)",
        "orderbook_strength": "체결강도 및 호가 수급의 유효성"
    }},
    "reason": "최종 결정을 내린 핵심적인 근거 한 문장"
}}""".strip()

    # ── LM Studio 호출 ──────────────────────────────────────────────────────

    async def ask(
        self, prompt_text: str, chart_paths: dict[int, Path]
    ) -> dict[str, Any]:
        """LM Studio에 차트와 프롬프트를 전송하고 판정 결과를 반환한다.

        실패 시 action=HOLD로 안전하게 강등한다.
        """
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
            "model": config.ARBITER_MODEL,
            "max_tokens": config.ARBITER_MAX_TOKENS,
            "temperature": config.ARBITER_TEMPERATURE,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        }

        timeout = aiohttp.ClientTimeout(total=config.ARBITER_TIMEOUT_SEC)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(config.ARBITER_BASE_URL, json=payload) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
        except aiohttp.ClientError as exc:
            logger.error("[LM Studio] HTTP 오류: %s", exc)
            return {"action": "HOLD", "reason": str(exc)}
        except Exception as exc:
            logger.error("[LM Studio] 호출 실패: %s", exc)
            return {"action": "HOLD", "reason": str(exc)}

        try:
            raw_text = _extract_lm_response_text(data)
            parsed = _extract_json(raw_text)
        except Exception as exc:
            logger.error("[LM Studio] 응답 파싱 실패: %s | raw=%s", exc, data)
            return {"action": "HOLD", "reason": f"응답 파싱 실패: {exc}"}

        parsed["action"] = normalize_action(parsed.get("action"))
        parsed.setdefault("reason", "사유 없음")
        return parsed
