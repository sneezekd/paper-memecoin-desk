#!/usr/bin/env python3
"""Paper Memecoin Desk (Solana) - PAPER ONLY. No wallet, no signing, no live orders, no live switch.

  python run.py scan-once           # Risk manages open positions, then Scout > Filter > Analyst > Executor, Logger snapshot
  python run.py manage              # Fast path: re-mark open positions, ratchet trails, fire stops (no scout)
  python run.py tick                # what the scheduler runs: scan-once + dashboard-data
  python run.py report [--hours 24]
  python run.py recap [--hours 12]  # morning recap -> reports/recap_latest.md
  python run.py reject-audit [--days 7]
  python run.py dashboard-data      # -> reports/dashboard.json
  python run.py config
"""
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk.engine import Desk, load_config
from desk import report as R


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("scan-once", "manage", "tick", "dashboard-data", "config"):
        sub.add_parser(c)
    sub.add_parser("report").add_argument("--hours", type=float, default=24)
    sub.add_parser("recap").add_argument("--hours", type=float, default=12)
    sub.add_parser("reject-audit").add_argument("--days", type=float, default=7)
    a = ap.parse_args()
    if a.cmd == "scan-once":
        Desk().scan_once()
    elif a.cmd == "tick":
        Desk().scan_once(); R.dashboard_data(); print("dashboard.json updated")
    elif a.cmd == "manage":
        info = Desk().manage_only()
        print("exits:", info["exits"], "stop_moves:", info["stop_moves"], "open:", info["open"], flush=True)
        R.dashboard_data()
    elif a.cmd == "report":
        print(R.report(a.hours))
    elif a.cmd == "recap":
        print(R.recap(a.hours))
    elif a.cmd == "reject-audit":
        print(json.dumps(R.reject_audit(a.days), indent=1))
    elif a.cmd == "dashboard-data":
        d = R.dashboard_data(); print(json.dumps({"balance": d["balance"], "open": len(d["open_positions"]),
                                                  "funnel_24h": d["funnel_24h"]}, indent=1, default=str))
    elif a.cmd == "config":
        print(json.dumps(load_config(), indent=2))


if __name__ == "__main__":
    main()
