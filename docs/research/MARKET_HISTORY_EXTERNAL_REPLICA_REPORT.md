# market-history 외부 저장소 복제 리포트 (#73)

- schema: `history-external-replica/report/v1` · interval: **1d + 1m** · source DB: `data/market_history.sqlite`
- 수집 코드 커밋(source_commit): `bb5580f` (PR #71 merge, P0-30 일봉 10년 backfill)
- **canonical main-file SHA-256**: `4c72beccd8c625d6b72e1ce8437f363042304855ce8728b785a233dcac9e026f`
- generated_at_utc: 2026-07-22T16:25Z (hash 비대상)
- **상태: COMPLETE** — 도구·테스트·검증 완료 + iCloud Drive 로 실제 외부 복제·업로드 검증 완료(아래 "외부 복제본").

이 리포트는 운영 provenance 다. 기존 P0 일봉 manifest
(`docs/research/P0_US30_DAILY_10Y_MANIFEST.json`, `data_content_sha256=756e5bd7…`)의
**데이터 지문은 변경하지 않는다**. 외부 복제 provenance 는 데이터 내용 hash 와
분리해 이 문서(및 gitignored `reports/` receipt)에만 기록한다.

## 배경 / 목적

canonical `data/market_history.sqlite` 와 `data/backups/*.sqlite` 백업은
**로컬 디스크에만** 존재한다(gitignore, GitHub 미저장). 디스크 손실 시 Toss API
재소모(재수집 비용) 위험이 남는다(#73). 목적은 canonical DB(또는 검증된 백업)를
**외부 저장소**(외장 디스크/NAS/검증 가능한 클라우드 remote)에 복제하고,
원본↔복제본 무결성과 임시 복원 가능성을 증명하는 것이다.

★ 같은 내부 APFS 볼륨의 다른 폴더에 복사한 것은 #73 완료로 인정하지 않는다.
★ 시크릿·자격증명은 외부 저장소 설정에서 분리한다. 데이터 파일만 복제한다.

## 도구: `scripts/replicate_history_backup.py`

표준 라이브러리만 사용하는 재사용 CLI. 핵심 계약:

| 옵션 | 설명 |
| --- | --- |
| `--source-db` | 복제할 원본 DB(변경하지 않음) |
| `--target-dir` | 외부 저장소 디렉터리 |
| `--target-id` | target 식별 라벨(provenance) |
| `--target-kind` | `external_disk`/`nas`/`cloud_sync`/`cloud_object`/`other` |
| `--dry-run` | read-only 검사만, 파일 생성 안 함 |
| `--receipt` | 상세 receipt JSON(절대경로 포함, `reports/` gitignore 권장) |
| `--sanitized-receipt` | sanitized JSON(절대경로/홈경로 제외) |
| `--retention-count` | 남길 최신 복제본 수(기본 **OFF**, 삭제 안 함) |
| `--allow-same-device` | 동일 device target 허용(검증된 클라우드 동기화 폴더 등, 명시 opt-in) |

**안전 모델(fail-closed):**

1. 원본은 immutable read-only(`mode=ro&immutable=1`)로만 연다 → 원본 파일 미변경,
   -wal/-shm 사이드카도 생성하지 않는다. 미체크포인트 WAL 이 있으면 원본을 sqlite
   로 열지 않고 원본+`-wal` 을 임시본으로 **복사**해 checkpoint 후 읽는다.
2. 일관 snapshot 은 **SQLite backup API** 로 생성한다(단순 `cp` 아님).
3. `<name>.partial` 로 먼저 쓰고 flush/fsync 후 같은 파일시스템에서 **atomic rename**.
4. 기존 같은 이름 파일을 조용히 덮어쓰지 않는다(overwrite 거부).
5. 저장소 내부·원본 경로·동일 파일·동일 device target 을 거부(symlink realpath 해석).
6. SHA-256 + `PRAGMA integrity_check` + interval 행수/symbol/기간이 모두 일치할
   때에만 SUCCESS 를 확정한다.
7. 외부 복제본을 **임시 로컬 경로**로 복원하는 restore smoke test 로 복원 가능성을
   증명한다(canonical 위치에는 복원하지 않는다).
8. 실패 시 partial(및 rename 이후 검증 실패면 방금 만든 실패본)을 성공본처럼
   남기지 않는다. 기존의 다른 성공 복제본은 보존한다.
9. 시크릿·자격증명·환경변수 값은 읽지도 기록하지도 않는다.

테스트: `tests/test_history_backup_replication.py` (24 케이스) — 정상 snapshot,
WAL 일관성, atomic rename, overwrite 거부, 동일/저장소내부/동일device 거부,
path traversal 거부, 손상 DB 시 SUCCESS 금지, 실패 시 성공본 보존, restore smoke,
receipt 결정성·시크릿 비노출, dry-run 무생성, retention.

## 원본(canonical) 지문

immutable read-only 로 측정(원본 미변경):

- file: `data/market_history.sqlite` · size: **548,896,768 bytes** ·
  SHA-256: `4c72becc…e026f`
- `PRAGMA integrity_check`: **ok**
- journal_mode: WAL(체크포인트 완료, 활성 writer 없음)
- total rows: **1,001,836** · distinct symbols: **34**

| interval | rows | symbols | min_ts (UTC) | max_ts (UTC) |
| --- | --- | --- | --- | --- |
| 1d | 76,690 | 30 | 2016-03-18T04:00:00+00:00 | 2026-07-21T04:00:00+00:00 |
| 1m | 925,146 | 15 | 2025-11-17T07:20:00+00:00 | 2026-07-20T09:21:00+00:00 |

## 도구 실기 검증 (외부 대상과 무관, 실 523MB 원본)

저장소 밖 스크래치 디렉터리(볼륨 외부, 검증 후 삭제)로 실제 523MB 복제를 수행해
전 경로를 검증했다:

- status: **success** · 소요 ~12s
- snapshot SHA-256 == replica SHA-256: `ae572a0f…8483` (일치)
- replica `integrity_check`: **ok** · rows/symbols/기간: 원본과 완전 일치
  (1d 76,690/30, 1m 925,146/15, total 1,001,836, symbols 34)
- restore smoke: **ok**(임시 복원본 read-only 재검증, restored SHA == replica SHA)
- 원본 미변경 재확인: canonical main-file SHA-256 = `4c72becc…e026f` (검증 전후 동일),
  -wal/-shm 사이드카 미생성

replica SHA(`ae572a0f…`)가 canonical **파일** SHA(`4c72becc…`)와 다른 것은 정상이다:
backup API 는 새 페이지 레이아웃의 fresh DB 를 만든다. #73 무결성 기준은
snapshot(.partial) == 외부 복제본(SHA 동일) + 원본↔복제본 **의미**(행수/symbol/기간)
동일이며, 둘 다 충족한다.

## 외부 복제본 (external replica) — 상태: COMPLETE

canonical DB 를 실제 외부(클라우드) 저장소인 **Apple iCloud Drive** 로 복제하고
업로드를 검증했다. 대상은 사용자의 iCloud 동기화 폴더(Documents 하위
`market-history-backup/`, target_kind=`cloud_sync`)이며, iCloud 데몬(`brctl`)이 이
경로를 추적·업로드함을 직접 확인했다(홈 절대경로는 gitignored receipt 에만 기록).

| 항목 | 값 |
| --- | --- |
| target_id | `icloud-documents` |
| target_kind | `cloud_sync` (Apple iCloud Drive) |
| replica 파일명 | `market_history.replica.20260722T160829Z.sqlite` |
| size_bytes | 548,896,768 |
| SHA-256 (snapshot == replica) | `ae572a0f4be126c64afd443c9e5661bd08acf62da105d2c2af7014fa22418483` |
| created_utc | 2026-07-22T16:08:29Z |
| integrity_check | ok |
| rows / symbols / 기간 | 1d 76,690/30, 1m 925,146/15, total 1,001,836 / 34 심볼 — 원본과 완전 일치 |
| restore smoke | ok (임시 로컬 복원본 read-only 재검증, restored SHA == replica SHA) |
| 같은 내부 디스크 아님 증명 | iCloud 업로드로 증명. `brctl` 로 업로드 진행 0%→90%+ 관측 후 `☁`(in-cloud) 전이 + 컨테이너 `caught-up`(last-sync 2026-07-22T16:24Z). 로컬 캐시는 내부 디스크이나 데이터가 Apple iCloud 서버로 실제 업로드됨(단순 내부 폴더 복사와 구별). |

- 원본 snapshot(.partial) SHA == 외부 복제본 SHA == `ae572a0f…8483`.
- 업로드 완료 후 로컬 복제본 재검증: size 548,896,768, integrity ok, SHA `ae572a0f…`
  유지(미변조·미eviction, 로컬 데이터 온전).
- 상세 provenance(절대경로 포함)는 gitignored
  `reports/history_external_replica_receipt.<UTC>.json`, sanitized 지문은
  `docs/research/MARKET_HISTORY_EXTERNAL_REPLICA_RECEIPT.json`(추적).

### 재현/독립 대조

```
# 외부 복제본 SHA-256 독립 대조(원본 snapshot 과 동일해야 함)
shasum -a 256 "<iCloud>/market-history-backup/market_history.replica.20260722T160829Z.sqlite"
# → ae572a0f4be126c64afd443c9e5661bd08acf62da105d2c2af7014fa22418483

# iCloud 업로드 상태(☁ = in-cloud, caught-up)
brctl monitor com.apple.CloudDocs | grep market_history.replica   # ☁, no ↑
brctl status  com.apple.CloudDocs                                 # caught-up
```

## 안전 / 무결성 재확인

- canonical DB·기존 백업(`data/backups/*.sqlite`): 삭제·이동·압축·VACUUM·수정 **0**.
  canonical main-file SHA-256 은 세션 내내 `4c72becc…e026f` 로 불변.
- 매매·Paper·RiskManager·전략·config: 변경 **0**(이 작업은 데이터 durability 도구만 추가).
- 기존 P0 일봉 manifest `data_content_sha256=756e5bd7…`: **무변경**.
- git diff: `*.sqlite`/`reports/` 는 gitignore 로 커밋 대상 아님. 시크릿 커밋 **0**.

## 자동화 / 보존 정책 (초안)

- retention: 기본 **OFF**. `--retention-count N` 명시 시에만 target 의 과거 복제본을
  최신 N 개로 정리(방금 만든 복제본은 보호, 복제본 네이밍 패턴만 대상).
- 수집 후 자동 복제 hook: 외부 대상 확정 후 별도 이슈로 결정(#73 범위 밖).
