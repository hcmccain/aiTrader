from datetime import datetime, date

from portfolio import database as db
from portfolio.models import AssetType, TradeAction, Position, PortfolioSummary
from data.market import get_current_price, get_sp500_return_since, is_market_open
from broker import alpaca as broker


def initialize():
    db.init_db()


def _asset_type_from_alpaca(asset_class: str) -> str:
    """Map Alpaca asset_class strings to our AssetType values."""
    mapping = {
        "us_equity": "stock",
        "us_option": "option",
        "crypto": "stock",
    }
    return mapping.get(asset_class, "stock")


def get_portfolio_summary(agent_id: int) -> PortfolioSummary:
    starting_capital = db.get_starting_capital(agent_id)
    return _portfolio_from_db(agent_id, starting_capital)


def _portfolio_from_db(agent_id: int, starting_capital: float) -> PortfolioSummary:
    """Original DB-only portfolio summary (fallback when Alpaca is unavailable)."""
    cash = db.get_cash(agent_id)
    raw_positions = db.get_positions(agent_id)

    positions: list[Position] = []
    invested_value = 0.0

    for p in raw_positions:
        current_price = get_current_price(p["symbol"])
        if current_price is None:
            last_trade = db.get_last_trade_price(agent_id, p["symbol"])
            current_price = last_trade if last_trade else p["avg_cost"]

        mult = 100 if p["asset_type"] == "option" else 1
        market_value = current_price * p["quantity"] * mult
        cost_basis = p["avg_cost"] * p["quantity"] * mult
        unrealized_pnl = market_value - cost_basis
        unrealized_pnl_pct = (unrealized_pnl / cost_basis * 100) if cost_basis > 0 else 0.0

        positions.append(Position(
            id=p["id"],
            symbol=p["symbol"],
            asset_type=AssetType(p["asset_type"]),
            quantity=p["quantity"],
            avg_cost=p["avg_cost"],
            current_price=current_price,
            market_value=market_value,
            unrealized_pnl=unrealized_pnl,
            unrealized_pnl_pct=unrealized_pnl_pct,
        ))
        invested_value += market_value

    total_value = cash + invested_value
    total_return = total_value - starting_capital
    total_return_pct = (total_return / starting_capital * 100) if starting_capital > 0 else 0.0

    prev_snapshot = db.get_previous_day_snapshot(agent_id)

    today_str = date.today().isoformat()
    today_trades = db.get_trades(agent_id, limit=1, start_date=today_str)
    market_open = is_market_open()

    if not market_open and not today_trades and prev_snapshot:
        daily_return = 0.0
        daily_return_pct = 0.0
        total_value = prev_snapshot["total_value"]
    elif prev_snapshot:
        prev_value = prev_snapshot["total_value"]
        daily_return = total_value - prev_value
        daily_return_pct = (daily_return / prev_value * 100) if prev_value > 0 else 0.0

        realized_today = db.get_realized_pnl_today(agent_id)
        if realized_today is not None and daily_return < 0 < realized_today:
            daily_return = realized_today
            daily_return_pct = (daily_return / prev_value * 100) if prev_value > 0 else 0.0
    else:
        daily_return = total_return
        daily_return_pct = total_return_pct

    return PortfolioSummary(
        total_value=round(total_value, 2),
        cash=round(cash, 2),
        invested_value=round(invested_value, 2),
        total_return=round(total_return, 2),
        total_return_pct=round(total_return_pct, 2),
        daily_return=round(daily_return, 2),
        daily_return_pct=round(daily_return_pct, 2),
        num_positions=len(positions),
        positions=positions,
    )


def validate_trade(
    agent_id: int,
    symbol: str,
    asset_type: AssetType,
    action: TradeAction,
    quantity: float,
    price: float,
) -> tuple[bool, str]:
    """Validate a trade against risk guardrails. Returns (is_valid, reason)."""
    risk = db.get_risk_params(agent_id)
    allowed_types = risk.get("allowed_asset_types", [])

    asset_type_key = asset_type.value
    if asset_type_key not in allowed_types:
        return False, f"Asset type '{asset_type_key}' is not allowed for this agent. Allowed: {', '.join(allowed_types)}"

    max_position_pct = risk["max_position_pct"]
    max_options_pct = risk["max_options_pct"]
    min_cash_reserve_pct = risk["min_cash_reserve_pct"]

    summary = get_portfolio_summary(agent_id)
    cash = summary.cash
    total_value = summary.total_value
    multiplier = 100 if asset_type == AssetType.OPTION else 1
    total_cost = price * quantity * multiplier

    if action == TradeAction.BUY:
        min_cash = total_value * min_cash_reserve_pct
        if cash - total_cost < min_cash:
            return False, f"Trade would leave cash below minimum reserve ({min_cash_reserve_pct*100:.0f}% = ${min_cash:.2f})"

        existing_value = 0.0
        for p in summary.positions:
            if p.symbol == symbol:
                existing_value = p.market_value
                break
        new_position_value = existing_value + total_cost
        if total_value > 0 and new_position_value / total_value > max_position_pct:
            return False, f"Position would exceed max single position size ({max_position_pct*100:.0f}%)"

        import re as _re
        underlying_ticker = _re.match(r'^([A-Z]+)', symbol)
        if underlying_ticker:
            ticker = underlying_ticker.group(1)
            from datetime import timedelta
            cutoff = (datetime.now() - timedelta(minutes=10)).isoformat()
            conn = db.get_connection()
            recent_sell = conn.execute(
                "SELECT timestamp FROM trades WHERE agent_id=? AND action='sell' "
                "AND symbol LIKE ? AND timestamp >= ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (agent_id, f"{ticker}%", cutoff)
            ).fetchone()
            conn.close()
            if recent_sell:
                return False, (
                    f"COOLDOWN: You sold {ticker} options within the last 10 minutes "
                    f"(at {recent_sell['timestamp'][11:19]}). Wait before re-buying. "
                    f"Trade a DIFFERENT ticker instead."
                )

        if asset_type == AssetType.OPTION:
            if price < 0.10:
                return False, f"Option price ${price:.2f} is too low — penny options under $0.10 are rejected"
            if price > 10.00:
                return False, f"Option price ${price:.2f} is too expensive — max $10.00 per contract for risk management"

            import re
            underlying_match = re.match(r'^([A-Z]+)\d', symbol)
            if underlying_match:
                underlying = underlying_match.group(1)
                same_underlying = [
                    p for p in summary.positions
                    if p.symbol.startswith(underlying) and p.symbol != symbol
                ]
                if len(same_underlying) >= 1:
                    existing_syms = [p.symbol for p in same_underlying]
                    return False, (
                        f"DIVERSIFY: You already hold {len(same_underlying)} option(s) on {underlying} "
                        f"({', '.join(existing_syms)}). Buy a DIFFERENT stock's options instead."
                    )

            options_value = sum(
                p.market_value for p in summary.positions if p.asset_type == AssetType.OPTION
            )
            if total_value > 0 and (options_value + total_cost) / total_value > max_options_pct:
                return False, f"Options allocation would exceed max ({max_options_pct*100:.0f}%)"

    elif action == TradeAction.SELL:
        held = None
        for p in summary.positions:
            if p.symbol == symbol:
                held = p
                break
        if not held:
            return False, f"No existing position in {symbol} to sell"
        if held.quantity < quantity:
            return False, f"Cannot sell {quantity} shares — only hold {held.quantity}"

    return True, "OK"


def execute_trade(
    agent_id: int,
    symbol: str,
    asset_type: AssetType,
    action: TradeAction,
    quantity: float,
    price: float,
    reasoning: str = "",
) -> tuple[bool, str]:
    """Execute a trade via Alpaca (or simulated if Alpaca is not configured).
    Returns (success, message)."""
    is_valid, reason = validate_trade(agent_id, symbol, asset_type, action, quantity, price)
    if not is_valid:
        return False, reason

    if broker.is_configured():
        return _execute_via_alpaca(agent_id, symbol, asset_type, action, quantity, price, reasoning)

    return _execute_simulated(agent_id, symbol, asset_type, action, quantity, price, reasoning)


def _execute_via_alpaca(
    agent_id: int,
    symbol: str,
    asset_type: AssetType,
    action: TradeAction,
    quantity: float,
    price: float,
    reasoning: str,
) -> tuple[bool, str]:
    """Submit a market order to Alpaca and update the agent's local ledger."""
    multiplier = 100 if asset_type == AssetType.OPTION else 1
    estimated_cost = price * quantity * multiplier

    if action == TradeAction.BUY:
        try:
            account = broker.get_account()
            if estimated_cost > account["buying_power"]:
                return False, f"Trade (${estimated_cost:.2f}) exceeds Alpaca buying power (${account['buying_power']:.2f})"
        except Exception as e:
            return False, f"Cannot verify Alpaca buying power: {e}"

    try:
        result = broker.submit_market_order(
            symbol=symbol,
            qty=quantity,
            side=action.value,
            agent_id=agent_id,
        )
    except Exception as e:
        return False, f"Alpaca order failed: {e}"

    if result["status"] != "filled":
        return False, f"Order not filled: {result.get('error', result['status'])}"

    filled_price = result["filled_avg_price"]
    filled_qty = result["qty"]
    total_cost = filled_price * filled_qty * multiplier
    cash = db.get_cash(agent_id)

    if action == TradeAction.BUY:
        db.update_cash(agent_id, cash - total_cost)

        existing = db.get_position(agent_id, symbol, asset_type.value)
        if existing:
            old_cost = existing["quantity"] * existing["avg_cost"]
            new_cost = old_cost + (filled_price * filled_qty)
            new_quantity = existing["quantity"] + filled_qty
            new_avg_cost = new_cost / new_quantity
            db.upsert_position(agent_id, symbol, asset_type.value, new_quantity, new_avg_cost)
        else:
            db.upsert_position(agent_id, symbol, asset_type.value, filled_qty, filled_price)

        db.insert_trade(
            agent_id, symbol, asset_type.value, action.value,
            filled_qty, filled_price, total_cost, reasoning,
            realized_pnl=0, avg_cost_basis=round(filled_price, 4),
        )
        return True, f"Bought {filled_qty} {symbol} at ${filled_price:.2f} (total: ${total_cost:.2f}) [Alpaca filled]"

    elif action == TradeAction.SELL:
        db.update_cash(agent_id, cash + total_cost)

        existing = db.get_position(agent_id, symbol, asset_type.value)
        avg_cost_basis = existing["avg_cost"] if existing else filled_price
        new_quantity = (existing["quantity"] - filled_qty) if existing else 0
        db.upsert_position(agent_id, symbol, asset_type.value, new_quantity, avg_cost_basis)

        realized_pnl = (filled_price - avg_cost_basis) * filled_qty * multiplier
        db.insert_trade(
            agent_id, symbol, asset_type.value, action.value,
            filled_qty, filled_price, total_cost, reasoning,
            realized_pnl=round(realized_pnl, 2), avg_cost_basis=round(avg_cost_basis, 4),
        )
        return True, f"Sold {filled_qty} {symbol} at ${filled_price:.2f} (P&L: ${realized_pnl:+.2f}) [Alpaca filled]"

    return False, "Unknown action"


def _execute_simulated(
    agent_id: int,
    symbol: str,
    asset_type: AssetType,
    action: TradeAction,
    quantity: float,
    price: float,
    reasoning: str,
) -> tuple[bool, str]:
    """Original simulated (paper) trade execution when Alpaca is not configured."""
    multiplier = 100 if asset_type == AssetType.OPTION else 1
    total_cost = price * quantity * multiplier
    cash = db.get_cash(agent_id)

    if action == TradeAction.BUY:
        db.update_cash(agent_id, cash - total_cost)

        existing = db.get_position(agent_id, symbol, asset_type.value)
        if existing:
            old_cost = existing["quantity"] * existing["avg_cost"]
            new_cost = old_cost + (price * quantity)
            new_quantity = existing["quantity"] + quantity
            new_avg_cost = new_cost / new_quantity
            db.upsert_position(agent_id, symbol, asset_type.value, new_quantity, new_avg_cost)
        else:
            db.upsert_position(agent_id, symbol, asset_type.value, quantity, price)

        db.insert_trade(agent_id, symbol, asset_type.value, "buy", quantity, price, total_cost, reasoning,
                        realized_pnl=0, avg_cost_basis=price)
        return True, f"Bought {quantity} {symbol} at ${price:.2f} (total: ${total_cost:.2f})"

    elif action == TradeAction.SELL:
        db.update_cash(agent_id, cash + total_cost)

        existing = db.get_position(agent_id, symbol, asset_type.value)
        new_quantity = existing["quantity"] - quantity
        db.upsert_position(agent_id, symbol, asset_type.value, new_quantity, existing["avg_cost"])

        avg_cost = existing["avg_cost"]
        realized_pnl = (price - avg_cost) * quantity * multiplier
        db.insert_trade(agent_id, symbol, asset_type.value, "sell", quantity, price, total_cost, reasoning,
                        realized_pnl=round(realized_pnl, 2), avg_cost_basis=round(avg_cost, 4))
        return True, f"Sold {quantity} {symbol} at ${price:.2f} (P&L: ${realized_pnl:+.2f})"

    return False, "Unknown action"


def take_daily_snapshot(agent_id: int):
    """Record end-of-day portfolio state for an agent."""
    summary = get_portfolio_summary(agent_id)
    starting_capital = db.get_starting_capital(agent_id)
    total_return_pct = ((summary.total_value - starting_capital) / starting_capital * 100) if starting_capital > 0 else 0.0

    prev = db.get_previous_day_snapshot(agent_id)
    if prev:
        daily_return_pct = ((summary.total_value - prev["total_value"]) / prev["total_value"] * 100) if prev["total_value"] > 0 else 0.0
    else:
        daily_return_pct = total_return_pct

    sp500_return = get_sp500_return_since(agent_id)

    today = date.today().isoformat()
    db.insert_daily_snapshot(
        agent_id=agent_id,
        date=today,
        total_value=summary.total_value,
        cash=summary.cash,
        invested=summary.invested_value,
        daily_return_pct=round(daily_return_pct, 4),
        total_return_pct=round(total_return_pct, 4),
        sp500_total_return_pct=round(sp500_return, 4),
        num_positions=summary.num_positions,
    )


def take_intraday_snapshot(agent_id: int, session_phase: str = ""):
    """Record an intraday portfolio snapshot after each trading session."""
    summary = get_portfolio_summary(agent_id)
    db.insert_intraday_snapshot(
        agent_id=agent_id,
        total_value=summary.total_value,
        cash=summary.cash,
        invested=summary.invested_value,
        num_positions=summary.num_positions,
        session_phase=session_phase,
    )
    take_daily_snapshot(agent_id)
