"""Immutable raw 페이지 스냅샷 — 공급자 응답을 **parse 전에** 원본 그대로 보존한다.

계약: `docs/research/DATA_CONTRACT_4STRATEGY_V1.md` §4.1 / AMENDMENT A1 · ADR-0001 · 이슈 #75.

현 provider(`TossMarketDataProvider._parse`)는 응답을 즉시 canonical `Candle` 로 바꾸고 raw
dict 를 버린다. 이 모듈은 **canonical 변환 전의 provider 응답 payload** 를 content-addressed
immutable 스냅샷으로 보존한다 → 재요청 없이 재현·감사 가능(계약 §4.1).

불변 속성(계약 요구):
- **parse 전 보존**: emit 은 provider 응답 body(dict)를 받는다(= 파싱 전). 캔들 재구성이 아니다.
- **atomic**: `<final>.<uniq>.partial` 로 먼저 쓰고 flush→fsync→os.replace(같은 dir, 원자적 rename).
- **content-addressed**: 경로/파일명 = payload 의 SHA-256. 동일 payload → 동일 파일(멱등, 재기록 안 함).
  다른 payload → 다른 hash → 충돌 없음.
- **provenance**: 요청 맥락(symbol, interval, before/cursor, requested_at, received_at, source,
  content_sha256, 바이트수, 캔들수, next_before)을 append-only receipts JSONL 에 기록.
- **secret 금지**: payload(응답 body)만 저장. 자격증명·토큰·인증 헤더는 받지도 저장하지도 않는다.
  provenance 는 **고정 화이트리스트 필드**만 기록한다(임의 요청 헤더 dump 안 함).
- **부분 파일 미승격**: 완료본은 `<sha>.json.gz`, 미완은 `*.partial`. 열거는 `*.json.gz` 만 → crash
  로 남은 partial 은 완료본으로 오인되지 않는다.
- **중단·재개 안전**: 완료본은 immutable(존재 시 재기록 안 함) → 재실행이 기존 raw 를 손상시키지 않는다.
- **경로 gitignore**: 기본 저장 위치는 `data/` 하위(gitignore). manifest/receipt 에 홈 절대경로·secret 없음.
- **외부 복제 대비**: content 트리 + receipts JSONL 는 파일 기반이라 #73 외부복제 대상에 raw 계층을
  그대로 포함할 수 있게 설계(경계 = 이 root 디렉터리).
- **단일 writer 전제**: receipts.jsonl append 는 **한 프로세스의 순차 backfill**(현
  `backfill_symbols` 는 종목 순차 실행)을 전제하며 동시 writer 안전성을 보장하지 않는다.
  병렬 수집을 도입하려면 run별 receipts 분리 또는 파일 잠금이 선행돼야 한다(content 파일
  자체는 content-addressed + atomic rename 이라 동시 기록에도 안전).

★ 자본 안전: 주문/broker/risk/pipeline 을 import 하지 않는다. Toss API 호출 없음(순수 저장).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from dataclasses import dataclass

# gzip 결정론: mtime=0 으로 헤더 타임스탬프를 고정해 동일 payload → byte 동일 압축 산출.
_GZIP_MTIME = 0

# provenance receipts 파일명(root 하위, append-only JSONL).
RECEIPTS_BASENAME = "receipts.jsonl"

# provenance 에 절대 넣지 않을 secret-스러운 키(방어적 — payload/맥락에 섞여 들어와도 차단).
_SECRET_HINT_KEYS = ("authorization", "token", "secret", "password", "cookie",
                     "client_secret", "client_id", "api_key", "apikey", "bearer")


def canonical_bytes(payload: dict) -> bytes:
    """payload 를 결정론적 JSON 바이트로 직렬화(정렬·최소 구분자). 동일 내용 → 동일 바이트."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def content_sha256(payload: dict) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


@dataclass(frozen=True)
class RawSnapshotRecord:
    symbol: str
    interval: str
    before: str | None
    content_sha256: str
    rel_path: str            # root 기준 상대경로(홈 절대경로 미포함)
    payload_bytes: int       # 압축 전(canonical JSON) 바이트
    compressed_bytes: int    # gzip 후 바이트
    candle_count: int
    next_before: str | None
    written: bool            # True=새로 기록, False=이미 존재(멱등 dedup)


def _rel_path(symbol: str, interval: str, sha: str) -> str:
    # content-addressed + 2-hex 샤딩(디렉터리 폭발 방지). 상대경로만(외부복제/이식성).
    safe_symbol = symbol.replace("/", "_").replace(os.sep, "_")
    return os.path.join(safe_symbol, interval, sha[:2], f"{sha}.json.gz")


class RawSnapshotStore:
    """content-addressed immutable raw 스냅샷 저장소.

    root_dir 는 gitignore 되는 위치(예: data/raw_snapshots/)를 권장한다. 생성 시 없으면 만든다.
    """

    def __init__(self, root_dir: str):
        self.root = os.path.abspath(root_dir)
        os.makedirs(self.root, exist_ok=True)
        self._temp_seq = 0

    # ---- 저장(parse 전 payload) ----
    def emit(self, *, symbol: str, interval: str, before: str | None,
             requested_at: str, received_at: str, source: str,
             payload: dict) -> RawSnapshotRecord:
        """provider 응답 payload(파싱 전)를 immutable 스냅샷으로 저장하고 provenance 기록.

        멱등: 동일 payload 는 재기록하지 않는다(written=False). 다른 payload 는 다른 파일.
        """
        blob = canonical_bytes(payload)
        sha = hashlib.sha256(blob).hexdigest()
        rel = _rel_path(symbol, interval, sha)
        final = os.path.join(self.root, rel)
        gz = gzip.compress(blob, mtime=_GZIP_MTIME)

        written = False
        if not os.path.exists(final):
            os.makedirs(os.path.dirname(final), exist_ok=True)
            self._atomic_write(final, gz)
            written = True

        candles = payload.get("candles", []) or []
        next_before = payload.get("nextBefore")
        rec = RawSnapshotRecord(
            symbol=symbol, interval=interval, before=before,
            content_sha256=sha, rel_path=rel,
            payload_bytes=len(blob), compressed_bytes=len(gz),
            candle_count=len(candles), next_before=next_before, written=written,
        )
        self._append_receipt(rec, requested_at=requested_at, received_at=received_at,
                             source=source)
        return rec

    def _atomic_write(self, final: str, data: bytes) -> None:
        """temp(.partial) → flush → fsync → os.replace(원자적 rename). dir fsync 로 내구성."""
        self._temp_seq += 1
        tmp = f"{final}.{os.getpid()}.{self._temp_seq}.partial"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, final)          # 같은 dir → 원자적. 실패 시 tmp 잔존(완료본 아님).
            self._fsync_dir(os.path.dirname(final))
        finally:
            if os.path.exists(tmp):         # replace 성공 시엔 이미 사라짐. 실패 시 정리.
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    @staticmethod
    def _fsync_dir(path: str) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _append_receipt(self, rec: RawSnapshotRecord, *, requested_at: str,
                        received_at: str, source: str) -> None:
        """append-only provenance(고정 화이트리스트 필드만 — secret/헤더 미포함)."""
        entry = {
            "symbol": rec.symbol,
            "interval": rec.interval,
            "before": rec.before,
            "requested_at": requested_at,
            "received_at": received_at,
            "source": source,
            "content_sha256": rec.content_sha256,
            "rel_path": rec.rel_path,
            "payload_bytes": rec.payload_bytes,
            "compressed_bytes": rec.compressed_bytes,
            "candle_count": rec.candle_count,
            "next_before": rec.next_before,
            "written": rec.written,
        }
        # 방어: secret-스러운 키가 값에 섞였는지 얕은 검사(있으면 계약 위반 → 즉시 실패).
        _assert_no_secret_keys(entry)
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
        path = os.path.join(self.root, RECEIPTS_BASENAME)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    # ---- 재현/조회(read-only) ----
    def read_payload(self, rel_path: str) -> dict:
        """저장된 raw 스냅샷을 복원(gzip 해제 → JSON). 재현/감사용."""
        with gzip.open(os.path.join(self.root, rel_path), "rb") as f:
            return json.loads(f.read().decode("utf-8"))

    def list_content_files(self) -> list[str]:
        """완료본(`*.json.gz`)만 열거(정렬, 상대경로). `*.partial` 은 제외."""
        out: list[str] = []
        for dirpath, _dirs, files in os.walk(self.root):
            for name in files:
                if name.endswith(".json.gz"):
                    rel = os.path.relpath(os.path.join(dirpath, name), self.root)
                    out.append(rel)
        return sorted(out)

    def manifest(self) -> dict:
        """raw 계층 결정론 manifest — 완료본만, (rel_path, sha) 정렬 해시.

        write 순서/시각과 무관하게 동일 content 집합이면 동일 raw_content_sha256.
        content 파일명 자체가 sha 이므로 파일명에서 sha 를 취해 재해시(재압축 불필요).
        """
        rels = self.list_content_files()
        per = []
        for rel in rels:
            sha = os.path.basename(rel)[: -len(".json.gz")]
            per.append({"rel_path": rel, "content_sha256": sha})
        blob = json.dumps(per, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            "schema": "raw_snapshot_manifest/v1",
            "root_basename": os.path.basename(self.root.rstrip(os.sep)),
            "content_file_count": len(rels),
            "raw_content_sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
            "files": per,
        }


def _assert_no_secret_keys(entry: dict) -> None:
    for k in entry:
        if any(hint in str(k).lower() for hint in _SECRET_HINT_KEYS):
            raise ValueError(f"raw provenance 에 secret-스러운 키 {k!r} 금지(계약 §5).")


# ---------------------------------------------------------------------------
# 용량 추정(해석적 — 라이브 API 호출 없음). ADR-0001 근거값 재현용.
# ---------------------------------------------------------------------------
def measure_page(payload: dict) -> dict:
    """한 페이지 payload 의 canonical/gzip 바이트와 캔들수(결정론). API 호출 없음."""
    blob = canonical_bytes(payload)
    gz = gzip.compress(blob, mtime=_GZIP_MTIME)
    candles = payload.get("candles", []) or []
    n = len(candles) or 1
    return {
        "candle_count": len(candles),
        "payload_bytes": len(blob),
        "compressed_bytes": len(gz),
        "bytes_per_candle_raw": round(len(blob) / n, 2),
        "bytes_per_candle_gz": round(len(gz) / n, 2),
        "gzip_ratio": round(len(gz) / len(blob), 4) if blob else None,
    }


def estimate_bulk_raw_storage(*, sample_payload: dict, symbols: int, years: float,
                              bars_per_trading_day: float, trading_days_per_year: float,
                              page_size: int = 200) -> dict:
    """30종목×N년 raw 저장량을 표본 페이지 gzip 비율로 **해석적** 추정(라이브 수집 없음).

    입력 근거: `phase1a_probe_result.json`(755 1m-bars/거래일 등)·PROBE_REPORT.
    전략 성과·forward return 을 계산하지 않는다(순수 용량 산술).
    """
    m = measure_page(sample_payload)
    total_bars = symbols * years * trading_days_per_year * bars_per_trading_day
    pages = total_bars / page_size
    raw_gz_bytes = total_bars * m["bytes_per_candle_gz"]
    raw_json_bytes = total_bars * m["bytes_per_candle_raw"]
    return {
        "assumptions": {
            "symbols": symbols, "years": years,
            "bars_per_trading_day": bars_per_trading_day,
            "trading_days_per_year": trading_days_per_year,
            "page_size": page_size,
            "sample_gzip_ratio": m["gzip_ratio"],
            "sample_bytes_per_candle_gz": m["bytes_per_candle_gz"],
        },
        "estimated_total_bars": round(total_bars),
        "estimated_pages": round(pages),
        "estimated_raw_json_bytes": round(raw_json_bytes),
        "estimated_raw_gz_bytes": round(raw_gz_bytes),
        "estimated_raw_gz_gib": round(raw_gz_bytes / (1024 ** 3), 2),
        "estimated_raw_json_gib": round(raw_json_bytes / (1024 ** 3), 2),
    }
