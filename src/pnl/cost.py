"""마찰비용(commission + slippage) 관측 집계 — 계좌 전체 + 종목별. read-only 순수 함수.

관측성(observability) 전용. 매매/판단 로직에 관여하지 않으며 Entry/Exit/Risk/AI 동작을
전혀 바꾸지 않는다. RealizedTrade(이미 symbol·gross_pnl·commission·slippage·net_pnl 보유)를
집계해 회전비용을 리포트/메트릭에서 드러낸다.

핵심 지표:
    total_cost   = commission + slippage
    cost_ratio   = total_cost / gross_pnl        (gross_pnl > 0 일 때만 의미)
    cost_impact  = total_cost / |net_pnl|        (net_pnl != 0 일 때만 의미)

주의 — gross_pnl 이 0 또는 음수이면 비율이 왜곡/발산하므로 계산하지 않고 unstable 로 표기한다
(예: Session #004 초반처럼 gross≈0 인 구간). division-by-zero 및 오해를 방지한다.
결정론적: 순수 집계 함수(clock/RNG 없음).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from src.pnl.models import RealizedTrade

_EPS = 1e-9

# 경고 임계 (cost_ratio 기준)
WARNING_RATIO = 0.30
SEVERE_RATIO = 0.50

# severity 라벨
OK = "OK"
WARNING = "WARNING"
SEVERE = "SEVERE"
UNSTABLE = "UNSTABLE"          # gross_pnl <= 0 → 비율 무의미(unstable / not meaningful)


def _classify(cost_ratio: float | None) -> str:
    """cost_ratio → severity. None(=unstable) 은 UNSTABLE."""
    if cost_ratio is None:
        return UNSTABLE
    if cost_ratio >= SEVERE_RATIO:
        return SEVERE
    if cost_ratio >= WARNING_RATIO:
        return WARNING
    return OK


def _ratio(cost: float, gross: float) -> float | None:
    """cost / gross. gross <= 0(또는 ~0) 이면 None(unstable)."""
    return (cost / gross) if gross > _EPS else None


def _impact_net(cost: float, net: float) -> float | None:
    """cost / |net|. net ~ 0 이면 None."""
    return (cost / abs(net)) if abs(net) > _EPS else None


def _turnover(trades: list[RealizedTrade]) -> float:
    """왕복 거래대금(진입 체결가*수량 + 청산 체결가*수량 합). 회전율 프록시."""
    return sum(abs(t.entry_price * t.quantity) + abs(t.exit_price * t.quantity)
               for t in trades)


@dataclass
class SymbolCost:
    """한 종목의 비용 분해."""
    symbol: str
    trade_count: int
    gross_pnl: float
    commission: float
    slippage: float
    total_cost: float                 # commission + slippage
    net_pnl: float
    cost_ratio: float | None          # total_cost / gross_pnl (gross>0), else None(unstable)
    cost_impact_net: float | None     # total_cost / |net_pnl| (net!=0), else None
    avg_holding_minutes: float | None
    turnover: float                   # 왕복 거래대금
    severity: str                     # OK | WARNING | SEVERE | UNSTABLE
    ratio_stable: bool                # cost_ratio 가 의미있는지(gross>0)

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("gross_pnl", "commission", "slippage", "total_cost",
                  "net_pnl", "turnover"):
            d[k] = round(d[k], 6)
        for k in ("cost_ratio", "cost_impact_net", "avg_holding_minutes"):
            if d[k] is not None:
                d[k] = round(d[k], 4)
        return d


@dataclass
class CostAnalysis:
    """계좌 전체 비용 관측 + 종목별 분해."""
    trades: int
    gross_pnl: float
    commission: float
    slippage: float
    total_cost: float
    net_pnl: float
    cost_ratio: float | None
    cost_impact_net: float | None
    severity: str
    ratio_stable: bool
    per_symbol: list[SymbolCost] = field(default_factory=list)
    warning_ratio: float = WARNING_RATIO
    severe_ratio: float = SEVERE_RATIO

    def to_dict(self) -> dict:
        return {
            "trades": self.trades,
            "gross_pnl": round(self.gross_pnl, 6),
            "commission": round(self.commission, 6),
            "slippage": round(self.slippage, 6),
            "total_cost": round(self.total_cost, 6),
            "net_pnl": round(self.net_pnl, 6),
            "cost_ratio": round(self.cost_ratio, 4) if self.cost_ratio is not None else None,
            "cost_impact_net": (round(self.cost_impact_net, 4)
                                if self.cost_impact_net is not None else None),
            "severity": self.severity,
            "ratio_stable": self.ratio_stable,
            "warning_ratio": self.warning_ratio,
            "severe_ratio": self.severe_ratio,
            "per_symbol": [s.to_dict() for s in self.per_symbol],
        }


def _aggregate(symbol: str, trades: list[RealizedTrade]) -> SymbolCost:
    gross = sum(t.gross_pnl for t in trades)
    comm = sum(t.commission for t in trades)
    slip = sum(t.slippage for t in trades)
    net = sum(t.net_pnl for t in trades)
    cost = comm + slip
    ratio = _ratio(cost, gross)
    hm = [t.holding_minutes for t in trades if t.holding_minutes is not None]
    return SymbolCost(
        symbol=symbol, trade_count=len(trades), gross_pnl=gross,
        commission=comm, slippage=slip, total_cost=cost, net_pnl=net,
        cost_ratio=ratio, cost_impact_net=_impact_net(cost, net),
        avg_holding_minutes=(sum(hm) / len(hm)) if hm else None,
        turnover=_turnover(trades), severity=_classify(ratio),
        ratio_stable=ratio is not None)


def cost_analysis(trades: list[RealizedTrade]) -> CostAnalysis:
    """실현 거래 리스트 → 계좌/종목별 비용 관측. 매매 로직 무관, read-only."""
    by_symbol: dict[str, list[RealizedTrade]] = {}
    for t in trades:
        by_symbol.setdefault(t.symbol, []).append(t)
    per_symbol = [_aggregate(sym, ts) for sym, ts in by_symbol.items()]
    # 정렬(결정론): 비율 있는 종목이 앞(높은 순), unstable 은 뒤, 동률은 symbol.
    per_symbol.sort(key=lambda s: (0 if s.cost_ratio is not None else 1,
                                   -(s.cost_ratio or 0.0), s.symbol))

    gross = sum(t.gross_pnl for t in trades)
    comm = sum(t.commission for t in trades)
    slip = sum(t.slippage for t in trades)
    net = sum(t.net_pnl for t in trades)
    cost = comm + slip
    ratio = _ratio(cost, gross)
    return CostAnalysis(
        trades=len(trades), gross_pnl=gross, commission=comm, slippage=slip,
        total_cost=cost, net_pnl=net, cost_ratio=ratio,
        cost_impact_net=_impact_net(cost, net), severity=_classify(ratio),
        ratio_stable=ratio is not None, per_symbol=per_symbol)
