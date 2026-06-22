"""
config.py — 환경 변수 및 전략 파라미터
이 파일의 값을 변경해 전략을 조정한다. 코드 내 하드코딩 금지.
"""

import os
from dotenv import load_dotenv

# .env 파일 로드 (환경 변수에 적용)
load_dotenv()

# ── 종목 설정 ──────────────────────────────────────
TICKER: str = os.environ.get("TICKER", "005930")  # 거래 종목 코드
MARKET: str = os.environ.get("MARKET", "KOSPI")  # KOSPI / KOSDAQ

# ── KIS API 인증 (하이브리드 지원) ──────────────────────
# 실시간 데이터(시세)는 항상 실투자 키를 사용하고, 거래는 환경에 맞게 사용합니다.
KIS_REAL_APP_KEY: str = os.environ.get(
    "KIS_REAL_APP_KEY", os.environ.get("KIS_APP_KEY", "")
)
KIS_REAL_APP_SECRET: str = os.environ.get(
    "KIS_REAL_APP_SECRET", os.environ.get("KIS_APP_SECRET", "")
)
KIS_REAL_ACCOUNT_NO: str = os.environ.get(
    "KIS_REAL_ACCOUNT_NO", os.environ.get("KIS_ACCOUNT_NO", "")
)

KIS_PAPER_APP_KEY: str = os.environ.get(
    "KIS_PAPER_APP_KEY", os.environ.get("KIS_APP_KEY", "")
)
KIS_PAPER_APP_SECRET: str = os.environ.get(
    "KIS_PAPER_APP_SECRET", os.environ.get("KIS_APP_SECRET", "")
)
KIS_PAPER_ACCOUNT_NO: str = os.environ.get(
    "KIS_PAPER_ACCOUNT_NO", os.environ.get("KIS_ACCOUNT_NO", "")
)

KIS_IS_PAPER: bool = (
    os.environ.get("KIS_IS_PAPER", "true").lower() != "false"
)  # True: 모의투자

# ── KIS API 엔드포인트 ───────────────────────────────
BASE_URL_REAL: str = "https://openapi.koreainvestment.com:9443"
BASE_URL_PAPER: str = "https://openapivts.koreainvestment.com:29443"

# ── 실행 주기 ────────────────────────────────────────
POSITION_CHECK_SEC: int = 10  # 포지션 모니터링 간격 (초)
POSITION_CHECK_FAST_SEC: int = int(
    os.environ.get("POSITION_CHECK_FAST_SEC", "3")
)  # 청산 기준선 근접 시 포지션 모니터링 간격 (초)
POSITION_CHECK_FAST_ATR_BUFFER: float = float(
    os.environ.get("POSITION_CHECK_FAST_ATR_BUFFER", "1.0")
)  # 현재가가 손절/익절/트레일링 기준선에서 1 ATR 이내면 빠른 주기로 전환
CANDLE_COUNT: int = int(
    os.environ.get("CANDLE_COUNT", "1000")
)  # 누적할 기술 지표 계산용 봉 개수 (5분봉/HTF 지표 안정화를 위해 1000개 권장)

# ── 봉 단위 설정 ─────────────────────────────────────
# 1: 1분봉 그대로 분석, 3: 1분봉을 3분봉으로 리샘플링하여 분석
# --interval 파라미터로 실행 시 오버라이드 가능
CANDLE_INTERVAL: int = int(os.environ.get("CANDLE_INTERVAL", "1"))

# ── 장 운영 시간 ─────────────────────────────────────
# 한국 주식 시장 시간대별 분봉 데이터 제공 현황:
#
# 시간대               구분                     캔들(1분봉)   비고
# 07:30~08:30         장전 시간외 종가매매        X            전일 종가 고정, 가격 변동 없음
# 08:30~09:00         장전 동시호가               X (미제공)   KIS FHKST03010230 실측 확인
# 09:00~15:30         정규장                      O            FHKST03010200 / FHKST03010230
# 15:30~16:00         장후 시간외 종가매매        X            당일 종가 고정, 가격 변동 없음
# 16:00~20:00         시간외 단일가               X (미제공)   KIS FHKST03010230 미지원 확인
#
# ※ FHKST03010230(fid_pw_data_incu_yn=Y) 실측 결과: 15:30 이후 데이터 없음.
#    시간외 분봉이 필요하다면 별도 API 엔드포인트 조사가 필요하다.
#
# 진입 가능 시작 시각 (장 시작 직후 갭 변동 회피-> +60분)
# 오전 8:00~9:00 매매정보는 가져올 수 없기 때문에 지난 정규장에 이어 갭으로 표시된다.
MARKET_OPEN_TIME: str = "09:30"
# 장 마감 시각 (HOLD_OVERNIGHT=False 시 미청산 포지션 강제 청산)
MARKET_CLOSE_TIME: str = "15:00"


# ── 오버나이트 및 종료 설정 ─────────────────────────────────────
# HOLD_OVERNIGHT: True이면 장 마감 시 청산하지 않고 포지션 유지 (오버나이트 허용)
#               False이면 기존 방식대로 장 마감 전 강제 청산
HOLD_OVERNIGHT: bool = os.environ.get("HOLD_OVERNIGHT", "true").lower() == "true"

# FORCE_CLOSE_ON_EXIT: True이면 프로그램 종료 시(Ctrl+C 등) 미청산 포지션을 강제 청산함.
#                    False이면 청산하지 않고 그대로 포지션을 유지함 (재시작 시 복구됨).
FORCE_CLOSE_ON_EXIT: bool = (
    os.environ.get("FORCE_CLOSE_ON_EXIT", "false").lower() == "true"
)

# ── 지표 계산 파라미터 ───────────────────────────────
RSI_PERIOD: int = int(os.environ.get("RSI_PERIOD", "14"))
BB_PERIOD: int = int(os.environ.get("BB_PERIOD", "20"))


EMA_SHORT: int = int(os.environ.get("EMA_SHORT", "5"))
EMA_LONG: int = int(os.environ.get("EMA_LONG", "20"))
EMA_TREND: int = int(os.environ.get("EMA_TREND", "60"))

# HTF(상위 프레임) 리샘플링 배수.
# CANDLE_INTERVAL × HTF_MULTIPLIER 분봉으로 리샘플링해 EMA_LONG 계산.
# 예) CANDLE_INTERVAL=1 → 5분봉, CANDLE_INTERVAL=3 → 15분봉.
HTF_MULTIPLIER: int = int(os.environ.get("HTF_MULTIPLIER", "5"))

# ── 리스크 관리 ──────────────────────────────────────
# 스톱로스 후 재진입 쿨다운 시간 (분). 손절 발생 후 이 시간이 경과할 때까지 신규 진입을 차단한다.
STOP_LOSS_COOLDOWN_MINUTES: int = int(
    os.environ.get("STOP_LOSS_COOLDOWN_MINUTES", "20")
)

# ── 속도 기반 긴급 손절 (Flash Crash 대응) ───────────────
# 포지션 모니터 루프에서 매 틱(10초)마다 수집한 가격으로 최근 구간의
# 고점 대비 현재가 낙폭을 계산한다. 단순 시작점-끝점 비교보다 계단식 하락에 강하다.
# 손절가 도달 전이라도 급락이 확인되면 선제 청산한다.
VELOCITY_STOP_ENABLED: bool = (
    os.environ.get("VELOCITY_STOP_ENABLED", "true").lower() == "true"
)
# 속도 측정 윈도우 (초). 이 구간 안에서 최근 고점 대비 현재가 낙폭을 측정한다.
VELOCITY_STOP_WINDOW_SEC: int = int(os.environ.get("VELOCITY_STOP_WINDOW_SEC", "30"))
# VELOCITY_STOP 비활성화 시각. 정규장 마감 전 시간외 전환 구간(15:20~15:30)에서
# API가 마지막 정규가를 반환하다가 한 번에 새 가격을 내려보내면 30초 윈도우 내
# 급락처럼 보여 오발동할 수 있다. 이 시각 이후에는 VELOCITY_STOP을 비활성화한다.
# 정규장은 15:30에 끝나지만, KIS API가 마지막 호가를 내려보내는 타이밍이 15:20~15:30 으로 불규칙하다.
# 10분 여유를 두면 실제 장중 Flash Crash는 전부 감지하면서 전환 구간 오발동은 차단한다
VELOCITY_STOP_CUTOFF_TIME: str = os.environ.get("VELOCITY_STOP_CUTOFF_TIME", "15:20")
# 윈도우 내 낙폭이 ATR × 이 배수를 초과하면 즉시 청산.
# 고정 비율(%) 대신 ATR 기준을 사용해 변동성이 클 때는 임계값이 자동으로 넓어지고
# 조용한 장에서는 좁아지도록 한다.
# 예) 3.0 → 30초 내 최근 고점 대비 ATR×3 이상 급락 시 선제 청산
VELOCITY_STOP_ATR_MULT: float = float(os.environ.get("VELOCITY_STOP_ATR_MULT", "3.0"))
# 급락 판단에 필요한 최소 틱 수. 너무 적은 샘플의 우연한 점프는 무시한다.
VELOCITY_STOP_MIN_TICKS: int = int(os.environ.get("VELOCITY_STOP_MIN_TICKS", "4"))
# 최근 윈도우 내 가격 이동 중 하락 틱 비율이 이 값 이상일 때만 급락으로 인정한다.
# 예) 0.67 → 최근 움직임의 67% 이상이 하락일 때만 발동
VELOCITY_STOP_DOWN_TICK_RATIO: float = float(
    os.environ.get("VELOCITY_STOP_DOWN_TICK_RATIO", "0.67")
)

# ── 연속 음봉 선제 청산 (Band Walk 대응) ─────────────────
# 최근 N봉이 모두 일정 몸통 비율 이상의 음봉이면 강한 하락 추세로 판단해 선제 청산한다.
CONSEC_BEARISH_STOP_ENABLED: bool = (
    os.environ.get("CONSEC_BEARISH_STOP_ENABLED", "true").lower() == "true"
)
# 선제 청산을 발동하는 연속 음봉 최소 개수
CONSEC_BEARISH_COUNT: int = int(os.environ.get("CONSEC_BEARISH_COUNT", "3"))
# 각 음봉의 몸통 비율 하한 (전체 고저 범위 대비). 꼬리 긴 음봉은 제외.

CONSEC_BEARISH_BODY_RATIO: float = float(
    os.environ.get("CONSEC_BEARISH_BODY_RATIO", "0.5")
)

SINGLE_TRADE_RATIO: float = float(
    os.environ.get("SINGLE_TRADE_RATIO", "0.3")
)  # 1회 진입 매수 비율
MAX_POSITION_RATIO: float = float(
    os.environ.get("MAX_POSITION_RATIO", "0.9")
)  # 총 최대 보유 비율
STOP_LOSS_RATIO: float = float(
    os.environ.get("STOP_LOSS_RATIO", "0.05")
)  # 손절 비율 (5%)
TAKE_PROFIT_RATIO: float = float(
    os.environ.get("TAKE_PROFIT_RATIO", "0.08")
)  # 익절 비율 (8%)
TRAILING_STOP_RATIO: float = float(
    os.environ.get("TRAILING_STOP_RATIO", "0.015")
)  # 트레일링 스탑 비율 (1.5%)
MAX_DAILY_LOSS_RATIO: float = float(
    os.environ.get("MAX_DAILY_LOSS_RATIO", "0.05")
)  # 일일 최대 손실 한도 (5%)
MAX_TRADES_PER_DAY: int = int(
    os.environ.get("MAX_TRADES_PER_DAY", "20")
)  # 일일 최대 거래 횟수

# ── ATR 변동성 기반 동적 손/익절 설정 ──────────────────
# 동적 손익절 일체를 사용하지 않고, 고정 비율을 적용한다.
USE_ATR_STOP: bool = os.environ.get("USE_ATR_STOP", "true").lower() == "true"
ATR_SL_MULTIPLIER: float = float(os.environ.get("ATR_SL_MULTIPLIER", "5.0"))
# 손절 = 진입가 - (ATR × ATR_SL_MULTIPLIER).
# 가드레일: 최소 MIN_SL_RATIO(0.5%).
ATR_TP_MULTIPLIER: float = float(os.environ.get("ATR_TP_MULTIPLIER", "5.0"))
# 익절 = 진입가 + (ATR × ATR_TP_MULTIPLIER).
# ×3 설정 배경: TP 도달 빈도를 높여 트레일링 스탑 전환 기회를 확보.
#   TP×4 대비 TP 근접 실패 빈도가 낮아지고, 트레일링으로 추가 수익도 유지 가능.
#   수익성이 개선되면 ×4~×5로 상향 검토.

# ATR 기반 SL 최소 하한선 (진입가 대비 비율).
# 저변동성 구간에서 ATR이 극도로 작아지면 SL이 호가 노이즈 수준(0.1% 등)으로
# 설정되어 진입 직후 손절되는 문제를 방지.
# 예) 0.008 → SL이 최소 진입가의 0.8% 이상 떨어져야 함.
MIN_SL_RATIO: float = float(os.environ.get("MIN_SL_RATIO", "0.008"))

# ATR 기반 TP 최소 하한선 (진입가 대비 비율).
# 익절 역시 노이즈 수준(예: 0.2%)으로 너무 일찍 청산되는 것을 방지.
# 예) 0.015 → TP가 최소 진입가의 1.5% 이상 올라야 함.
MIN_TP_RATIO: float = float(os.environ.get("MIN_TP_RATIO", "0.015"))

# ── 시장 환경 필터 임계값 ────────────────────────────
# 지수 폭락 체크는 주식 시장 안정성에 대한 중요한 방어 수단이지만,
# 저점매수 등을 통해 변동성을 먹는 것이 목적인 만큼, 시장 전체의 폭락으로 매수 기회를 차단하는 것은 타당하지 않다.
# 최저점에서 거래 중단은 비합리적.
# 주석 처리하여 더 넓은 매수 범위를 허용한다.
# MARKET_DROP_THRESHOLD: float = float(
#     os.environ.get("MARKET_DROP_THRESHOLD", "-5.0")
# )  # 지수 폭락 기준 (%)

# ── 수수료 설정 ──────────────────────────────────────
# 매수/매도 양방향 브로커 수수료율 (한국투자증권 기본 0.015%)
BROKER_FEE_RATE: float = float(os.environ.get("BROKER_FEE_RATE", "0.00015"))
# 매도 시에만 부과되는 거래세율 (KOSPI: 0.20%, KOSDAQ: 0.20% — 2025년 기준)
TRANSACTION_TAX_RATE: float = float(os.environ.get("TRANSACTION_TAX_RATE", "0.0020"))

# ── 로그 설정 ────────────────────────────────────────
LOG_DIR: str = os.environ.get("LOG_DIR", "logs")
LOG_FILE: str = os.environ.get("LOG_FILE", "trades.jsonl")  # JSON Lines 형식

# ── 종목 코드 파일 경로 ─────────────────────────────
STOCK_CODE_DIR: str = os.environ.get(
    "STOCK_CODE_DIR", "."
)  # kospi_code.mst, kosdaq_code.mst 위치

# ── Arbiter 파라미터 (LM Studio) ────────────────────────────────
ARBITER_BASE_URL: str = os.environ.get("ARBITER_BASE_URL", None)

# QWEN(Alibaba Cloud Model Studio)
# OPENAI_API_KEY: str = os.environ.get("ALIBABA_API_KEY", None)

# 🧩 Qwen 3.6의 '눈(Vision)'이 작동하는 방식:
# 1. 원본 비율 및 픽셀의 무조건 보존
#   - Qwen 3.6은 입력된 이미지를 억지로 정사각형으로 구겨 넣지 않는다.
#     1920x1080 해상도가 들어오면 그 16:9 가로 세로 비율을 그대로 유지한 상태에서 내부적으로 필요한 만큼 조각(Patch)을 낸다.
#   - 이 때문에 주식 차트에서 가장 중요한 시간축(가로)과 가격축(세로)의 왜곡이 전혀 일어나지 않는다.
# 2. 컨텍스트 윈도우의 체급 차이
#   - Gemma 4는 이미지 토큰을 1120개로 묶어둔 반면, Qwen 3.6 시리즈는 모델에 따라 최소 26만 토큰에서 최신 Plus/Flash 모델의 경우 최대 100만(1M) 토큰 콘텍스트를 지원합니다.이미지 하나가 정밀하게 쪼개져 4,000~8,000개의 비전 토큰을 소모하더라도, Qwen에게는 전체 콘텍스트 허용량의 1%도 되지 않는 사소한 크기입니다. 구글처럼 서버 비용 때문에 이미지 토큰을 억지로 숨통을 틀어막을 필요가 없는 구조입니다.자동 최적화 (vLLM 및 LM Studio)Qwen 3.6 비전 모델을 API나 LM Studio로 호출할 때는 개발자가 헤드 가이드를 위해 무언가를 지정할 필요가 없습니다. 모델과 엔진(qwen_vl_utils)이 이미지 해상도를 판단하여 글자가 읽히는 최적의 토큰 수로 자동 분할(Dynamic Resolution)해 연산합니다.

# ARBITER_MODEL: str = os.environ.get("ARBITER_MODEL", "qwen/qwen3.6-27b")
ARBITER_MODEL: str = os.environ.get("ARBITER_MODEL", "qwen/qwen3.6-35b-a3b")

# 고해상도 차트 이미지의 정확한 해석은 qwen이 월등히 나은 것으로 판단함
# ⭐ LM Studio 설정 창을 건드리지 않고, 호출할 때만 고해상도를 켜는 핵심 옵션
# "max_tokens": 4096,
# "extra_body": {
#     "image_token_budget": 1120
# }
# 위와 같이 payload에 추가 옵션을 주어도 결국 qwen이 우수함. 왜냐하면:
# 1. qwen은 처음부터 멀티모달로 학습된 LLM, gemma4는 텍스트로 학습 후 vision 학습을 추가
# 2. qwen은 이미지를 토큰화하는 과정에서 분할 후 해석해 압축 과정이 없지만,
#    gemma4는 정사각형 이미지로 일단 압축해 해석함.
#    image_token_budge을 늘리면 인식 성능이 개선되지만 여전히 qwen을 넘어서진 못함.
# 3. gemma4는 이미지의 정교한 인식보다는 전체적인 스타일, 분류 등에 유리함
# 4. 결과로, 주식 차트엔 qwen 3.6
#
# 1920x1080 차트 같은 고해상도 이미지는 최소 2,000~4,000개의 비전 토큰으로 쪼개서 봐야
# 선과 글씨가 안 깨진다.
# Gemma 4 27B는 최대 한도가 1120개라, 1920x1080 이미지를 강제로 압축(Downsampling)하여 집어넣는다.
# 이 때문에 체급이 무색하게 캔들의 미세한 꼬리가 뭉개지는 현상이 발생한다.
#
# ARBITER_MODEL: str = os.environ.get("ARBITER_MODEL", "google/gemma-4-26b-a4b")
# ARBITER_MODEL: str = os.environ.get("ARBITER_MODEL", "google/gemma-4-31b")

ARBITER_MAX_TOKENS: int = int(
    os.environ.get("ARBITER_MAX_TOKENS", "-1")
)  # -1이면 최대값, 16384 -> 65536
ARBITER_TEMPERATURE: float = float(os.environ.get("ARBITER_TEMPERATURE", "0.1"))
ARBITER_TIMEOUT_SEC: int = int(os.environ.get("ARBITER_TIMEOUT_SEC", "120"))
ARBITER_MIN_CONFIDENCE: int = int(os.environ.get("ARBITER_MIN_CONFIDENCE", "60"))
