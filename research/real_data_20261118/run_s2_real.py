"""S2 basis reversion vs futures price mean reversion on real data (research only).

Spot: TWSE TAIEX 5-min bars (bar-start labels, available at bar end). Futures: TXFR1 5-min day bars as
an MTX price proxy, front month from the generated calendar. Carry: CBC rediscount rate (available at
its effective date's open) and realized TAIEX dividend points with ex-date after the bar and on/before
expiry (treated as known at the start of the trade date: ex-dividend schedules are announced ahead,
but this is an assumption). Basis z-scores only use the same contract month, so each front-month
period is run as its own backtest; equity restarts at CASH for every period.
Usage: uv run --locked python run_s2_real.py 2017-06-01 2026-09-04
"""

import csv
import json
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from gtts.data.calendar import TradingCalendar
from gtts.data.quality import FeatureEvent
from gtts.data.schema import TAIPEI
from gtts.features.basis import CarryInputs
from gtts.research.metrics import calculate_metrics, completed_position_cycles
from gtts.risk.margin import Costs, Margin, Schedules
from gtts.strategies.basis_reversion import compare_basis

DATA = Path(r"D:\fadacai\期貨價差\data")
FM = DATA / "finmind"
CASH = D(1_000_000)
start, end = (date.fromisoformat(x) for x in sys.argv[1:3])
calendar = TradingCalendar.from_yaml(FM / "gtts_calendar_2017_2026.yaml")
margins = tuple(
    Margin(effective_from=datetime.fromisoformat(m["effective_from"]), initial=D(m["initial"]),
           maintenance=D(m["maintenance"]))
    for m in json.loads((FM / "mtx_margins_approx.json").read_text(encoding="utf-8"))
)
schedules = Schedules(
    costs={"MTX": (Costs(effective_from=date(2017, 5, 15), broker_fee_per_contract=D(25),
                         tax_rate=D(".00002"), slippage_ticks=1),)},
    margins={"MTX": margins},
)
t0 = time.time()
lo, hi = start.isoformat(), (end + timedelta(days=1)).isoformat()


def front_month(day):
    month = f"{day.year}{day.month:02d}"
    if day > calendar.last_trade_date("MTX", month):
        nxt = day.replace(day=1) + timedelta(days=32)
        month = f"{nxt.year}{nxt.month:02d}"
    return month


# --- spot -----------------------------------------------------------------------------------
spots = {}
for name in ("IX0001_5min_20160101_20240726.csv", "IX0001_5min_20240729_20261001.csv"):
    with (DATA / name).open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if not (lo <= row["timestamp"] < hi):
                continue
            s = datetime.fromisoformat(row["timestamp"]).replace(tzinfo=TAIPEI)
            if not calendar.is_trade_date(s.date()):
                continue
            spots[s] = FeatureEvent(
                instrument_id="TAIEX", trade_date=s.date(), session="day", bar_start=s,
                bar_end=s + timedelta(minutes=5), available_at=s + timedelta(minutes=5),
                values={k: D(row[k]) for k in ("open", "high", "low", "close")},
            )

# --- futures ----------------------------------------------------------------------------------
first_volume = {}
with (DATA / "TXFR1_1min_20160101_20260905.csv").open(encoding="utf-8-sig", newline="") as handle:
    for row in csv.DictReader(handle):
        ts = row["timestamp"]
        if lo <= ts < hi and ts[15] in "05" and "08:45" <= ts[11:16] < "13:45":
            first_volume[ts] = int(float(row["volume"]))
futures = defaultdict(list)  # contract month -> bars
with (DATA / "TXFR1_5min_20160101_20260905.csv").open(encoding="utf-8-sig", newline="") as handle:
    for row in csv.DictReader(handle):
        if not (lo <= row["timestamp"] < hi):
            continue
        trade_date = date.fromisoformat(row["trading_date"])
        if not calendar.is_trade_date(trade_date):
            continue
        month = front_month(trade_date)
        session = calendar.day_session("MTX", trade_date, month)
        s = datetime.fromisoformat(row["timestamp"]).replace(tzinfo=TAIPEI)
        e = s + timedelta(minutes=5)
        if session is None or not (session.opens_at <= s and e <= session.closes_at):
            continue
        volume = int(float(row["volume"]))
        futures[month].append(FeatureEvent(
            instrument_id="MTX", contract_month=month, trade_date=trade_date, session="day",
            bar_start=s, bar_end=e, available_at=e, open_price_available_at=s,
            open_volume=min(first_volume.get(row["timestamp"], 0), volume), open_volume_available_at=s,
            values={k: int(float(row[k])) for k in ("open", "high", "low", "close")} | {"volume": volume},
        ))

# --- carry ------------------------------------------------------------------------------------
rates = []
with (FM / "cbc_rates_EG28D01.csv").open(encoding="utf-8-sig", newline="") as handle:
    for row in csv.reader(handle):
        if row and row[0].strip('"\ufeff').isdigit():
            rates.append((datetime.strptime(row[0].strip('"'), "%Y%m%d").date(), D(row[1].strip('"')) / 100))
rates.sort()
dividends = []
with (FM / "taiex_dividend_points.csv").open(encoding="utf-8", newline="") as handle:
    for row in csv.DictReader(handle):
        if float(row["dividend_points"]) > 0:
            dividends.append((date.fromisoformat(row["date"]), D(row["dividend_points"])))


def carry_for(bar):
    day = bar.trade_date
    effective, rate = max((r for r in rates if r[0] <= day), default=(None, None))
    expiry = calendar.last_trade_date("MTX", bar.contract_month)
    pv = sum((pts for ex, pts in dividends if day < ex <= expiry), D(0))
    open_at = datetime.combine(day, datetime.min.time(), TAIPEI)
    rate_at = datetime.combine(effective, datetime.min.time(), TAIPEI) if effective else None
    if pv:
        return CarryInputs(annual_rate=rate, rate_available_at=rate_at, dividend_status="modeled",
                           dividend_pv=pv, dividend_available_at=open_at)
    return CarryInputs(annual_rate=rate, rate_available_at=rate_at, dividend_status="none")


print(f"spots={len(spots)} futures={sum(map(len, futures.values()))} months={len(futures)} "
      f"load={time.time() - t0:.0f}s", flush=True)

# --- run per front-month period ---------------------------------------------------------------
totals = {name: defaultdict(D) for name in ("basis", "price_only")}
counts = {name: defaultdict(int) for name in ("basis", "price_only")}
by_year = {name: defaultdict(D) for name in ("basis", "price_only")}
for month in sorted(futures):
    bars = futures[month]
    days = {bar.trade_date for bar in bars}
    spot_rows = [row for s, row in spots.items() if s.date() in days]
    carry = {(month, bar.bar_start): carry_for(bar) for bar in bars}
    comparison = compare_basis(spot_rows, bars, carry, calendar, schedules, CASH)
    for name in ("basis", "price_only"):
        result = getattr(comparison, name)
        m = calculate_metrics(result, initial_equity=CASH)
        trades = len(list(completed_position_cycles(result)))
        totals[name]["gross"] += m.gross_pnl
        totals[name]["cost"] += m.total_cost
        totals[name]["net"] += m.net_pnl
        counts[name]["trades"] += trades
        counts[name]["fills"] += m.fill_count
        by_year[name][month[:4]] += m.net_pnl
        for audit in result.audit:
            counts[name]["audit:" + str(getattr(audit, "code", None) or getattr(audit, "reason", "?"))] += 1
    print(f"{month} bars={len(bars)} basis_net={by_year['basis'][month[:4]]:.0f} t={time.time() - t0:.0f}s",
          flush=True)

print(f"{'policy':<12}{'trades':>7}{'fills':>7}{'gross':>11}{'cost':>10}{'net':>11}")
for name in ("basis", "price_only"):
    t = totals[name]
    print(f"{name:<12}{counts[name]['trades']:>7}{counts[name]['fills']:>7}{t['gross']:>11.0f}"
          f"{t['cost']:>10.0f}{t['net']:>11.0f}")
    print("   by year:", {y: int(v) for y, v in sorted(by_year[name].items())})
    print("   audits:", dict(sorted(((k, v) for k, v in counts[name].items() if k.startswith("audit")),
                                    key=lambda kv: -kv[1])[:5]))
