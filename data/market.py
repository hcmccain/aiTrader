import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta, time
from typing import Optional
import logging
import pytz

logger = logging.getLogger(__name__)

ET = pytz.timezone("US/Eastern")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

# Federal holidays when US stock market is closed (2026)
MARKET_HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
}


def is_market_open() -> bool:
    now_et = datetime.now(ET)
    if now_et.strftime("%Y-%m-%d") in MARKET_HOLIDAYS_2026:
        return False
    if now_et.weekday() >= 5:
        return False
    return MARKET_OPEN <= now_et.time() <= MARKET_CLOSE


def get_market_session_phase() -> str:
    """Return 'pre_market', 'morning', 'midday', 'afternoon', 'closing', or 'closed'."""
    now_et = datetime.now(ET)
    t = now_et.time()
    if t < time(9, 25):
        return "closed"
    if t < MARKET_OPEN:
        return "pre_market"
    if t < time(11, 0):
        return "morning"
    if t < time(14, 0):
        return "midday"
    if t < time(15, 30):
        return "afternoon"
    if t <= MARKET_CLOSE:
        return "closing"
    return "closed"


def get_sessions_remaining_today() -> int:
    from config import INTRADAY_INTERVAL_MINUTES
    now_et = datetime.now(ET)
    if now_et.time() >= MARKET_CLOSE:
        return 0
    minutes_left = (MARKET_CLOSE.hour * 60 + MARKET_CLOSE.minute) - (now_et.hour * 60 + now_et.minute)
    return max(0, minutes_left // INTRADAY_INTERVAL_MINUTES)


def get_position_changes(agent_id: int) -> list[dict]:
    """Get current P&L for each position to show the agent what moved since last check."""
    from portfolio.database import get_positions
    changes = []
    for p in get_positions(agent_id):
        current = get_current_price(p["symbol"])
        if current is None:
            continue
        cost = p["avg_cost"]
        pnl = (current - cost) * p["quantity"]
        pnl_pct = ((current - cost) / cost * 100) if cost > 0 else 0
        changes.append({
            "symbol": p["symbol"],
            "asset_type": p["asset_type"],
            "qty": p["quantity"],
            "avg_cost": round(cost, 2),
            "current": round(current, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
        })
    return changes


def get_current_price(symbol: str) -> Optional[float]:
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.fast_info
        price = getattr(info, "last_price", None)
        if price is None:
            hist = ticker.history(period="1d")
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
        if price is None:
            price = _get_option_price(symbol)
        return round(float(price), 2) if price else None
    except Exception as e:
        logger.warning(f"Failed to get price for {symbol}: {e}")
        return None


def _parse_option_symbol(symbol: str) -> Optional[tuple]:
    """Parse an OCC option symbol into (underlying, exp_date, type C/P, strike)."""
    import re
    match = re.match(r'^([A-Z]+)(\d{6})([CP])(\d+)$', symbol)
    if not match:
        return None
    underlying = match.group(1)
    exp_raw = match.group(2)
    opt_type = match.group(3)
    strike = int(match.group(4)) / 1000
    exp_date = f"20{exp_raw[:2]}-{exp_raw[2:4]}-{exp_raw[4:6]}"
    return underlying, exp_date, opt_type, strike


def _black_scholes_price(spot: float, strike: float, days_to_exp: float, iv: float, opt_type: str, risk_free: float = 0.05) -> float:
    """Calculate theoretical option price using Black-Scholes."""
    import math
    if days_to_exp <= 0:
        if opt_type == "C":
            return max(0, spot - strike)
        return max(0, strike - spot)

    t = days_to_exp / 365.0
    d1 = (math.log(spot / strike) + (risk_free + 0.5 * iv ** 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)

    def norm_cdf(x):
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    if opt_type == "C":
        return spot * norm_cdf(d1) - strike * math.exp(-risk_free * t) * norm_cdf(d2)
    else:
        return strike * math.exp(-risk_free * t) * norm_cdf(-d2) - spot * norm_cdf(-d1)


def _get_option_price(symbol: str) -> Optional[float]:
    """Get a live option price using chain data + Black-Scholes with live underlying price."""
    try:
        parsed = _parse_option_symbol(symbol)
        if not parsed:
            return None
        underlying, exp_date, opt_type, strike = parsed

        spot = get_current_price(underlying)
        if spot is None:
            return None

        # Try chain first for bid/ask midpoint
        ticker = yf.Ticker(underlying)
        if exp_date in (ticker.options or []):
            try:
                chain = ticker.option_chain(exp_date)
                df = chain.calls if opt_type == "C" else chain.puts
                def _f(v, d=0.0):
                    try:
                        x = float(v)
                        return d if (x != x) else x
                    except (TypeError, ValueError):
                        return d

                row = df[abs(df["strike"] - strike) < 0.01]
                if not row.empty:
                    r = row.iloc[0]
                    bid = _f(r.get("bid", 0))
                    ask = _f(r.get("ask", 0))
                    last_price = _f(r.get("lastPrice", 0))
                    iv = _f(r.get("impliedVolatility", 0.5), 0.5)

                    # Use bid/ask midpoint if both are available and reasonable
                    if bid > 0 and ask > 0 and ask >= bid:
                        mid = (bid + ask) / 2
                        return round(mid, 2)

                    # Use lastPrice from chain when bid/ask are stale or zero
                    if last_price > 0.01:
                        return round(last_price, 2)

                    # Fall back to Black-Scholes with the chain's IV and live spot
                    exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
                    days = max(0.1, (exp_dt - datetime.now()).total_seconds() / 86400)
                    bs_price = _black_scholes_price(spot, strike, days, iv, opt_type)
                    if bs_price > 0:
                        return round(bs_price, 2)
            except Exception:
                pass

        # Last resort: Black-Scholes with default IV
        exp_dt = datetime.strptime(exp_date, "%Y-%m-%d")
        days = max(0.1, (exp_dt - datetime.now()).total_seconds() / 86400)
        bs_price = _black_scholes_price(spot, strike, days, 0.5, opt_type)
        return round(bs_price, 2) if bs_price > 0.005 else None

    except Exception as e:
        logger.debug(f"Option price lookup failed for {symbol}: {e}")
    return None


def get_market_data(symbol: str, period: str = "1mo") -> Optional[dict]:
    """Get price history, moving averages, and volume for a symbol."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period)
        if hist.empty:
            return None

        closes = hist["Close"]
        current = float(closes.iloc[-1])

        sma_20 = float(closes.rolling(20).mean().iloc[-1]) if len(closes) >= 20 else None
        sma_50 = float(closes.rolling(50).mean().iloc[-1]) if len(closes) >= 50 else None

        period_return = ((current - float(closes.iloc[0])) / float(closes.iloc[0])) * 100
        high = float(hist["High"].max())
        low = float(hist["Low"].min())
        avg_volume = int(hist["Volume"].mean())

        info = ticker.info
        name = info.get("shortName", symbol)
        sector = info.get("sector", "N/A")
        pe_ratio = info.get("trailingPE")
        market_cap = info.get("marketCap")
        dividend_yield = info.get("dividendYield")

        return {
            "symbol": symbol,
            "name": name,
            "sector": sector,
            "current_price": round(current, 2),
            "period_high": round(high, 2),
            "period_low": round(low, 2),
            "period_return_pct": round(period_return, 2),
            "sma_20": round(sma_20, 2) if sma_20 else None,
            "sma_50": round(sma_50, 2) if sma_50 else None,
            "avg_volume": avg_volume,
            "pe_ratio": round(pe_ratio, 2) if pe_ratio else None,
            "market_cap": market_cap,
            "dividend_yield": round(dividend_yield * 100, 2) if dividend_yield else None,
            "price_history": [
                {"date": d.strftime("%Y-%m-%d"), "close": round(float(c), 2)}
                for d, c in zip(hist.index, closes)
            ][-30:],
        }
    except Exception as e:
        logger.warning(f"Failed to get market data for {symbol}: {e}")
        return None


def get_options_chain(symbol: str) -> Optional[dict]:
    """Get available options contracts for a symbol."""
    try:
        ticker = yf.Ticker(symbol)
        expirations = ticker.options
        if not expirations:
            return None

        from datetime import datetime as _dt, timedelta
        today_dt = _dt.now()
        today = today_dt.strftime("%Y-%m-%d")
        target_min = (today_dt + timedelta(days=5)).strftime("%Y-%m-%d")
        target_max = (today_dt + timedelta(days=21)).strftime("%Y-%m-%d")
        nearest = expirations[0]
        for exp in expirations:
            if target_min <= exp <= target_max:
                nearest = exp
                break
        else:
            for exp in expirations:
                if exp > today:
                    nearest = exp
                    break
        chain = ticker.option_chain(nearest)

        def _safe_float(val, default=0.0):
            try:
                f = float(val)
                return default if (f != f) else f  # NaN check
            except (TypeError, ValueError):
                return default

        def _safe_int(val, default=0):
            try:
                f = float(val)
                return default if (f != f) else int(f)
            except (TypeError, ValueError):
                return default

        def format_options(df, current_price=None, is_calls=True):
            if current_price and "strike" in df.columns and len(df) > 15:
                df = df.copy()
                if is_calls:
                    # For calls: show mostly OTM (strike > price) — those are cheap
                    otm = df[df["strike"] >= current_price * 0.98].head(12)
                    itm = df[df["strike"] < current_price * 0.98].tail(3)
                    df = pd.concat([itm, otm]).sort_values("strike")
                else:
                    # For puts: show mostly OTM (strike < price) — those are cheap
                    otm = df[df["strike"] <= current_price * 1.02].tail(12)
                    itm = df[df["strike"] > current_price * 1.02].head(3)
                    df = pd.concat([otm, itm]).sort_values("strike")
                df = df.head(15)
            else:
                df = df.head(15)
            records = []
            for _, row in df.iterrows():
                records.append({
                    "contractSymbol": row.get("contractSymbol", ""),
                    "strike": _safe_float(row.get("strike")),
                    "lastPrice": _safe_float(row.get("lastPrice")),
                    "bid": _safe_float(row.get("bid")),
                    "ask": _safe_float(row.get("ask")),
                    "volume": _safe_int(row.get("volume")),
                    "openInterest": _safe_int(row.get("openInterest")),
                    "impliedVolatility": round(_safe_float(row.get("impliedVolatility")) * 100, 2),
                })
            return records

        current_price = get_current_price(symbol)
        return {
            "symbol": symbol,
            "current_price": current_price,
            "expiration": nearest,
            "all_expirations": list(expirations[:5]),
            "calls": format_options(chain.calls, current_price, is_calls=True),
            "puts": format_options(chain.puts, current_price, is_calls=False),
        }
    except Exception as e:
        logger.warning(f"Failed to get options for {symbol}: {e}")
        return None


_FALLBACK_UNIVERSE = [
    "TSLA","AMD","PLTR","COIN","SHOP","ROKU","SNOW","NET","CRWD",
    "SMCI","ARM","MRVL","QCOM","MU","INTC",
    "NFLX","DIS","ABNB","DASH","LYFT","PINS","HIMS",
    "MRNA","CRSP","EDIT","TDOC","EXAS","DXCM",
    "RIVN","LCID","NIO","PLUG","FSLR","ENPH",
    "SOFI","HOOD","AFRM","UPST","MARA","RIOT",
    "IONQ","JOBY","GEVO","ASTS",
    "TQQQ","SOXL","UPRO","TNA","LABU",
    "NVDA","AAPL","META","AMZN","GOOGL",
]


def _fetch_yahoo_screener(screen_id: str, count: int = 25) -> list[str]:
    """Fetch tickers from Yahoo Finance screener (day_gainers, day_losers, most_actives)."""
    import urllib.request
    import json as _json

    url = f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds={screen_id}&count={count}"
    headers = {"User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())
        quotes = data.get("finance", {}).get("result", [{}])[0].get("quotes", [])
        return [q["symbol"] for q in quotes if "." not in q.get("symbol", ".")]
    except Exception as e:
        logger.debug(f"Yahoo screener '{screen_id}' failed: {e}")
        return []


def _fetch_trending_tickers() -> list[str]:
    """Fetch currently trending tickers from Yahoo Finance."""
    import urllib.request
    import json as _json

    url = "https://query1.finance.yahoo.com/v1/finance/trending/US?count=30"
    headers = {"User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())
        quotes = data.get("finance", {}).get("result", [{}])[0].get("quotes", [])
        return [q["symbol"] for q in quotes if "." not in q.get("symbol", "")]
    except Exception as e:
        logger.debug(f"Yahoo trending failed: {e}")
        return []


def get_top_movers(limit: int = 10) -> list[dict]:
    """Find today's biggest movers using Yahoo Finance screeners + fallback scan."""
    # Pull real market movers from Yahoo Finance
    symbols_seen = set()
    scan_symbols = []

    for source_fn in [
        lambda: _fetch_yahoo_screener("day_gainers", 20),
        lambda: _fetch_yahoo_screener("day_losers", 20),
        lambda: _fetch_yahoo_screener("most_actives", 20),
        lambda: _fetch_trending_tickers(),
    ]:
        try:
            tickers = source_fn()
            for s in tickers:
                if s not in symbols_seen:
                    symbols_seen.add(s)
                    scan_symbols.append(s)
        except Exception:
            continue

    if len(scan_symbols) < 20:
        for s in _FALLBACK_UNIVERSE:
            if s not in symbols_seen:
                symbols_seen.add(s)
                scan_symbols.append(s)

    logger.info(f"Scanning {len(scan_symbols)} symbols for movers ({len(scan_symbols) - len(_FALLBACK_UNIVERSE)} from live screeners)")

    movers = []
    for sym in scan_symbols:
        try:
            t = yf.Ticker(sym)
            hist = t.history(period="1d", interval="5m")
            if hist.empty or len(hist) < 2:
                continue
            open_p = float(hist["Open"].iloc[0])
            now_p = float(hist["Close"].iloc[-1])
            high = float(hist["High"].max())
            low = float(hist["Low"].min())
            vol = float(hist["Volume"].sum())
            if open_p <= 0 or low <= 0:
                continue
            change_pct = (now_p - open_p) / open_p * 100
            range_pct = (high - low) / low * 100
            movers.append({
                "symbol": sym,
                "price": round(now_p, 2),
                "change_pct": round(change_pct, 2),
                "day_low": round(low, 2),
                "day_high": round(high, 2),
                "range_pct": round(range_pct, 1),
                "volume": int(vol),
                "direction": "UP" if change_pct > 0 else "DOWN",
            })
        except Exception:
            continue

    movers.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
    return movers[:limit]


def get_market_news() -> list[dict]:
    """Fetch market context: index performance + top mover summaries."""
    import urllib.request
    import json as _json

    context = []

    indices = {"^GSPC": "S&P 500", "^IXIC": "NASDAQ", "^DJI": "Dow Jones", "^VIX": "VIX (Fear Index)"}
    for sym, name in indices.items():
        try:
            t = yf.Ticker(sym)
            hist = t.history(period="2d")
            if len(hist) >= 2:
                prev = float(hist["Close"].iloc[-2])
                now = float(hist["Close"].iloc[-1])
                chg = (now - prev) / prev * 100
                context.append({
                    "type": "index",
                    "name": name,
                    "symbol": sym,
                    "price": round(now, 2),
                    "change_pct": round(chg, 2),
                    "direction": "UP" if chg > 0 else "DOWN",
                })
        except Exception:
            continue

    sectors = {"XLK": "Tech", "XLF": "Financials", "XLE": "Energy", "XLV": "Healthcare",
               "XBI": "Biotech", "ARKK": "Innovation/Growth", "SMH": "Semiconductors"}
    for sym, name in sectors.items():
        try:
            t = yf.Ticker(sym)
            hist = t.history(period="1d", interval="5m")
            if not hist.empty and len(hist) >= 2:
                open_p = float(hist["Open"].iloc[0])
                now_p = float(hist["Close"].iloc[-1])
                chg = (now_p - open_p) / open_p * 100
                context.append({
                    "type": "sector",
                    "name": name,
                    "symbol": sym,
                    "price": round(now_p, 2),
                    "change_pct": round(chg, 2),
                    "direction": "UP" if chg > 0 else "DOWN",
                })
        except Exception:
            continue

    # Top unusual volume stocks from screener
    try:
        unusual = _fetch_yahoo_screener("most_actives", 10)
        for sym in unusual[:5]:
            try:
                t = yf.Ticker(sym)
                info = t.fast_info
                price = getattr(info, "last_price", None)
                if price:
                    context.append({
                        "type": "unusual_volume",
                        "symbol": sym,
                        "price": round(float(price), 2),
                        "note": "Unusually high trading volume today",
                    })
            except Exception:
                continue
    except Exception:
        pass

    return context


def search_symbols(query: str) -> list[dict]:
    """Search for symbols by name or ticker."""
    common_symbols = {
        "technology": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSM", "AVGO"],
        "finance": ["JPM", "BAC", "GS", "MS", "V", "MA", "BRK-B"],
        "healthcare": ["JNJ", "UNH", "PFE", "ABBV", "MRK", "LLY", "TMO"],
        "energy": ["XOM", "CVX", "COP", "SLB", "EOG"],
        "consumer": ["WMT", "PG", "KO", "PEP", "COST", "MCD", "NKE"],
        "industrial": ["CAT", "DE", "HON", "UPS", "RTX", "LMT", "GE"],
        "commodities": ["GLD", "SLV", "USO", "UNG", "CORN", "WEAT", "DBA"],
        "etf": ["SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "ARKK"],
        "mutual_fund": ["VFIAX", "FXAIX", "VTSAX", "VBTLX", "VGTSX"],
        "bonds": ["TLT", "IEF", "SHY", "AGG", "BND", "LQD", "HYG"],
    }

    results = []
    query_lower = query.lower()

    for category, symbols in common_symbols.items():
        if query_lower in category:
            for sym in symbols:
                results.append({"symbol": sym, "category": category})

    if not results:
        try:
            ticker = yf.Ticker(query.upper())
            info = ticker.info
            if info.get("shortName"):
                results.append({
                    "symbol": query.upper(),
                    "name": info.get("shortName", ""),
                    "sector": info.get("sector", "N/A"),
                    "category": "search_result",
                })
        except Exception:
            pass

    return results[:15]


def get_sp500_return_since(agent_id: int) -> float:
    """Get S&P 500 total return since an agent was created."""
    try:
        from portfolio.database import get_agent
        agent = get_agent(agent_id)
        if not agent:
            return 0.0

        created = datetime.fromisoformat(agent["created_at"])
        spy = yf.Ticker("SPY")
        hist = spy.history(start=created.strftime("%Y-%m-%d"))
        if hist.empty or len(hist) < 2:
            return 0.0

        first_close = float(hist["Close"].iloc[0])
        last_close = float(hist["Close"].iloc[-1])
        return ((last_close - first_close) / first_close) * 100
    except Exception as e:
        logger.warning(f"Failed to get S&P 500 benchmark: {e}")
        return 0.0
