# AGENTS.md - 주식 자동매매 Agent 설계 문서 (현재 코드 기준)

> 이 문서는 현재 저장소의 실제 구현을 기준으로 유지한다.
> 존재하지 않는 모듈(Gate2, gate3_claude, history.py 등)을 전제로 코드를 생성하지 않는다.
> 리스크 규칙 우회 코드는 금지한다.

---

## 1. 프로젝트 개요

| 항목 | 내용 |
|------|------|
| 목적 | 한국투자증권 KIS API 기반 단일 종목 단기 자동매매 |
| 언어 | Python 3.11+ |
| 핵심 판단 | LM Studio(OpenAI 호환 API) 멀티차트 판정 |
| 필터 구조 | gate(시장 환경 필터) + arbiter(LM Studio 액션 판정) |
| 실행 환경 | Windows 우선(긴급 청산 키: s/S), 로컬/VM 상시 실행 |
| 거래 대상 | 단일 종목 (config.py의 TICKER 또는 실행 인자) |

---

## 2. 현재 프로젝트 구조

```text
tenkai/
├── AGENTS.md
├── config.py
├── main.py
├── status.py
├── make_charts.py
├── chart_config.py
├── requirements.txt
├── kis_api/
│   ├── __init__.py
│   ├── auth.py
│   ├── market.py
│   ├── market_websocket.py
│   └── order.py
├── filters/
│   ├── __init__.py
│   └── gate_market.py
├── strategy/
│   ├── __init__.py
│   ├── indicators.py
│   └── risk.py
├── logger/
│   ├── __init__.py
│   └── trade_log.py
├── logs/
│   └── trades_<ticker>.jsonl
└── tests/
    ├── test_filters.py
    ├── test_indicators.py
    └── test_risk.py
```

---

## 3. 전체 아키텍처 흐름

```text
[매 1분 루프]
    |
    v
[스냅샷 수집]
KIS: 현재가/분봉/호가/체결강도/지수/잔고
    |
    v
[gate] filters/gate_market.py
- 거래시간(개장~ENTRY_CUTOFF)
- 지수 급락
- 거래량 0
- 서킷브레이커
- 당일 손실 한도
    |
    +-- 실패 -> SKIP 로그
    |
    v
[지표/차트 준비]
- 1/3/5분봉 리샘플링
- RSI, BB, MACD, ATR 계산
- 차트 PNG 생성
    |
    v
[arbiter] LM Studio 판정
- 입력: 차트 3장 + 가격/호가/시장텍스트
- 출력: {"action": "BUY|SELL|HOLD", "reason": "..."}
    |
    v
[주문/리스크]
- BUY: can_enter + 수수료손익분기점 필터 통과 시 진입
- SELL: 보유 포지션 있을 때 청산
- HOLD: 관망
    |
    v
[모니터 루프(별도)]
- 손절/익절/트레일링
- 속도 기반 긴급 손절
- 연속 음봉 선제 청산
- 수동 긴급 청산(s/S)
```

---

## 4. 실행 파일(main.py)

### 실행 예시

```bash
python main.py --ticker 005930
python main.py --ticker 005930 --once --real
```

### 주요 인자

| 인자 | 설명 |
|------|------|
| --ticker | 종목 코드 |
| --real | 실투자 모드 (기본은 모의) |
| --debug | 디버그 로그 |
| --once | 1회 실행 후 종료 |
| --plot-count | arbiter 전달용 차트 봉 수 |
| --output-dir | 차트 출력 디렉터리 |

### 주요 함수/클래스

- DelphiTrader.initialize: 인증/포지션 복구
- DelphiTrader.run_cycle: 1회 판단 사이클
- DelphiTrader._position_monitor_loop: 리스크 청산 감시
- DelphiTrader._keyboard_monitor_loop: Windows 긴급 청산 키 감시
- DelphiTrader._ask_lm_studio: LM Studio 호출/파싱

---

## 5. config.py 설계(현재 유효 항목)

모든 값은 환경변수로 오버라이드 가능하다.

### 종목/인증/엔드포인트

- TICKER, MARKET
- KIS_REAL_*, KIS_PAPER_*, KIS_IS_PAPER
- BASE_URL_REAL, BASE_URL_PAPER

### 실행/시장 시간

- LOOP_INTERVAL_SEC
- POSITION_CHECK_SEC, POSITION_CHECK_FAST_SEC, POSITION_CHECK_FAST_ATR_BUFFER
- CANDLE_COUNT, CANDLE_INTERVAL
- MARKET_OPEN_TIME, MARKET_CLOSE_TIME
- HOLD_OVERNIGHT, FORCE_CLOSE_ON_EXIT

### 지표/진입 보조

- RSI_PERIOD, BB_PERIOD
- EMA_SHORT, EMA_LONG, HTF_MULTIPLIER
- MIN_PROFIT_BUFFER

### 리스크 관리

- STOP_LOSS_COOLDOWN_MINUTES
- VELOCITY_STOP_* (속도 기반 긴급 손절)
- CONSEC_BEARISH_* (연속 음봉 선제 청산)
- MAX_POSITION_RATIO, STOP_LOSS_RATIO, TAKE_PROFIT_RATIO, TRAILING_STOP_RATIO
- MAX_DAILY_LOSS_RATIO, MAX_TRADES_PER_DAY
- USE_ATR_STOP, ATR_SL_MULTIPLIER, ATR_TP_MULTIPLIER, MIN_SL_RATIO, MIN_TP_RATIO

### 기타

- MARKET_DROP_THRESHOLD
- BROKER_FEE_RATE, TRANSACTION_TAX_RATE
- LOG_DIR, LOG_FILE
- STOCK_CODE_DIR
- ARBITER_BASE_URL, ARBITER_MODEL, ARBITER_MAX_TOKENS,
  ARBITER_TEMPERATURE, ARBITER_TIMEOUT_SEC

주의:
- 과거 Gate2/Gate3 전용 설정은 제거되었으며 재도입하지 않는다.

---

## 6. gate(시장 환경 필터)

파일: filters/gate_market.py

```python
def gate_market_filter(market_data: dict) -> tuple[bool, dict]
```

체크 항목:

1. 거래 시간: MARKET_OPEN_TIME ~ ENTRY_CUTOFF_TIME
2. 시장 급락: market_change <= MARKET_DROP_THRESHOLD 이면 기각
3. 거래 활성: current_volume <= 0 이면 기각
4. 서킷브레이커: True 이면 기각
5. 당일 손실 한도: daily_loss_ratio >= MAX_DAILY_LOSS_RATIO 이면 기각 + halt 플래그

반환:

- passed: 통과 여부
- result:
  - passed
  - halt_trading_today
  - checks(list)

---

## 7. arbiter(LM Studio) 규칙

파일: main.py (DelphiTrader._build_prompt_text /_ask_lm_studio)

입력:

1. 1분/3분/5분 차트 PNG
2. 가격/호가/체결강도/시장 정보 텍스트
3. 지표 요약 텍스트

출력 계약(JSON):

```json
{
  "action": "BUY | SELL | HOLD",
  "reason": "짧은 근거"
}
```

실패 처리:

- HTTP/파싱 오류 시 action=HOLD로 강등
- action 값 비정상 시 HOLD로 정규화

---

## 8. 리스크 관리 규칙

파일: strategy/risk.py

핵심 원칙:

1. 포지션 중복 금지
2. 일일 거래 횟수/손실 한도 강제
3. 손절 후 쿨다운 적용
4. 수수료 손익분기점 미달 진입 금지(check_fee_viability)

진입 시:

- 수량 = floor(total_assets * MAX_POSITION_RATIO / current_price)
- SL/TP는 ATR 기반(USE_ATR_STOP) 또는 고정 비율 사용
- 최소/최대 가드레일(MIN_SL_RATIO, MIN_TP_RATIO) 적용

보유 중:

1. STOP_LOSS / TAKE_PROFIT / TRAILING_STOP 체크
2. VELOCITY_STOP 조건 충족 시 선제 청산
3. CONSEC_BEARISH 조건 충족 시 선제 청산
4. MARKET_CLOSE_TIME 이후 HOLD_OVERNIGHT=False면 강제 청산

종료 시:

- FORCE_CLOSE_ON_EXIT=True면 종료 청산
- False면 포지션 유지

---

## 9. 로깅 규칙

파일: logger/trade_log.py
형식: JSONL, 종목별 파일 logs/trades_{ticker}.jsonl

사이클 로그 키:

- timestamp, ticker, current_price
- gate_passed, gate_detail
- arbiter_passed, arbiter_direction, arbiter_reason
- action, order_price, order_qty, stop_loss, take_profit
- result
- extra 필드(decision_source, chart_paths 등)

청산 로그 키:

- event=CLOSE
- ticker, exit_price, entry_price, entry_time, qty
- pnl, pnl_ratio, close_reason

---

## 10. KIS API 연동 원칙

파일: kis_api/

- auth.py: 실전/모의 토큰 캐시 분리
- market.py: 시세/분봉/호가/잔고/지수/체결강도
- order.py: 시장가 매수/매도

운영 원칙:

1. 시세 조회는 실전 인증(auth_data) 기준
2. 주문은 KIS_IS_PAPER에 따라 모의/실전 분기
3. ETF/ETN 감지 시 거래세를 0으로 조정

---

## 11. 유틸리티

### status.py

```bash
python status.py --interval 10
python status.py --interval 10 --paper
```

- 계좌/보유종목/일일 요약/최근 청산 로그 모니터링
- 한글 폭 보정 테이블 출력

### make_charts.py

- 지표 프레임 생성
- 1/3/5분봉 차트 PNG 렌더링
- main.py의 arbiter 입력용 차트 생성에 사용

---

## 12. 코드 생성/수정 시 준수 사항

1. 안전 우선: 불확실하면 HOLD 또는 진입 스킵
2. 하드코딩 금지: 수치/임계값은 config.py 참조
3. 예외 처리 필수: 외부 I/O(KIS, LM Studio)는 실패 시 안전 경로로 처리
4. 비동기 유지: main 루프 및 API 호출은 async 흐름 유지
5. 타입 힌트 유지: 신규 함수/메서드 타입 명시
6. 리스크 규칙 불가침: strategy/risk.py 우회 로직 금지
7. 용어 통일: gate, arbiter 사용 (gate1/gate2/gate3 용어 사용 금지)
8. 존재하지 않는 파일/모듈을 전제로 한 코드 생성 금지
