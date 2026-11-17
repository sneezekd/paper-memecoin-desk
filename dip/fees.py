"""Paper fee model of the source strategy, charged on each side on the trade's USD size."""
import json
from pathlib import Path

CFG = json.loads((Path(__file__).resolve().parent / "config.json").read_text())["fees"]


def fee_usd(size, fees=None):
    """<small_usd: flat; small_usd..mid_top: pct; mid_top..big_top: flat; above big_top: fee_pct."""
    c = fees or CFG
    size = float(size or 0)
    if size <= 0:
        return 0.0
    if size < c["small_usd"]:
        return round(c["small_flat"], 6)
    if size <= c["mid_top"]:
        return round(size * c["mid_pct"] / 100.0, 6)
    if size <= c["big_top"]:
        return round(c["big_flat"], 6)
    return round(size * c["fee_pct"] / 100.0, 6)
