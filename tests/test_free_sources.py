"""Offline tests for free-source bundle/creator/analyst helpers. No network, no production DB."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from desk.sources import (bundle_share_from_rugcheck, prior_tokens_from_rugcheck, implied_ath_mcap,
                          extract_ath_and_current, is_rug_by_drawdown, classify_prior_rugs, analyst_weights)

def ok(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("ok:", msg)

# bundle: empty / missing nets => 0%, not unknown
rc0 = {"mint": "M", "token": {"supply": 1_000_000}, "graphInsidersDetected": 0}
pct, ev = bundle_share_from_rugcheck(rc0)
ok(pct == 0.0 and ev["insider_networks"] == 0, f"no networks => 0% (got {pct})")

rc_none = {"mint": "M", "token": {"supply": 1_000_000}, "insiderNetworks": None, "graphInsidersDetected": 0}
pct, ev = bundle_share_from_rugcheck(rc_none)
ok(pct == 0.0, f"insiderNetworks=None => 0% (got {pct})")

rc_hi = {"mint": "M", "token": {"supply": 1_000_000},
         "insiderNetworks": [{"tokenAmount": 80_000, "currentHolding": 50_000},
                             {"tokenAmount": 40_000, "currentHolding": 20_000}],
         "graphInsidersDetected": 6}
pct, ev = bundle_share_from_rugcheck(rc_hi)
ok(abs(pct - 12.0) < 1e-9, f"max(tokenAmount=120k, current=70k)/supply = 12% (got {pct})")
ok(pct > 10, "12% is above the 10% bundle kill")

rc_lo = {"mint": "M", "token": {"supply": 1_000_000},
         "insiderNetworks": [{"tokenAmount": 50_000, "currentHolding": 50_000}]}
pct, _ = bundle_share_from_rugcheck(rc_lo)
ok(abs(pct - 5.0) < 1e-9, f"5% bundle passes the 10% gate (got {pct})")

try:
    bundle_share_from_rugcheck({"token": {"supply": 1}})
    raise AssertionError("invalid report should raise")
except RuntimeError:
    ok(True, "invalid report fails closed")

# creatorTokens
ok(prior_tokens_from_rugcheck({"creatorTokens": None}, "A") == [], "null creatorTokens => no history")
ok(prior_tokens_from_rugcheck({}, "A") == [], "missing creatorTokens => no history")
priors = prior_tokens_from_rugcheck({"creatorTokens": [{"mint": "A", "symbol": "CUR"},
                                                      {"mint": "B", "symbol": "OLD", "token_ath_mc": 100000, "market_cap": 5000},
                                                      {"mint": "C", "symbol": "LIVE", "token_ath_mc": 80000, "market_cap": 20000}]},
                                   "A")
ok([p["mint"] for p in priors] == ["B", "C"], f"excludes current mint, got {priors}")

ok(is_rug_by_drawdown(100_000, 5_000, 30_000, 90), "90%+ drawdown from $100k ATH is a rug")
ok(not is_rug_by_drawdown(100_000, 20_000, 30_000, 90), "80% drawdown is not a rug under 90% rule")
ok(not is_rug_by_drawdown(20_000, 1_000, 30_000, 90), "never reached $30k ATH is not a rug")
ok(not is_rug_by_drawdown(None, 1_000, 30_000, 90), "unknown ATH is not counted as a rug")

# implied ATH from 24h change
ok(abs(implied_ath_mcap({"chg_h24": -95}, 2000) - 40_000) < 1e-6, " -95% of $2k implies $40k ATH")
ath, cur = extract_ath_and_current({}, {"mcap": 2000, "chg_h24": -95})
ok(cur == 2000 and abs(ath - 40_000) < 1e-6, "extract uses DexScreener implied ATH")

rugs, details = classify_prior_rugs(
    [{"mint": "B", "symbol": "RUG", "token_ath_mc": 100000, "market_cap": 1000},
     {"mint": "C", "symbol": "OK", "token_ath_mc": 80000, "market_cap": 20000},
     {"mint": "D", "symbol": "UNK"}],
    {}, 30_000, 90)
ok(rugs == ["RUG"], f"only verified drawdowns count as rugs, got {rugs} {details}")

# 2+ of last 10 kills; 1 does not
ok(len(rugs) < 2, "one rug is not a kill")
rugs2, _ = classify_prior_rugs(
    [{"mint": "B", "symbol": "R1", "token_ath_mc": 100000, "market_cap": 1000},
     {"mint": "C", "symbol": "R2", "token_ath_mc": 50000, "market_cap": 100}],
    {}, 30_000, 90)
ok(len(rugs2) == 2, "two rugs in lookback is a kill")

# analyst reweight
W = {"momentum": 20, "unique_buyers": 15, "buy_sell_ratio": 15, "holder_growth": 15, "liq_mcap": 15, "smart_wallets": 20}
keep = analyst_weights(W, True)
ok(keep == W and sum(keep.values()) == 100, "GMGN path keeps original weights")
drop = analyst_weights(W, False)
ok("smart_wallets" not in drop, "free path drops smart_wallets")
ok(abs(sum(drop.values()) - 100) < 1e-9, f"reweighted sum is 100, got {sum(drop.values())}")
ok(abs(drop["momentum"] - 25) < 1e-9, f"momentum 20 + 5 = 25, got {drop['momentum']}")
ok(abs(drop["unique_buyers"] - 18.75) < 1e-9, f"unique_buyers 18.75, got {drop['unique_buyers']}")
# coverage with holder_growth missing: 81.25% >= 60
cov = (sum(v for k, v in drop.items() if k != "holder_growth") / sum(drop.values())) * 100
ok(cov >= 60, f"4/5 factors still meet 60% coverage ({cov})")
print("ALL OFFLINE TESTS PASSED")
