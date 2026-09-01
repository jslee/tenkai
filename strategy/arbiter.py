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
import pandas as pd

import config
from kis_api.market import KISMarket

from strategy.indicators import _ema
from strategy.indicators import calc_rsi

if TYPE_CHECKING:
    from strategy.risk import RiskManager

logger = logging.getLogger(__name__)

USE_CHART_IMAGES = True

if USE_CHART_IMAGES:
    _PROMPT_FILE = Path(__file__).parent.parent / "prompt.yaml"
    _prompt_data: dict = yaml.safe_load(_PROMPT_FILE.read_text(encoding="utf-8"))
else:
    _PROMPT_FILE = Path(__file__).parent.parent / "prompt_nochart.yaml"
    _prompt_data: dict = yaml.safe_load(_PROMPT_FILE.read_text(encoding="utf-8"))

_SYSTEM_PROMPT: str = _prompt_data["system_prompt"]
_ANALYSIS_INTRO: str = _prompt_data["analysis_intro"]
_ANALYSIS_PRINCIPLES: str = _prompt_data["analysis_principles"]
_ANALYSIS_OUTPUT: str = _prompt_data["analysis_output"]

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

    def build_time_series_of_charts(self, snapshot: dict[str, Any]) -> str:
        """차트에 표시되는 모든 정보를 시계열 텍스트로 생성한다.

        각 interval별로 최근 차트 캔들 및 각종 지표 데이터를 테이블(Markdown 형식)로 변환한다.
        """
        lines = []
        interval_snapshots = snapshot.get("interval_snapshots", {})

        rename_map = {
            "label": "시간",
            "open": "시가",
            "high": "고가",
            "low": "저가",
            "close": "종가",
            "volume": "거래량",
            "bb_upper": "BB상한(2.0)",
            "bb_inner_upper": "BB상한(1.0)",
            "bb_mid": "BB기준",
            "bb_inner_lower": "BB하한(1.0)",
            "bb_lower": "BB하한(2.0)",
            "ema_short": f"EMA({config.EMA_SHORT})",
            "ema_long": f"EMA({config.EMA_LONG})",
            "ema_trend": f"EMA({config.EMA_TREND})",
            "rsi": "RSI",
            "macd": "MACD",
            "macd_signal": "Signal",
            "macd_hist": "Hist",
        }

        def fmt_price(val: Any) -> str:
            if pd.isna(val):
                return "-"
            try:
                f_val = float(val)
                if f_val.is_integer():
                    return f"{int(f_val):,}"
                return f"{f_val:,.1f}"
            except Exception:
                return str(val)

        def fmt_vol(val: Any) -> str:
            if pd.isna(val):
                return "-"
            try:
                return f"{int(float(val)):,}"
            except Exception:
                return str(val)

        def fmt_float(val: Any) -> str:
            if pd.isna(val):
                return "-"
            try:
                return f"{float(val):,.2f}"
            except Exception:
                return str(val)

        for interval in self.intervals:
            if interval not in interval_snapshots:
                continue

            snap = interval_snapshots[interval]
            df = snap.get("chart_frame")
            if df is None or df.empty:
                continue

            df = df.tail(10)  # df 뒤에서 10개만 남긴다

            lines.append(f"### {interval}분봉 차트 시계열 데이터 (최근 {len(df)}개 봉)")

            formatted_df = pd.DataFrame()
            if "label" in df.columns:
                formatted_df["시간"] = df["label"].astype(str)
            elif "timestamp" in df.columns:
                formatted_df["시간"] = df["timestamp"].astype(str)

            # 가격
            for col in ["open", "high", "low", "close"]:
                if col in df.columns:
                    formatted_df[rename_map[col]] = df[col].apply(fmt_price)

            # 거래량
            if "volume" in df.columns:
                formatted_df[rename_map["volume"]] = df["volume"].apply(fmt_vol)

            # 지표들
            for col in [
                "bb_upper",
                "bb_inner_upper",
                "bb_mid",
                "bb_inner_lower",
                "bb_lower",
                "ema_short",
                "ema_long",
                "ema_trend",
                "rsi",
                "macd",
                "macd_signal",
                "macd_hist",
            ]:
                if col in df.columns:
                    formatted_df[rename_map[col]] = df[col].apply(fmt_float)

            # 테이블 생성
            headers = list(formatted_df.columns)
            header_line = " | ".join(headers)
            sep_line = " | ".join(["---"] * len(headers))
            row_lines = []
            for row in formatted_df.itertuples(index=False):
                row_lines.append(" | ".join(str(x) for x in row))

            table_str = "\n".join([header_line, sep_line] + row_lines)
            lines.append(table_str)
            lines.append("")

        return "\n".join(lines).strip()

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
        indicators = snapshot["indicators"]

        # 신규 수급/체결 관련 추가 지표 포맷팅
        vwap = snapshot.get("vwap", 0.0)  # 거래량가중평균가
        price_vs_vwap = snapshot.get("price_vs_vwap", 0.0)  # 현재가이격률
        momentary_amt = snapshot.get("momentary_amt", 0)  # 순간체결대금
        momentary_amt_change_pct = snapshot.get(
            "momentary_amt_change_pct", 0.0
        )  # 이전 대비 증감률 (%)
        momentary_amt_ratio_to_median = snapshot.get(
            "momentary_amt_ratio_to_median", 1.0
        )  # 평시 중앙값 대비 배율 (배)
        tick_weighted_imbalance = snapshot.get(
            "tick_weighted_imbalance", 0.0
        )  # 호가 틱 가중지표

        # 시장지수 및 호가창
        market_context_text = (
            f"현재가: {current_price:,}\n"
            f"시장 지수 변동률: {snapshot['market_change']:.2f}%\n"
            f"체결강도: {snapshot.get('trade_strength', 0.0):.0f}%\n"
            f"매수잔량비율: {orderbook.get('buy_ratio', 0.5) * 100:.0f}%\n"
            f"매도호가 상위10(잔량): {_build_orderbook_str(orderbook.get('ask_prices', []), orderbook.get('ask_volumes', []))}\n"
            f"매수호가 상위10(잔량): {_build_orderbook_str(orderbook.get('bid_prices', []), orderbook.get('bid_volumes', []))}\n"
            f"당일 VWAP(거래량 가중평균가): {vwap:,.1f}원 (현재가 이격률: {price_vs_vwap:+.2f}%)\n"
            f"순간 체결대금 (최근 1분간): {momentary_amt / 100_000_000:,.2f}억원 (이전대비: {momentary_amt_change_pct:+.1f}%, 평시중앙값대비: {momentary_amt_ratio_to_median:.2f}배)\n"
            f"호가 틱 가중지표 (근접 호가 가중 잔량 비율): {tick_weighted_imbalance:+.4f} (양수=매수잔량 우세, 음수=매도잔량 우세, 범위: -1.0 ~ +1.0)"
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
        last_exit = snapshot.get("last_exit_info")
        if isinstance(last_exit, dict):
            net_pnl_ratio_pct = float(last_exit.get("net_pnl_ratio_pct", 0.0))
            result_label = "손실" if net_pnl_ratio_pct < 0 else "수익"
            last_exit_text = (
                f"매도 시간: {last_exit.get('exit_time', 'N/A').replace('T', ' ')}\n"
                f"매도 가격: {int(last_exit.get('exit_price', 0)):,}원\n"
                f"결과: {net_pnl_ratio_pct:+.2f}% ({result_label})"
            )
        else:
            last_exit_text = "없음 (청산 이력 없음)"

        # 제세금 및 수수료를 계산한다. ETF는 0.
        cost_ratio = (
            config.BROKER_FEE_RATE * 2 + config.TRANSACTION_TAX_RATE
        ) * 100  # 매수+매도 수수료 + 거래세

        time_series_text = self.build_time_series_of_charts(snapshot)

        return "\n\n".join(
            [
                _ANALYSIS_INTRO,
                f"""[차트 시계열 데이터]\n{time_series_text}""",
                f"""[시장지수 및 호가창 데이터]\n{market_context_text}""",
                f"""[보유 포지션]\n{position_text}""",
                f"""[최근 매도 정보]\n{last_exit_text}""",
                f"""[제세금 및 수수료]\n{cost_ratio:.3}%""",
                f"""[분석 및 판단 원칙]\n{_ANALYSIS_PRINCIPLES}""",
                f"""[출력 형식]\n{_ANALYSIS_OUTPUT}""",
            ]
        )

    # ── LM Studio 호출 ──────────────────────────────────────────────────────

    async def ask(
        self, prompt_text: str, chart_paths: dict[int, Path]
    ) -> dict[str, Any]:
        """LM Studio에 차트와 프롬프트를 전송하고 판정 결과를 반환한다.

        실패 시 action=HOLD로 안전하게 강등한다.
        """
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]

        if USE_CHART_IMAGES:
            for interval in self.intervals:
                content.append(
                    {
                        "type": "text",
                        "text": f"다음 이미지는 {interval}분봉 차트입니다.",
                    }
                )
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": _encode_image_to_data_url(chart_paths[interval])
                        },
                    }
                )

        payload: dict[str, Any] = {
            "model": config.ARBITER_MODEL,
            "temperature": config.ARBITER_TEMPERATURE,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        }
        if config.ARBITER_MAX_TOKENS > 0:
            payload["max_tokens"] = config.ARBITER_MAX_TOKENS
        elif getattr(config, "ARBITER_MAX_TOKENS", -1) == -1:
            payload["max_tokens"] = 4096

        headers = {}
        api_key = getattr(config, "OPENAI_API_KEY", None)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        url = config.ARBITER_BASE_URL or "http://127.0.0.1:1234/v1/chat/completions"
        if not url.endswith("/chat/completions"):
            url = url.rstrip("/") + "/chat/completions"

        timeout = aiohttp.ClientTimeout(total=config.ARBITER_TIMEOUT_SEC)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status >= 400:
                        err_body = await resp.text()
                        logger.error(
                            "[arbiter] AI 서버 에러 응답 (HTTP %d): %s",
                            resp.status,
                            err_body,
                        )
                    resp.raise_for_status()
                    data = await resp.json()
        except aiohttp.ClientError as exc:
            err_msg = str(exc) or type(exc).__name__
            logger.error("[arbiter] HTTP 오류: %s (%s)", err_msg, type(exc).__name__)
            return {"action": "HOLD", "reason": f"{err_msg} ({type(exc).__name__})"}
        except Exception as exc:
            err_msg = str(exc) or type(exc).__name__
            logger.error("[arbiter] 호출 실패: %s (%s)", err_msg, type(exc).__name__)
            return {"action": "HOLD", "reason": f"{err_msg} ({type(exc).__name__})"}

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
