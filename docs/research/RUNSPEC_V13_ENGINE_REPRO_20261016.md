# 실행 명세: v1.3 경로 엔진 재현 (수정 전후 비교)

_2026-10-16 작성. 근거: EVALUATION_PATCH_IMPLEMENTATION_PROMPT(사용자 전달, 2026-10-16) §2 단계 B·§8, MINIMAL_PATCH_PLAN §4.
이 문서는 **엔진 검증**이다. 전략 판정·Paper·승격 근거가 아니며, 결과로 전략 판정을 바꾸지 않는다._

## 무엇을 하나

- 기각된 v1.3 사전등록의 두 팔을 고정 기준점으로 쓴다: R-a `v1.0+`, R-b `v1.3`(1% 위험). 파라미터는 사전등록 그대로이고 바꾸지 않는다.
- 같은 연구 사본·같은 고정 v1.1 마스크(`84d22f46…`)·같은 이벤트 달력·같은 외부 CSV로
  수정 전 코드(`bab3c0d`)와 수정 후 코드를 돌려 거래·수량·순손익이 같은지 본다(CM030 = 기존 v1.0 기본 비용과 같은 값).
- 수정 후 코드로 CM020(슬리피지 0) 진단을 같은 두 팔에 한 번씩 돌린다. 진단이며 기준·승격에 쓰지 않는다.
- R-c(구형 탐색 엔진)는 이번 범위에서 뺀다.

## 예산

승인 `AUTH-20261016-V13-ENGINE-REPRO`: run_arm 8단위(1단위 = 한 팔 × 한 비용 모델, A·B 두 계좌 = run_account 2회).
계획 6단위(수정 전 CM030 2, 수정 후 CM030 2, 수정 후 CM020 2), 동일성 확인·오류 재시도 최대 2단위. 자동 재시도는 없다.

## 기존 실패·노출 이력

- 두 팔은 이미 결과를 본 기각 전략이다(PROJECT_STATUS 2026-10-10: v1.0+ G −2.65%, v1.3 G −8.47%, v1.3 기각).
- 데이터는 여러 연구가 반복해서 본 개발 구간이다(BACKTEST_AUDIT §5.2). B 구간 날짜 일부는 일봉 봉인 날짜와 겹친다(1분봉, 사본 매니페스트에 기록).
- 사본은 A 시작일(2023-04-11)부터라 2026-10-15 QQQ 보충분(그 이전)은 들어 있지 않다.

## 멈추는 조건

- 사본에서 다시 만든 마스크의 채점·제외일이 고정 마스크와 다르면 계산 없이 멈춘다(MASK_MISMATCH).
- 수정 전후 CM030 결과가 다르면 원인만 보고하고 진단 실행으로 넘어가지 않는다.
- 실행 오류는 자동 재시도하지 않는다.

```json run-spec
{
 "spec_id": "RUNSPEC_V13_ENGINE_REPRO_20261016",
 "run_type": "ENGINE_REPRO",
 "authorization_id": "AUTH-20261016-V13-ENGINE-REPRO",
 "cost_contract_version": "cost_contract/v2 (Charter v2.6)",
 "cost_model_ids": ["CM030_v1", "CM020_diag_v1"],
 "evaluation_contract_version": "v13-engine-repro/v1 (PREREG_DAYTRADE_V1_3_SIZING arms, v1.1 mask 84d22f46)",
 "data": {
  "copy_sha256": "378a1f42ab65c9ee39b6c8052a82ac014bf15ab519bfa7dda40ce1f6139431b6",
  "allowlist_sha256": "b06c04789ffb8a8ae5f9e1e3fbbff28d01ce1ba8732454fb4c9151019db331df",
  "mask_sha256": "84d22f461abff1a7e9ef3e4c80d337f5ef12667ea2bef66eb36abcd18404fac2",
  "symbols": ["QQQ", "TQQQ", "SQQQ"],
  "interval": "1m",
  "windows": [{"start": "2023-04-11", "end_exclusive": "2026-01-21"}]
 },
 "runs": [
  {"run_key": "pre-CM030-Ra", "phase": "pre", "policy": "v1.0+", "cost_model_id": "CM030_v1"},
  {"run_key": "pre-CM030-Rb", "phase": "pre", "policy": "v1.3", "cost_model_id": "CM030_v1"},
  {"run_key": "post-CM030-Ra", "phase": "post", "policy": "v1.0+", "cost_model_id": "CM030_v1"},
  {"run_key": "post-CM030-Rb", "phase": "post", "policy": "v1.3", "cost_model_id": "CM030_v1"},
  {"run_key": "diag-CM020-Ra", "phase": "diag", "policy": "v1.0+", "cost_model_id": "CM020_diag_v1"},
  {"run_key": "diag-CM020-Rb", "phase": "diag", "policy": "v1.3", "cost_model_id": "CM020_diag_v1"}
 ],
 "evidence": {
  "difference_from_prior_failures": "not a new hypothesis: engine reproduction of the rejected v1.0+/v1.3 arms with fixed rules",
  "changed_factors_and_reason": "code only: reporting fields and boundaries added (commits 428435c, fc0293d, a382680); no strategy parameter changes",
  "edge_over_cost_basis": "not applicable: no edge is claimed; CM020 is a diagnostic and decides nothing",
  "data_and_exposure_history": "DEV segments already seen many times (BACKTEST_AUDIT 5.2); copy starts 2023-04-11; B overlaps daily-seal dates (recorded in the copy manifest)",
  "prior_discard_reasons_still_apply": "yes: v1.3 rejected 2026-10-10 and all tested strategies discarded 2026-10-14; this run does not revisit them",
  "budget_stop_and_bad_result": "6 of 8 run_arm units; stop on MASK_MISMATCH, on a pre/post CM030 difference or on an error; no automatic retry",
  "instruction_source": "user: EVALUATION_PATCH_IMPLEMENTATION_PROMPT.md (2026-10-16) section 2 stage B and section 8"
 }
}
```
