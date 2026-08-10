# 계약 — Blind OOS Unlock 검증 로더

> 이슈 #78 · 구현: `src/history/oos_unlock.py`
> 성격: OOS 개봉 **자격 검증기**. unlock artifact 생성기나 성과 실행기가 아니다.

## 목적

`OOSUnlock(track=..., snapshot_hash="아무 문자열", review_status="approved")` 같은 형식상 그럴듯한
값으로 blind OOS를 열 수 없게 한다. 성과 경로가 받는 객체는 Git·snapshot·독립 리뷰 증거를 모두
대조한 `load_verified_oos_unlock()`의 반환값뿐이다.

현재 1개월 연구는 `INCONCLUSIVE_DATA`로 종료됐으므로 **실 unlock artifact를 만들지 않으며 OOS도
열지 않는다.** 이 계약은 향후 별도 연구 계획이 진입 조건을 충족했을 때 사용할 인프라다.

## Artifact schema v1

```json
{
  "schema": "oos-unlock/v1",
  "track": "intraday",
  "prereg": {
    "path": "docs/research/PREREG_EXAMPLE.md",
    "merge_commit": "<full 40-char commit on main>"
  },
  "implementation": {
    "commit": "<full 40-char reviewed commit on main>"
  },
  "snapshot": {
    "manifest_path": "docs/research/SNAPSHOT_MANIFEST.json",
    "hash_field": "data_content_sha256",
    "hash": "<64-char content SHA-256 frozen in prereg>"
  },
  "review": {
    "status": "approved",
    "url": "https://github.com/OWNER/REPO/pull/123#pullrequestreview-456",
    "reviewer": "independent-reviewer",
    "reviewed_commit": "<same full implementation commit>"
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
6. review는 PR 작성자와 다른 GitHub 계정의 독립 리뷰이며 implementation commit과 정확히 결속된다.
   - formal GitHub review: `state=APPROVED`, reviewer, `commit_id` 직접 대조
   - PR issue comment: exact comment URL, reviewer, 독립된 `VERDICT: APPROVE[D]` 줄, full
     implementation commit 문면 대조(`NOT APPROVED` 같은 부정문은 거부)
   - plain PR URL: matching formal APPROVED review가 있어야 한다.
7. GitHub API·인증·네트워크·JSON 검증 중 하나라도 실패하면 `OOSLockError`로 잠금을 유지한다.
8. ancestry 기준은 호출자가 바꿀 수 없는 로컬 정본 `main`이다.

## 사용 경계

```python
from src.history.oos_unlock import load_verified_oos_unlock

unlock = load_verified_oos_unlock(
    "docs/research/OOS_UNLOCK_<TRACK>_<VERSION>.json",
    repo_root="/path/to/repo",
)
```

검증 성공 객체만 `research_load(..., purpose="performance", unlock=unlock)`에 전달할 수 있다.
`coverage` 목적은 기존 계약대로 성과·수익률 계산에 사용할 수 없다.

## 불변식과 비범위

- 이 구현은 unlock artifact, OOS 결과, forward return, 전략 성과를 생성하지 않는다.
- GitHub 리뷰 검증기를 호출자가 `lambda True`로 교체하는 공개 주입 경로가 없다.
- private verified factory를 production 코드가 직접 호출하면 AST 경계 테스트가 실패한다.
- Python 프로세스 자체가 악의적으로 monkeypatch된 상황까지 보안 경계로 삼지 않는다. 연구 코드의
  우발적·편의적 우회를 fail-closed로 막는 provenance 계약이다.
- schema v1의 unlock은 **track 단위 bearer**다. 실제 artifact를 만들기 전, 호출 전략이 artifact의
  implementation/prereg와 같은 실행 주체인지 별도 실행 게이트에서 결속해야 한다. 이 PR은 실제
  artifact를 만들지 않으므로 그 후속 실행 결속을 열거나 우회하지 않는다(후속 #113).
