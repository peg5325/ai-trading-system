"""ingest_log.note provenance 파서 + 수집 run 교차검증 (이슈 #79).

배경(이슈 #79): manifest 의 `collection_completed_through` 는 지금까지 **CLI 인자**만 검사했다
(`cutoff < as_of` 면 fail-closed). 그러나 그 값은 실제 수집 때 `ingest_log` 에 기록된 cutoff 와
대조되지 않아, 오선언된 cutoff(≥ as_of)로 실제 미수집 구간을 OK 로 인증하는 경로가 남았다.

이 모듈은 그 연결을 채운다(**read-only**):
- `parse_ingest_note` — `backfill_history._ingest_note` 가 남긴 구조화 note 를 엄격 파싱한다
  (`reason=`, `completed_through=`, `error=` 등). error= 는 repr 이라 ';'/'=' 를 포함할 수 있어
  **맨 뒤에서 먼저 분리**한다(위치 split 취약성 제거). 여러 모듈에 파싱을 복제하지 않도록 단일 원천.
- `parse_stopped_reason` — 기존 daily/intraday_manifest 에 중복돼 있던 정지사유 추출을 통합
  (동작 동일 — 구조화 `reason=` 값 또는 레거시 bare note 통째).
- `verify_collection_provenance` — **명시적 collection run_id** 로 ingest_log provenance 를
  대조한다. 하나의 수집 실행(공유 run_id)이 유니버스 전 종목에 남았는지, 그 종목·재시도 로그의
  `completed_through` 가 모두 동일한지, 선언 cutoff·as_of 와 일치하는지 검증한다. 어긋나면
  `ProvenanceError` 로 **fail-closed**(missing/malformed/conflicting/mismatch/incomplete 전부).

레거시 처리(추측 승격 금지):
- run_id/cutoff provenance 가 없는 레거시 로그는 strict 검증(run_id 지정) 시 조용히 VERIFIED 로
  승격하지 않고 `INCONCLUSIVE_PROVENANCE` 로 fail-closed 한다.
- 기존 산출물의 의미를 소급 변경하지 않는다: run_id 미지정이면 기존 declared-only 방어만 유지되고
  provenance_status 는 `DECLARED_UNVERIFIED`/`NONE` 로 정직하게 표기한다(VERIFIED 아님).

★ 자본 안전: 순수 파서 + read-only SELECT 만. 주문/broker/risk/live/runtime/pipeline/전략/config 를
   import 하지 않는다. Toss API 호출·DB write 없음.
"""
from __future__ import annotations

import sqlite3
from datetime import date

# 공식 backfill 이 남기는 ingest_log.source. src.ingest.backfill_history.BACKFILL_SOURCE 와
# **반드시 동일**해야 한다(교차검증은 이 source 로만 필터해 import:* 레거시 로그를 섞지 않는다).
# 순환 import(history→ingest) 를 피하려고 여기 상수로 두고, 일치는 테스트로 고정한다.
BACKFILL_SOURCE = "toss_backfill"


class ProvenanceError(ValueError):
    """수집 run provenance 교차검증 실패 — manifest OK 인증 전 fail-closed.

    메시지는 어떤 run/symbol/필드가 문제인지 **구조적**으로만 담는다(시크릿·절대경로 없음).
    """


def parse_ingest_note(note: str | None) -> dict:
    """`_ingest_note` 의 검증 대상 known field 를 구조적으로 파싱한다.

    note 문법(`backfill_history._ingest_note`): "; " 로 조인된 `key=value` 토큰.
      reason=<enum>; [sessions_allowed=..; all_sessions=..; excluded={..}; stored=..];
      [completed_through=YYYY-MM-DD; excluded_after_cutoff=N; cutoff_excluded_days=..];
      [error=<repr>]
    forward-compatible parser 이므로 검증에 쓰지 않는 unknown/malformed 토큰(`excluded={...}` 포함)은
    무시한다. cutoff 검증에 필요한 `completed_through` 의 누락·형식 오류는 호출측이 fail-closed 한다.
    error= 는 repr 이라 ';'/'=' 를 포함할 수 있어 **가장 먼저 맨 뒤에서 분리**한다.
    `structured` 는 구조화 키(또는 error=)가 하나라도 있으면 True(레거시 bare note 구별용).
    """
    out = {
        "raw": note, "structured": False, "reason": None,
        "completed_through": None, "excluded_after_cutoff": None,
        "cutoff_excluded_days": None, "sessions_allowed": None,
        "all_sessions": None, "stored": None, "error": None,
    }
    if not note:
        return out
    text = note
    # error= 를 토큰 경계(문두 또는 직전이 ';'/"; ")에서 **먼저** 떼어낸다 — 그 값은 통째로 error.
    # 생산자 불변식(_ingest_note): error= 는 **항상 마지막 필드**이고, 그보다 앞의 필드 값은 enum·
    # ISO date·정수·세션명 CSV 뿐이라 토큰 경계의 'error=' 를 포함할 수 없다. 따라서 **첫** 경계
    # 매치가 곧 필드 경계다(그 뒤 전체가 error repr — ';'/'=' 포함 가능). 경계 밖의 'error='(예:
    # reason=weird_error=x)는 직전 문자가 경계가 아니므로 떼어내지 않는다(오분리 방지).
    marker = "error="
    idx = text.find(marker)
    if idx != -1 and (idx == 0 or text[idx - 1] == ";" or text[max(0, idx - 2):idx] == "; "):
        out["error"] = text[idx + len(marker):] or None
        text = text[:idx]
    saw_key = False
    for tok in text.split(";"):
        tok = tok.strip()
        if not tok or "=" not in tok:
            continue
        key, _, val = tok.partition("=")
        key = key.strip()
        val = val.strip()
        if key in ("reason", "completed_through", "excluded_after_cutoff",
                   "cutoff_excluded_days", "sessions_allowed", "all_sessions", "stored"):
            out[key] = val or None
            saw_key = True
    out["structured"] = saw_key or out["error"] is not None
    return out


def parse_stopped_reason(note: str | None) -> str | None:
    """ingest_log.note 에서 정지사유를 추출(구조화·bare 노트 양쪽 지원, 이슈 #72).

    구조화(`reason=`) 키가 있으면 그 값을, 없으면(레거시 bare 노트) 첫 세그먼트가 아니라 **통째로**
    유지한다(error repr 의 ';' 절단 방지). daily/intraday_manifest 의 이전 중복 구현과 동작 동일 —
    이제 이 단일 원천을 재사용한다.
    """
    if not note:
        return None
    for token in note.split(";"):
        token = token.strip()
        if token.startswith("reason="):
            return token[len("reason="):].strip() or None
    return note.strip() or None


def _date_or_none(text: str | None) -> date | None:
    try:
        return date.fromisoformat(text)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


def _verified_cutoff_from_rows(rows: list[sqlite3.Row], *, run_id: str) -> str:
    """같은 run_id 로 조회된 **모든 로그**의 cutoff 를 하나로 검증한다."""
    cutoffs: set[str] = set()
    missing_prov: list[str] = []
    malformed: dict[str, list[str]] = {}
    for row in rows:
        symbol = row["symbol"] or "<none>"
        parsed = parse_ingest_note(row["note"])
        ct = parsed["completed_through"]
        if ct is None:
            missing_prov.append(symbol)
        elif _date_or_none(ct) is None:
            malformed.setdefault(symbol, []).append(ct)
        else:
            cutoffs.add(ct)
    if missing_prov:
        raise ProvenanceError(
            f"INCONCLUSIVE_PROVENANCE: run_id={run_id!r} 로그에 completed_through provenance 없음"
            f"(레거시/비-cutoff 수집) — 종목 {sorted(set(missing_prov))} — 추측 승격 금지(fail-closed).")
    if malformed:
        raise ProvenanceError(
            f"MALFORMED_COMPLETED_THROUGH: run_id={run_id!r} completed_through 형식 오류"
            f"(YYYY-MM-DD 필요): {malformed} (fail-closed).")
    if not cutoffs:
        raise ProvenanceError(
            f"NO_VERIFIED_CUTOFF: run_id={run_id!r} 에서 검증 가능한 completed_through 없음(fail-closed).")
    if len(cutoffs) > 1:
        raise ProvenanceError(
            f"CONFLICTING_CUTOFF: run_id={run_id!r} 내 completed_through 불일치: "
            f"{sorted(cutoffs)} — resume/retry 는 동일 cutoff 여야 한다(fail-closed).")
    return cutoffs.pop()


def verify_resume_cutoff(
    con: sqlite3.Connection, *, run_id: str, interval: str, source: str,
    completed_through: str,
) -> str | None:
    """기존 collection run 을 resume 하기 전에 cutoff 일관성을 read-only 확인한다.

    해당 run 로그가 없으면 새 collection 이므로 ``None``을 반환한다. 로그가 있으면
    run 전체(유니버스 밖 로그 포함)의 provenance 를 검증하고 선언 cutoff 와 다를 때
    provider 호출 전에 fail-closed 한다.
    """
    if not run_id or not run_id.strip():
        raise ProvenanceError(
            "EMPTY_RUN_ID: collection run_id 가 비어 있음 — 유효한 run_id 를 지정하라(fail-closed).")
    rows = con.execute(
        "SELECT symbol, note FROM ingest_log "
        "WHERE run_id=? AND interval=? AND source=? ORDER BY symbol, rowid",
        (run_id, interval, source),
    ).fetchall()
    if not rows:
        return None
    verified = _verified_cutoff_from_rows(rows, run_id=run_id)
    if verified != completed_through:
        raise ProvenanceError(
            f"RESUME_CUTOFF_MISMATCH: 기존 run cutoff {verified} != 요청 cutoff "
            f"{completed_through} (run_id={run_id!r}) — 새 run_id 를 쓰거나 동일 cutoff 로 resume 하라"
            "(fail-closed).")
    return verified


def verify_collection_provenance(
    con: sqlite3.Connection, *, run_id: str, interval: str, source: str,
    symbols: list[str], declared_completed_through: str | None, as_of: str,
) -> dict:
    """명시적 collection run_id 로 ingest_log provenance 를 교차검증한다(read-only, fail-closed).

    검증 조건(이슈 #79 Scope A):
      1. run_id + interval + source 로 ingest_log 가 존재한다(다른 run 로그를 섞지 않음).
      2. 대상 유니버스 전 종목이 그 run 에 로그를 남겼다(누락 시 fail-closed, 다른 run 보충 금지).
      3. 각 로그 note 의 completed_through 가 존재하고 정확한 YYYY-MM-DD 형식이다.
      4. 같은 run 내부 모든 종목·재시도 로그의 completed_through 가 동일하다.
      5. 검증 cutoff 가 선언 cutoff(있으면)와 정확히 일치한다.
      6. 검증 cutoff 가 as_of 보다 이르지 않다((cutoff, as_of] 미수집 OK 인증 방지).
    성공 시 provenance dict(provenance_status=VERIFIED …)를 반환한다. 이 값은 실행 provenance 이며
    데이터 내용 지문(data_content_sha256)에 **포함하지 않는다**(호출측 규율).
    """
    # 빈/공백 run_id 는 검증 요청의 오입력이다 — NO_RUN_LOGS 로 흘려보내지 않고 명확히 거부한다.
    if not run_id or not run_id.strip():
        raise ProvenanceError(
            "EMPTY_RUN_ID: collection_run_id 가 비어 있음 — 유효한 run_id 를 지정하라(fail-closed).")
    universe = list(dict.fromkeys(symbols))  # 순서 보존 + 방어적 중복 제거
    rows = con.execute(
        "SELECT symbol, note FROM ingest_log "
        "WHERE run_id=? AND interval=? AND source=? ORDER BY symbol, rowid",
        (run_id, interval, source),
    ).fetchall()
    if not rows:
        raise ProvenanceError(
            f"NO_RUN_LOGS: run_id={run_id!r} interval={interval} source={source} 에 "
            f"ingest_log 가 없다 — 다른 run 로그로 보충하지 않는다(fail-closed).")

    by_symbol: dict[str | None, list[str | None]] = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], []).append(r["note"])

    missing = [s for s in universe if s not in by_symbol]
    if missing:
        raise ProvenanceError(
            f"MISSING_SYMBOLS: run_id={run_id!r} interval={interval} 에 대상 종목 로그 없음: "
            f"{missing} — 다른 run 로그로 보충 금지(fail-closed).")

    # run_id 는 collection identity 다. 대상 유니버스 밖 행도 같은 run 에 섞였다면
    # 그 provenance 를 무시하지 않고 전체 로그의 cutoff 일관성을 요구한다.
    verified = _verified_cutoff_from_rows(rows, run_id=run_id)

    if declared_completed_through is not None and declared_completed_through != verified:
        raise ProvenanceError(
            f"DECLARED_MISMATCH: 선언 cutoff {declared_completed_through} != ingest_log 검증 "
            f"cutoff {verified} (run_id={run_id!r}) — 오선언 방지 fail-closed.")
    if verified < as_of:
        raise ProvenanceError(
            f"VERIFIED_CUTOFF_BEFORE_ASOF: 검증 cutoff {verified} < as_of {as_of} "
            f"(run_id={run_id!r}) — (cutoff, as_of] 미수집으로 OK 인증 불가(fail-closed).")

    return {
        "provenance_status": "VERIFIED",
        "collection_run_id": run_id,
        "declared_completed_through": declared_completed_through,
        "verified_completed_through": verified,
        "verified_symbol_count": len(universe),
        "source": source,
        "interval": interval,
    }


def declared_only_provenance(*, declared_completed_through: str | None,
                             interval: str, source: str) -> dict:
    """run_id 미지정(strict 검증 없음) 경로의 provenance 표기 — 레거시 의미를 소급 변경하지 않는다.

    선언 cutoff 만 있으면 `DECLARED_UNVERIFIED`(정직: 인자만 방어, ingest_log 미대조),
    선언도 없으면 `NONE`. 어느 쪽도 VERIFIED 로 승격하지 않는다.
    """
    return {
        "provenance_status": ("DECLARED_UNVERIFIED" if declared_completed_through is not None
                              else "NONE"),
        "collection_run_id": None,
        "declared_completed_through": declared_completed_through,
        "verified_completed_through": None,
        "verified_symbol_count": None,
        "source": source,
        "interval": interval,
    }
