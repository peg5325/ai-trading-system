# P0-US30 3년 1분봉 공식 수집 및 품질 판정 보고

## 결론

- 공식 collection run `p0-us30-1m-20260721-r82`는 **30/30 종목의 최소 3년 범위
  수집을 완료**했다.
- 30/30 종목이 `as_of=2026-07-21` 기준 **3.0년 이상**의 1분봉 범위를 확보했다.
- collection provenance는 `VERIFIED`, SQLite `integrity_check`와 `quick_check`는 모두 `ok`,
  수집 로그의 conflict는 0이다.
- 품질 판정은 두 축으로 분리한다.
  - **기간 커버리지: GO** — 30/30이 3년 기준 충족.
  - **허용 세션·후행 경계 정합성: INCONCLUSIVE** — 기존 canonical DB에 있던
    OVERNIGHT 257,799행과 허용 세션 최신일의 1~2거래일 후행 결측 때문에 19종목만
    `OK`, 11종목은 `INCONCLUSIVE_DATA`.
- OVERNIGHT 행은 이번 공식 수집에서 새로 허용한 데이터가 아니다. 새 수집은
  `PRE,REGULAR,POST` 필터를 저장 전에 적용했다. 기존 행을 사후에 조용히 삭제하지 않고
  fail-closed 판정과 함께 보존했다.
- 따라서 이 스냅샷은 **3년 커버리지 확보본**으로 고정할 수 있지만, 30/30 clean-session
  데이터셋이라고 주장하거나 blind OOS 성과 분석에 바로 사용하는 것은 금지한다. 별도 승인된
  후행 구간 보충과 감사 삭제 또는 immutable filtered derivative가 먼저 필요하다.

## 고정 계약

| 항목 | 값 |
| --- | --- |
| universe | `config/universe_p0_us30.yaml`의 P0-US30 |
| interval | `1m` |
| collection run_id | `p0-us30-1m-20260721-r82` |
| completed-through / as_of | `2026-07-21` |
| 저장 허용 세션 | `PRE`, `REGULAR`, `POST` |
| 제외 세션 | `OVERNIGHT` |
| canonical DB | `data/market_history.sqlite` |
| raw root | `data/raw_snapshots/p0-us30-1m-20260721-r82` |
| 전략 성과·forward return·blind OOS | 미계산·미열람 |

## 실행 감사

1. 수집 전 SQLite backup을 생성하고 무결성을 확인했다.
   - `data/backups/market_history.pre-1m-bulk.20260724T041322Z.sqlite`
   - SHA-256:
     `ae572a0f4be126c64afd443c9e5661bd08acf62da105d2c2af7014fa22418483`
2. 최초 수집은 사용자 요청에 따라 인터넷 이동 전에 안전하게 SIGINT로 중단했다.
   완료된 5종목은 ingest log와 canonical commit으로 보존됐다.
3. 새 IP 등록 후 같은 run_id·cutoff·세션 계약으로 resume했다.
   - 최초 resume에서 전체 universe 기본값 때문에 SPY를 97 raw page 추가 조회한 뒤 중단했다.
     canonical conflict는 0이고, 이 추가 조회는 raw provenance에 남아 있다.
   - 이후 미완료 종목만 명시해 resume했다.
4. XLY 진행 중 안전 중단 후 spacing을 0.5초에서 0.25초로 낮췄다.
   XLY 잔여 페이지와 XLU~SOXS를 같은 run_id로 이어받았다.
5. 첫 패스의 3년 미달 14종목을 같은 run_id로 추가 수집했다.
   모든 종목이 provider history wall(`empty_budget_exhausted`)에 도달했고 3년 기준을 충족했다.
6. stderr의 3개 `KeyboardInterrupt`는 위의 의도된 안전 중단 기록이다.
   API 429, collection error, conflict는 0이다.

## 최종 수집·DB 증거

| 항목 | 값 |
| --- | ---: |
| canonical DB size | 12,203,712,512 bytes |
| canonical DB SHA-256 | `7220f22c8e48eca288ca6dbbf1714350f585b173a9fe7eb132824ef210d8df3c` |
| DB 전체 행수 | 22,912,026 |
| DB 1분봉 행수 / 심볼 | 22,835,336 / 34 |
| P0-US30 공식 1분봉 행수(≤as_of) | 22,773,133 |
| P0-US30 3년 충족 | 30/30 |
| ingest logs / distinct symbols | 44 / 30 |
| ingest rows_seen / rows_inserted | 26,481,893 / 21,193,378 |
| ingest duplicate / conflict | 0 / 0 |
| bars beyond as_of | 0 |
| SQLite integrity / quick check | `ok` / `ok` |

`ingest rows_inserted`는 이 run에서 추가된 행이고, P0 공식 총행수에는 수집 전에 존재하던
canonical 행도 포함된다. 두 수치를 같은 의미로 해석하지 않는다.

## 결정론적 스냅샷

- tracked manifest:
  `docs/research/P0_US30_INTRADAY_1M_MANIFEST.json`
- manifest schema: `p0_us30_intraday_1m_manifest/v2`
  (허용 세션 span/gap·후행 경계 판정을 추가해 v1에서 명시적으로 상승)
- data content SHA-256:
  `fc337950786f5fb07149869c561586917a2c55a2749a960768011b22d30680a9`
- raw 고유 payload 파일: 154,181
- raw content SHA-256:
  `ad26b2d1b9493af965ebd9824a2772ed1ef527716ec3db9be18609a40bea46d1`
- raw receipts: 198,410

raw receipts 수는 content 파일 수보다 크다. 재개·중복 조회는 receipt를 추가하지만
content-addressed payload 파일은 동일 SHA로 deduplicate하기 때문이다.

## 품질 판정

| 축 | 결과 | 근거 |
| --- | --- | --- |
| 30종목 존재 | PASS | 30/30 |
| 3년 범위 | PASS | 30/30 |
| as_of 경계 | PASS | 초과 행 0 |
| provenance | PASS | run_id/cutoff/30 symbols `VERIFIED` |
| 허용 세션만 존재 | FAIL-CLOSED | OVERNIGHT 257,799행 |
| 허용 세션 최신일 | FAIL-CLOSED | SPY·QQQ 1거래일, 나머지 9종목 2거래일 후행 결측 |
| 최종 종목 verdict | 19 `OK`, 11 `INCONCLUSIVE_DATA` | manifest 종목별 사유 참조 |

OVERNIGHT 혼입 종목은 SPY, QQQ, XLU, MSFT, NVDA, AMD, AVGO, GOOGL, VST, SOXL,
SOXS다. 같은 11종목이 후행 결측도 가진다. SPY·QQQ의 허용 세션 최신일은
`2026-07-20`(1거래일 결측), 나머지 9종목은 `2026-07-17`(2거래일 결측)이다.
삭제·재분류·후행 구간 추가 수집·성과 계산은 수행하지 않았다.

## 외부 복제와 restore smoke

Apple iCloud Drive의 `icloud-documents` target에 canonical DB와 raw snapshot을 각각
고유 파일명으로 복제했다. `brctl status com.apple.CloudDocs`는 두 파일 생성 이후
`caught-up`(last-sync `2026-07-26 12:38:27 KST`)을 보고했다.

### SQLite

| 항목 | 값 |
| --- | --- |
| replica | `market_history.p0-us30-1m-20260721-r82.replica.20260726T030228Z.sqlite` |
| replica size | 12,203,712,512 bytes |
| snapshot == replica SHA-256 | `28cc3525e176642736eed20d90fd4215c82311c3204c02648522c867c8e60e82` |
| integrity | `ok` |
| semantic match | true |
| restore smoke | `ok` |

SQLite backup API는 새 페이지 레이아웃을 만들 수 있으므로 canonical **파일** SHA와 replica
SHA가 다른 것은 정상이다. 판정 기준은 snapshot과 replica의 SHA 일치, integrity,
행수·심볼·기간의 의미 일치, restore smoke다.

### Raw

| 항목 | 값 |
| --- | --- |
| replica | `raw_snapshot.p0-us30-1m-20260721-r82.20260726T030228Z.tar` |
| archive size | 796,951,040 bytes |
| archive SHA-256 | `dfcd0f632156417f8edc40a4607744cfb54a2cf8ad2c6b8fd3ebafbbd6856b35` |
| restored payload files | 154,181 |
| restored raw content SHA-256 | `ad26b2d1b9493af965ebd9824a2772ed1ef527716ec3db9be18609a40bea46d1` |
| restore content match | true |

첫 raw smoke 구현은 manifest 전체 객체를 비교해 임시 복원 디렉터리의 `root_basename`
차이만으로 실패했다. 아카이브는 재생성하지 않았고, root 위치와 무관한 schema·파일 목록·
파일 수·content hash로 판정식을 교정해 재실행했다. 최종 restore content match는 true다.

## 테스트

- manifest/provenance targeted:
  `65 passed`
- 전체 테스트:
  `1024 passed`
- collection/manifest/replica 생성 중 전략 성과·forward return·blind OOS:
  미계산·미열람
- 주문·broker·risk·Paper·전략·config:
  변경 없음

## 독립 검증

별도 verifier가 tracked 5개 산출물과 gitignored DB·receipt 증거를 read-only로 대조했다.

- verdict: **APPROVE**
- blocking findings: **0**
- provenance, 30/30 3년 커버리지, 당시 판정의 19 `OK`/11 `INCONCLUSIVE_DATA`,
  OVERNIGHT 257,799행, DB/raw 외부 복제 receipt, tracked 산출물의
  비밀정보·절대경로 부재를 확인했다.
- 이 초기 검증 뒤 PR 재검수에서 허용 세션 최신일 후행 결측을 추가 발견했다. 판정 수(19/11)는
  변하지 않았지만 “OVERNIGHT가 유일한 사유”라는 서술은 철회했고, manifest 판정·리포트·
  회귀 테스트를 fail-closed로 보강했다. gitignored 원본 stderr의 로컬 절대경로는 감사 원문으로
  유지되며 tracked 산출물 무경로 주장에 포함되지 않는다.
- 최종 독립 재검토에서 code-reviewer는 차단 없는 `COMMENT`, architect는 evidence-freeze
  범위의 `WATCH`로 판정했다. 두 검토 모두 이 PR의 증거 동결은 허용하되 ORB 연구 입력
  준비 완료로 해석하지 않고, #84의 tail repair와 clean-session immutable snapshot을
  선행하도록 요구했다.
- 독립 검증자는 대용량 전체 data hash 재계산을 중단했지만, 본 작업의 별도 결정론 재실행에서
  `data_content_sha256`, `raw_content_sha256`, 종목별 품질 결과와 provenance가 모두
  원본 manifest와 일치했다.

## 남은 결정

이번 작업은 최소 3년 커버리지 수집과 현재 품질 판정을 완료했다. ORB 공식 성과 연구 전에는
먼저 11종목의 후행 1~2거래일을 동일 cutoff 계약으로 보충하고 manifest를 다시 고정해야 한다.
그 다음 OVERNIGHT 257,799행에 대해 다음 중 하나를 별도 사전 승인·감사 하에 선택해야 한다.
이 blocking 후속은 GitHub 이슈 `#84`로 추적한다.

1. pre-bulk backup과 외부 복제본을 근거로 legacy OVERNIGHT 257,799행을 canonical DB에서
   감사 삭제한 뒤 manifest·외부 복제를 다시 고정한다.
2. canonical DB를 변경하지 않고 PRE/REGULAR/POST만 포함하는 immutable filtered derivative를
   만들고 그 snapshot hash를 ORB 연구 입력으로 잠근다.

어느 경로도 이번 결과를 보고 사후에 품질 기준을 완화해서는 안 된다.
