"""LM Studio arbiter — 멀티차트 기반 BUY/SELL/HOLD 판정 모듈."""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

import aiohttp
import numpy as np

import config
from kis_api.market import KISMarket
from strategy.indicators import _ema

if TYPE_CHECKING:
    from strategy.risk import RiskManager

logger = logging.getLogger(__name__)

_PROMPT_FILE = Path(__file__).parent.parent / "prompt.yaml"
_prompt_data: dict = yaml.safe_load(_PROMPT_FILE.read_text(encoding="utf-8"))

_SYSTEM_PROMPT: str = _prompt_data["system_prompt"]
_ANALYSIS_INTRO: str = _prompt_data["analysis_intro"]
_ANALYSIS_PRINCIPLES: str = _prompt_data["analysis_principles"]

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
    if start == -1:
        raise ValueError("JSON 블록을 찾을 수 없습니다.")

    json_str = text[start:]

    # 1. 일반적인 완결된 JSON 파싱 시도
    end = json_str.rfind("}") + 1
    if end > 0:
        try:
            return json.loads(json_str[:end])
        except json.JSONDecodeError:
            pass

    # 2. 잘린 JSON 복구 시도 (토큰 제한으로 끝이 짤렸을 때)
    fixes = [
        "}",
        '"}',
        '"} }',
        '"} }',
        '"", "dummy": ""}',
    ]
    for fix in fixes:
        try:
            test_str = json_str + fix
            test_end = test_str.rfind("}") + 1
            if test_end > 0:
                return json.loads(test_str[:test_end])
        except json.JSONDecodeError:
            continue

    # 3. 최후의 수단: 정규식을 이용해 파싱 가능한 key-value 만 추출
    try:
        recovered = {}
        # 문자열 매칭
        str_matches = re.findall(r'"([^"]+)"\s*:\s*"([^"]*?)(?="|,|\s*$)', json_str)
        for k, v in str_matches:
            recovered[k] = v
        # 숫자 매칭
        num_matches = re.findall(r'"([^"]+)"\s*:\s*([0-9.]+)', json_str)
        for k, v in num_matches:
            try:
                recovered[k] = float(v) if "." in v else int(v)
            except ValueError:
                pass

        if recovered:
            return recovered
    except Exception:
        pass

    raise ValueError("JSON 파싱 및 복구 실패")


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

    @property
    def system_prompt(self) -> str:
        """시스템 프롬프트 텍스트를 반환합니다."""
        return _SYSTEM_PROMPT

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
            ticker_dir = self.output_dir / self.ticker
            _render_interval_chart(
                frame,
                self.ticker,
                self.stock_name,
                interval,
                ticker_dir,
                timestamp=timestamp,
            )
            day = now.strftime("%Y%m%d")
            hour = now.strftime("%H")
            mm = now.strftime("%H%M")
            chart_paths[interval] = ticker_dir / day / hour / f"{mm}_{interval}m.png"
        return chart_paths

    # ── 프롬프트 생성 ───────────────────────────────────────────────────────

    def build_prompt(self, snapshot: dict[str, Any]) -> str:
        """스냅샷으로부터 LM Studio 프롬프트 텍스트를 생성한다."""
        orderbook = snapshot["orderbook"]
        current_price = snapshot["price_data"]["current_price"]

        # indicators = snapshot["indicators"]
        # closes_1m = [
        #     float(c["close"]) for c in reversed(snapshot.get("analysis_candles", []))
        # ]
        # closes_3m = [
        #     float(c["close"])
        #     for c in reversed(
        #         snapshot.get("interval_snapshots", {})
        #         .get(3, {})
        #         .get("analysis_candles", [])
        #     )
        # ]
        # ema_1m = {
        #     5: indicators.get("ema_short", 0.0),
        #     20: indicators.get("ema_long", 0.0),
        #     30: _ema(closes_1m, 30)[-1] if len(closes_1m) >= 30 else 0.0,
        #     60: _ema(closes_1m, 60)[-1] if len(closes_1m) >= 60 else 0.0,
        # }
        # ema_3m = {
        #     20: _ema(closes_3m, 20)[-1] if len(closes_3m) >= 20 else 0.0,
        #     60: _ema(closes_3m, 60)[-1] if len(closes_3m) >= 60 else 0.0,
        # }

        # # 그래프로 인식하는 것보다 정확한 수치로 제시하는 것이 판단에 더 도움이 될 것.
        # core_anchor = (
        #     f"1m EMA: [5: {ema_1m[5]:,.0f} / 20: {ema_1m[20]:,.0f} / 30: {ema_1m[30]:,.0f} / 60: {ema_1m[60]:,.0f}]\n"
        #     f"3m EMA: [20: {ema_3m[20]:,.0f} / 60: {ema_3m[60]:,.0f}]\n"
        #     f"1m RSI: {indicators.get('rsi', 0.0):.0f}\n"
        # )

        # 시장지수 및 호가창
        market_context_text = (
            f"현재가: {current_price:,}\n"
            f"시장 지수 변동률: {snapshot['market_change']:.2f}%\n"
            f"체결강도: {snapshot.get('trade_strength', 0.0):.0f}%\n"
            f"매수잔량비율: {orderbook.get('buy_ratio', 0.5) * 100:.0f}%\n"
            f"매도호가 상위5(잔량): {_build_orderbook_str(orderbook.get('ask_prices', []), orderbook.get('ask_volumes', []))}\n"
            f"매수호가 상위5(잔량): {_build_orderbook_str(orderbook.get('bid_prices', []), orderbook.get('bid_volumes', []))}"
        )

        # 현재 보유 포지션
        position_text = "없음 (미보유 상태)"
        pos = self.risk.position
        if pos:
            pnl_ratio = (current_price - pos.entry_price) / pos.entry_price * 100
            # if pos.direction == "SELL":
            #     pnl_ratio = -pnl_ratio
            position_text = (
                f"진입가 {pos.entry_price:,}" f"(현재 수익률 {pnl_ratio:+.2f}%)"
            )

        # 최근 매도 정보
        # last_exit = snapshot.get("last_exit_info")
        # if isinstance(last_exit, dict):
        #     net_pnl_ratio_pct = float(last_exit.get("net_pnl_ratio_pct", 0.0))
        #     result_label = "손실" if net_pnl_ratio_pct < 0 else "수익"
        #     last_exit_text = (
        #         f"매도 시간: {last_exit.get('exit_time', 'N/A').replace('T', ' ')}\n"
        #         f"매도 가격: {int(last_exit.get('exit_price', 0)):,}원\n"
        #         f"결과: {net_pnl_ratio_pct:+.2f}% ({result_label})"
        #     )
        # else:
        #     last_exit_text = "없음 (청산 이력 없음)"

        # 제세금 및 수수료를 계산한다. ETF는 0.
        cost_ratio = (
            config.BROKER_FEE_RATE * 2 + config.TRANSACTION_TAX_RATE
        ) * 100  # 매수+매도 수수료 + 거래세

        return f"""
{_ANALYSIS_INTRO}

[시장지수 및 호가창 데이터]
{market_context_text}

[보유 포지션]
{position_text}

[제세금 및 수수료]
{cost_ratio:.3}%

[분석 및 판단 원칙]
{_ANALYSIS_PRINCIPLES}
   
[출력 형식]
반드시 아래 구조의 JSON 객체 하나만 반환하십시오. 다른 설명은 생략합니다.

{{
    "action": "BUY" | "SELL" | "HOLD",
    "confidence": 0~100,
    "analysis": {{
        "thinking": thinking process for action.
        "trend_context": "시장의 배경 (상승/하락/횡보)",
        "entry_trigger": "발견된 진입 신호 (눌림목/돌파/반등 등)",
        "orderbook_strength": "체결강도 및 호가 수급의 유효성"
    }},
    "reason": "최종 결정을 내린 근거를 자세히 설명"
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
                {"role": "system", "content": _SYSTEM_PROMPT},
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
            logger.error("[arbiter] HTTP 오류: %s", exc)
            return {"action": "HOLD", "reason": str(exc)}
        except Exception as exc:
            logger.error("[arbiter] 호출 실패: %s", exc)
            return {"action": "HOLD", "reason": str(exc)}

        try:
            choices = data.get("choices", [])
            if choices:
                finish_reason = choices[0].get("finish_reason", "")
                if finish_reason == "length":
                    logger.warning(
                        "[arbiter] ⚠ 경고: AI 응답이 최대 토큰 수 한도(max_tokens=%d)를 초과하여 중간에 잘렸습니다. "
                        "일부 데이터는 자동 복구 로직을 통해 복원되었으나 분석 내용이 불완전할 수 있습니다. "
                        "완전한 응답을 원하시면 config.py의 ARBITER_MAX_TOKENS 값을 늘리세요.",
                        config.ARBITER_MAX_TOKENS,
                    )
            raw_text = _extract_lm_response_text(data)
            parsed = _extract_json(raw_text)
        except Exception as exc:
            logger.error("[arbiter] 응답 파싱 실패: %s | raw=%s", exc, data)
            return {"action": "HOLD", "reason": f"응답 파싱 실패: {exc}"}

        parsed["action"] = normalize_action(parsed.get("action"))
        parsed.setdefault("reason", "사유 없음")
        return parsed
