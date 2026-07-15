"""Phase 6 승격조건 판정 리포트 — 여러 세션 shadow outcome + coverage/regime 집계 배치(PR #38).

plan §Phase 6 의 7개 최소조건 충족 여부를 (market, policy, horizon) 셀별로 표/JSON 출력한다.
**read-only·행동 변경 없음**(런타임 미투입). 승격 신호는 entry_only=True(ORDERED) 기준.
조건 #5(비용·MDD)는 항상 DEFER(price-direction shadow 로 판정 불가).

세션 지정:
  --session data/journal_us_phase6_s001.sqlite            # candles/manifest 추론
  --session data/journal_x.sqlite:data/market_x.sqlite    # candles 명시
  --session data/j.sqlite:data/m.sqlite:data/j.sqlite.manifest.json  # 셋 다 명시
추론 규칙: candles = journal 경로의 'journal'→'market' 치환, manifest = '{journal}.manifest.json'.

사용:
  ./.venv/bin/python scripts/phase6_promotion_report.py \
      --session data/journal_us_phase6_s001.sqlite \
      --session data/journal_us_phase6_s002.sqlite --horizons 30,60
  ... --json
설계: docs/DESIGN_DELTA_V2_1.md §2-4, plan §Phase 6
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.journal.sqlite_store import SqliteJournalStore  # noqa: E402
from src.runtime.isolation.manifest import read_manifest  # noqa: E402
from src.shadow.promotion import evaluate_promotion  # noqa: E402
from src.shadow.promotion_io import analyze_session, to_signal  # noqa: E402
from src.storage.candle_store import CandleStore  # noqa: E402


def resolve_session_spec(spec: str) -> tuple[str, str, str]:
    """'journal[:candles[:manifest]]' → (journal, candles, manifest) 절대/상대 경로.

    candles 미지정 → journal 경로의 'journal'→'market' 치환. manifest 미지정 →
    '{journal}.manifest.json'(resolve_session_paths 규약)."""
    parts = spec.split(":")
    journal = parts[0]
    candles = parts[1] if len(parts) > 1 and parts[1] else _infer_candles(journal)
    manifest = parts[2] if len(parts) > 2 and parts[2] else journal + ".manifest.json"
    return journal, candles, manifest


def _infer_candles(journal: str) -> str:
    d, base = os.path.split(journal)
    return os.path.join(d, base.replace("journal", "market", 1))


def _load_session(spec: str, horizons: tuple[int, ...]):
    journal_path, candles_path, manifest_path = resolve_session_spec(spec)
    if not os.path.exists(journal_path):
        raise FileNotFoundError(f"journal 없음: {journal_path}")
    if not os.path.exists(candles_path):
        raise FileNotFoundError(f"candles 없음: {candles_path} (--session j:candles 로 명시)")
    manifest = read_manifest(manifest_path) if os.path.exists(manifest_path) else None
    label = os.path.splitext(os.path.basename(journal_path))[0]
    session_id = getattr(getattr(manifest, "identity", None), "session_id", None) or label

    journal = SqliteJournalStore(journal_path)
    candles = CandleStore(candles_path)
    try:
        inputs, aggregates = analyze_session(
            journal, candles, manifest=manifest, session_id=session_id, horizons=horizons)
    finally:
        journal.close()
        candles.close()
    return inputs, aggregates


_STATUS_MARK = {"PASS": "PASS ", "FAIL": "FAIL ", "DEFER": "DEFER"}


def _print_report(report):
    print(f"\n=== market={report.market} policy={report.policy_id} "
          f"horizon=+{report.horizon_min}m — GATE: {report.gate_status} ===")
    for c in report.criteria:
        print(f"  [{_STATUS_MARK.get(c.status, c.status)}] {c.title:<28} : {c.detail}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Phase 6 승격조건 판정 리포트(read-only)")
    ap.add_argument("--session", action="append", default=[],
                    help="journal[:candles[:manifest]] (반복 지정)")
    ap.add_argument("--horizons", default="30,60", help="쉼표 구분 분 단위 horizon")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        horizons = tuple(int(h) for h in args.horizons.split(",") if h.strip())
    except ValueError:
        print(f"--horizons 는 쉼표 구분 정수여야 합니다(입력: {args.horizons!r})", file=sys.stderr)
        return 2
    if not horizons:
        print("--horizons 가 비어있습니다.", file=sys.stderr)
        return 2

    if not args.session:
        print("세션이 지정되지 않았습니다. --session <journal.sqlite> 를 최소 1개 지정하세요.",
              file=sys.stderr)
        print("(실 Phase 6 데이터(market_context+shadow ON) 축적 전에는 판정할 세션이 없습니다.)",
              file=sys.stderr)
        return 2

    sessions = []
    for spec in args.session:
        try:
            sessions.append(_load_session(spec, horizons))
        except Exception as e:  # noqa: BLE001 — 세션 로드 실패는 명확히 보고하고 중단(fail-fast)
            print(f"세션 로드 실패({spec}): {e}", file=sys.stderr)
            return 1

    markets = sorted({inp.market for inp, _ in sessions if inp.market})
    policy_ids = sorted({p for _, agg in sessions for h in horizons for p in agg[h]})

    # 기계 판독용 요약(항상 산출)
    n_primary = {m: sum(1 for inp, _ in sessions if inp.market == m
                        and inp.completion_status == "FULL" and inp.manual_flatten_count == 0)
                 for m in markets}

    if args.json:
        out = {
            "meta": {"n_sessions": len(sessions), "markets": markets,
                     "policies": policy_ids, "horizons": list(horizons),
                     "n_primary_by_market": n_primary,
                     "note": "price-direction shadow; #5(cost/MDD) always DEFER; "
                             "signal=entry_only(ORDERED)"},
            "reports": {m: {p: {h: evaluate_promotion(
                [to_signal(inp, agg[h].get(p), h) for inp, agg in sessions],
                policy_id=p, horizon_min=h, market=m).to_dict()
                for h in horizons} for p in policy_ids} for m in markets},
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"=== Phase 6 승격조건 판정 (세션 {len(sessions)}개) ===")
    print("⚠ price-direction shadow: 결정봉 종가 대비 방향 프록시(실 체결/비용/포트폴리오 MDD 아님).")
    print("  승격 신호=entry_only(ORDERED) 집계. 조건 #5(비용·MDD)는 항상 DEFER.")
    print(f"  시장={markets or '없음'} · primary(FULL) 세션수={n_primary or '없음'} · "
          f"정책={policy_ids or '없음'}")
    if not policy_ids:
        print("\n(shadow 정책 신호가 없어 판정할 셀이 없습니다 — shadow.enabled 세션인지 확인.)")
        return 0
    for m in markets:
        for p in policy_ids:
            for h in horizons:
                signals = [to_signal(inp, agg[h].get(p), h) for inp, agg in sessions]
                _print_report(evaluate_promotion(signals, policy_id=p, horizon_min=h, market=m))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
