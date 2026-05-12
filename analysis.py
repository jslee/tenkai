"""analysis.py — 단일 종목 심층 분석 도구.

일봉/주봉/월봉 차트를 생성하고 AI에 심층 분석을 요청한다.
picker.py가 다수 종목을 빠르게 스크리닝하는 용도라면,
analysis.py는 특정 종목 한 개를 집중적으로 분석하는 용도이다.

실행 예:
    python analysis.py --name 삼성전자
    python analysis.py --ticker 005930
    python analysis.py --ticker 005930 --count 200 --output-dir charts/analysis
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from colorama import Fore, Style, init as _colorama_init

_colorama_init(autoreset=True)

import config
from kis_api import KISAuth, KISMarket
from picker import (
    _ask_lm_studio as _picker_ask,
    _build_period_charts,
    _encode_image,
    _extract_json,
    _create_market,
    SYSTEM_PROMPT,
)
import aiohttp
import numpy as np

logger = logging.getLogger(__name__)

ANALYSIS_PROMPT_TEMPLATE = """당신은 한국 주식 전문 애널리스트입니다. 아래 종목에 대해 심층 분석 보고서를 작성하십시오.

[종목 정보]
- 종목코드: {ticker}
- 종목명:   {name}
- 첨부 차트: 일봉 / 주봉 / 월봉 (순서대로 3장)
  * 포함 지표: 캔들, 볼린저밴드, RSI, 거래량, MACD, EMA(5, 20, 60)

[분석 요구사항]
1. 멀티 타임프레임 추세 분석
   - 월봉: 장기 추세 방향과 현재 사이클 위치
   - 주봉: 중기 추세 및 주요 지지/저항 레벨
   - 일봉: 단기 모멘텀, 최근 5거래일 패턴 (눌림목 vs 추세 전환 판별)

2. 기술적 지표 분석
   - RSI: 과매수/과매도 여부, 다이버전스 포착
   - MACD: 골든/데드크로스, 히스토그램 방향
   - 볼린저밴드: 수축/확장 상태, 밴드 터치 신호
   - EMA: 정배열/역배열, 지지/저항 역할

3. 거래량 분석
   - 가격 상승 시 거래량 증가 여부 (상승 신뢰도)
   - 하락 시 거래량 감소 여부 (눌림목 vs 이탈 판별)
   - 최근 거래량 이상 급증/급감 감지

4. 매매 전략
   - 현재 구간 진단: 눌림목/돌파 직전/과열/침체 중 하나
   - 매수 타이밍: 즉시/눌림목 대기/돌파 대기/관망 중 하나
   - 매수 진입 조건 (어떤 조건이 충족되면 진입할 것인가)
   - 손절 기준가 (기술적 근거 명시)
   - 목표가 1차 / 2차

5. 최신 뉴스 및 이벤트
   - 웹 검색을 통해 최신 공시, 실적, 테마, 업종 이슈를 조사한다.
   - 뉴스가 차트 흐름과 일치하는지 또는 괴리가 있는지 판단한다.

6. 종합 의견
   - 매수 매력도 점수 (0~100)
   - 핵심 투자 포인트 1~2가지
   - 주요 리스크 요인 1~2가지

[출력 형식]
반드시 아래 JSON 하나만 반환하라. 다른 설명은 생략한다.

주의: 입력 데이터에 명시되지 않은 구체적인 가격이나 수치를 임의로 생성하지 않는다. 
가격을 언급해야 할 경우, 'EMA 60선 부근' 또는 '직전 저점 수준'과 같이 차트상의 기술적 지표나 위치를 기준으로 설명하라.

{{
    "score": 0~100,
    "timing": "즉시" | "눌림목 대기" | "돌파 대기" | "관망",
    "trend": "강한 상승" | "상승" | "횡보" | "하락" | "강한 하락",
    "timeframe": {{
        "monthly": "월봉 추세 요약",
        "weekly": "주봉 추세 요약",
        "daily": "일봉 최근 패턴 요약 (눌림목 vs 전환 판별 포함)"
    }},
    "indicators": {{
        "rsi": "RSI 상태 및 다이버전스 여부",
        "macd": "MACD 상태",
        "bb": "볼린저밴드 상태",
        "ema": "EMA 배열 상태"
    }},
    "volume": "거래량 분석 요약",
    "strategy": {{
        "current_zone": "눌림목" | "돌파 직전" | "과열" | "침체" | "횡보",
        "entry_condition": "진입 조건 설명",
        "stop_loss": "손절 기준가 또는 기준 설명",
        "target1": "1차 목표가 또는 기준 설명",
        "target2": "2차 목표가 또는 기준 설명"
    }},
    "news": "최신 뉴스 및 이벤트 요약과 차트와의 상관관계",
    "score-reason": "매수 매력도 점수 산정 핵심 근거",
    "timing-reason": "타이밍 판단 근거 및 권고사항",
    "pros": ["투자 포인트 1", "투자 포인트 2"],
    "risks": ["리스크 요인 1", "리스크 요인 2"]
}}"""


async def _ask_analysis(
    ticker: str,
    name: str,
    chart_paths: dict[str, Path],
) -> dict[str, Any]:
    """심층 분석 프롬프트로 LM Studio에 요청한다."""
    prompt_text = ANALYSIS_PROMPT_TEMPLATE.format(ticker=ticker, name=name)

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    for period_code, label in [("D", "일봉"), ("W", "주봉"), ("M", "월봉")]:
        path = chart_paths.get(period_code)
        if path is None or not path.exists():
            logger.error("[%s] 차트 파일 없음: %s", ticker, path)
            return {"score": 0, "score-reason": "차트 없음"}
        content.append({"type": "text", "text": f"다음은 {label} 차트입니다."})
        content.append({"type": "image_url", "image_url": {"url": _encode_image(path)}})

    payload = {
        "model": config.ARBITER_MODEL,
        "max_tokens": max(config.ARBITER_MAX_TOKENS, 2048),
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
    except Exception as exc:
        logger.error("[%s] AI 호출 실패: %s", ticker, exc)
        return {"score": 0, "score-reason": str(exc)}

    try:
        choices = data.get("choices", [])
        if not choices:
            raise ValueError("choices 없음")
        message = choices[0].get("message", {})
        raw_text = message.get("content") or message.get("reasoning_content") or ""
        if isinstance(raw_text, list):
            raw_text = "\n".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in raw_text
            ).strip()
        parsed = _extract_json(raw_text)
        parsed["score"] = int(parsed.get("score", 0))
        return parsed
    except Exception as exc:
        logger.error("[%s] 응답 파싱 실패: %s", ticker, exc)
        return {"score": 0, "score-reason": f"파싱 실패: {exc}"}


def _print_report(ticker: str, name: str, r: dict[str, Any]) -> None:
    score = r.get("score", 0)
    score_color = Fore.RED if score >= 70 else Fore.YELLOW if score >= 40 else Fore.BLUE
    trend_color_map = {
        "강한 상승": Fore.RED,
        "상승": Fore.LIGHTRED_EX,
        "횡보": Fore.YELLOW,
        "하락": Fore.LIGHTBLUE_EX,
        "강한 하락": Fore.BLUE,
    }
    trend_color = trend_color_map.get(r.get("trend", ""), Fore.WHITE)

    sep = "=" * 64

    print()
    print(sep)
    print(
        f"  {Style.BRIGHT}{name}({ticker}){Style.RESET_ALL}"
        f"  매력도 {score_color}{Style.BRIGHT}{score}점{Style.RESET_ALL}"
        f"  {trend_color}{r.get('trend', '')}{Style.RESET_ALL} 추세"
        f"  {Fore.CYAN}{r.get('timing', '')}{Style.RESET_ALL} 타이밍"
    )
    print(sep)

    # 멀티 타임프레임
    tf = r.get("timeframe", {})
    if tf:
        print(f"\n{Fore.YELLOW}[ 타임프레임 분석 ]{Style.RESET_ALL}")
        print(f"  월봉: {tf.get('monthly', '')}")
        print(f"  주봉: {tf.get('weekly', '')}")
        print(f"  일봉: {tf.get('daily', '')}")

    # 기술적 지표
    ind = r.get("indicators", {})
    if ind:
        print(f"\n{Fore.YELLOW}[ 기술적 지표 ]{Style.RESET_ALL}")
        print(f"  RSI  : {ind.get('rsi', '')}")
        print(f"  MACD : {ind.get('macd', '')}")
        print(f"  BB   : {ind.get('bb', '')}")
        print(f"  EMA  : {ind.get('ema', '')}")

    # 거래량
    if r.get("volume"):
        print(f"\n{Fore.YELLOW}[ 거래량 ]{Style.RESET_ALL}")
        print(f"  {r['volume']}")

    # 매매 전략
    st = r.get("strategy", {})
    if st:
        print(f"\n{Fore.YELLOW}[ 매매 전략 ]{Style.RESET_ALL}")
        print(
            f"  현재 구간  : {Fore.CYAN}{st.get('current_zone', '')}{Style.RESET_ALL}"
        )
        print(f"  진입 조건  : {st.get('entry_condition', '')}")
        print(f"  손절 기준  : {Fore.BLUE}{st.get('stop_loss', '')}{Style.RESET_ALL}")
        print(f"  목표가 1차 : {Fore.RED}{st.get('target1', '')}{Style.RESET_ALL}")
        print(f"  목표가 2차 : {Fore.RED}{st.get('target2', '')}{Style.RESET_ALL}")

    # 점수 근거 / 타이밍 근거
    print(f"\n{Fore.YELLOW}[ 점수 산정 근거 ]{Style.RESET_ALL}")
    print(f"  {r.get('score-reason', '')}")
    print(f"\n{Fore.YELLOW}[ 타이밍 근거 및 권고 ]{Style.RESET_ALL}")
    print(f"  {r.get('timing-reason', '')}")

    # 뉴스
    if r.get("news"):
        print(f"\n{Fore.YELLOW}[ 최신 뉴스 / 이벤트 ]{Style.RESET_ALL}")
        print(f"  {r['news']}")

    # 투자 포인트 / 리스크
    pros = r.get("pros", [])
    risks = r.get("risks", [])
    if pros:
        print(f"\n{Fore.GREEN}[ 투자 포인트 ]{Style.RESET_ALL}")
        for p in pros:
            print(f"  + {p}")
    if risks:
        print(f"\n{Fore.RED}[ 리스크 요인 ]{Style.RESET_ALL}")
        for rk in risks:
            print(f"  - {rk}")

    print()
    print(sep)
    print()


async def _run(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    market = _create_market()
    await market._auth_data.get_token()

    # --name 으로 종목 역조회
    if args.name:
        matches = market.find_ticker_by_name(args.name)
        if not matches:
            print(
                f"오류: '{args.name}' 에 해당하는 종목을 찾을 수 없습니다.",
                file=sys.stderr,
            )
            sys.exit(1)
        if len(matches) == 1:
            ticker, name = matches[0]
            print(f"  종목 확인: {name} ({ticker})")
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
            ticker, name = matches[sel - 1]
    else:
        ticker = args.ticker.strip()  # type: ignore[union-attr]
        name = market.get_stock_name(ticker) or ticker
    print(f"\n{ticker} {name} — 차트 생성 중...")

    chart_paths = await _build_period_charts(
        market, ticker, name, output_dir, args.count
    )
    if chart_paths is None:
        print(f"오류: {ticker} 차트 생성 실패", file=sys.stderr)
        sys.exit(1)

    print("  → AI 심층 분석 요청 중...")
    result = await _ask_analysis(ticker, name, chart_paths)

    _print_report(ticker, name, result)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="단일 종목 심층 분석 도구")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--ticker",
        help="분석할 종목 코드. 예: 005930",
    )
    group.add_argument(
        "--name",
        help="분석할 종목명 키워드. 예: 삼성전자",
    )
    parser.add_argument(
        "--output-dir",
        default="charts/periods",
        help="차트 저장 디렉터리 (기본: charts/periods)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=120,
        help="차트에 표시할 최대 봉 수 (기본: 120)",
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
