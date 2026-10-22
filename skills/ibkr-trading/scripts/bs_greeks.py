#!/usr/bin/env python3
"""
Black-Scholes 期权 Greeks / IV 计算器（不依赖 IB Gateway，纯本地数学）

补 quote_option.py / option_risk.py 的盲区：收盘后 IBKR 不返回 modelGreeks
（delta=0），或要算"假设场景"（改 spot/IV/到期看 Greeks 怎么变）时，用这个。
给 spot + strike + 到期 + 市价 就能反推 IV 并算全套 Greeks，盘后照算。

用法:
  # 给市价反推 IV + 算 Greeks
  bs_greeks.py --spot 701.64 --strike 610 --expiry 2027-01-15 --type put --price 22.45

  # 直接给 DTE（不用日历日期）
  bs_greeks.py --spot 630 --strike 600 --dte 30 --type call --price 40

  # 指定 IV（不反推），看理论价 + Greeks
  bs_greeks.py --spot 630 --strike 600 --expiry 2026-09-18 --type call --vol 0.42

  # 站在未来某天算（theta 衰减推演）
  bs_greeks.py --spot 630 --strike 600 --expiry 2026-05-15 --date 2026-03-01 --type call --price 50

  # JSON 输出
  bs_greeks.py --spot 701.64 --strike 610 --expiry 2027-01-15 --type put --price 22.45 --json

注意:
  - 纯 Black-Scholes，无股息、欧式定价；美式 / 高股息标的的 Greeks 会有偏差
  - 反推 IV 用 Newton-Raphson，失败回退二分法
  - 不连 IB、不取行情，spot/price 要自己传（从 quote_option.py 拿）

Adapted from staskh/trading_skills (MIT); scipy.stats.norm replaced with
math.erf-based normal CDF/PDF so this stays pure-stdlib (no scipy/numpy).
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")


def _ncdf(x: float) -> float:
    """Standard normal CDF via erf (matches scipy.stats.norm.cdf to ~1e-16)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return d1, d2


def bs_price(S: float, K: float, T: float, r: float, sigma: float, option_type: str) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if option_type == "call" else max(0.0, K - S)
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if option_type == "call":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def bs_vega(S: float, K: float, T: float, r: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return 0.0
    d1, _ = _d1_d2(S, K, T, r, sigma)
    return S * _npdf(d1) * math.sqrt(T)


def bs_greeks(S: float, K: float, T: float, r: float, sigma: float, option_type: str) -> dict:
    if T <= 0:
        return {"error": "Option has expired"}
    if sigma <= 0:
        return {"error": "Invalid volatility"}

    d1, d2 = _d1_d2(S, K, T, r, sigma)
    sqrt_T = math.sqrt(T)
    N_d1 = _ncdf(d1)
    N_d2 = _ncdf(d2)
    n_d1 = _npdf(d1)

    if option_type == "call":
        delta = N_d1
        theta = (-S * n_d1 * sigma / (2 * sqrt_T) - r * K * math.exp(-r * T) * N_d2) / 365
        rho = K * T * math.exp(-r * T) * N_d2 / 100
        price = S * N_d1 - K * math.exp(-r * T) * N_d2
    else:
        delta = N_d1 - 1
        N_neg_d2 = _ncdf(-d2)
        theta = (-S * n_d1 * sigma / (2 * sqrt_T) + r * K * math.exp(-r * T) * N_neg_d2) / 365
        rho = -K * T * math.exp(-r * T) * N_neg_d2 / 100
        price = K * math.exp(-r * T) * N_neg_d2 - S * (1 - N_d1)

    gamma = n_d1 / (S * sigma * sqrt_T)
    vega = S * n_d1 * sqrt_T / 100

    return {
        "price": round(price, 4),
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 4),
        "vega": round(vega, 4),
        "rho": round(rho, 4),
    }


def implied_vol(
    market_price: float, S: float, K: float, T: float, r: float, option_type: str,
    max_iter: int = 100, tol: float = 1e-6,
) -> float | None:
    """Newton-Raphson with bisection fallback."""
    if market_price <= 0 or T <= 0:
        return None
    sigma = 0.3
    for _ in range(max_iter):
        price = bs_price(S, K, T, r, sigma, option_type)
        vega = bs_vega(S, K, T, r, sigma)
        if vega < 1e-10:
            return _iv_bisection(market_price, S, K, T, r, option_type)
        diff = price - market_price
        if abs(diff) < tol:
            return sigma
        sigma = sigma - diff / vega
        sigma = min(max(sigma, 0.001), 5.0)
    return _iv_bisection(market_price, S, K, T, r, option_type)


def _iv_bisection(
    market_price: float, S: float, K: float, T: float, r: float, option_type: str,
    max_iter: int = 100, tol: float = 1e-6,
) -> float | None:
    low, high = 0.001, 5.0
    for _ in range(max_iter):
        mid = (low + high) / 2
        price = bs_price(S, K, T, r, mid, option_type)
        if abs(price - market_price) < tol:
            return mid
        if price > market_price:
            high = mid
        else:
            low = mid
    return (low + high) / 2


def calculate_greeks(
    spot: float, strike: float, option_type: str,
    expiry: str | None = None, dte: int | None = None, as_of_date: str | None = None,
    market_price: float | None = None, rate: float = 0.05, volatility: float | None = None,
) -> dict:
    if dte is not None:
        days_to_expiry = dte
        expiry_str = f"{dte} DTE"
    elif expiry is not None:
        expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        ref_date = (
            datetime.strptime(as_of_date, "%Y-%m-%d").date()
            if as_of_date else datetime.now(_NY).date()
        )
        days_to_expiry = (expiry_date - ref_date).days
        expiry_str = expiry
    else:
        return {"error": "Must provide either expiry or dte"}

    T = days_to_expiry / 365
    if T <= 0:
        return {"error": "Option has expired or expires today"}

    if market_price is not None and market_price > 0:
        iv = implied_vol(market_price, spot, strike, T, rate, option_type)
        if iv is None:
            iv = 0.30
    elif volatility is not None:
        iv = volatility
    else:
        iv = 0.30

    greeks = bs_greeks(spot, strike, T, rate, iv, option_type)

    return {
        "spot": round(spot, 2),
        "strike": strike,
        "expiry": expiry_str,
        "days_to_expiry": days_to_expiry,
        "option_type": option_type,
        "market_price": round(market_price, 2) if market_price else None,
        "iv": round(iv * 100, 2),
        "risk_free_rate": rate,
        "greeks": greeks,
        "generated_at": datetime.now(_NY).strftime("%Y-%m-%d %H:%M ET"),
        "source": "black-scholes (no live data)",
    }


def _print_human(r: dict) -> None:
    if "error" in r:
        print(f"ERROR: {r['error']}")
        return
    g = r["greeks"]
    if "error" in g:
        print(f"ERROR: {g['error']}")
        return
    typ = r["option_type"].upper()
    print(f"{typ} K={r['strike']} exp={r['expiry']} ({r['days_to_expiry']}d)  spot={r['spot']}")
    mp = r["market_price"]
    print(f"  IV       {r['iv']:.2f}%   {'(反推自市价 ' + str(mp) + ')' if mp else '(给定/默认)'}")
    print(f"  理论价    {g['price']}")
    print(f"  Δ delta  {g['delta']:+.4f}   标的 ±$1 → 期权 ±${abs(g['delta']):.2f}")
    print(f"  Γ gamma  {g['gamma']:+.6f}  delta 对标的的变化率")
    print(f"  Θ theta  {g['theta']:+.4f}   每天时间价值变化 {g['theta']:+.2f}")
    print(f"  V vega   {g['vega']:+.4f}   IV ±1% → 期权 ±${g['vega']:.2f}")
    print(f"  ρ rho    {g['rho']:+.4f}   利率 ±1% → 期权 ±${g['rho']:.2f}")
    print(f"  [{r['source']}; {r['generated_at']}]")


def main():
    p = argparse.ArgumentParser(description="Black-Scholes Greeks / IV calculator (no IB)")
    p.add_argument("--spot", type=float, required=True, help="标的现价")
    p.add_argument("--strike", type=float, required=True, help="行权价")
    p.add_argument("--type", choices=["call", "put"], required=True, help="期权类型")
    p.add_argument("--expiry", help="到期日 YYYY-MM-DD（与 --dte 二选一）")
    p.add_argument("--dte", type=int, help="距到期天数（与 --expiry 二选一）")
    p.add_argument("--date", help="站在某天算 YYYY-MM-DD（默认今天，NY）")
    p.add_argument("--price", type=float, help="期权市价（反推 IV）")
    p.add_argument("--vol", type=float, help="指定 IV（小数，如 0.30），不反推")
    p.add_argument("--rate", type=float, default=0.05, help="无风险利率（默认 0.05）")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    args = p.parse_args()

    if not args.expiry and args.dte is None:
        p.error("必须提供 --expiry 或 --dte")

    result = calculate_greeks(
        spot=args.spot, strike=args.strike, option_type=args.type,
        expiry=args.expiry, dte=args.dte, as_of_date=args.date,
        market_price=args.price, rate=args.rate, volatility=args.vol,
    )

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_human(result)


if __name__ == "__main__":
    main()
