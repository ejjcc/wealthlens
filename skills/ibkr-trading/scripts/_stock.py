#!/usr/bin/env python3
"""Shared, deterministic helpers for US stock status and order preflight."""

import asyncio
import contextlib
import datetime as dt
import fcntl
import math
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from _ib import Contract, Stock


ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
ACTIVE_ORDER_STATUSES = {
    "ApiPending", "PendingSubmit", "PendingCancel", "PreSubmitted", "Submitted"
}
FAILED_ORDER_STATUSES = {"ApiCancelled", "Cancelled", "Inactive"}
DONE_ORDER_STATUSES = FAILED_ORDER_STATUSES | {"Filled"}
DEFAULT_ORDER_LOCK = os.path.expanduser("~/ibc/run/stock_order.lock")
DEFAULT_IBC_INI = os.path.expanduser("~/ibc/config.ini")
DEFAULT_GATEWAY_LOG = os.path.expanduser("~/ibc/logs/gateway.out.log")
LSOF_BIN = "/usr/sbin/lsof"
LIVE_QUOTE_SOURCES = {"tick_by_tick", "streaming_mkt_data"}


@dataclass(frozen=True)
class SessionInfo:
    name: str
    route: Optional[str]
    active: bool
    et_time: str
    note: str

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def _as_et(now: Optional[dt.datetime] = None) -> dt.datetime:
    now = now or dt.datetime.now(ET)
    if now.tzinfo is None:
        return now.replace(tzinfo=ET)
    return now.astimezone(ET)


def classify_us_stock_session(now: Optional[dt.datetime] = None) -> SessionInfo:
    """Classify the normal US stock session.

    This is a routing guard, not an exchange calendar. Holidays are caught by
    the separate live-quote freshness check instead of being guessed here.
    """
    now_et = _as_et(now)
    minute = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()  # Mon=0 ... Sun=6
    stamp = now_et.isoformat()

    if weekday == 5:
        return SessionInfo("closed", None, False, stamp, "Saturday")
    if weekday == 6:
        if minute >= 20 * 60:
            return SessionInfo("overnight", "OVERNIGHT", True, stamp,
                               "Sunday overnight session")
        return SessionInfo("closed", None, False, stamp, "Sunday before 20:00 ET")

    if minute < 3 * 60 + 50:
        return SessionInfo("overnight", "OVERNIGHT", True, stamp,
                           "US stock overnight session")
    if minute < 4 * 60:
        return SessionInfo("session_gap", None, False, stamp,
                           "03:50-04:00 ET overnight/premarket gap")
    if minute < 9 * 60 + 30:
        return SessionInfo("premarket", "SMART", True, stamp, "US premarket")
    if minute < 16 * 60:
        return SessionInfo("regular", "SMART", True, stamp, "US regular session")
    if minute < 20 * 60:
        return SessionInfo("afterhours", "SMART", True, stamp, "US after-hours")
    if weekday <= 3:
        return SessionInfo("overnight", "OVERNIGHT", True, stamp,
                           "US stock overnight session")
    return SessionInfo("closed", None, False, stamp, "Friday after 20:00 ET")


def route_is_live(route: str, session: SessionInfo) -> bool:
    return bool(session.active and session.route == route.upper())


def is_marketable(action: str, limit_price: float,
                  bid: Optional[float], ask: Optional[float]) -> Optional[bool]:
    action = action.upper()
    if action == "BUY":
        return None if ask is None else limit_price >= ask
    if action == "SELL":
        return None if bid is None else limit_price <= bid
    raise ValueError("action must be BUY or SELL")


def sell_stop_is_safe(stop_price: float, bid: Optional[float]) -> Optional[bool]:
    """A SELL stop must trigger below the current bid or it fires immediately."""
    if bid is None:
        return None
    return float(stop_price) < float(bid)


def quote_age_seconds(quote: Dict[str, object],
                      now: Optional[dt.datetime] = None) -> Optional[float]:
    value = quote.get("time")
    if not value:
        return None
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    now = now or dt.datetime.now(UTC)
    return max(0.0, (now.astimezone(UTC) - value.astimezone(UTC)).total_seconds())


def _positive_number(value) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def is_fresh_live_quote(quote: Dict[str, object], max_age: float = 10.0) -> bool:
    """Accept only current, two-sided live quotes from approved IB feeds."""
    age = quote_age_seconds(quote)
    bid, ask = quote.get("bid"), quote.get("ask")
    return bool(
        quote.get("source") in LIVE_QUOTE_SOURCES
        and _positive_number(bid) and _positive_number(ask)
        and float(ask) >= float(bid)
        and age is not None and age <= max_age
    )


def latest_tick_with(ticks, attribute: str):
    """Return the newest tick of a requested shape from a shared Ticker list."""
    matches = [tick for tick in ticks if hasattr(tick, attribute)]
    return matches[-1] if matches else None


async def qualify_stock(ib, symbol: str, route: str, currency: str = "USD") -> Contract:
    """Qualify through SMART first so a direct route retains primaryExchange."""
    symbol = symbol.upper()
    route = route.upper()
    base = Stock(symbol, "SMART", currency)
    qualified = await ib.qualifyContractsAsync(base)
    if not qualified or qualified[0] is None or not qualified[0].conId:
        raise RuntimeError("cannot qualify {}".format(symbol))
    base = qualified[0]
    if route == "SMART":
        return base
    if route != "OVERNIGHT":
        raise ValueError("unsupported stock route: {}".format(route))
    direct = Stock(symbol, route, currency, primaryExchange=base.primaryExchange)
    qualified = await ib.qualifyContractsAsync(direct)
    if not qualified or qualified[0] is None or not qualified[0].conId:
        raise RuntimeError("{} is not available on OVERNIGHT".format(symbol))
    return qualified[0]


async def _fetch_streaming_quote(ib, contract: Contract,
                                 timeout: float = 8.0) -> Optional[Dict[str, object]]:
    """Fetch a post-subscription live L1 book when no TBT update arrives.

    Only native live BID/ASK ticks (1/2 with marketDataType=1) are accepted.
    Delayed/frozen ticks, cached fields, one-sided books and crossed books fail
    closed.  Reception times are tracked separately and the older side is used
    as the quote timestamp.
    """
    ticker = ib.reqMktData(contract, "", False, False)
    state = {
        "bid": None, "bidSize": None, "bidTime": None,
        "ask": None, "askSize": None, "askTime": None,
        "last": None, "lastSize": None,
    }

    def on_update(updated):
        try:
            market_data_type = int(updated.marketDataType)
        except (TypeError, ValueError):
            return
        if market_data_type != 1:
            return
        received = dt.datetime.now(UTC)
        for tick in list(updated.ticks):
            tick_type = int(tick.tickType)
            price = float(tick.price)
            size = float(tick.size)
            if tick_type == 1 and _positive_number(price) and _positive_number(size):
                state.update(bid=price, bidSize=size, bidTime=received)
            elif tick_type == 2 and _positive_number(price) and _positive_number(size):
                state.update(ask=price, askSize=size, askTime=received)
            elif tick_type == 4 and _positive_number(price):
                state.update(last=price, lastSize=size if _positive_number(size) else None)

    ticker.updateEvent += on_update
    deadline = asyncio.get_event_loop().time() + timeout
    try:
        while asyncio.get_event_loop().time() < deadline:
            bid, ask = state["bid"], state["ask"]
            if bid is not None and ask is not None and ask >= bid:
                quote_time = min(state["bidTime"], state["askTime"])
                quote = {
                    "source": "streaming_mkt_data",
                    "route": contract.exchange,
                    "time": quote_time.isoformat(),
                    "bid": bid, "bidSize": state["bidSize"],
                    "ask": ask, "askSize": state["askSize"],
                    "last": state["last"], "lastSize": state["lastSize"],
                }
                quote["ageSeconds"] = round(quote_age_seconds(quote) or 0.0, 3)
                return quote
            await asyncio.sleep(0.1)
    finally:
        with contextlib.suppress(Exception):
            ticker.updateEvent -= on_update
        with contextlib.suppress(Exception):
            ib.cancelMktData(contract)
    return None


async def fetch_stock_quote(ib, contract: Contract, timeout: float = 4.0) -> Dict[str, object]:
    """Fetch a route-specific live bid/ask, preferring tick-by-tick data.

    If a quiet book produces no TBT update, a post-subscription native live L1
    BID/ASK feed is accepted. A one-minute historical trade fallback remains
    informational only and can never pass the live execution gate.
    """
    bid_ticker = ib.reqTickByTickData(contract, "BidAsk", 0, False)
    last_ticker = ib.reqTickByTickData(contract, "Last", 0, False)
    deadline = asyncio.get_event_loop().time() + timeout
    try:
        while asyncio.get_event_loop().time() < deadline:
            if latest_tick_with(bid_ticker.tickByTicks, "bidPrice") is not None:
                break
            await asyncio.sleep(0.1)
        # The IB client may return the same shared Ticker object for both requests,
        # so select by tick shape rather than assuming the final item type.
        bid_tick = latest_tick_with(bid_ticker.tickByTicks, "bidPrice")
        last_tick = latest_tick_with(last_ticker.tickByTicks, "price")
    finally:
        ib.cancelTickByTickData(contract, "BidAsk")
        ib.cancelTickByTickData(contract, "Last")

    if bid_tick:
        quote = {
            "source": "tick_by_tick",
            "route": contract.exchange,
            "time": bid_tick.time.isoformat(),
            "bid": float(bid_tick.bidPrice),
            "bidSize": float(bid_tick.bidSize),
            "ask": float(bid_tick.askPrice),
            "askSize": float(bid_tick.askSize),
            "last": float(last_tick.price) if last_tick else None,
            "lastSize": float(last_tick.size) if last_tick else None,
        }
        quote["ageSeconds"] = round(quote_age_seconds(quote) or 0.0, 3)
        return quote

    streaming_quote = await _fetch_streaming_quote(ib, contract)
    if streaming_quote is not None:
        return streaming_quote

    # Do not invent a bid/ask from BID_ASK bars. A last trade fallback is useful
    # for diagnosis but is never sufficient to pass the live execution gate.
    try:
        bars = await ib.reqHistoricalDataAsync(
            contract, endDateTime="", durationStr="300 S", barSizeSetting="1 min",
            whatToShow="TRADES", useRTH=False, formatDate=2, keepUpToDate=False,
            timeout=5,
        )
    except Exception:
        bars = []
    if bars:
        bar = bars[-1]
        bar_time = bar.date
        if isinstance(bar_time, dt.datetime) and bar_time.tzinfo is None:
            bar_time = bar_time.replace(tzinfo=ET)
        quote = {
            "source": "historical_trade",
            "route": contract.exchange,
            "time": bar_time.isoformat() if hasattr(bar_time, "isoformat") else str(bar_time),
            "bid": None, "bidSize": None, "ask": None, "askSize": None,
            "last": float(bar.close), "lastSize": None,
        }
        quote["ageSeconds"] = round(quote_age_seconds(quote) or 0.0, 3)
        return quote
    return {
        "source": "unavailable", "route": contract.exchange, "time": None,
        "bid": None, "bidSize": None, "ask": None, "askSize": None,
        "last": None, "lastSize": None, "ageSeconds": None,
    }


def snapshot_position(ib, symbol: str, account: Optional[str] = None) -> Dict[str, float]:
    # A stock and its options share the same contract.symbol.  Position sizing
    # for stock orders must never count option contracts as shares.
    rows = [
        p for p in ib.positions(account)
        if p.contract.secType == "STK" and p.contract.symbol == symbol
    ]
    qty = sum(float(p.position) for p in rows)
    if not rows or not qty:
        return {"quantity": qty, "avgCost": 0.0}
    weighted = sum(float(p.position) * float(p.avgCost) for p in rows)
    return {"quantity": qty, "avgCost": weighted / qty}


def active_stock_trades(ib, symbol: str):
    return [
        t for t in ib.openTrades()
        if t.contract.secType == "STK" and t.contract.symbol == symbol
        and t.orderStatus.status in ACTIVE_ORDER_STATUSES
        and float(t.orderStatus.remaining or 0) > 0
    ]


def order_to_dict(trade) -> Dict[str, object]:
    c, o, s = trade.contract, trade.order, trade.orderStatus
    return {
        "orderId": o.orderId, "permId": o.permId, "symbol": c.symbol,
        "route": c.exchange, "action": o.action,
        "quantity": float(o.totalQuantity), "orderType": o.orderType,
        "limitPrice": None if o.orderType == "MKT" else float(o.lmtPrice),
        "tif": o.tif, "status": s.status, "filled": float(s.filled or 0),
        "remaining": float(s.remaining or 0), "avgFillPrice": float(s.avgFillPrice or 0),
    }


def execution_to_dict(fill) -> Dict[str, object]:
    e = fill.execution
    return {
        "time": e.time.isoformat() if hasattr(e.time, "isoformat") else str(e.time),
        "symbol": fill.contract.symbol, "side": e.side, "shares": float(e.shares),
        "price": float(e.price), "orderId": e.orderId, "permId": e.permId,
        "execId": e.execId, "exchange": e.exchange,
    }


def redirect_bypass_configured(path: str = DEFAULT_IBC_INI) -> bool:
    try:
        with open(os.path.expanduser(path)) as f:
            values = [line.split("=", 1)[1].strip().lower() for line in f
                      if line.startswith("BypassRedirectOrderWarning=")]
    except OSError:
        return False
    return len(values) == 1 and values[0] == "yes"


def _gateway_start_time(port: int = 4001) -> Optional[dt.datetime]:
    try:
        pid = subprocess.check_output(
            [LSOF_BIN, "-nP", "-iTCP:{}".format(port), "-sTCP:LISTEN", "-t"],
            text=True, stderr=subprocess.DEVNULL,
        ).splitlines()[0]
        value = subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", pid], text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return dt.datetime.strptime(value, "%a %b %d %H:%M:%S %Y").astimezone()
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None


def redirect_bypass_applied(log_path: str = DEFAULT_GATEWAY_LOG,
                            gateway_start: Optional[dt.datetime] = None,
                            ini_path: str = DEFAULT_IBC_INI) -> Tuple[bool, str]:
    """Require evidence that the running Gateway applied the IBC checkbox."""
    if not redirect_bypass_configured(ini_path):
        return False, "BypassRedirectOrderWarning is not yes in config.ini"
    gateway_start = gateway_start or _gateway_start_time()
    if gateway_start is None:
        return False, "cannot determine running Gateway start time"
    pattern = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}):\d+ IBC: "
        r"'Bypass Redirect Order warning for Stock API Orders' is (?:now|already) selected"
    )
    latest = None
    try:
        with open(os.path.expanduser(log_path), errors="replace") as f:
            for line in f:
                match = pattern.search(line)
                if match:
                    latest = dt.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").astimezone()
    except OSError:
        return False, "cannot read Gateway log"
    if latest is None:
        return False, "Gateway log has no applied redirect-bypass confirmation"
    # IBC applies settings a few seconds after the Java process starts.
    if latest < gateway_start - dt.timedelta(seconds=5):
        return False, "config changed after the running Gateway started; restart required"
    return True, "redirect-bypass applied at {}".format(latest.isoformat())


@contextlib.contextmanager
def exclusive_order_lock(path: str = DEFAULT_ORDER_LOCK):
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(path, "a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another stock_order --execute process is already running")
        handle.seek(0)
        handle.truncate()
        handle.write("pid={} time={}\n".format(os.getpid(), dt.datetime.now().isoformat()))
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
