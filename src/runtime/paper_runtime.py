"""PaperRuntime — 운영 가능한 Paper Trading 런타임.

PaperTradingLoop 을 감싸 운영 관심사를 더한다:
- 상태 관리(run/stop/pause/resume), graceful shutdown
- 매 Cycle Health Check + status.json 대시보드
- Recovery(API/Network/AI/SQLite Lock 자동 복구 — 매매보다 생존 우선)
- 종료 Report 자동 생성, 로그(rotation)

실주문 절대 금지: PaperBrokerProvider 만 사용(loop 에 주입됨). OpenAI 는 옵션(기본 Fake).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import traceback
from typing import Callable

from datetime import datetime

from src.journal.models import ts_from_cycle_id
from src.runtime.logging_setup import setup_loggers
from src.runtime.metrics import RuntimeMetrics
from src.runtime.metrics_store import build_cycle_metric, cycle_delta
from src.runtime.report import generate_markdown, write_report

# 상태
STARTING = "STARTING"
RUNNING = "RUNNING"
PAUSED = "PAUSED"
STOPPED = "STOPPED"


class PaperRuntime:
    def __init__(self, loop, config, loggers=None,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 metrics_store=None, now_iso: Callable[[], str] | None = None):
        self.loop = loop
        self.cfg = config
        self.loggers = loggers or setup_loggers(
            config.log_dir, config.log_max_bytes, config.log_backup_count)
        self._monotonic = monotonic
        self._sleep = sleep
        self.state = STOPPED
        self.metrics = RuntimeMetrics()
        self.metrics_store = metrics_store            # 선택: cycle별 지표 영속(SSoT)
        self._now_iso = now_iso or _utc_now_iso
        self._metrics_prev = self.metrics.to_dict()   # per-cycle delta 기준
        self._last_report = None
        self._last_health: dict = {}
        self._run_start = 0.0

    # ---- 제어 ----
    def run(self, max_iterations: int | None = None, install_signal_handler: bool = False,
            after_cycle=None) -> str:
        if self.state == STOPPED:
            self.state = RUNNING          # 새 시작 (PAUSED 로 들어오면 유지)
        self._run_start = self._monotonic()
        self.loggers["runtime"].info(f"runtime START symbols={self.cfg.symbols} poll={self.cfg.poll_interval_s}s")
        if install_signal_handler:
            import signal
            signal.signal(signal.SIGINT, lambda *a: self.stop())
        it = 0
        try:
            while self.state != STOPPED:
                if max_iterations is not None and it >= max_iterations:
                    break
                it += 1
                if self.state == PAUSED:
                    self._sleep(self.cfg.poll_interval_s)
                    continue
                self._run_one_cycle_safe()
                if after_cycle is not None:
                    after_cycle(self)         # 테스트/운영 훅 (stop/pause 트리거)
                self._sleep(self.cfg.poll_interval_s)
        except KeyboardInterrupt:
            self.loggers["runtime"].info("KeyboardInterrupt → graceful shutdown")
        finally:
            return self._shutdown()

    def stop(self) -> None:
        self.state = STOPPED

    def pause(self) -> None:
        if self.state == RUNNING:
            self.state = PAUSED
            self.loop.pause()
            self.loggers["runtime"].info("PAUSED")

    def resume(self) -> None:
        if self.state == PAUSED:
            self.state = RUNNING
            self.loop.resume()
            self.loggers["runtime"].info("RESUMED")

    # ---- 1 사이클 (Recovery 포함) ----
    def _run_one_cycle_safe(self):
        t0 = self._monotonic()
        report = None
        try:
            report = self.loop.tick()
        except sqlite3.OperationalError as e:
            self.metrics.sqlite_locks += 1
            self.loggers["error"].error(f"SQLite lock, retry once: {e}")
            try:
                report = self.loop.tick()               # 자동 복구: 1회 재시도
            except Exception as e2:
                self.metrics.exceptions += 1
                self.loggers["error"].error(f"retry failed: {e2}\n{traceback.format_exc()}")
        except Exception as e:                            # API/Network/AI/기타 → 흡수, 생존
            self.metrics.exceptions += 1
            self.loggers["error"].error(f"cycle exception: {e}\n{traceback.format_exc()}")

        dt = self._monotonic() - t0
        if report is not None:
            self.metrics.record_cycle(report, dt)
            self._log_cycle(report)
            if report.error:      # DATA_ERROR/부분 실패/파이프라인 오류 모두 기록
                self.loggers["error"].error(f"{report.cycle_id} {report.reason}: {report.error}")
            self._last_report = report
        self._last_health = self.health_check()
        self._record_metrics(report, dt)
        self._update_status(report)
        return report

    def _record_metrics(self, report, dt_s: float) -> None:
        """cycle별 지표(per-cycle delta) + health 를 MetricsStore 에 append."""
        after = self.metrics.to_dict()
        delta = cycle_delta(self._metrics_prev, after)
        self._metrics_prev = after
        if self.metrics_store is None:
            return
        cm = build_cycle_metric(report, dt_s, self._last_health, delta,
                                created_at=self._now_iso(), account=getattr(self.loop, "account", None))
        try:
            self.metrics_store.record(cm)
        except Exception as e:                        # 관측 실패가 매매/생존을 막지 않음
            self.loggers["error"].error(f"metrics_store record failed: {e}")

    # ---- Health Check ----
    def health_check(self) -> dict:
        last = self._last_report
        api = not (last is not None and last.reason == "DATA_ERROR")
        ai_ok = True
        if last is not None and last.status == "RAN" and last.pipeline_result:
            for o in last.pipeline_result.get("outcomes", []):
                al = o.get("ai_log")
                if al and al.get("error"):
                    ai_ok = False
        journal_ok = self.loop.journal is not None
        status = "OK" if (api and ai_ok and journal_ok) else "DEGRADED"
        return {"status": status, "api": api, "ai": ai_ok, "broker": True,
                "journal": journal_ok, "replay": journal_ok}

    # ---- Dashboard (status.json) ----
    def _update_status(self, report) -> None:
        last_cand = last_ai = last_order = 0
        last_cycle = None
        if report is not None:
            last_cycle = report.cycle_id
            pr = report.pipeline_result or {}
            for o in pr.get("outcomes", []):
                if o.get("candidate") is not None:
                    last_cand += 1
                if o.get("ai_decision") is not None:
                    last_ai += 1
                rec = o.get("order_record")
                if rec and rec.get("status") == "FILLED":
                    last_order += 1
        data = {
            "running": self.state == RUNNING,
            "state": self.state,
            "last_cycle": last_cycle,
            "candidate": last_cand,
            "ai": last_ai,
            "order": last_order,
            "pnl": self._paper_pnl(),
            "health": self._last_health,
            "metrics": self.metrics.to_dict(),
        }
        account = getattr(self.loop, "account", None)
        if account is not None:
            data["equity"] = {
                "initial": account.initial_capital,
                "current": account.last_equity,
                "peak": account.peak_equity,
                "drawdown_pct": account.last_drawdown_pct,
                "daily_return_pct": account.last_daily_return_pct,
            }
        status_dir = os.path.dirname(self.cfg.status_path)
        if status_dir:
            os.makedirs(status_dir, exist_ok=True)
        tmp = self.cfg.status_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.cfg.status_path)          # 원자적 교체

    def _paper_pnl(self) -> dict:
        positions = self.loop.positions.all() if hasattr(self.loop.positions, "all") else {}
        gross_cost = sum(p["quantity"] * p["avg_price"] for p in positions.values())
        return {"open_positions": len(positions), "gross_cost": round(gross_cost, 2),
                "note": "v1: unrealized pnl 은 mark price 필요"}

    # ---- 로깅 ----
    def _log_cycle(self, report) -> None:
        self.loggers["runtime"].info(
            f"cycle {report.cycle_id} status={report.status} reason={report.reason} session={report.session}")
        pr = report.pipeline_result or {}
        for o in pr.get("outcomes", []):
            rec = o.get("order_record")
            if rec and rec.get("status") == "FILLED":
                self.loggers["trade"].info(
                    f"{report.cycle_id} {rec['symbol']} {rec['side']} {rec['kind']} "
                    f"qty={rec['filled_quantity']} @ {rec['avg_fill_price']}")

    # ---- Graceful Shutdown ----
    def _shutdown(self) -> str:
        self.state = STOPPED
        try:
            self.loop.stop()
        except Exception:
            pass
        self._flush_stores()
        path = self._generate_report()
        self._update_status(self._last_report)
        self.loggers["runtime"].info(f"graceful SHUTDOWN, report={path}")
        return path

    def _flush_stores(self) -> None:
        for store in (getattr(self.loop, "store", None), getattr(self.loop, "journal", None)):
            for name in ("flush", "commit"):
                fn = getattr(store, name, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
                    break

    def _generate_report(self) -> str:
        report_date = "unknown"
        if self._last_report is not None and self._last_report.cycle_id:
            ts = ts_from_cycle_id(self._last_report.cycle_id)
            if ts:
                report_date = ts[:10].replace("-", "")
        content = generate_markdown(
            self.metrics, self._last_health or self.health_check(), self.cfg,
            report_date=report_date, run_duration_s=self._monotonic() - self._run_start,
            realized=self._realized_summary(), performance=self._performance_summary())
        return write_report(content, self.cfg.reports_dir, report_date)

    def _realized_summary(self) -> dict | None:
        """Journal 의 FILLED 체결 → FIFO 실현손익 요약(리포트용). 실패해도 종료를 막지 않음."""
        journal = getattr(self.loop, "journal", None)
        if journal is None:
            return None
        try:
            from src.pnl.journal_pnl import realized_from_journal
            _, _, summary = realized_from_journal(journal)
            return summary.to_dict()
        except Exception as e:
            self.loggers["error"].error(f"realized pnl summary failed: {e}")
            return None

    def _performance_summary(self) -> dict | None:
        """자금관리 계좌(equity_curve/실현거래) → 공용 성과 지표(리포트용). 실패해도 종료를 막지 않음."""
        account = getattr(self.loop, "account", None)
        if account is None:
            return None
        try:
            from src.portfolio.performance import performance_summary
            return performance_summary(account.initial_capital, account.equity_curve,
                                       account.realized_trades())
        except Exception as e:
            self.loggers["error"].error(f"performance summary failed: {e}")
            return None


def _utc_now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
