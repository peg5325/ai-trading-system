# 장전 증거 정규화 계약 v1 (read-only shadow)

## 목적과 권한 경계

`premarket-provider-bundle/v1`의 뉴스·공시·거시 증거와 동일 stage/as-of에 고정된
시장 캔들 스냅샷을 결합해 `premarket-input/v2`를 만든다. 이 입력은
`premarket-daily-brief/v2`의 `LONG_WATCH`, `SHORT_WATCH`, `NO_TRADE` 판정에만 쓰인다.

이 계층은 주문, broker, 계좌, borrow/locate, 전략 승격, forward return, 손익 또는 성과를
계산하거나 호출하지 않는다. 모든 strategy template ID는 `null`로 둔다. 따라서 후보가 있어도
기본 결과는 `NO_TRADE`이고, watchlist는 조사·일지용이다.

## 단계와 산출물

1. `premarket-provider-bundle/v1`: Marketaux, SEC EDGAR, FRED/ALFRED 증거.
2. `premarket-market-bundle/v1`: Toss read-only 캔들, raw snapshot manifest, 수집 receipt.
3. `premarket-input/v2`: 두 bundle을 순수·결정론적으로 정규화한 로컬 입력.
4. `premarket-daily-brief/v2`: 기존 planner가 만드는 방향별 shadow watchlist.

각 단계는 JSON write-once이고 동일 입력 재실행만 허용한다. 자격증명, 인증 헤더, 뉴스 전문은
산출물에 넣지 않는다.

## 시장 스냅샷 수집

- 대상은 `config/premarket_daily_brief.yaml`이 허용하는 28종목이다. inverse ETF는 제외한다.
- provider bundle의 `trading_day`, `stage`, `as_of`를 그대로 사용한다.
- 1분봉은 `bar.ts_utc + 1분 <= as_of`인 완결 봉만 허용한다.
- 일봉은 거래소 날짜가 `trading_day`보다 이전인 완결 봉만 허용한다.
- 종목별 1분봉은 최소 120개, 일봉은 최소 20개를 요구한다. SPY와 QQQ는 regime용 완결
  일봉 200개를 요구한다.
- 페이지 수를 제한하고 요청 사이 0.4초 간격을 적용하며, 모든 원시 Toss payload를
  `RawSnapshotStore`에 parse 전에 보존한다.
- Toss가 cutoff와 같은 시각의 형성 중 1분봉 또는 해당 거래일의 미완성 일봉을 포함하는 것은
  공급자 경계 의미로 인정한다. 이 두 종류는 계산에서 제외하고 제외 행수를 감사 필드에 남긴다.
- cutoff보다 실제로 뒤인 1분봉, trading day보다 뒤인 일봉, cursor 역행·정체, 캔들 충돌·부족,
  심볼 누락은 bundle defect로 남긴다.
- 5분봉은 별도 구현을 만들지 않고 `src.bars.resample.resample_1m`의 canonical 경로를 사용한다.
- canonical `data/market_history.sqlite`는 읽거나 쓰지 않는다. 9월 장전 시점과 7월 고정 DB를
  섞지 않는다.

## `premarket-input/v2` 시간 의미

- `as_of`: stage의 증거 cutoff. 이 시각보다 뒤에 관측되거나 공개된 정보는 금지한다.
- `generated_at`: 실제 수집·정규화가 끝난 시각. `as_of` 이상이고 해당 stage 실행창 안이어야 한다.
  PRIMARY는 VERIFICATION 전, VERIFICATION은 FREEZE 전, FREEZE는 정규장 개장 전에 끝나야 한다.
- source/event/candidate/regime의 `observed_at`·`available_at`은 `as_of` 이하여야 한다.
- `ingested_at`은 `available_at` 이상, `generated_at` 이하여야 한다. 네트워크 응답이 cutoff 뒤에
  도착하는 정상 live 수집을 미래정보로 오인하지 않는다.
- 뉴스는 `published_at`, 공시는 `accepted_at`, 1분봉은 봉 종료, 일봉은 해당 정규장 종료를
  availability로 사용한다.
- FRED 관측은 point-in-time vintage와 자체 freshness gate를 통과한 값만 사용하며,
  `MACRO_RELEASE`는 일정 정보로만 기록하고 발표 결과로 해석하지 않는다.

## 증거 이벤트

- NEWS: `published_at <= as_of`이고 provider bundle에 포함된 항목. sentiment를 보존한다.
- FILING: `accepted_at <= as_of`이고 직전 24시간 이내인 8-K·10-Q·10-K. form 자체는 방향성이
  없으므로 sentiment는 0이다.
- MACRO와 release calendar는 regime source/context이며 종목 catalyst로 변환하지 않는다.
- 동일 event ID의 상충 revision, 허용 종목 밖 symbol, source join 실패는 fail-closed다.
- Daily Brief source freshness는 기본 180분을 유지하되, FILING source는 이 계약의 24시간
  lookback과 동일한 1,440분을 사용한다. 종류별 override는 config에 명시하고, 미지정 kind는
  기본값을 상속한다.

## 후보 점수

후보는 NEWS 또는 최근 FILING event가 하나 이상 있고, 시장 feature가 완결된 종목만 만든다.
각 점수 범위는 planner 계약과 같은 `[0,1]`, catalyst만 `[-1,1]`이다.

- `catalyst_score`: 해당 종목 NEWS sentiment의 산술평균을 `[-1,1]`로 clip한다. 뉴스가 없으면 0.
  FILING은 evidence에는 포함하지만 부호를 만들지 않는다.
- `trend_score`: 완결 1분봉을 5분봉으로 파생한 뒤 `close > EMA20`과 `EMA7 > EMA20`을 각각
  0.5점으로 계산한다.
- `relative_strength_score`: 직전 완결 일봉 종가 대비 최신 완결 1분봉 종가 수익률의 28종목
  cross-sectional percentile. 동점은 평균 rank, 최종 후보 정렬은 symbol로 결정한다.
- `volume_score`: 최신 완결 5분봉 거래량 / 이전 20개 완결 5분봉 평균 비율을
  `<0.7=0.3`, `<1.0=0.5`, `<1.5=0.7`, `>=1.5=0.9`로 고정한다.
- `liquidity_score`: 직전 20개 완결 일봉의 `close × volume` 중앙값을 28종목 percentile로
  변환한다.

candidate `available_at`은 사용한 시장 봉과 event의 availability 중 최댓값,
`ingested_at`은 정규화 `generated_at`이다. 모든 candidate는 MARKET source와 실제 사용한
event source를 참조한다.

## 시장 regime

결과를 보기 전에 다음 규칙을 고정한다.

- SPY·QQQ 각각 직전 완결 일봉 종가와 그 시점까지의 SMA200을 계산한다.
- VIX는 FRED `VIXCLS`의 point-in-time 최신 유효값을 사용한다.
- `RISK_ON`: SPY와 QQQ가 모두 SMA200 위이고 VIX < 25.
- `RISK_OFF`: SPY와 QQQ가 모두 SMA200 아래이거나 VIX >= 30.
- 그 밖의 혼합 상태, feature 부족, VIX 25~30은 `UNCERTAIN`.

이 규칙은 전략 수익성 주장이 아니라 shadow 분류 정책이다. 다른 FRED series는 기록과
품질 확인에 사용하지만 이번 버전의 regime 부호에는 사용하지 않는다.

## fail-closed 조건

- 두 bundle의 trading day, stage, as-of 불일치.
- provider 또는 market bundle의 전체 상태가 OK가 아님.
- 미래·미완결 캔들, 필수 benchmark 부족, source/event/candidate join 실패.
- 정규화 산출물이 `premarket-input/v2` 계약을 통과하지 못함.
- 결과 파일 충돌 또는 raw provenance 불일치.

실패 시 빈 추천을 성공처럼 만들지 않고 `NO_TRADE` 가능한 감사 산출물 또는 명시적 CLI 실패로
종료한다. 어떤 경우에도 주문으로 대체하지 않는다.

## 검증

- fixture 기반 pure normalization, 입력 순서 불변, 중복/충돌, 미래정보, stage mismatch.
- 1m 완료 경계와 일봉 거래일 경계.
- 점수 5종과 percentile 동점 규칙.
- regime 3상태와 VIX 경계값 25/30.
- LONG/SHORT 별도 승인과 주문 0건 불변.
- 실제 2026-09-15 PRIMARY/VERIFICATION/FREEZE bundle을 각각 재현하고 코드·JSON·문서 정합 확인.
