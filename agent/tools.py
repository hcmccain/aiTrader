import json
from portfolio.manager import get_portfolio_summary, execute_trade
from portfolio.models import AssetType, TradeAction
from portfolio.database import get_trades
from data.market import get_market_data, get_options_chain, search_symbols, get_current_price, get_top_movers, get_market_news


TOOL_DEFINITIONS = [
    {
        "name": "get_portfolio_summary",
        "description": "Get the current portfolio state including all positions, cash balance, total value, and performance metrics.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_top_movers",
        "description": "Scans LIVE Yahoo Finance screeners (top gainers, top losers, most active, trending) plus 50+ volatile stocks to find today's biggest movers. Returns stocks sorted by % change with price, volume, direction, day range. USE THIS FIRST to find real market opportunities.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_market_news",
        "description": "Get the latest market news headlines from Yahoo Finance. Shows what's driving the market today — earnings, FDA approvals, analyst upgrades, sector moves, macro events. Use this to understand WHY stocks are moving and find opportunities.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_market_data",
        "description": "Get price history, technical indicators (SMA 20/50), fundamentals (PE, market cap, dividend yield), and recent performance for a stock, ETF, mutual fund, or commodity symbol.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Ticker symbol (e.g. AAPL, SPY, GLD, VFIAX)",
                },
                "period": {
                    "type": "string",
                    "description": "History period: 1d, 5d, 1mo, 3mo, 6mo, 1y, 2y",
                    "default": "3mo",
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_options_chain",
        "description": "Get available options contracts (calls and puts) for a symbol, including strike prices, premiums, volume, and implied volatility.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Ticker symbol to get options for",
                },
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "search_symbols",
        "description": "Search for tradeable symbols by sector name (technology, finance, healthcare, energy, consumer, industrial, commodities, etf, bonds) or by ticker symbol.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Sector name or ticker symbol to search for",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "place_trade",
        "description": "Execute a paper trade (buy or sell). The trade will be validated against risk management rules before execution.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Ticker symbol or options contract symbol",
                },
                "asset_type": {
                    "type": "string",
                    "enum": ["stock", "etf", "mutual_fund", "commodity", "option"],
                    "description": "Type of asset being traded. Use 'etf' for ETFs (SPY, QQQ, XLE, TQQQ, etc.), 'stock' for individual equities, 'commodity' for commodity ETFs (GLD, SLV, USO), 'option' for options contracts, 'mutual_fund' for mutual funds.",
                },
                "action": {
                    "type": "string",
                    "enum": ["buy", "sell"],
                    "description": "Buy or sell",
                },
                "quantity": {
                    "type": "number",
                    "description": "Number of shares/contracts to trade",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Brief explanation for why you are making this trade",
                },
            },
            "required": ["symbol", "asset_type", "action", "quantity", "reasoning"],
        },
    },
    {
        "name": "get_trade_history",
        "description": "Get recent trade history to review past decisions and their outcomes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Number of recent trades to return (default 20)",
                    "default": 20,
                },
            },
            "required": [],
        },
    },
]


def handle_tool_call(tool_name: str, tool_input: dict, agent_id: int) -> str:
    """Execute a tool call scoped to a specific agent. Returns JSON string."""
    try:
        if tool_name == "get_portfolio_summary":
            summary = get_portfolio_summary(agent_id)
            return summary.model_dump_json(indent=2)

        elif tool_name == "get_top_movers":
            data = get_top_movers(limit=15)
            return json.dumps(data, indent=2)

        elif tool_name == "get_market_news":
            data = get_market_news()
            return json.dumps(data, indent=2)

        elif tool_name == "get_market_data":
            data = get_market_data(
                symbol=tool_input["symbol"],
                period=tool_input.get("period", "3mo"),
            )
            if data is None:
                return json.dumps({"error": f"No data found for {tool_input['symbol']}"})
            return json.dumps(data, indent=2)

        elif tool_name == "get_options_chain":
            data = get_options_chain(tool_input["symbol"])
            if data is None:
                return json.dumps({"error": f"No options available for {tool_input['symbol']}"})
            return json.dumps(data, indent=2)

        elif tool_name == "search_symbols":
            results = search_symbols(tool_input["query"])
            return json.dumps(results, indent=2)

        elif tool_name == "place_trade":
            symbol = tool_input["symbol"]
            asset_type = AssetType(tool_input["asset_type"])
            action = TradeAction(tool_input["action"])
            quantity = float(tool_input["quantity"])
            reasoning = tool_input.get("reasoning", "")

            price = get_current_price(symbol)
            if price is None:
                return json.dumps({"success": False, "message": f"Cannot get current price for {symbol}"})

            success, message = execute_trade(
                agent_id=agent_id,
                symbol=symbol,
                asset_type=asset_type,
                action=action,
                quantity=quantity,
                price=price,
                reasoning=reasoning,
            )
            return json.dumps({"success": success, "message": message, "price": price})

        elif tool_name == "get_trade_history":
            trades = get_trades(agent_id, limit=tool_input.get("limit", 20))
            return json.dumps(trades, indent=2)

        else:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

    except Exception as e:
        return json.dumps({"error": str(e)})
