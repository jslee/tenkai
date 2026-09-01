"""picker.py — 관심 종목 자동 스크리닝 도구.

watchlist_stocks.csv (종목코드:종목명 형식)를 읽어 종목별로
일봉/주봉/월봉 차트를 그린 뒤 AI에 분석을 요청하고,
점수 높은 순으로 결과를 출력한다.
매수할 종목을 선정하는 사전 작업으로 활용한다.

실행 예:
    python picker.py
    python picker.py --csv my_watchlist.csv --output-dir charts/periods --count 120
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import aiohttp
import numpy as np

import config
from kis_api import KISAuth, KISMarket
from make_charts import (
    _build_period_indicator_frame,
    _render_period_chart,
    chart_config,
)

from colorama import Fore, Style, init as _colorama_init

_colorama_init(autoreset=True)

logger = logging.getLogger(__name__)

# - Skip any internal monologue or thought process
SYSTEM_PROMPT = """Follow these instructions strictly:
- Do NOT output thinking or reasoning steps
- Provide direct, concise answers only
- Return only the final answer requested by the user
"""

# 점수 산정에 대한 세부지침 없이 AI의 평가 능력에 맡기는 형태로 전환
PICKER_PROMPT_TEMPLATE = """당신은 한국 주식 단기 스윙 트레이딩을 평가하는 '포트폴리오 스크리닝 에이전트'입니다.
당장 내일 지정 종목을 매수할지 말지를 결정하는 것이 당신의 역할입니다.
{guideline}

[입력 데이터]
- 종목코드: {ticker}
- 종목명: {name}
- 일봉/주봉/월봉 차트 이미지 (순서대로 3장 첨부)
  * 차트에 포함된 지표: 캔들,볼린저밴드,RSI,거래량,MACD,EMA(5, 20, 60)
  * 오른쪽 마지막 캔들이 현재 시점이다.
  * 캔들 적색은 상승/매수, 청색은 하락/매도를 나타낸다.

[기본 지표]
{core_anchor}

[재무 정보]
{financial_info}

[평가 원칙]
- 내일 당장 진입할 만한 종목인지 평가해 점수를 매긴다. 
- 차트에 포함된 지표와 기본 지표, 뉴스, 커뮤니티 정보를 종합하여 매수 매력도를 채점한다.
- 일봉 → 주봉 → 월봉 순으로 비중을 두어 추세를 확인한다
- 추세 속의 '눌림목(Pullback)'인지 아니면 추세가 꺾이는 '추세 전환(Reversal)'인지를 명확히 판별한다.
- 매수 타이밍(즉시/눌림목 대기/돌파 대기/관망)을 한 가지로 명시한다.
- 현재 눌림목 구간인지, 돌파 직전인지, 관망할 때인지 명확하게 제시하고 판단 근거와 권고사항을 제시한다.
- 반드시 웹 검색을 통해 이 종목의 최신 뉴스(실적, 공시, 테마 등)와 주식 커뮤니티에서 관련 정보를 찾아 최종 판단에 반영한다.

[점수 산정 가이드라인 - 정밀 스코어링]
모든 점수는 0~100점 사이로 소수점 첫째 자리까지 계산한다. (예: 82.7점)
*주의: 절대 5단위나 10단위로 끊어서 계산하지 마라. 반드시 각 지표와 뉴스, 커뮤니티 정보를 반영하여 정밀한 소수점 점수를 산출하라.*

[출력 형식]
반드시 아래 JSON 하나만 반환하라. 다른 설명은 생략한다.

{{
    "score": 0~100.0,
    "timing": 즉시|눌림목 대기|돌파 대기|관망,
    "trend": 강한 상승|상승|횡보|하락|강한 하락,
    "news": 검색된 뉴스의 핵심 내용과 차트와의 상관관계를 상세히 설명,
    "community": 검색된 커뮤니티의 핵심 내용과 차트와의 상관관계를 상세히 설명,
    "score-reason": 매수 매력도 결정의 근거를 상세히 설명,
    "timing-reason": 타이밍 판단 근거 및 타이밍에 대비하는 권고사항을 상세히 설명,
    "thinking": thinking process for action
}}"""

# 세부적인 점수 산정 가이드라인을 제시하는 것이 AI의 능력을 오히려 제한하는 것으로 판단되어 축소하기로 함.
PICKER_PROMPT_TEMPLATE_0 = """당신은 한국 주식 단기 스윙 트레이딩을 평가하는 '포트폴리오 스크리닝 에이전트'입니다.
당장 내일 지정 종목을 매수할지 말지를 결정하는 것이 당신의 역할입니다.  

[입력 데이터]
- 종목코드: {ticker}
- 종목명: {name}
- 일봉/주봉/월봉 차트 이미지 (순서대로 3장 첨부)
  * 차트에 포함된 지표: 캔들,볼린저밴드,RSI,거래량,MACD,EMA(5, 20, 60)

[평가 원칙]
1. 월봉 → 주봉 → 일봉 순으로 추세를 확인하되, 상위 차트의 추세와 일봉의 단기 움직임 사이의 괴리(Divergence)를 반드시 포착한다.
2. 특히 최근 3~5거래일간의 일봉 패턴을 정밀 분석하여, 이것이 상승 추세 속의 '눌림목(Pullback)'인지 아니면 추세가 꺾이는 '추세 전환(Reversal)'인지를 명확히 판별한다.
3. 하락 시 거래량의 변화를 확인하여 매수세의 이탈 여부를 판단한다 (거래량 없는 하락은 눌림목으로 간주).
4. 현재 가격이 주요 지지선(EMA 20, 볼린저밴드 중심선 등)에 맞닿아 있는지 확인하여 '눌림목 대기' 또는 '돌파 대기'의 근거로 삼는다.
5. 추세·모멘텀·거래량·지지저항·지표 수렴/발산을 종합하여 단기 매수 매력도를 0~100점으로 채점한다.
6. 현재 가격이 중장기 이동평균선 대비 과열/침체 구간인지 판단한다.
7. 매수 타이밍(즉시/눌림목 대기/돌파 대기/관망)을 한 가지로 명시한다.
8. 현재 눌림목 구간인지, 돌파 직전인지, 관망할 때인지 명확하게 제시하고 판단 근거와 권고사항을 제시한다.
9. 반드시 웹 검색을 통해 이 종목의 최신 뉴스(실적, 공시, 테마 등)를 찾아 분석에 포함한다.
10. 검색된 뉴스가 차트의 변동성이나 향후 방향성에 미칠 영향을 분석하여 최종 판단에 반영한다.

[점수 산정 가이드라인 - 정밀 스코어링]
- 모든 점수는 0~100점 사이이며, 소수점 첫째 자리까지 계산한다. (예: 82.7점)
- 반드시 지표의 수치와 뉴스, 커뮤니티 정보를 반영하여 정밀한 소수점 점수를 산출하라.

[출력 형식]
반드시 아래 JSON 하나만 반환하라. 다른 설명은 생략한다.

{{
    "score": 0~100.0,
    "timing": 즉시|눌림목 대기|돌파 대기|관망,
    "trend": 강한 상승|상승|횡보|하락|강한 하락,
    "news": 검색된 뉴스의 핵심 내용과 차트와의 상관관계를 상세히 설명,
    "score-reason": 매수 매력도를 결정한 핵심 근거 한 문장,
    "timing-reason": 타이밍 판단 근거 및 타이밍에 대비하는 권고사항,
    "thinking": thinking process for action.
}}"""

# 필요시 [점수 산정 가이드라인]에 추가하여 사용할 것
# 1. 추세 동조화 (30점 만점)
#    - 월/주/일봉 모두 상승 정배열: +30
#    - 상위 차트 대비 일봉의 단기 눌림 발생: +20
#    - 주봉은 상승이나 일봉 역배열(괴리): +10
#    - 전체적 하락 추세 진행 중: 0점 이하

# 2. 지지 및 저항 근거 (30점 만점)
#    - 주요 이평선(EMA 20, 60) 및 BB 중심선 지지 확인: +30
#    - 돌파 직전의 박스권 상단 위치: +20
#    - 주요 지지선 이탈 및 저항선 근접: +10

# 3. 거래량 및 모멘텀 (25점 만점)
#    - 하락 시 거래량 급감(매수세 유지): +25
#    - 상승 시 거래량 동반 확인: +15
#    - RSI/MACD 과열 구간 진입: -10

# 4. 뉴스 및 재료 가중치 (15점 만점)
#    - 강력한 호재(실적, 수주, 테마 편입): +15
#    - 단순 뉴스로 변동성 확대 우려: +5
#    - 악재 또는 불확실성 증가: -15
# *주의: 절대 5단위나 10단위로 끊어서 계산하지 마라. 반드시 각 지표의 수치를 반영하여 정밀한 소수점 점수를 산출하라.*

# ── 유틸 ─────────────────────────────────────────────────────────────────────


def _encode_image(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _extract_json(text: str) -> dict:
    text = re.sub(r"```(?:json)?", "", text).strip()
    start = text.find("{")
    if start == -1:
        raise ValueError("JSON 블록 없음")

    json_str = text[start:]

    # 1. 일반적인 완결된 JSON 파싱 시도
    end = json_str.rfind("}") + 1
    if end > 0:
        try:
            return json.loads(json_str[:end])
        except json.JSONDecodeError:
            pass

    # 2. 잘린 JSON 복구 시도 (토큰 제한으로 끝이 짤렸을 때)
    # 뒷부분을 닫아주며 파싱이 성립하는지 무차별 시도
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


def _parse_watchlist(csv_path: Path) -> list[tuple[str, str]]:
    """종목코드:종목명 형식의 CSV를 파싱한다."""
    items: list[tuple[str, str]] = []
    with csv_path.open(encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":", 1)
            if len(parts) == 2:
                ticker, name = parts[0].strip(), parts[1].strip()
            else:
                ticker = parts[0].strip()
                name = ticker
            if ticker:
                items.append((ticker, name))
    return items


def _create_market() -> KISMarket:
    auth = KISAuth(
        app_key=config.KIS_REAL_APP_KEY,
        app_secret=config.KIS_REAL_APP_SECRET,
        account_no=config.KIS_REAL_ACCOUNT_NO,
        base_url=config.BASE_URL_REAL,
        is_paper=False,
    )
    return KISMarket(auth_data=auth, auth_trade=auth)


# ── 차트 생성 ────────────────────────────────────────────────────────────────


async def _build_period_charts(
    market: KISMarket,
    ticker: str,
    name: str,
    output_dir: Path,
) -> tuple[dict[str, Path], dict[str, dict[str, float]]] | None:
    """일봉/주봉/월봉 차트 PNG를 생성하고 {D/W/M: path} 와 기본 지표 요약을 반환한다."""
    import pandas as pd

    # (주기코드, 라벨, API조회할데이터개수, 차트에그릴데이터개수)
    period_configs = [
        ("D", "일봉", 200, 60),
        ("W", "주봉", 100, 30),
        ("M", "월봉", 60, 15),
    ]

    paths: dict[str, Path] = {}
    indicators_summary: dict[str, dict[str, float]] = {}

    for period_code, period_label, fetch_count, plot_count in period_configs:
        try:
            if period_code == "D":
                candles = await market.get_daily_candles(ticker, days=fetch_count)
            elif period_code == "W":
                candles = await market.get_weekly_candles(ticker, weeks=fetch_count)
            else:
                candles = await market.get_monthly_candles(ticker, months=fetch_count)
        except Exception as exc:
            logger.warning("[%s] %s 캔들 조회 실패: %s", ticker, period_label, exc)
            return None

        if not candles:
            logger.warning("[%s] %s 데이터 없음", ticker, period_label)
            return None

        candles_asc = list(reversed(candles))
        df = _build_period_indicator_frame(candles_asc, period_code)

        if not df.empty:
            last_row = df.iloc[-1]
            indicators_summary[period_code] = {
                "close": float(last_row.get("close", 0.0)),
                "ema_short": float(last_row.get("ema_short", 0.0)),
                "ema_long": float(last_row.get("ema_long", 0.0)),
                "ema_trend": float(last_row.get("ema_trend", 0.0)),
                "bb_upper": float(last_row.get("bb_upper", 0.0)),
                "bb_mid": float(last_row.get("bb_mid", 0.0)),
                "bb_lower": float(last_row.get("bb_lower", 0.0)),
                "rsi": float(last_row.get("rsi", 50.0)),
                "macd": float(last_row.get("macd", 0.0)),
                "macd_signal": float(last_row.get("macd_signal", 0.0)),
                "macd_hist": float(last_row.get("macd_hist", 0.0)),
            }

        # 충분한 데이터로 지표를 계산한 후, 플롯할 개수만큼만 잘라내어 차트를 그립니다.
        if plot_count > 0 and len(df) > plot_count:
            df = df.tail(plot_count).reset_index(drop=True)
            df["x"] = np.arange(len(df), dtype=float)

        ticker_dir = output_dir / ticker
        path = _render_period_chart(
            df, ticker, name, period_code, period_label, ticker_dir
        )
        paths[period_code] = path

        await asyncio.sleep(0.3)  # API 호출 간격

    return (paths, indicators_summary) if len(paths) == 3 else None


# ── LM Studio 호출 ───────────────────────────────────────────────────────────


async def _ask_to_picker(
    ticker: str,
    name: str,
    chart_paths: dict[str, Path],
    indicators_summary: dict[str, dict[str, float]],
    financial_ratios: list[dict[str, Any]],
    guideline: str = "",
) -> dict[str, Any]:
    """일봉/주봉/월봉 차트를 LM Studio에 전달하고 채점 결과를 반환한다."""
    ind_d = indicators_summary.get("D", {})
    ind_w = indicators_summary.get("W", {})
    ind_m = indicators_summary.get("M", {})

    core_anchor = (
        f"일봉:\n"
        f"  - 현재가: {int(ind_d.get('close', 0)):,}원\n"
        f"  - EMA: [5일: {ind_d.get('ema_short', 0):,.0f} / 20일: {ind_d.get('ema_long', 0):,.0f} / 60일: {ind_d.get('ema_trend', 0):,.0f}]\n"
        f"  - 볼린저밴드: [상단: {ind_d.get('bb_upper', 0):,.0f} / 기준선: {ind_d.get('bb_mid', 0):,.0f} / 하단: {ind_d.get('bb_lower', 0):,.0f}]\n"
        f"  - RSI: {ind_d.get('rsi', 50.0):.1f}\n"
        f"  - MACD: MACD={ind_d.get('macd', 0):+,.1f} / Signal={ind_d.get('macd_signal', 0):+,.1f} / Hist={ind_d.get('macd_hist', 0):+,.1f}\n\n"
        f"주봉:\n"
        f"  - EMA: [5주: {ind_w.get('ema_short', 0):,.0f} / 20주: {ind_w.get('ema_long', 0):,.0f} / 60주: {ind_w.get('ema_trend', 0):,.0f}]\n"
        f"  - RSI: {ind_w.get('rsi', 50.0):.1f}\n\n"
        f"월봉:\n"
        f"  - EMA: [5개월: {ind_m.get('ema_short', 0):,.0f} / 20개월: {ind_m.get('ema_long', 0):,.0f} / 60개월: {ind_m.get('ema_trend', 0):,.0f}]\n"
        f"  - RSI: {ind_m.get('rsi', 50.0):.1f}"
    )

    fin_text = ""
    if financial_ratios:
        fin_text += "[최근 재무 비율 (분기)]\n"
        for f in financial_ratios[:3]:
            yymm = f.get("stac_yymm", "")
            if len(yymm) == 6:
                yymm = f"{yymm[:4]}-{yymm[4:]}"

            def _fmt(val):
                if val is None or val == "":
                    return "N/A"
                try:
                    v = float(val)
                    if v.is_integer():
                        return f"{int(v):,}"
                    return f"{v:,.2f}"
                except ValueError:
                    return str(val)

            fin_text += (
                f"- 결산년월: {yymm}\n"
                f"  * ROE(자기자본이익률): {_fmt(f.get('roe_val'))}%\n"
                f"  * 부채비율: {_fmt(f.get('lblt_rate'))}%\n"
                f"  * 매출액증가율: {_fmt(f.get('grs'))}%\n"
                f"  * 영업이익증가율: {_fmt(f.get('bsop_prfi_inrt'))}%\n"
                f"  * 당기순이익증가율: {_fmt(f.get('ntin_inrt'))}%\n"
                f"  * EPS(주당순이익): {_fmt(f.get('eps'))}원\n"
                f"  * BPS(주당순자산): {_fmt(f.get('bps'))}원\n"
                f"  * 유보율: {_fmt(f.get('rsrv_rate'))}%\n"
            )
    else:
        fin_text = "[최근 재무 비율]\n데이터 없음\n"

    prompt_text = PICKER_PROMPT_TEMPLATE.format(
        ticker=ticker,
        name=name,
        core_anchor=core_anchor,
        financial_info=fin_text,
        guideline=guideline,
    )

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    for period_code, label in [("D", "일봉"), ("W", "주봉"), ("M", "월봉")]:
        path = chart_paths.get(period_code)
        if path is None or not path.exists():
            logger.error("[%s] 차트 파일 없음: %s", ticker, path)
            return {
                "score": 0,
                "timing": "",
                "trend": "",
                "news": "",
                "community": "",
                "score-reason": "",
                "timing-reason": "",
            }
        content.append({"type": "text", "text": f"다음은 {label} 차트입니다."})
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": _encode_image(path)},
            }
        )

    payload: dict[str, Any] = {
        "model": config.ARBITER_MODEL,
        "temperature": config.ARBITER_TEMPERATURE,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
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
                        "[%s] AI 서버 에러 응답 (HTTP %d): %s",
                        ticker,
                        resp.status,
                        err_body,
                    )
                resp.raise_for_status()
                data = await resp.json()
    except Exception as exc:
        err_msg = str(exc) or type(exc).__name__
        logger.error("[%s] AI 호출 실패: %s (%s)", ticker, err_msg, type(exc).__name__)
        return {
            "score": 0,
            "timing": "",
            "trend": "",
            "news": "",
            "community": "",
            "score-reason": f"{err_msg} ({type(exc).__name__})",
            "timing-reason": "",
        }

    try:
        choices = data.get("choices", [])
        if not choices:
            raise ValueError("choices 없음")

        finish_reason = choices[0].get("finish_reason", "")
        if finish_reason == "length":
            logger.warning(
                "[%s] ⚠ 경고: AI 응답이 최대 토큰 수 한도(max_tokens=%d)를 초과하여 중간에 잘렸습니다. "
                "일부 데이터는 자동 복구 로직을 통해 복원되었으나 근거 내용이 불완전할 수 있습니다. "
                "완전한 응답을 원하시면 config.py의 ARBITER_MAX_TOKENS 값을 더 늘리세요.",
                ticker,
                config.ARBITER_MAX_TOKENS,
            )

        message = choices[0].get("message", {})
        raw_text = message.get("content") or message.get("reasoning_content") or ""
        if isinstance(raw_text, list):
            raw_text = "\n".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in raw_text
            ).strip()
        parsed = _extract_json(raw_text)
        parsed["score"] = float(parsed.get("score", 0))
        parsed.setdefault("timing", "")
        parsed.setdefault("trend", "")
        parsed.setdefault("news", "")
        parsed.setdefault("community", "")
        parsed.setdefault("score-reason", "")
        parsed.setdefault("timing-reason", "")
        return parsed
    except Exception as exc:
        logger.error("[%s] 응답 파싱 실패: %s | raw=%s", ticker, exc, data)
        return {
            "score": 0,
            "timing": "",
            "trend": "",
            "news": "",
            "community": "",
            "score-reason": f"파싱 실패: {exc}",
            "timing-reason": "",
        }


# ── 메인 ─────────────────────────────────────────────────────────────────────


def _print_results(results: list[dict[str, Any]]) -> None:
    """점수 내림차순으로 결과를 출력한다."""
    ranked = sorted(results, key=lambda r: r["score"], reverse=True)

    for rank, r in enumerate(ranked, 1):
        print()
        print(
            f"{Fore.GREEN}{rank}등 {r['name']}({r['ticker']}) {r['score']}점 {r.get('trend', '')} 추세{Style.RESET_ALL}"
        )
        print(f"{r.get('score-reason', '')}")
        print(
            f"{Fore.YELLOW}타이밍{Style.RESET_ALL}:{Fore.BLUE}{r.get('timing', '')} 시점{Style.RESET_ALL}"
        )
        print(f"{r.get('timing-reason', '')}")
        print(f"{Fore.YELLOW}뉴스분석:{Style.RESET_ALL}")
        print(f"{r.get('news', '')}")
        print(f"{Fore.YELLOW}커뮤니티분석:{Style.RESET_ALL}")
        print(f"{r.get('community', '')}")


def _save_report(results: list[dict[str, Any]]) -> None:
    """분석 결과를 마크다운 리포트 파일로 저장한다."""
    from datetime import datetime

    today_str = datetime.now().strftime("%Y%m%d")

    report_dir = Path("reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    filepath = report_dir / f"report_{today_str}.md"

    ranked = sorted(results, key=lambda r: r["score"], reverse=True)

    lines = []
    lines.append(
        f"# 주식 스크리닝 리포트 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})\n"
    )

    for rank, r in enumerate(ranked, 1):
        lines.append(
            f"## {rank}등 {r['name']}({r['ticker']}) - {r['score']}점 ({r.get('trend', '')} 추세)"
        )
        lines.append(f"- **매수 매력도 근거**: {r.get('score-reason', '')}")
        lines.append(
            f"- **타이밍**: {r.get('timing', '')} 시점 ({r.get('timing-reason', '')})"
        )
        lines.append(f"- **뉴스 분석**: {r.get('news', '')}")
        lines.append(f"- **커뮤니티 분석**: {r.get('community', '')}")

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(
            f"\n{Fore.CYAN}리포트가 성공적으로 저장되었습니다: {filepath}{Style.RESET_ALL}"
        )
    except Exception as exc:
        logger.error("리포트 파일 저장 실패: %s", exc)


async def _run(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    market = _create_market()
    await market._auth_data.get_token()

    # watchlist 결정
    if args.name:
        matches = market.find_ticker_by_name(args.name)
        if not matches:
            print(
                f"오류: '{args.name}' 에 해당하는 종목을 찾을 수 없습니다.",
                file=sys.stderr,
            )
            sys.exit(1)
        if len(matches) == 1:
            ticker_code, ticker_name = matches[0]
            print(f"  종목 확인: {ticker_name} ({ticker_code})")
        else:
            print(f"'{args.name}' 검색 결과 {len(matches)}건:")
            for i, (code, nm) in enumerate(matches, 1):
                print(f"  {i:>3}. {nm} ({code})")
            try:
                sel = int(input("선택 번호를 입력하세요: ").strip())
                if not 1 <= sel <= len(matches):
                    raise ValueError
            except (ValueError, EOFError):
                print("올바른 번호를 입력하세요.", file=sys.stderr)
                sys.exit(1)
            ticker_code, ticker_name = matches[sel - 1]
        watchlist = [(ticker_code, ticker_name)]
    elif args.ticker:
        # --ticker 지정 시 watchlist 없이 바로 실행
        tickers = [t.strip() for t in args.ticker.split(",") if t.strip()]
        watchlist = [(t, t) for t in tickers]
    else:
        csv_path = Path(args.csv)
        if not csv_path.exists():
            print(f"오류: 파일을 찾을 수 없습니다 — {csv_path}", file=sys.stderr)
            sys.exit(1)
        watchlist = _parse_watchlist(csv_path)
        if not watchlist:
            print("오류: watchlist가 비어 있습니다.", file=sys.stderr)
            sys.exit(1)

    results: list[dict[str, Any]] = []

    for idx, (ticker, name) in enumerate(watchlist, 1):
        print(f"[{idx}/{len(watchlist)}] {ticker} {name} — 차트 생성 중...")

        result = await _build_period_charts(market, ticker, name, output_dir)
        if result is None:
            print(f"  ⚠ {ticker} 차트 생성 실패 — 건너뜀")
            continue
        chart_paths, indicators_summary = result

        print(f"  → 재무 비율 조회...")
        try:
            financial_ratios = await market.get_financial_ratio(ticker)
        except Exception as exc:
            logger.warning("[%s] 재무비율 조회 실패: %s", ticker, exc)
            financial_ratios = []

        print(f"  → 분석 요청...")
        decision = await _ask_to_picker(
            ticker,
            name,
            chart_paths,
            indicators_summary,
            financial_ratios,
            guideline=args.guideline,
        )
        decision["ticker"] = ticker
        decision["name"] = name
        results.append(decision)
        print(
            f"  score={decision['score']}  trend={decision['trend']}  "
            f"timing={decision['timing']}"
        )

        await asyncio.sleep(0.5)

    if not results:
        print("분석 결과가 없습니다.")
        return

    _print_results(results)
    _save_report(results)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="관심 종목 스크리닝 도구")
    parser.add_argument(
        "--csv",
        default="watchlist_stocks.csv",
        help="종목코드:종목명 형식의 CSV 파일 경로 (기본: watchlist_stocks.csv)",
    )
    parser.add_argument(
        "--output-dir",
        default="charts/periods",
        help="차트 저장 디렉터리 (기본: charts/periods)",
    )
    parser.add_argument(
        "--ticker",
        default=None,
        help="특정 종목만 실행 (쉼표로 복수 지정 가능). 예: 005930 또는 005930,000660",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="종목명으로 단일 종목 지정. 예: 삼성전자  (--ticker 대신 사용 가능)",
    )
    parser.add_argument(
        "--guideline",
        default="",
        help="AI 분석 시 적용할 추가 가이드라인 지침",
    )
    parser.add_argument("--debug", action="store_true", help="DEBUG 로그 출력")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
