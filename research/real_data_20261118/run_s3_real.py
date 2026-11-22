"""S3 vs Donchian/ATR baselines on real TXFR1 5-min day-session bars (research only).

Assumptions (not verified quotes): TXFR1 prices/volume stand in for MTX; opening volume is the
first 1-minute volume of each 5-minute bar; costs and margins below are placeholders.
Usage: uv run --locked python run_s3_real.py 2026-01-01 2026-09-05
"""

import csv
import os
import sys
import time
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from gtts.data.calendar import TradingCalendar
from gtts.data.quality import FeatureEvent
from gtts.data.schema import TAIPEI
from gtts.research.metrics import calculate_metrics, completed_position_cycles
from gtts.risk.margin import Costs, Margin, Schedules
from gtts.strategies.regime_breakout import compare_regime_breakout

DATA = Path(r"D:\fadacai\期貨價差\data")
FIVE = DATA / "TXFR1_5min_20160101_20260905.csv"
ONE = DATA / "TXFR1_1min_20160101_20260905.csv"
CASH = D(1_000_000)

start, end = (date.fromisoformat(x) for x in sys.argv[1:3])
calendar = TradingCalendar.from_yaml(Path("configs/contracts.yaml"))
schedules = Schedules(
    costs={"MTX": (Costs(effective_from=date(2026, 1, 1), broker_fee_per_contract=D(25),
                         tax_rate=D(".00002"), slippage_ticks=1),)},
    margins={"MTX": (Margin(effective_from=datetime(2026, 1, 1, tzinfo=TAIPEI),
                            initial=D(100_000), maintenance=D(77_000)),)},
)


def rows(path):
    lo, hi = start.isoformat(), (end + timedelta(days=1)).isoformat()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if lo <= row["timestamp"] < hi:
                yield row


t0 = time.time()
first_volume = {}
for row in rows(ONE):
    ts = datetime.fromisoformat(row["timestamp"])
    if ts.minute % 5 == 0:
        first_volume[ts] = int(float(row["volume"]))

bars, skipped = [], 0
for row in rows(FIVE):
    bar_start = datetime.fromisoformat(row["timestamp"]).replace(tzinfo=TAIPEI)
    trade_date = date.fromisoformat(row["trading_date"])
    # The CSV contract label lags the roll; derive the front month from the calendar.
    month = f"{trade_date.year}{trade_date.month:02d}"
    if trade_date > calendar.last_trade_date("MTX", month):
        nxt = trade_date.replace(day=1) + timedelta(days=32)
        month = f"{nxt.year}{nxt.month:02d}"
    if os.environ.get("S3_PROXY") == "1":
        month = "202612"  # continuous proxy: one listed, unexpired label; roll gaps and expiry 13:30 close ignored
    session = calendar.day_session("MTX", trade_date, month) if calendar.is_trade_date(trade_date) else None
    bar_end = bar_start + timedelta(minutes=5)
    if session is None or not (session.opens_at <= bar_start and bar_end <= session.closes_at):
        skipped += 1
        continue
    volume = int(float(row["volume"]))
    opening = min(first_volume.get(bar_start.replace(tzinfo=None), 0), volume)
    bars.append(FeatureEvent(
        instrument_id="MTX", contract_month=month, trade_date=trade_date, session="day",
        bar_start=bar_start, bar_end=bar_end, available_at=bar_end,
        open_price_available_at=bar_start, open_volume=opening, open_volume_available_at=bar_start,
        values={k: int(float(row[k])) for k in ("open", "high", "low", "close")} | {"volume": volume},
    ))
print(f"bars={len(bars)} skipped={skipped} load={time.time() - t0:.1f}s", flush=True)

t1 = time.time()
comparison = compare_regime_breakout(bars, calendar, schedules, CASH)
print(f"compare={time.time() - t1:.1f}s")
print(f"{'policy':<20}{'trades':>7}{'fills':>7}{'gross':>12}{'cost':>10}{'net':>12}")
for name in ("s3", "donchian_baseline", "atr_baseline"):
    result = getattr(comparison, name)
    m = calculate_metrics(result, initial_equity=CASH)
    cycles = list(completed_position_cycles(result))
    print(f"{name:<20}{len(cycles):>7}{m.fill_count:>7}{m.gross_pnl:>12.0f}{m.total_cost:>10.0f}"
          f"{m.net_pnl:>12.0f}")
    rejects = {}
    for audit in result.audit:
        code = getattr(audit, "code", None) or getattr(audit, "reason", None)
        rejects[code] = rejects.get(code, 0) + 1
    print("   audit:", dict(sorted(rejects.items(), key=lambda kv: -kv[1])[:6]))
