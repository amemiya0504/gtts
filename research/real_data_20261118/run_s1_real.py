"""S1 institutional Bayes vs baselines on real data (research only).

Data: FinMind TX institutional OI (2018-06-05+), TXFR1 5-min day bars as an MTX price proxy with the
front month derived from the generated 2017-2026 calendar, first 1-minute volume as opening volume.
Split fixed before running: train < TRAIN_BEFORE, OOS TRAIN_BEFORE..TEST_BEFORE.
Costs/margins are placeholders/approximations. Usage: uv run --locked python run_s1_real.py [end]
"""

import csv
import json
import os
import random
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from itertools import pairwise
from pathlib import Path

from gtts.data.calendar import TradingCalendar
from gtts.data.quality import FeatureEvent
from gtts.data.schema import TAIPEI
from gtts.data.sources import Observation
from gtts.research.metrics import calculate_metrics, completed_position_cycles
from gtts.risk.margin import Costs, Margin, Schedules
from gtts.strategies.institutional_bayes import (
    VARIANTS,
    compare_institutional,
    fit_institutional,
    opportunity,
    session_outcome,
)

DATA = Path(r"D:\fadacai\期貨價差\data")
FM = DATA / "finmind"
CASH = D(1_000_000)
FIRST = date(2018, 3, 1)  # 60-day institutional lookback needs data from 2018-06-05
LAST = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 9, 4)
TRAIN_BEFORE = datetime(2023, 1, 1, tzinfo=TAIPEI)
TEST_BEFORE = datetime.combine(LAST + timedelta(days=1), datetime.min.time(), TAIPEI)
PARTICIPANT = {"外資": "外資及陸資", "外資及陸資": "外資及陸資", "投信": "投信", "自營商": "自營商"}

calendar = TradingCalendar.from_yaml(FM / "gtts_calendar_2017_2026.yaml")
margins = tuple(
    Margin(effective_from=datetime.fromisoformat(m["effective_from"]), initial=D(m["initial"]),
           maintenance=D(m["maintenance"]))
    for m in json.loads((FM / "mtx_margins_approx.json").read_text(encoding="utf-8"))
)
schedules = Schedules(
    costs={"MTX": (Costs(effective_from=date(2017, 5, 15), broker_fee_per_contract=D(os.environ.get("S1_FEE", "25")),
                         tax_rate=D(".00002"), slippage_ticks=int(os.environ.get("S1_SLIP", "1"))),)},
    margins={"MTX": margins},
)
t0 = time.time()

# --- institutional observations ------------------------------------------------------------
# S1_HORIZON=k: feed the k-day rolling sum of OI levels, so the feature's one-day change equals the
# k-day change level_t - level_{t-k} (past-only); k=1 is the original S1.
HORIZON = int(__import__("os").environ.get("S1_HORIZON", "1"))
raw = sorted(json.loads((FM / "tx_institutional.json").read_text(encoding="utf-8")), key=lambda r: r["date"])
recent = defaultdict(list)
observations = []
for row in raw:
    day = date.fromisoformat(row["date"])
    participant = PARTICIPANT.get(row["institutional_investors"])
    if participant is None or not (calendar.covered_from <= day <= LAST) or not calendar.is_trade_date(day):
        continue
    levels = recent[participant]
    levels.append(row["long_open_interest_balance_volume"] - row["short_open_interest_balance_volume"])
    if len(levels) < HORIZON:
        continue
    session = calendar.day_session("TX", day)
    following = calendar.day_session("TX", calendar.next_trade_date(day))
    if session is None or following is None:
        continue
    net = sum(levels[-HORIZON:])
    observations.append(Observation(
        source_id="taifex-institutional", instrument_id="TX", trade_date=day,
        event_at=session.closes_at, ingested_at=following.opens_at, available_at=following.opens_at,
        availability_estimated=True, participant=participant, source_revision="finmind",
        checksum="finmind", values={"open_interest_net": net},
    ))

# --- bars ----------------------------------------------------------------------------------
lo, hi = FIRST.isoformat(), (LAST + timedelta(days=1)).isoformat()
first_volume = {}
with (DATA / "TXFR1_1min_20160101_20260905.csv").open(encoding="utf-8-sig", newline="") as handle:
    for row in csv.DictReader(handle):
        ts = row["timestamp"]
        if lo <= ts < hi and ts[15] in "05" and "08:45" <= ts[11:16] < "13:45":
            first_volume[ts] = int(float(row["volume"]))


def front_month(day):
    month = f"{day.year}{day.month:02d}"
    if day > calendar.last_trade_date("MTX", month):
        nxt = day.replace(day=1) + timedelta(days=32)
        month = f"{nxt.year}{nxt.month:02d}"
    return month


by_day = defaultdict(list)
with (DATA / "TXFR1_5min_20160101_20260905.csv").open(encoding="utf-8-sig", newline="") as handle:
    for row in csv.DictReader(handle):
        if not (lo <= row["timestamp"] < hi):
            continue
        trade_date = date.fromisoformat(row["trading_date"])
        if not calendar.is_trade_date(trade_date):
            continue
        month = front_month(trade_date)
        session = calendar.day_session("MTX", trade_date, month)
        start = datetime.fromisoformat(row["timestamp"]).replace(tzinfo=TAIPEI)
        end = start + timedelta(minutes=5)
        if session is None or not (session.opens_at <= start and end <= session.closes_at):
            continue
        volume = int(float(row["volume"]))
        by_day[trade_date].append(FeatureEvent(
            instrument_id="MTX", contract_month=month, trade_date=trade_date, session="day",
            bar_start=start, bar_end=end, available_at=end, open_price_available_at=start,
            open_volume=min(first_volume.get(row["timestamp"], 0), volume),
            open_volume_available_at=start,
            values={k: int(float(row[k])) for k in ("open", "high", "low", "close")} | {"volume": volume},
        ))
HOLDOUT = os.environ.get("S1_HOLDOUT") == "1"
HOLDOUT_START = date(2026, 9, 7)
if HOLDOUT:
    tick_from = date(2026, 8, 11)
    for day in [d for d in by_day if d >= tick_from]:
        del by_day[day]
    with (FM / "mtx_5min_by_contract.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            start = datetime.fromisoformat(row["bar_start"]).replace(tzinfo=TAIPEI)
            trade_date = start.date()
            if not (tick_from <= trade_date <= LAST) or row["contract_month"] != front_month(trade_date):
                continue
            session = calendar.day_session("MTX", trade_date, row["contract_month"])
            end = start + timedelta(minutes=5)
            if session is None or not (session.opens_at <= start and end <= session.closes_at):
                continue
            volume = int(row["volume"])
            # Opening volume is the size printed at the bar's first trade; known at the open only
            # when that trade is at bar_start (shortcut: later first trades still count as open).
            by_day[trade_date].append(FeatureEvent(
                instrument_id="MTX", contract_month=row["contract_month"], trade_date=trade_date,
                session="day", bar_start=start, bar_end=end, available_at=end,
                open_price_available_at=start, open_volume=min(int(row["open_volume"]), volume),
                open_volume_available_at=start,
                values={k: int(float(row[k])) for k in ("open", "high", "low", "close")} | {"volume": volume},
            ))
    for rows in by_day.values():
        rows.sort(key=lambda r: r.bar_start)
days = sorted(by_day)
print(f"horizon={HORIZON} observations={len(observations)} days={len(days)} bars={sum(map(len, by_day.values()))} "
      f"load={time.time() - t0:.0f}s", flush=True)

# --- opportunities and fixed-horizon outcomes ----------------------------------------------
obs_by_date = defaultdict(list)
for row in observations:
    obs_by_date[row.trade_date].append(row)
opportunities, outcomes, missing = [], [], defaultdict(int)
for previous, day in pairwise(days):
    rows = by_day[day]
    first = rows[0]
    # 62 trade dates of OI fit in 120 calendar days; the feature itself enforces the exact window.
    window = [o for d, group in obs_by_date.items() if 0 < (day - d).days <= 120 for o in group]
    candidate = opportunity(window, by_day[previous] + rows, first, calendar, schedules)
    if candidate is None:
        missing["opportunity"] += 1
        continue
    opportunities.append(candidate)
    by_start = {r.bar_start: r for r in rows}
    entry, exit = by_start.get(candidate.feature.decision_at), by_start.get(candidate.exit_at)
    label = session_outcome(candidate, entry, exit, calendar, schedules) if entry and exit else None
    if label is None:
        missing["outcome"] += 1
    else:
        outcomes.append(label)
print(f"opportunities={len(opportunities)} outcomes={len(outcomes)} skipped={dict(missing)} "
      f"t={time.time() - t0:.0f}s", flush=True)

model = fit_institutional(outcomes, train_before=TRAIN_BEFORE)
if model is None:
    sys.exit("model could not be fit")
train = [o for o in outcomes if o.available_at < TRAIN_BEFORE]
print(f"model: fit_count={model.fit_count} train_outcomes={len(train)} "
      f"up_rate={sum(o.up for o in train) / len(train):.3f} calib_brier={model.calibration.brier}", flush=True)

test_from = HOLDOUT_START if HOLDOUT else TRAIN_BEFORE.date()
oos_bars = [bar for d in days if test_from <= d <= LAST for bar in by_day[d]]
opportunities = [o for o in opportunities if o.bar.trade_date >= test_from]
report = compare_institutional(model, opportunities, oos_bars, calendar, schedules, CASH,
                               test_before=TEST_BEFORE)
print(f"compare t={time.time() - t0:.0f}s OOS opportunities={len(report.opportunities)}")
print(f"{'variant':<24}{'orders':>7}{'trades':>7}{'gross':>11}{'cost':>9}{'net':>11}  top audits")
for variant in VARIANTS:
    result = report.runs[variant]
    m = calculate_metrics(result, initial_equity=CASH)
    orders = sum(d.order is not None for d in report.decisions[variant])
    trades = len(list(completed_position_cycles(result)))
    audits = defaultdict(int)
    for a in result.audit:
        audits[getattr(a, "code", None) or getattr(a, "reason", "?")] += 1
    top = dict(sorted(audits.items(), key=lambda kv: -kv[1])[:4])
    print(f"{variant:<24}{orders:>7}{trades:>7}{m.gross_pnl:>11.0f}{m.total_cost:>9.0f}{m.net_pnl:>11.0f}  {top}")
for variant, cal in report.calibration.items():
    print(variant, "OOS brier", cal.brier, "n", cal.count,
          [(b.count, round(b.mean_probability, 3), round(b.positive_rate, 3)) for b in cal.bins])

# --- trade-level checks for one variant ------------------------------------------------------
VARIANT = os.environ.get("S1_VARIANT", "institutional_net")
result = report.runs[VARIANT]
per_day = defaultdict(lambda: {"net": D(0), "gross": D(0), "side": None, "reasons": []})
for fill in sorted(result.fills, key=lambda f: f.fill_at):
    t = per_day[fill.trade_date]
    t["net"] += fill.net_cash_change
    t["gross"] += fill.gross_pnl
    t["reasons"].append(fill.reason)
    if t["side"] is None:
        t["side"] = fill.side
trades = sorted(per_day.items())
if trades:
    nets = [float(t["net"]) for _, t in trades]
    mean = sum(nets) / len(nets)
    sd = (sum((x - mean) ** 2 for x in nets) / (len(nets) - 1)) ** 0.5 if len(nets) > 1 else 0
    print(f"{VARIANT}: n={len(nets)} mean_net={mean:.0f} sd={sd:.0f} t={mean / sd * len(nets) ** .5 if sd else 0:.2f} "
          f"win={sum(x > 0 for x in nets) / len(nets):.2%} "
          f"pf={sum(x for x in nets if x > 0) / -sum(x for x in nets if x < 0):.2f}")
    equity, peak, mdd = 0.0, 0.0, 0.0
    for x in nets:
        equity += x; peak = max(peak, equity); mdd = min(mdd, equity - peak)
    print(f"   max_drawdown={mdd:.0f}")
    for side in ("buy", "sell"):
        rows = [float(t["net"]) for _, t in trades if t["side"] == side]
        print(f"   {side}: n={len(rows)} net={sum(rows):.0f}")
    years = defaultdict(list)
    for day, t in trades:
        years[day.year].append(float(t["net"]))
    print("   by year:", {y: (len(v), round(sum(v))) for y, v in sorted(years.items())})
    reasons = defaultdict(int)
    for _, t in trades:
        reasons[str(t["reasons"][-1])] += 1
    print("   exit reasons:", dict(reasons))
    # Direction skill: fixed-horizon label (08:50 open -> 13:40 open, after costs) on the traded days,
    # signal side vs random sides on the same days.
    labels = {o.opportunity.bar.trade_date: o for o in outcomes}
    traded = [(day, t["side"]) for day, t in trades if day in labels]
    def score(sides):
        return sum(float(labels[d].net(sd)) for (d, _), sd in zip(traded, sides))
    actual = score([sd for _, sd in traded])
    rng = random.Random(20261118)
    draws = [score([rng.choice(("buy", "sell")) for _ in traded]) for _ in range(5000)]
    p = (1 + sum(x >= actual for x in draws)) / (1 + len(draws))
    print(f"   label check: n={len(traded)} signal_net={actual:.0f} random_mean={sum(draws) / len(draws):.0f} "
          f"p_one_sided={p:.4f} bonferroni_x3={min(1, 3 * p):.4f}")
