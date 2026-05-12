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

SYSTEM_PROMPT = """Follow these instructions strictly:
- Do NOT output thinking or reasoning steps
- Provide direct, concise answers only
- Skip any internal monologue or thought process
- Return only the final answer requested by the user
"""

PICKER_PROMPT_TEMPLATE = """당신은 한국 주식 단기 스윙 트레이딩을 평가하는 '포트폴리오 스크리닝 에이전트'입니다.

[입력 데이터]
- 종목코드: {ticker}
- 종목명:   {name}
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

[출력 형식]
반드시 아래 JSON 하나만 반환하라. 다른 설명은 생략한다.

{{
    "score": 0~100,
    "timing": 즉시|눌림목 대기|돌파 대기|관망,
    "trend": 강한 상승|상승|횡보|하락|강한 하락,
    "news": 검색된 뉴스의 핵심 내용과 차트와의 상관관계를 상세히 설명,
    "score-reason": 매수 매력도를 결정한 핵심 근거 한 문장,
    "timing-reason": 타이밍 판단 근거 및 타이밍에 대비하는 권고사항,
}}"""


# ── 유틸 ─────────────────────────────────────────────────────────────────────


def _encode_image(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _extract_json(text: str) -> dict:
    text = re.sub(r"```(?:json)?", "", text).strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("JSON 블록 없음")
    return json.loads(text[start:end])


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
    plot_count: int,
) -> dict[str, Path] | None:
    """일봉/주봉/월봉 차트 PNG를 생성하고 {D/W/M: path} 를 반환한다.
    데이터를 가져오지 못하면 None을 반환한다.
    """
    import pandas as pd

    period_configs = [
        ("D", "일봉", 200),
        ("W", "주봉", 100),
        ("M", "월봉", 60),
    ]

    paths: dict[str, Path] = {}
    for period_code, period_label, default_count in period_configs:
        try:
            if period_code == "D":
                candles = await market.get_daily_candles(ticker, days=default_count)
            elif period_code == "W":
                candles = await market.get_weekly_candles(ticker, weeks=default_count)
            else:
                candles = await market.get_monthly_candles(ticker, months=default_count)
        except Exception as exc:
            logger.warning("[%s] %s 캔들 조회 실패: %s", ticker, period_label, exc)
            return None

        if not candles:
            logger.warning("[%s] %s 데이터 없음", ticker, period_label)
            return None

        candles_asc = list(reversed(candles))
        df = _build_period_indicator_frame(candles_asc, period_code)
        if plot_count > 0:
            df = df.tail(plot_count).reset_index(drop=True)
            df["x"] = np.arange(len(df), dtype=float)

        ticker_dir = output_dir / ticker
        path = _render_period_chart(
            df, ticker, name, period_code, period_label, ticker_dir
        )
        paths[period_code] = path

        await asyncio.sleep(0.3)  # API 호출 간격

    return paths if len(paths) == 3 else None


# ── LM Studio 호출 ───────────────────────────────────────────────────────────


async def _ask_lm_studio(
    ticker: str,
    name: str,
    chart_paths: dict[str, Path],
) -> dict[str, Any]:
    """일봉/주봉/월봉 차트를 LM Studio에 전달하고 채점 결과를 반환한다."""
    prompt_text = PICKER_PROMPT_TEMPLATE.format(ticker=ticker, name=name)

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
    except Exception as exc:
        logger.error("[%s] AI 호출 실패: %s", ticker, exc)
        return {
            "score": 0,
            "timing": "",
            "trend": "",
            "news": "",
            "score-reason": str(exc),
            "timing-reason": "",
        }

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
        parsed.setdefault("timing", "")
        parsed.setdefault("trend", "")
        parsed.setdefault("news", "")
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
            f"{Fore.GREEN}{rank}등 {r['name']}({r['ticker']}) {r['score']}점 {r['trend']} 추세{Style.RESET_ALL}"
        )
        print(f"{r['score-reason']}")
        print(
            f"{Fore.YELLOW}타이밍{Style.RESET_ALL}:{Fore.BLUE}{r['timing']} 시점{Style.RESET_ALL}"
        )
        print(f"{r['timing-reason']}")
        print(f"{Fore.YELLOW}뉴스분석:{Style.RESET_ALL}")
        print(f"{r['news']}")


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

        chart_paths = await _build_period_charts(
            market, ticker, name, output_dir, args.count
        )
        if chart_paths is None:
            print(f"  ⚠ {ticker} 차트 생성 실패 — 건너뜀")
            continue

        print(f"  → 분석 요청...")
        decision = await _ask_lm_studio(ticker, name, chart_paths)
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
        "--count",
        type=int,
        default=120,
        help="차트에 표시할 최대 봉 수 (기본: 120)",
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
