# Tenkai — KIS API 기반 AI 자동매매 시스템

Local AI와 한국투자증권 KIS API를 연결하는 단일 종목 단기 자동매매 시스템.

---

## 요구사항

- Python 3.11+
- [LM Studio](https://lmstudio.ai/) — 로컬 LLM 서버 (기본 `http://127.0.0.1:1234`)
- 한국투자증권 OpenAPI 앱 키 (실투자 / 모의투자)

```bash
pip install -r requirements.txt
```

`.env` 파일을 프로젝트 루트에 생성한다.

```env
KIS_REAL_APP_KEY=...
KIS_REAL_APP_SECRET=...
KIS_REAL_ACCOUNT_NO=...

KIS_PAPER_APP_KEY=...
KIS_PAPER_APP_SECRET=...
KIS_PAPER_ACCOUNT_NO=...

KIS_IS_PAPER=true          # false 로 변경하면 실투자 모드

ARBITER_BASE_URL=http://127.0.0.1:1234/v1/chat/completions
ARBITER_MODEL=google/gemma-4-26b-a4b
```

---

## 실행 파일 개요

- picker로 유망 종목을 선정한다.
- analysis로 선정된 종목에 언제 들어갈지 파악한다.
- analysis는 이미 보유중인 종목에 대한 대응 전략도 제공한다.
- 종목 매수가 결정되면 main.py으로 자동매매한다.
- 자동매매 중 게좌 모니터링은 status를 이용한다.

| 파일 | 용도 |
|---|---|
| `main.py` | 자동매매 메인 루프 |
| `picker.py` | 관심 종목 스크리닝 (다수 종목 → 점수 순 출력) |
| `analysis.py` | 단일 종목 심층 분석 보고서 |
| `status.py` | 계좌 현황 실시간 모니터 |

---

## main.py — 자동매매

1분 주기로 시장 환경을 필터링하고, 차트(1분/3분/5분봉)를 LM Studio에 전달하여 BUY/SELL/HOLD 판단을 받아 주문을 실행한다.

### 실행

```bash
# 모의투자 (기본)
python main.py --ticker 005930

# 종목명으로 지정
python main.py --name 삼성전자

# 실투자 모드
python main.py --ticker 005930 --real

# 1회만 실행
python main.py --ticker 005930 --once
```

### 파라미터

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `--ticker` | config.TICKER | 종목 코드 |
| `--name` | — | 종목명으로 지정 (`--ticker` 대신 사용) |
| `--real` | False | 실투자 모드 (미지정 시 모의투자) |
| `--once` | False | 1회 실행 후 종료 |
| `--plot-count` | 120 | AI에 전달할 차트 봉 수 |
| `--output-dir` | charts | 차트 저장 디렉터리 |
| `--debug` | False | DEBUG 로그 출력 |

### 동작 흐름

```
[매 1분]
  ↓
gate (시장 환경 필터)
  - 거래 시간 / 지수 급락 / 서킷브레이커 / 일일 손실 한도 체크
  ↓
차트 생성 (1분봉 / 3분봉 / 5분봉 PNG)
  ↓
LM Studio arbiter 호출
  - 입력: 차트 3장 + 가격/호가/지표 텍스트
  - 출력: {"action": "BUY|SELL|HOLD", "reason": "..."}
  ↓
주문 실행 / 포지션 관리
  ↓
모니터 루프 (별도)
  - 손절 / 익절 / 트레일링 스탑
  - 속도 기반 긴급 손절 (Flash Crash 대응)
  - 연속 음봉 선제 청산
  - 콘솔 's' 키 긴급 청산 (Windows)
```

### 리스크 규칙 (config.py 기본값)

| 항목 | 기본값 | 설명 |
|---|---|---|
| 손절 | ATR × 3.0 | 최소 0.8% 이상 |
| 익절 | ATR × 3.0 | 최소 1.5% 이상 |
| 트레일링 스탑 | 1.5% | 고점 대비 |
| 일일 최대 손실 | 5% | 초과 시 당일 거래 중단 |
| 일일 최대 거래 횟수 | 5회 | |
| 손절 후 재진입 쿨다운 | 10분 | |
| 1회 매수 비율 | 10% | 총 자산 대비 |

---

## picker.py — 종목 스크리닝

`watchlist_stocks.csv`에 등록된 종목들을 일봉/주봉/월봉 차트로 분석하고, AI가 매수 매력도 점수(0~100)를 부여한다. 점수 높은 순으로 출력한다.

### 실행

```bash
# watchlist 전체 스크리닝
python picker.py

# 특정 종목 코드로 실행
python picker.py --ticker 005930
python picker.py --ticker 005930,000660,035720   # 복수 지정

# 종목명으로 실행
python picker.py --name 삼성전자

# CSV 파일 지정
python picker.py --csv my_watchlist.csv
```

### 파라미터

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `--csv` | watchlist_stocks.csv | 종목 목록 파일 |
| `--ticker` | — | 특정 종목 코드 (쉼표로 복수 지정) |
| `--name` | — | 종목명으로 단일 종목 지정 |
| `--output-dir` | charts/periods | 차트 저장 디렉터리 |
| `--count` | 120 | 차트 봉 수 |
| `--debug` | False | DEBUG 로그 출력 |

### watchlist_stocks.csv 포맷

```
# 주석 줄
005930:삼성전자
000660:SK하이닉스
035720:카카오
```

`watchlist_stocks.csv`에는 코스피 시총 상위 100 종목이 기본 수록되어 있다.

### 출력 예시

```
1등 삼성전자(005930) 82점 상승 추세
이평선 정배열 + 볼린저밴드 상단 돌파 시도. RSI 60 미만으로 과열 아님.
타이밍:즉시 시점
5일선 지지 확인 후 거래량 동반 상승. 즉시 진입 고려.
뉴스분석:
HBM4 양산 계획 발표로 AI 서버 수요 수혜 기대. 차트 상승 흐름과 일치.
```

점수별 색상: **빨강** 70점 이상 / **노랑** 40~69점 / **파랑** 40점 미만

---

## analysis.py — 심층 분석

단일 종목을 대상으로 일봉/주봉/월봉 차트를 생성하고, AI에 멀티 타임프레임 심층 분석 보고서를 요청한다.

picker.py가 "어떤 종목을 살지"를 고르는 도구라면, analysis.py는 "이 종목을 언제/어떻게 살지"를 상세히 분석하는 도구다.

### 실행

```bash
# 종목 코드로
python analysis.py --ticker 005930

# 종목명으로
python analysis.py --name 삼성전자

# 봉 수·저장 디렉터리 지정
python analysis.py --ticker 005930 --count 200 --output-dir charts/analysis
```

### 파라미터

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `--ticker` | — | 종목 코드 (`--name`과 택일, 필수) |
| `--name` | — | 종목명 (`--ticker`와 택일, 필수) |
| `--output-dir` | charts/analysis | 차트 저장 디렉터리 |
| `--count` | 120 | 차트 봉 수 |
| `--debug` | False | DEBUG 로그 출력 |

### 분석 항목

AI가 아래 항목을 분석하고 JSON으로 반환한다.

- **멀티 타임프레임 추세** — 월봉/주봉/일봉 각각의 흐름과 상호 관계
- **기술적 지표** — RSI 다이버전스, MACD 크로스, 볼린저밴드 수축/확장, EMA 배열
- **거래량 분석** — 상승/하락 시 거래량 신뢰도, 급증/급감 감지
- **매매 전략** — 현재 구간 진단, 진입 조건, 손절 기준, 목표가 1·2차
- **최신 뉴스** — 웹 검색을 통한 공시/실적/테마 이슈와 차트 상관관계
- **종합 점수** — 매수 매력도 0~100점, 투자 포인트, 리스크 요인

### 출력 예시

```
================================================================
  삼성전자(005930)  매력도 78점  상승 추세  눌림목 대기 타이밍
================================================================

[ 점수 산정 근거 ]
  월봉 우상향 + 주봉 지지권 진입, 일봉 5일선 눌림목 패턴 확인

[ 타이밍 근거 및 권고 ]
  일봉 기준 5일선(68,200원) 지지 여부 확인 후 거래량 동반 반등 시 진입 고려.
  손절 67,000원 / 1차 목표 71,000원 / 2차 목표 74,500원

[ 최신 뉴스 / 이벤트 ]
  HBM4 양산 일정 확정 공시. 엔비디아 공급 계약 기대감 형성 중.
================================================================
```

---

## status.py — 계좌 모니터

계좌 잔고, 보유 종목 손익, 당일 거래 성과, 최근 거래 내역을 주기적으로 화면에 갱신한다.

### 실행

```bash
# 실투자 계좌 모니터 (10초 간격)
python status.py

# 모의투자 계좌 모니터
python status.py --paper

# 갱신 주기 변경
python status.py --interval 5
python status.py --paper --interval 30
```

### 파라미터

| 파라미터 | 기본값 | 설명 |
|---|---|---|
| `--interval` | 10 | 갱신 주기 (초) |
| `--paper` | False | 모의투자 계좌 조회 |

### 표시 항목

- **계좌 요약** — 총 평가금액, 예수금, 총 손익
- **보유 종목** — 종목명, 수량, 평균단가, 현재가, 손익금, 손익률
- **오늘 종목별 누적 성과** — 거래 횟수, 승/패, 손익금, 손익률 (`logs/trades_*.jsonl` 집계)
- **최근 거래 내역** — 최근 10건 청산 로그

화면을 덮어쓰며 갱신하므로 스크롤이 발생하지 않는다. `Ctrl+C`로 종료한다.

---

## 설정 (config.py / .env)

모든 파라미터는 `.env` 또는 환경 변수로 오버라이드할 수 있다.

### 주요 항목

```env
# 종목
TICKER=005930

# 장 시간
MARKET_OPEN_TIME=09:10
MARKET_CLOSE_TIME=15:10
HOLD_OVERNIGHT=false          # true: 오버나이트 허용
FORCE_CLOSE_ON_EXIT=true      # 프로그램 종료 시 포지션 강제 청산

# 리스크
USE_ATR_STOP=true
ATR_SL_MULTIPLIER=3.0
ATR_TP_MULTIPLIER=3.0
MIN_SL_RATIO=0.008
MIN_TP_RATIO=0.015
TRAILING_STOP_RATIO=0.015
MAX_DAILY_LOSS_RATIO=0.05
MAX_TRADES_PER_DAY=5
STOP_LOSS_COOLDOWN_MINUTES=10
MAX_POSITION_RATIO=0.5

# LM Studio
ARBITER_BASE_URL=http://127.0.0.1:1234/v1/chat/completions
ARBITER_MODEL=google/gemma-4-26b-a4b
ARBITER_MAX_TOKENS=512
ARBITER_TEMPERATURE=0.1
ARBITER_TIMEOUT_SEC=60
```

---

## 프로젝트 구조

```
tenkai/
├── main.py               # 자동매매 메인 루프
├── picker.py             # 종목 스크리닝
├── analysis.py           # 단일 종목 심층 분석
├── status.py             # 계좌 모니터
├── make_charts.py        # 차트 PNG 생성 유틸
├── config.py             # 전략 파라미터
├── chart_config.yaml     # 차트 표시 설정
├── watchlist_stocks.csv  # 스크리닝 대상 종목 목록
├── requirements.txt
├── kis_api/
│   ├── auth.py           # KIS 인증 / 토큰 캐시
│   ├── market.py         # 시세 / 분봉 / 호가 / 잔고
│   ├── market_websocket.py
│   └── order.py          # 시장가 주문
├── filters/
│   └── gate_market.py    # 시장 환경 필터
├── strategy/
│   ├── arbiter.py        # LM Studio 호출 / 응답 파싱
│   ├── indicators.py     # 기술 지표 계산
│   └── risk.py           # 포지션 / 리스크 관리
├── logger/
│   └── trade_log.py      # JSONL 거래 로그
└── logs/
    └── trades_<ticker>.jsonl
```

---

## 로그

거래 내역은 `logs/trades_<ticker>.jsonl`에 JSON Lines 형식으로 저장된다.

- **사이클 로그** — 매 판단 주기마다 gate/arbiter 결과, 주문 정보 기록
- **청산 로그** — `event=CLOSE`, 진입가/청산가/수량/손익/청산 사유 기록

---

## Naver Search MCP
<https://github.com/isnow890/naver-search-mcp/blob/main/RELEASE_NOTES.md>

API 사용을 위한 등록페이지:
<https://developers.naver.com/apps/#/myapps/EhvcspVdFnDVgN3E3aBe/overview>

### MCP 등록
```json
{
  "mcpServers": {
    "naver-search": {
      "type": "stdio",
      "command": "cmd",
      "args": [
        "/c",
        "node",
        "D:\\Projects\\tenkai\\naver-search-mcp\\dist\\src\\index.js"
      ],
      "cwd": "D:\\Projects\\tenkai\\naver-search-mcp",
      "env": {
        "NAVER_CLIENT_ID": "...",
        "NAVER_CLIENT_SECRET": "..."
      }
    }
  }
}
```

### conda 환경에서 사용

```bash
conda env config vars set NAVER_CLIENT_ID=...
conda env config vars set NAVER_CLIENT_SECRET=...
```

## vLLM 적용
- Windows 호환성 문제로 실행에러 발생. WSL2를 이용
- 빠르지만 고정 메모리를 요구. 전용서버가 아니라면 장점 없음

### 필수 요구사항
- GPU (최소 24GB VRAM 권장)
- CUDA 12.9 이상
  ```bash
  nvidia-smi
  ```
- Python 3.10+
- 최신 transformers 라이브러리

```bash
conda create -n vllm python=3.12
conda activate vllm
```

### CUDA & torch
```bash
conda install pytorch torchvision torchaudio pytorch-cuda=12.4 -c pytorch -c nvidia
```

### vLLM
```bash
pip install vllm
```
