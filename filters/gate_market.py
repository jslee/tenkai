"""
filters/gate_market.py — 시장 환경 필터 (gate)

원칙: 룰 기반, 빠르게 판단, KIS API 호출 최소화
불확실한 상황에서는 항상 passed=False (거래 안 함)

체크 항목:
- 코스피 지수 변동: MARKET_DROP_THRESHOLD 초과 폭락 시 기각
- 거래 활성: 당일 누적 거래량 > 0 (거래 정지 여부)
- 서킷브레이커 발동 여부
- 당일 손실 한도 초과 여부

거래 시간 체크는 run_cycle() 진입 전에 수행한다.
"""

import logging

import config

logger = logging.getLogger(__name__)

# 연속된 동일 기각 사유의 중복 로깅 방지용 상태 변수
_last_gate_fail_reason = None


def gate_market_filter(market_data: dict) -> tuple[bool, dict]:
    """
    시장 환경 필터를 실행한다.

    Args:
        market_data: {
            "market_change": float,             # 지수 등락률 (%)
            "current_volume": int,              # 당일 누적 거래량
            "circuit_breaker": bool,            # 서킷브레이커 발동 여부
            "daily_loss_ratio": float,          # 당일 누적 손실률 (양수=손실)
        }

    Returns:
        (passed: bool, result: dict)
        result에 실패 이유 또는 통과 내역 포함
    """
    global _last_gate_fail_reason

    checks: list[dict] = []
    passed = True
    halt_trading_today = False

    # ── 1. 지수 폭락 체크 ──────────────────────────────────────
    # 지수 폭락 체크는 주식 시장 안정성에 대한 중요한 방어 수단이지만,
    # 변동성을 먹는 것이 목적인 만큼, 시장 전체의 폭락으로 매수 기회를 차단하는 것은 타당하지 않다.
    # 최저점에서 거래 중단은 비합리적.
    # 주석 처리하여 더 넓은 매수 범위를 허용한다.
    #
    # market_change: float = market_data.get("market_change", 0.0)
    # market_name = config.MARKET.upper()
    # if market_change <= config.MARKET_DROP_THRESHOLD:
    #     checks.append(
    #         {
    #             "check": "market_change",
    #             "passed": False,
    #             "detail": f"{market_name} 폭락 ({market_change:.2f}% <= {config.MARKET_DROP_THRESHOLD}%)",
    #         }
    #     )
    #     passed = False
    # else:
    #     checks.append(
    #         {
    #             "check": "market_change",
    #             "passed": True,
    #             "detail": f"{market_name} 정상 ({market_change:.2f}%)",
    #         }
    #     )

    # ── 3. 거래량 확인 (거래 활성 여부) ──────────────────────────────────
    # 누적 거래량 vs 20일 평균 비교는 일중 분포(U자 패턴)로 인해 시간대별 편향이 크다.
    # Gate 2에서 캔들 단위 거래량 급증을 정밀하게 분석하므로,
    # gate는 "이 종목이 오늘 거래가 살아있는지"만 확인한다.
    current_volume: int = market_data.get("current_volume", 0)
    if current_volume <= 0:
        checks.append(
            {
                "check": "volume",
                "passed": False,
                "detail": "거래량 없음 (거래 정지 상태)",
            }
        )
        passed = False
    else:
        checks.append(
            {
                "check": "volume",
                "passed": True,
                "detail": f"거래 활성 (누적 {current_volume:,}주)",
            }
        )

    # ── 4. 서킷브레이커 확인 ─────────────────────────────────────────────
    circuit_breaker: bool = market_data.get("circuit_breaker", False)
    if circuit_breaker:
        checks.append(
            {
                "check": "circuit_breaker",
                "passed": False,
                "detail": "서킷브레이커 발동 중",
            }
        )
        passed = False
    else:
        checks.append({"check": "circuit_breaker", "passed": True, "detail": "미발동"})

    # ── 5. 당일 손실 한도 확인 ────────────────────────────────────────────
    daily_loss_ratio: float = market_data.get("daily_loss_ratio", 0.0)
    if daily_loss_ratio >= config.MAX_DAILY_LOSS_RATIO:
        checks.append(
            {
                "check": "daily_loss_limit",
                "passed": False,
                "detail": f"당일 손실 한도 초과 ({daily_loss_ratio:.2%} >= {config.MAX_DAILY_LOSS_RATIO:.0%})",
            }
        )
        passed = False
        halt_trading_today = True
    else:
        checks.append(
            {
                "check": "daily_loss_limit",
                "passed": True,
                "detail": f"당일 손실 {daily_loss_ratio:.2%}",
            }
        )

    result = {
        "passed": passed,
        "halt_trading_today": halt_trading_today,
        "checks": checks,
    }

    if not passed:
        failed_checks = [c for c in checks if not c["passed"]]
        fail_reason = " | ".join(c["detail"] for c in failed_checks)
        if fail_reason != _last_gate_fail_reason:
            logger.info("[Gate] 기각 — %s", fail_reason)
            _last_gate_fail_reason = fail_reason
    else:
        _last_gate_fail_reason = None
        logger.debug("[Gate] 통과")

    return passed, result
