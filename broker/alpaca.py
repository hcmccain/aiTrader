"""Alpaca broker integration — wraps the Alpaca Trading API for order execution,
account/position queries, and real-time market data (IEX feed)."""

import logging
import time
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, ClosePositionRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockLatestTradeRequest,
    StockLatestQuoteRequest,
    StockSnapshotRequest,
)

from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER

logger = logging.getLogger(__name__)

_client: Optional[TradingClient] = None
_data_client: Optional[StockHistoricalDataClient] = None


def _get_client() -> TradingClient:
    global _client
    if _client is None:
        if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
            raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env")
        _client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
    return _client


def _get_data_client() -> StockHistoricalDataClient:
    global _data_client
    if _data_client is None:
        if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
            raise RuntimeError("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in .env")
        _data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    return _data_client


def get_account() -> dict:
    """Return Alpaca account info: cash, portfolio value, buying power, etc."""
    acct = _get_client().get_account()
    return {
        "account_number": acct.account_number,
        "cash": float(acct.cash),
        "portfolio_value": float(acct.portfolio_value),
        "buying_power": float(acct.buying_power),
        "equity": float(acct.equity),
        "long_market_value": float(acct.long_market_value),
        "short_market_value": float(acct.short_market_value),
        "currency": acct.currency,
        "status": acct.status.value if acct.status else "UNKNOWN",
        "pattern_day_trader": getattr(acct, "pattern_day_trader", False),
        "daytrade_count": int(getattr(acct, "daytrade_count", 0)),
        "trading_blocked": getattr(acct, "trading_blocked", False),
        "paper": ALPACA_PAPER,
        "created_at": acct.created_at.isoformat() if getattr(acct, "created_at", None) else None,
    }


def get_positions() -> list[dict]:
    """Return all open positions from Alpaca."""
    positions = _get_client().get_all_positions()
    result = []
    for p in positions:
        result.append({
            "symbol": p.symbol,
            "qty": float(p.qty),
            "side": p.side.value if p.side else "long",
            "avg_entry_price": float(p.avg_entry_price),
            "current_price": float(p.current_price),
            "market_value": float(p.market_value),
            "unrealized_pl": float(p.unrealized_pl),
            "unrealized_plpc": float(p.unrealized_plpc) * 100,
            "asset_class": p.asset_class.value if p.asset_class else "us_equity",
        })
    return result


def get_position(symbol: str) -> Optional[dict]:
    """Return a single position by symbol, or None if not held."""
    try:
        p = _get_client().get_open_position(symbol)
        return {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "side": p.side.value if p.side else "long",
            "avg_entry_price": float(p.avg_entry_price),
            "current_price": float(p.current_price),
            "market_value": float(p.market_value),
            "unrealized_pl": float(p.unrealized_pl),
            "unrealized_plpc": float(p.unrealized_plpc) * 100,
        }
    except Exception:
        return None


def submit_market_order(
    symbol: str,
    qty: float,
    side: str,
    time_in_force: str = "day",
    agent_id: int = None,
) -> dict:
    """Submit a market order and wait for it to fill. Returns order details."""
    from uuid import uuid4

    client = _get_client()

    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
    tif = TimeInForce.DAY if time_in_force == "day" else TimeInForce.GTC

    client_order_id = f"agent{agent_id}_{uuid4().hex[:8]}" if agent_id else None

    order_data = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        time_in_force=tif,
        client_order_id=client_order_id,
    )

    order = client.submit_order(order_data)
    logger.info(f"Order submitted: {order.id} ({client_order_id}) — {side} {qty} {symbol}")

    filled_order = _wait_for_fill(str(order.id), timeout=30)
    return filled_order


def _wait_for_fill(order_id: str, timeout: int = 30) -> dict:
    """Poll until the order is filled, cancelled, or timeout is reached."""
    client = _get_client()
    deadline = time.time() + timeout

    while time.time() < deadline:
        order = client.get_order_by_id(order_id)
        status = order.status

        if status == OrderStatus.FILLED:
            return {
                "order_id": str(order.id),
                "status": "filled",
                "symbol": order.symbol,
                "side": order.side.value,
                "qty": float(order.filled_qty),
                "filled_avg_price": float(order.filled_avg_price),
                "filled_at": order.filled_at.isoformat() if order.filled_at else None,
            }

        if status in (OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED):
            return {
                "order_id": str(order.id),
                "status": status.value,
                "symbol": order.symbol,
                "side": order.side.value,
                "qty": float(order.qty),
                "filled_avg_price": None,
                "error": f"Order {status.value}",
            }

        time.sleep(0.5)

    return {
        "order_id": order_id,
        "status": "timeout",
        "error": f"Order did not fill within {timeout}s",
    }


def close_position(symbol: str) -> dict:
    """Close an entire position for a symbol."""
    try:
        client = _get_client()
        order = client.close_position(symbol)
        return {
            "order_id": str(order.id) if hasattr(order, "id") else None,
            "status": "closing",
            "symbol": symbol,
        }
    except Exception as e:
        return {"status": "error", "symbol": symbol, "error": str(e)}


def is_configured() -> bool:
    """Check whether Alpaca credentials are present."""
    return bool(ALPACA_API_KEY and ALPACA_SECRET_KEY)


# ---------------------------------------------------------------------------
# Real-time market data (IEX feed — free with Alpaca account)
# ---------------------------------------------------------------------------

def get_latest_trade_price(symbol: str) -> Optional[float]:
    """Get the real-time last trade price for a symbol."""
    try:
        client = _get_data_client()
        request = StockLatestTradeRequest(symbol_or_symbols=symbol)
        trades = client.get_stock_latest_trade(request)
        trade = trades.get(symbol)
        if trade:
            return round(float(trade.price), 2)
    except Exception as e:
        logger.debug(f"Alpaca latest trade failed for {symbol}: {e}")
    return None


def get_latest_quote(symbol: str) -> Optional[dict]:
    """Get the real-time bid/ask quote for a symbol."""
    try:
        client = _get_data_client()
        request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quotes = client.get_stock_latest_quote(request)
        quote = quotes.get(symbol)
        if quote:
            bid = float(quote.bid_price)
            ask = float(quote.ask_price)
            mid = round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else None
            return {
                "bid": round(bid, 2),
                "ask": round(ask, 2),
                "mid": mid,
                "bid_size": int(quote.bid_size),
                "ask_size": int(quote.ask_size),
            }
    except Exception as e:
        logger.debug(f"Alpaca latest quote failed for {symbol}: {e}")
    return None


def get_snapshot(symbol: str) -> Optional[dict]:
    """Get a full market snapshot: latest trade, quote, and daily bar."""
    try:
        client = _get_data_client()
        request = StockSnapshotRequest(symbol_or_symbols=symbol)
        snapshots = client.get_stock_snapshot(request)
        snap = snapshots.get(symbol)
        if not snap:
            return None

        result = {"symbol": symbol}

        if snap.latest_trade:
            result["price"] = round(float(snap.latest_trade.price), 2)

        if snap.latest_quote:
            result["bid"] = round(float(snap.latest_quote.bid_price), 2)
            result["ask"] = round(float(snap.latest_quote.ask_price), 2)

        if snap.daily_bar:
            result["open"] = round(float(snap.daily_bar.open), 2)
            result["high"] = round(float(snap.daily_bar.high), 2)
            result["low"] = round(float(snap.daily_bar.low), 2)
            result["close"] = round(float(snap.daily_bar.close), 2)
            result["volume"] = int(snap.daily_bar.volume)

        return result
    except Exception as e:
        logger.debug(f"Alpaca snapshot failed for {symbol}: {e}")
    return None


def get_latest_trade_prices_batch(symbols: list[str]) -> dict[str, float]:
    """Get real-time prices for multiple symbols in one call."""
    prices = {}
    try:
        client = _get_data_client()
        request = StockLatestTradeRequest(symbol_or_symbols=symbols)
        trades = client.get_stock_latest_trade(request)
        for sym, trade in trades.items():
            if trade and trade.price:
                prices[sym] = round(float(trade.price), 2)
    except Exception as e:
        logger.debug(f"Alpaca batch trade prices failed: {e}")
    return prices
