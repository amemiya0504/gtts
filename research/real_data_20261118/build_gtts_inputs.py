"""Turn downloaded FinMind/TAIFEX/CBC files into GTTS inputs (research only).

Outputs in D:/fadacai/期貨價差/data/finmind:
  gtts_calendar_2017_2026.yaml  calendar from TaiwanStockTradingDate + MTX last trade dates
  mtx_margins_approx.json       monthly MTX margin = 7% of last month-end notional (approximation)
  mtx_5min_by_contract.csv      MTX day-session 5-min bars per contract month from TAIFEX ticks
"""

import csv
import io
import json
import os
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(r"D:\fadacai\期貨價差\data")
FM = ROOT / "finmind"
GTTS = Path(os.environ["GTTS_REPO"])  # local gtts checkout (configs/contracts.yaml)
START, END = date(2017, 5, 15), date(2026, 12, 31)

# --- calendar --------------------------------------------------------------------------
trading = {date.fromisoformat(r["date"]) for r in json.loads((FM / "trading_dates.json").read_text("utf-8"))}
trading = {d for d in trading if START <= d <= END}
closed, extra, day = [], [], START
while day <= END:
    if day.weekday() < 5 and day not in trading:
        closed.append(day)
    elif day.weekday() >= 5 and day in trading:
        extra.append(day)
    day += timedelta(days=1)
daily = json.loads((FM / "mtx_daily.json").read_text("utf-8"))
last_trade = {}
for row in daily:
    month = row["contract_date"]
    if row["trading_session"] == "position" and month.isdigit() and len(month) == 6 and row["volume"] > 0:
        last_trade[month] = max(last_trade.get(month, ""), row["date"])
base = yaml.safe_load((GTTS / "configs/contracts.yaml").read_text(encoding="utf-8"))
latest = max(r["date"] for r in daily)
# A contract still trading on the last downloaded date has no observed last trade date yet.
overrides = {m: date.fromisoformat(d) for m, d in sorted(last_trade.items())
             if START <= date.fromisoformat(d) and d < latest}
calendar = {
    "covered_from": START, "covered_through": END,
    "closed_dates": closed, "extra_open_dates": extra,
    "cash_close": base["cash_close"], "contracts": base["contracts"],
    "expiry_overrides": {"TX": overrides, "MTX": overrides},
}
out = FM / "gtts_calendar_2017_2026.yaml"
out.write_text("# Generated from FinMind TaiwanStockTradingDate and MTX last traded dates; not TAIFEX-verified.\n"
               + yaml.safe_dump(calendar, allow_unicode=True, sort_keys=False), encoding="utf-8")
print(f"calendar: {len(trading)} trade dates, {len(closed)} closed weekdays, {len(extra)} weekend opens, "
      f"{len(overrides)} expiry dates")

# --- margins (approximation anchored on 2024-10 84,500 / 2025-08 76,500 / 2026-11 175,250) ---
closes = defaultdict(dict)
for row in daily:
    if row["trading_session"] == "position" and row["close"] > 0 and row["contract_date"].isdigit():
        closes[row["date"]][row["contract_date"]] = (row["volume"], row["close"])
month_end = {}
for d in sorted(closes):
    front = max(closes[d].values())[1]  # highest-volume contract
    month_end[d[:7]] = front
margins = []
for ym, close in sorted(month_end.items()):
    y, m = map(int, ym.split("-"))
    effective = date(y + m // 12, m % 12 + 1, 1)
    initial = round(close * 50 * 0.07 / 250) * 250
    margins.append({"effective_from": f"{effective}T00:00:00+08:00", "initial": initial,
                    "maintenance": round(initial * 0.767 / 250) * 250})
(FM / "mtx_margins_approx.json").write_text(json.dumps(margins, indent=0), encoding="utf-8")
print("margins:", margins[0], margins[-1])

# --- TAIFEX ticks -> MTX per-contract 5-minute day bars ----------------------------------
bars = {}
for path in sorted((ROOT / "raw").glob("Daily_*.zip")):
    with zipfile.ZipFile(path) as archive:
        name = archive.namelist()[0]
        text = archive.read(name).decode("cp950", errors="replace")
    reader = csv.reader(io.StringIO(text))
    next(reader)
    for row in reader:
        if len(row) < 6 or row[1].strip() != "MTX":
            continue
        month = row[2].strip()
        if not (month.isdigit() and len(month) == 6):
            continue  # weekly contracts and spreads
        hhmmss = row[3].strip().zfill(6)
        t = datetime.strptime(row[0].strip() + hhmmss, "%Y%m%d%H%M%S")
        if not ("084500" <= hhmmss < "134500"):
            continue
        start = t.replace(minute=t.minute - t.minute % 5, second=0)
        price, qty = float(row[4]), int(row[5]) // 2  # B+S counts both sides
        key = (month, start)
        bar = bars.get(key)
        first_second = hhmmss.endswith("00") and t.minute % 5 == 0 and t.second == 0
        if bar is None:
            bars[key] = bar = {"open": price, "high": price, "low": price, "close": price, "volume": 0,
                               "open_volume": 0, "first_ts": t}
        bar["high"], bar["low"], bar["close"] = max(bar["high"], price), min(bar["low"], price), price
        bar["volume"] += qty
        if t == bar["first_ts"]:
            bar["open_volume"] += qty  # trades printed at the bar's first trade timestamp
with open(FM / "mtx_5min_by_contract.csv", "w", newline="", encoding="utf-8") as handle:
    writer = csv.writer(handle)
    writer.writerow(["bar_start", "contract_month", "open", "high", "low", "close", "volume", "open_volume",
                     "first_trade_at"])
    for (month, start), b in sorted(bars.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        writer.writerow([start.isoformat(sep=" "), month, b["open"], b["high"], b["low"], b["close"],
                         b["volume"], b["open_volume"], b["first_ts"].isoformat(sep=" ")])
days = sorted({s.date() for _, s in bars})
print(f"ticks: {len(bars)} MTX bars, {len(days)} days {days[0]}..{days[-1]}, "
      f"months {sorted({m for m, _ in bars})}")
