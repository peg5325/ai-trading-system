# 장전 증거 공급자 v1 계약

## 목적과 경계

이 계층은 Marketaux Free, SEC EDGAR, FRED/ALFRED의 응답을 읽어
`premarket-provider-bundle/v1`을 만든다. 산출물은 증거 수집 결과일 뿐이며 LONG/SHORT 판단,
후보 점수, 시장 regime, 전략 승인, 주문 또는 broker 권한을 갖지 않는다.

`premarket-provider-bundle/v1`은 `premarket-input/v1`이 아니다. 후속 정규화 단계가 provider
item을 source/event로 조인하고 별도의 시장 데이터·후보·regime 근거를 완성한 뒤에만 기존
Daily Brief에 입력할 수 있다. 특히 이 PR은 Daily Brief가 요구하는 `MARKET` source나 후보
점수를 생성하지 않는다.

## 공급자 계약

- Marketaux: `MARKETAUX_API_TOKEN`이 있을 때만 `/v1/news/all`을 호출한다. 프로젝트 quota
  ledger가 UTC 거래일별 60회를 hard cap으로 막고 Free plan 한도인 요청당 3건만 받는다.
  sanitized item에는 뉴스 전문·description·snippet을 넣지 않고 URL, 시각, 종목, sentiment,
  headline hash만 남긴다.
- SEC EDGAR: `SEC_EDGAR_USER_AGENT`에 연락 가능한 이메일이 있을 때만
  `data.sec.gov/submissions/CIK##########.json`을 호출한다. 고정 CIK와 form allowlist만 사용하고
  기본 속도는 2 req/sec다.
- FRED/ALFRED: `FRED_API_KEY`가 있을 때만 고정 series allowlist를 호출한다. `realtime_start`와
  `realtime_end`를 collection `as_of` 날짜로 고정하고 observation의 realtime 범위를 보존한다.

자격증명이 하나라도 없으면 해당 provider는 네트워크를 호출하지 않고 `UNAVAILABLE`을 남긴다.
하나 이상의 provider가 `OK`가 아니면 bundle은 `FAIL_CLOSED`다.

## HTTP와 원본 보존

- HTTPS와 config의 host allowlist만 허용한다.
- timeout, 재시도 횟수, 응답 바이트 상한을 config로 제한한다.
- 429와 일시적 5xx만 bounded retry한다. redirect는 따라가지 않는다.
- 응답 body는 JSON parse 전에 exact bytes 기준 SHA-256으로 `data/` 아래 gzip 보존한다.
- request URL receipt에서는 query를 제거하므로 API token/key가 남지 않는다.
- raw content와 immutable receipt의 집합은 결정론적 manifest로 묶는다.
- credential, request header, raw payload, 뉴스 전문은 Git에 커밋하지 않는다.

## 실행

```bash
python scripts/collect_premarket_evidence.py \
  --trading-day 2026-09-09 \
  --as-of 2026-09-09T12:00:00Z \
  --out-json data/premarket/provider-bundle-2026-09-09.json
```

출력 JSON과 raw/receipt/quota는 기본적으로 모두 gitignored `data/` 아래에 위치한다. 동일
`as_of`, 동일 응답, 동일 raw content 집합에서는 artifact hash가 같다.

## 공식 API 근거

- Marketaux documentation: `https://www.marketaux.com/documentation`
- SEC EDGAR API documentation: `https://www.sec.gov/search-filings/edgar-application-programming-interfaces`
- SEC fair access policy: `https://www.sec.gov/about/webmaster-frequently-asked-questions`
- FRED observations API: `https://fred.stlouisfed.org/docs/api/fred/series_observations.html`
- FRED real-time periods: `https://fred.stlouisfed.org/docs/api/fred/realtime_period.html`
