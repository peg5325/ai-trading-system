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

- Marketaux: `MARKETAUX_API_KEY`가 있을 때만 `/v1/news/all`을 호출한다. 실제 UTC 달력일
  기준 quota ledger가 모든 stage의 실제 요청을 합쳐 60회로 제한한다. Free plan의 요청당
  3건 제한 때문에 page를 순차 수집하며, 남은 page가 있는데 stage 또는 quota cap에 닿으면
  성공으로 표시하지 않는다. sanitized item에는 URL, 시각, 종목, sentiment와 headline hash만
  남긴다.
- SEC EDGAR: `SEC_USER_AGENT`에 연락 가능한 이메일이 있을 때만
  `data.sec.gov/submissions/CIK##########.json`을 호출한다. 고정 CIK와 form allowlist만 사용하고
  기본 속도는 2 req/sec다.
- FRED/ALFRED: `FRED_API_KEY`가 있을 때만 고정 series allowlist와 당일 release calendar를
  호출한다. `realtime_start`와 `realtime_end`를 collection `as_of` 날짜로 고정하고 observation의
  realtime 범위를 보존한다. FRED가 공식 결측값으로 사용하는 `.`은 유효 관측 후보에서 제외하며,
  그 결과 필수 series에 유효값이 없으면 missing으로 fail-closed한다. 다른 비수치·NaN·무한대는
  invalid로 fail-closed한다. 필수 series가 비거나 series별 freshness를 넘으면 fail-closed다.
  freshness는 달력일 기준이며, `DTWEXBGS`는 H.10의 묶음 발표 주기와 긴 주말을 포함하도록
  14일, 그 외 일별 시장 series는 7일을 사용한다. 7일을 초과하는 series 설정에는
  `freshness_reason`을 의무화하며, 각 산출 item에는 관측 나이·허용 일수·근거를 함께 남긴다.

자격증명이 하나라도 없으면 해당 provider는 네트워크를 호출하지 않고 `UNAVAILABLE`을 남긴다.
하나 이상의 provider가 `OK`가 아니면 bundle은 `FAIL_CLOSED`다.

## HTTP와 원본 보존

- HTTPS와 config의 host allowlist만 허용한다.
- timeout, 재시도 횟수, 응답 바이트 상한을 config로 제한한다.
- 429와 일시적 5xx만 bounded retry한다. redirect는 따라가지 않는다.
- 429의 `Retry-After`는 config 상한 안에서 우선 적용하고 실제 적용한 대기를 receipt에 남긴다.
- 응답 body는 JSON parse 전에 exact bytes 기준 SHA-256으로 `data/` 아래 gzip 보존한다.
- receipt의 `requested_at`과 `received_at`은 실제 wall-clock UTC이며, 사용자 `as_of`
  cutoff와 분리한다.
- request URL receipt에서는 query를 제거하므로 API token/key가 남지 않는다.
- raw content와 immutable receipt의 집합은 결정론적 manifest로 묶는다.
- credential, request header, raw payload, 뉴스 전문은 Git에 커밋하지 않는다.

## 실행

```bash
python scripts/collect_premarket_evidence.py \
  --trading-day 2026-09-09 \
  --stage PRIMARY \
  --out-json data/premarket/provider-bundle-2026-09-09.json
```

CLI는 기존 NYSE 캘린더로 거래일과 PRIMARY/VERIFICATION/FREEZE 실행창을 네트워크 전에
검증한다. `--as-of`를 생략하면 stage의 고정 cutoff를 사용하며, 지정하면 그 cutoff와 정확히
같아야 한다. 출력 JSON과 raw/receipt/quota는 기본적으로 모두 gitignored `data/` 아래에
위치한다.

## 공식 API 근거

- Marketaux documentation: `https://www.marketaux.com/documentation`
- SEC EDGAR API documentation: `https://www.sec.gov/search-filings/edgar-application-programming-interfaces`
- SEC fair access policy: `https://www.sec.gov/about/webmaster-frequently-asked-questions`
- FRED observations API: `https://fred.stlouisfed.org/docs/api/fred/series_observations.html`
- FRED v2 missing-value contract: `https://fred.stlouisfed.org/docs/api/fred/v2/release_observations.html`
- FRED real-time periods: `https://fred.stlouisfed.org/docs/api/fred/realtime_period.html`
