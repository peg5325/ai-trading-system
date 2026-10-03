"""Korean daily journal (AGENTS.md "Daily trading journal"). Facts come only from the result dict.

The retrospective section is deliberately left for the assistant to write after reading the
recent journal entries; the generator only fills the facts.
"""
from __future__ import annotations

from src.daytrade_paper import rules as R

SITUATION_KO = {
    "S0": "S0 이벤트일(진입 없음)",
    "S1": "S1 상승 추세 시작(TQQQ)",
    "S2": "S2 하락 추세 시작(SQQQ)",
    "S3": "S3 갭하락 반등(TQQQ)",
    "S4": "S4 해당 없음(진입 없음)",
    "S5": "S5 오후 큰 움직임 추종",
    "INCOMPLETE_DATA": "INCOMPLETE_DATA 데이터 불완전(거래 없음)",
}
KIND_KO = {"S1": "S1", "S2": "S2", "S3": "S3", "S5": "S5"}
RETRO_HEADINGS = ("잘한 점", "잘못한 점과 개선안", "지난 개선안 점검", "다음 세션 적용 변경")


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:+.2f}%"


def _usd(x) -> str:
    return "n/a" if x is None else f"{x:+,.2f}"


def _num(x, nd: int = 4) -> str:
    return "n/a" if x is None else f"{x:,.{nd}f}"


def _checks(lines: list[str], title: str, checks: list[dict]) -> None:
    lines.append(f"- {title}")
    for c in checks:
        lines.append(f"  - [{'O' if c['ok'] else 'X'}] {c['detail']}")


def render(result: dict) -> str:
    r = result
    sit = " + ".join(SITUATION_KO.get(s, s) for s in r["situations"])
    pnl = r["pnl"]
    bm = r["benchmarks"]
    L: list[str] = [
        f"# 매매일지 {r['date']} — {R.LABEL}",
        "",
        (f"> 탐색용 Paper(규칙 {r['rules_version']}, `{r['contract']}`). 실제 주문 없음. "
         "결과는 우위의 증거가 아니다. 사실 부분은 `scripts/run_daytrade_paper.py`가 "
         f"`reports/daytrade_paper/{r['date']}.json`에서 자동 생성했다."),
        "",
        "## 요약",
        "",
        f"- 상황: {sit}",
        f"- 거래: {len(r['trades'])}건",
        f"- 손익(비용 차감 후): {_usd(pnl['day_pnl'])} USD ({_pct(pnl['day_return'])}), 비용 {pnl['costs']:.2f} USD",
        f"- 계좌: {r['equity_start']:,.2f} → {pnl['equity_end']:,.2f} USD",
        "",
        "## 상황 판단과 이유",
        "",
    ]
    ev = r["events"]
    ev_txt = ", ".join(f"{i['type']}({i.get('title', '')})" for i in ev.get("items", [])) or "없음"
    if not ev.get("known", True):
        ev_txt += " — 이벤트 달력 범위 밖(미확인)"
    L.append(f"- 이벤트: {ev_txt}")
    inp = r.get("inputs")
    if inp:
        L += [
            (f"- 추세: {'BULL' if inp['bull'] else 'BEAR'} (전일 {inp['prev_session']} 종가 "
             f"{_num(inp['prev_close'], 2)}, SMA50 {_num(inp['sma50'], 2)})"),
            f"- ATR14: {_pct(inp['atr14_pct'])}, 갭: {_pct(inp['gap'])} ({inp['gap_source']})",
            (f"- 위험 모드: {inp['risk_mode']} (전일 VIX {_num(inp['vix_prev_close'], 2)}, "
             f"VIX9D {_num(inp['vix9d_prev_close'], 2)}, 비율 {_num(inp['vix9d_vix_ratio'], 3)})"),
            f"- 기록만: 전일 GEX {_num(inp['gex_prev'], 0)}, 월물 옵션 만기일 {'예' if inp['monthly_opex'] else '아니오'}",
        ]
    op = r.get("opening")
    if op:
        L.append(f"- 판정 {op['decision_time']}, 개장 구간 {op['opening_window']}: QQQ 09:30 시가 "
                 f"{_num(op['open_0930'], 2)}, 직전 봉 종가 {_num(op['last_close'], 2)}, VWAP {_num(op['vwap'], 2)}, "
                 f"범위 {_pct(op['range_pct'])}, RVOL {_num(op['rvol'], 2)}")
    ch = r.get("checks", {})
    if "S0" in ch:
        L.append(f"- S0: {ch['S0']}")
    for key in ("S1", "S2"):
        if key in ch:
            _checks(L, f"{key} 조건", ch[key])
    if "S3" in ch:
        _checks(L, f"S3 조건({ch['S3']['decision_time']})", ch["S3"]["checks"])
    for key in ("S1_stop", "S2_stop", "S3_stop"):
        if key in ch:
            L.append(f"- 손절 기준: {ch[key]}")
    if "S5" in ch:
        L.append(f"- S5: {ch['S5']['detail']}")
    L += ["", "## 진입·청산", ""]
    if r["trades"]:
        L.append("| 상황 | 종목 | 진입 시각 | 진입가(체결) | 수량 | 명목 USD | 손절가 | 청산 시각 | 청산가(체결) | 청산 이유 | 손익 USD |")
        L.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for t in r["trades"]:
            L.append(
                f"| {KIND_KO[t['kind']]} | {t['symbol']} 매수→매도 | {t['entry_time']} | "
                f"{_num(t['entry_fill'])} | {t['shares']:.4f} | {t['notional']:,.2f} | {_num(t['stop'])} | "
                f"{t['exit_time']}({t['exit_at']}) | {_num(t['exit_fill'])} | {t['exit_reason_ko']} | {_usd(t['pnl'])} |")
        L.append("")
        L.append("크기 = 계좌×0.5% ÷ 손절폭, 상한 35%(HIGH면 절반). 체결가에 편도 슬리피지 0.05% 반영, "
                 "수수료 각 0.10%, 매도 SEC fee 0.00206%.")
    else:
        L.append("- 거래 없음")
    L += ["", "## 거래하지 않은 구간과 이유", ""]
    L += [f"- {x}" for x in r["no_trade_reasons"]] or ["- 없음"]
    if r["halt"].get("triggered"):
        L.append(f"- 일일 손실 한도 발동: {r['halt']}")
    L += [
        "",
        "## 결과 비교(비용 차감 후 vs 1배 보유)",
        "",
        "| 항목 | 수익률 | USD(계좌 전액 기준) |",
        "| --- | --- | --- |",
        f"| 이 시스템(비용 차감 후) | {_pct(pnl['day_return'])} | {_usd(pnl['day_pnl'])} |",
        f"| QQQ 1배 시가→종가 보유 | {_pct(bm['QQQ_open_to_close'])} | {_usd(bm['QQQ_hold_pnl'])} |",
        f"| TQQQ 시가→종가 보유 | {_pct(bm['TQQQ_open_to_close'])} | {_usd(bm['TQQQ_hold_pnl'])} |",
        "",
        f"비교 기준은 {bm['note']}.",
        "",
        "## 데이터 품질",
        "",
    ]
    dq = r["data_quality"]
    L.append(f"- 상태: {'정상' if dq['ok'] else 'INCOMPLETE_DATA'}; 정규장 봉 수 {dq['regular_bar_counts']} "
             f"(기대 {dq['expected_bars']}), 과거 QQQ 세션 {dq['prior_sessions']}개")
    L += [f"- 문제: {x}" for x in dq["issues"]]
    for k, e in r.get("external", {}).items():
        L.append(f"- 외부 {k}: {e.get('status')} (값 {e.get('value')}, 날짜 {e.get('date')})")
    L += [
        "",
        "## 회고 (assistant 작성)",
        "",
        "> 아래는 assistant가 최근 일지를 읽은 뒤 쓴다. 개선안은 다음 세션부터 새 규칙 버전으로만 적용한다.",
        "",
    ]
    for h in RETRO_HEADINGS:
        L += [f"### {h}", "", "(assistant 작성 대기)", ""]
    return "\n".join(L).rstrip() + "\n"
