# P0-US30 ORB clean-session 연구 스냅샷

## 판정

**COMPLETE — ORB 사전등록·구현에 사용할 수 있는 고정 1분봉 입력 스냅샷이다.**

- 대상: P0-US30, interval `1m`
- 범위: 2021-12-01 ~ 2026-07-21
- 허용 세션: `PRE`, `REGULAR`, `POST`
- 종목 verdict: **30 OK / 0 INCONCLUSIVE_DATA**
- tail gap: **0종목**
- `OVERNIGHT`: **0행**
- `as_of` 초과: **0행**
- collection provenance: **VERIFIED** (`p0-us30-1m-20260721-r82`)

이 판정은 데이터 coverage·무결성에 한정한다. 전략 성과, forward return, blind OOS
성과는 계산하거나 열람하지 않았다.

## canonical tail repair 감사

PR #83 품질 판정에서 허용 세션 최신일이 뒤처진 11종목만 최신 구간을 재조회했다.

`SPY`, `QQQ`, `XLU`, `MSFT`, `NVDA`, `AMD`, `AVGO`, `GOOGL`, `VST`, `SOXL`,
`SOXS`

tail repair는 다음 규율을 적용했다.

- 동일 run id와 cutoff 사용:
  `p0-us30-1m-20260721-r82`, `completed_through=2026-07-21`
- 저장 전 세션 필터: `PRE,REGULAR,POST`
- `OVERNIGHT`와 cutoff 이후 거래일은 저장하지 않음
- 최신부터 재조회하되 기존 deep backfill cursor는 11/11 byte-equivalent 값으로 보존
- canonical 기존 행 삭제·갱신 없음

첫 조회는 공급자가 2026-07-24 일자 경계에서 `nextBefore=null`을 반환해 저장 0행으로
종료했다. DB candle과 cursor는 변하지 않았고 ingest/raw 감사 로그만 남았다. 두 번째
조회는 기존 검증된 UTC 일자 경계 gap-skip을 사용해 2026-07-21까지 도달했다.

| 항목 | 결과 |
| --- | ---: |
| 공식 추가 행 | 19,219 |
| 기존 행 갱신 | 0 |
| duplicate | 10,988 |
| conflict candle | 1 |
| cutoff 이후 제외 | 전 종목 적용 |
| cursor 변경 | 0 / 11 |
| 전체 canonical 1m 행수 | 22,835,336 → 22,854,555 |
| raw payload 파일 | 154,181 → 154,621 |
| canonical 1d | 76,690행, data SHA `756e5bd7…` 불변 |

conflict는 `QQQ` 2026-07-20T09:09:00Z 한 봉의 `close`와 `volume` 차이다. 저장소
first-write-wins 계약에 따라 기존 값이 유지됐고 canonical 업데이트는 0이다. 이 사실은
`import_conflict`와 해당 run의 `ingest_log`에 보존됐다.

tail repair 후 canonical P0 manifest는 tail gap 0과 as_of 초과 0을 확인했다. 다만
canonical에는 PR #83에서 보존한 `OVERNIGHT` 257,799행이 그대로 있으므로
19 OK / 11 INCONCLUSIVE_DATA다. 원본 삭제 대신 clean derivative를 만든 이유다.

## immutable clean derivative

canonical을 WAL 없는 정지 상태에서 immutable read로 열고, P0-US30·1m·as_of 이하·허용
세션만 새 SQLite에 명시 컬럼명으로 복사했다. `.partial`에서 모든 검증을 통과한 뒤에만
atomic rename으로 승격했다.

| 항목 | 값 |
| --- | --- |
| basename | `p0_us30_orb_clean_1m_asof_20260721.bd9fc14.sqlite` |
| size | 7,285,862,400 bytes |
| rows / symbols | 22,534,553 / 30 |
| PRE / REGULAR / POST | 6,164,815 / 12,827,823 / 3,541,915 |
| file SHA-256 | `3c2c475125594bd7fe8c59d994bdc6d4cb9f1f1e0b69005b9fbcbf5eb852ca33` |
| content SHA-256 | `d0d145518938686937895fb3c886a4c2fd996588f129e8234a5913b6be2597e4` |
| manifest data SHA-256 | `e270a1b56d2466a856ef92e32abb58730953b7b77ce1038d7b596f66f477bd0c` |
| raw content SHA-256 | `22c02118c7c8bffc225fc021e2c4eb9ad4bf7b1d8c66df8e927909c46e609ed4` |
| integrity / quick | `ok` / `ok` |
| source SHA before == after | `4c213dd6…` == `4c213dd6…` |

snapshot content SHA는 저장 필드의 결정론 직렬화 지문이고, manifest data SHA는 기존
coverage manifest의 의미 필드 지문이다. 서로 다른 계약이라 값이 다른 것이 정상이다.

## 구현 감사 이력

첫 대용량 파생 시도는 최종 파일로 승격되지 않았고 `.partial`이 제거됐다. 재실행에서
원인은 운영 DB의 `ingest_log`가 과거 ALTER migration으로 신규 DB와 컬럼 순서가 달랐는데
초기 구현이 `SELECT *`를 사용한 것이었다. candle 데이터가 아니라 provenance 행의 컬럼
매핑 결함이며, fail-closed `NO_RUN_LOGS`가 이를 차단했다.

수정 후 모든 복사 대상 테이블을 명시 컬럼명으로 복사한다. 운영 DB와 같은 legacy column
order를 실제 SQLite로 재현하는 회귀 테스트를 추가했고, provenance `VERIFIED`까지
검증했다. 실패 단계명도 예외에 포함해 같은 진단 유실을 막았다.

## 외부 복제와 restore smoke

Apple iCloud Drive `icloud-documents` target에 clean SQLite와 raw snapshot을 각각
고유 이름으로 복제했다. `brctl status com.apple.CloudDocs`는 생성 이후
2026-07-26 19:01:53 KST에 `caught-up`을 보고했다.

### clean SQLite

- replica:
  `p0_us30_orb_clean_1m_asof_20260721.bd9fc14.replica.20260726T092500Z.sqlite`
- snapshot == replica SHA-256:
  `0edb571e4ad3059ab298572e9d220b10b6a16d0945854998b6b2bafb0d9a32ef`
- replica integrity: `ok`
- 의미 일치: true
- restore smoke: `ok`

SQLite backup API는 새 페이지 레이아웃을 만들 수 있으므로 derivative 원본 파일 SHA와
replica SHA가 다른 것은 정상이다. 판정은 backup snapshot==replica, integrity, 행수·기간·
심볼 의미 일치와 restore smoke다.

### raw snapshot

- archive:
  `raw_snapshot.p0-us30-1m-20260721-r82.clean-tail.payload-verified.bd9fc14.20260726T092500Z.tar`
- size: 698,777,600 bytes
- archive SHA-256:
  `79849bf309a8de1fed90a21c02ff3abe85d6e37394445b67fc8f013bf65f3fbc`
- restored files: 154,621
- source payload SHA 검증: 154,621 / 154,621
- restored payload SHA 검증: 154,621 / 154,621
- restored raw content SHA-256:
  `22c02118c7c8bffc225fc021e2c4eb9ad4bf7b1d8c66df8e927909c46e609ed4`
- source==restore manifest: true

## 산출물 경계

- tracked: manifest, report, sanitized snapshot/replica receipts, 생성·복제 도구와 테스트
- gitignored: canonical DB, clean SQLite 본체, full raw tree, 상세 운영 receipt
- 변경 없음: 주문, broker, risk, Paper, 전략, runtime config
- 미열람: 전략 성과, forward return, blind OOS

## 테스트

- tail repair·snapshot·raw manifest/replica targeted: **54 passed**
- 전체 pytest: **1039 passed**
- `git diff --check`: clean
