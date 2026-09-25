# 장전·사후평가 로컬 운영 계약

## 목적

장전 증거 수집과 사후평가가 Codex 사용량이나 대화형 에이전트의 연속 실행에 의존하지 않도록,
저장소의 결정론적 CLI와 macOS `launchd`가 PRIMARY → VERIFICATION → FREEZE → 장마감
사후평가를 실행한다. 이 계약은 주문·브로커·Paper 체결·승격 권한을 추가하지 않는다.

## 실행기

`scripts/run_premarket_pipeline.py`는 다음 두 가지 동작만 제공한다.

- `--stage PRIMARY|VERIFICATION|FREEZE`: 해당 NYSE stage의 provider bundle, market bundle,
  normalized input, Daily Brief를 순서대로 생성한다.
- `--postclose`: FREEZE Brief의 watchlist와 SPY만 최신 정규장 1분봉으로 보충한 뒤,
  read-only post-close 방향성 평가기를 실행한다.

거래일 인자를 생략하면 현재 New York 날짜를 사용한다. NYSE 비거래일 또는 DST 보조 trigger의
비해당 시간에는 네트워크를 호출하지 않고 `SKIPPED`로 끝난다. 실제 stage window에서 실패해
Daily Brief가 완성되지 않으면 immutable `INCOMPLETE` operation receipt를 남긴다. 각 stage는
기존 immutable artifact가 있으면 동일 파일을 재사용하며, 완결 stage는 immutable `COMPLETE`
operation receipt를 쓴다.

## 실패 처리

Toss market collector는 OAuth·HTTP·timeout·네트워크 예외를 traceback만 남기고 끝내지 않는다.
`premarket-market-bundle/v1`의 표준 `FAIL_CLOSED` artifact와 sanitized receipt를 생성한다.
따라서 다음 정규화/Brief 단계는 계속 실행되어 감사 가능한 `NO_TRADE`로 닫힌다.

이 artifact에는 candle을 추측하거나 채우지 않는다. 결함은 예를 들어
`TOSS_HTTP_STATUS_403`, `TOSS_HTTP_TIMEOUT`, `TOSS_NETWORK_ERROR`로만 기록한다. 원문 오류,
credential, request header는 산출물·로그·Git에 기록하지 않는다.

FREEZE Brief가 없거나 변조되었으면 post-close는 backfill과 평가를 시작하지 않고
`INCOMPLETE/MISSING_FREEZE_BRIEF`를 보고한다. 불완전 1분봉도 기존 post-close 계약대로
0 수익으로 대체하지 않는다.

## launchd 일정

`scripts/install_premarket_launchd.py --install`은 현재 macOS 사용자의 LaunchAgents에
아래 여덟 jobs를 설치한다. New York DST에 따라 같은 stage의 두 KST 시각 중 하나만 실제
NYSE window에 들어가며, 다른 하나는 `SKIPPED/STAGE_WINDOW_NOT_REACHED`로 끝난다. 시간은
macOS 로컬 시간대(KST)이며 NYSE 휴장일은 실행기가 스스로 skip한다.

| Job | KST | 동작 |
| --- | ---: | --- |
| primary | 21:01, 22:01 | PRIMARY stage, DST-safe pair |
| verification | 22:01, 23:01 | VERIFICATION stage, DST-safe pair |
| freeze | 22:11, 23:11 | FREEZE stage, DST-safe pair |
| postclose | 05:20, 06:20 | 같은 시점의 ET 날짜 FREEZE 정규장 사후평가, DST-safe pair |

plist에는 credential을 넣지 않는다. Python CLI가 프로젝트 `.env`를 로드한다. stdout/stderr는
`reports/premarket-launchd/`에 남고, 생성 산출물과 operation receipt는 `reports/` 및 `data/`
gitignore 경로에만 보관한다.

설치 후에는 `scripts/install_premarket_launchd.py --status`로 plist 존재와 launchd load 상태를
확인한다. 실제 Toss OAuth 403은 로컬 코드가 해결할 수 없는 외부 계정/IP/credential 상태이므로
`NO_TRADE`로 닫고, 별도 연결 점검 뒤에만 다음 거래일을 유효 관측으로 취급한다.

## 2026-09-24 장애 근거

해당 거래일은 전향적 유효 관측이 아니다.

- PRIMARY는 Marketaux timeout과 Toss OAuth 403을 `FAIL_CLOSED → NO_TRADE`로 보존했다.
- VERIFICATION은 같은 Toss 403 뒤에 Codex workspace credit 부족으로 중단됐다.
- FREEZE는 시작 직후 Codex workspace credit 부족으로 실행되지 않았다.

이 계약은 Codex credit 부족을 제거하는 것이 아니라, 해당 의존성을 데이터 수집 경로에서
제거한다. Marketaux 및 Toss 공급자 오류는 계속 fail-closed다.
