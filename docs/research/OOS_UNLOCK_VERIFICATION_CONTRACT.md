# 계약 — Blind OOS Unlock 검증 로더

> 이슈 #78, #113 · 구현: `src/history/oos_unlock.py`
> 성격: OOS 개봉 **자격 검증기**. unlock artifact 생성기나 성과 실행기가 아니다.

## 목적

`OOSUnlock(track=..., snapshot_hash="아무 문자열", review_status="approved")` 같은 형식상 그럴듯한
값이나, 다른 전략에 발급된 track 단위 artifact로 blind OOS를 열 수 없게 한다. artifact 검증 뒤
현재 실행 전략·사전등록 config·구현 파일·snapshot을 다시 대조한 `OOSExecutionGrant`만 성과
경로에 전달할 수 있다.

현재 1개월 연구는 `INCONCLUSIVE_DATA`로 종료됐으므로 **실 unlock artifact를 만들지 않으며 OOS도
열지 않는다.** 이 계약은 향후 별도 연구 계획이 진입 조건을 충족했을 때 사용할 인프라다.

## Artifact schema v2

v1은 실제 artifact가 만들어지기 전에 폐기됐다. v2는 실행 주체 identity를 artifact에 포함한다.

```json
{
  "schema": "oos-unlock/v2",
  "track": "intraday",
  "execution": {
    "strategy_id": "p0-us30-orb"
  },
  "prereg": {
    "path": "docs/research/PREREG_EXAMPLE.md",
    "merge_commit": "<full 40-char commit on main>",
    "config_path": "docs/research/PREREG_EXAMPLE.json",
    "config_sha256": "<64-char SHA-256 of config blob at prereg commit>"
  },
  "implementation": {
    "commit": "<full 40-char reviewed commit on main>",
    "paths": [
      "scripts/example_runner.py",
      "src/research/example/strategy.py"
    ]
  },
  "snapshot": {
    "manifest_path": "docs/research/SNAPSHOT_MANIFEST.json",
    "hash_field": "data_content_sha256",
    "hash": "<64-char content SHA-256 frozen in prereg>"
  },
  "review": {
    "implementation": {
      "status": "approved",
      "url": "https://github.com/OWNER/REPO/pull/123#pullrequestreview-456",
      "reviewer": "independent-reviewer",
      "reviewed_commit": "<same full implementation commit>"
    },
    "artifact": {
      "status": "approved",
      "url": "https://github.com/OWNER/REPO/pull/124#pullrequestreview-789",
      "reviewer": "independent-artifact-reviewer"
    }
  }
}
```

허용 snapshot hash leaf는 `data_content_sha256`, `raw_content_sha256`, `content_sha256`뿐이다.
파일 자체 SHA처럼 재복제로 바뀌는 값이나 임의 필드는 unlock identity로 쓸 수 없다.

## 필수 검증

1. artifact와 manifest/prereg 경로가 저장소 내부의 일반 파일이다(symlink·path escape 거부).
2. artifact가 Git 추적·clean 상태이고 현재 내용이 `main`에 병합됐다.
3. artifact 경로의 Git 이력은 최초 `add` 한 번뿐이다. 수정·덮어쓰기는 append-only 위반이다.
4. prereg/implementation은 full commit SHA이며 실존하고 둘 다 `main`의 ancestor다.
5. manifest의 지정 content hash가 artifact와 같고, 같은 hash가 prereg merge commit의 문면에
   결과 확인 전에 고정되어 있다.
6. `review.implementation`은 PR 작성자와 다른 GitHub 계정의 독립 리뷰이며 implementation
   commit과 정확히 결속된다.
   - formal GitHub review: `state=APPROVED`, reviewer, `commit_id` 직접 대조
   - PR issue comment: exact comment URL, reviewer, 독립된 `VERDICT: APPROVE[D]` 줄, full
     implementation commit 문면 대조(`NOT APPROVED` 같은 부정문은 거부)
   - plain PR URL: matching formal APPROVED review가 있어야 한다.
7. GitHub API·인증·네트워크·JSON 검증 중 하나라도 실패하면 `OOSLockError`로 잠금을 유지한다.
8. ancestry 기준은 호출자가 바꿀 수 없는 로컬 정본 `main`이다.
9. strategy ID, prereg/config 경로, 구현 경로 목록, snapshot 경로/필드는 exact match다.
10. prereg config의 Git blob SHA-256은 prereg merge commit과 artifact 선언값이 같아야 한다.
11. 구현 경로는 정렬·중복 없는 비어 있지 않은 목록이며 `src/`, `scripts/`, `analysis/` 아래
    Python 파일만 허용한다. 각 파일은 독립 리뷰 implementation commit에 존재해야 한다.
12. `review.artifact`는 artifact에 self-reference commit SHA를 넣지 않는다. verifier가
    append-only 최초 add commit을 Git에서 계산하고, 그 실제 commit에 대한 별도 독립 GitHub
    승인을 대조한다. 따라서 구현 경로 목록 자체도 리뷰되지 않은 채 추가될 수 없다.

## 사용 경계

```python
from src.history.oos_unlock import (
    OOSExecutionSpec,
    bind_verified_oos_execution,
    load_verified_oos_unlock,
)

unlock = load_verified_oos_unlock(
    "docs/research/OOS_UNLOCK_<TRACK>_<VERSION>.json",
    repo_root="/path/to/repo",
)

grant = bind_verified_oos_execution(
    unlock,
    OOSExecutionSpec(
        strategy_id="p0-us30-orb",
        prereg_path="docs/research/PREREG_EXAMPLE.md",
        config_path="docs/research/PREREG_EXAMPLE.json",
        implementation_paths=(
            "scripts/example_runner.py",
            "src/research/example/strategy.py",
        ),
        snapshot_manifest_path="docs/research/SNAPSHOT_MANIFEST.json",
        snapshot_hash_field="data_content_sha256",
    ),
    repo_root="/path/to/repo",
)
```

결속 성공 객체만 `research_load(..., purpose="performance", strategy_id="p0-us30-orb",
execution_grant=grant)`에 전달할 수 있다. 로더는 grant의 track과 strategy ID를 모두 대조하고,
결속기는 현재 `main`의 config/구현/snapshot을 prereg·리뷰 commit·artifact와 다시 비교한다.
`coverage` 목적은 기존 계약대로 성과·수익률 계산에 사용할 수 없다.

## 불변식과 비범위

- 이 구현은 unlock artifact, OOS 결과, forward return, 전략 성과를 생성하지 않는다.
- GitHub 리뷰 검증기를 호출자가 `lambda True`로 교체하는 공개 주입 경로가 없다.
- private artifact/grant factory를 production 코드가 직접 호출하면 AST 경계 테스트가 실패한다.
- Python 프로세스 자체가 악의적으로 monkeypatch된 상황까지 보안 경계로 삼지 않는다. 연구 코드의
  우발적·편의적 우회를 fail-closed로 막는 provenance 계약이다.
- `OOSUnlock`은 증거 객체이며 성과 경로를 직접 열 수 없다. `OOSExecutionGrant`는 artifact의
  strategy/config/implementation/snapshot과 현재 main이 모두 동일할 때만 생성된다.
- 구현 경로 목록의 완전성은 독립 리뷰가 확인한다. 검증기는 비어 있음·경로 변조·리뷰 commit에
  없는 파일·현재 main drift를 막고, artifact add commit 리뷰를 별도로 강제하지만, 의미상 필요한
  파일을 자동 추론하지 않는다.
