import json
import logging
import re
from datetime import datetime

from portfolio.database import (
    DEFAULT_MODEL, DEFAULT_SCOUT_MODEL, get_agent, insert_agent_log,
    insert_token_usage, update_agent_strategy, get_trades,
)
from agent.prompts import build_system_prompt, build_scout_prompt, build_strategy_prompt, build_review_prompt
from agent.tools import TOOL_DEFINITIONS, handle_tool_call
from agent.providers import get_provider, create_client, convert_tools, call_model, append_assistant, append_tool_results
from portfolio.manager import take_intraday_snapshot
from data.market import get_position_changes, get_sessions_remaining_today

logger = logging.getLogger(__name__)


def _build_user_message(agent_name: str, session_phase: str, position_changes: list[dict],
                        cash: float = 0, num_positions: int = 0, is_options_only: bool = False,
                        check_interval: int = 15) -> str:
    """Build the user message with time-of-day context and position changes."""
    now_str = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    sessions_left = get_sessions_remaining_today(interval_minutes=check_interval)

    msg = (
        f"It is {now_str}. "
        f"Session phase: {session_phase.upper().replace('_', ' ')}. "
        f"Sessions remaining today: {sessions_left}. "
        f"You are managing the '{agent_name}' portfolio.\n\n"
    )

    if is_options_only and cash > 200:
        msg += (
            f"**You have ${cash:,.0f} in cash and {num_positions} open position(s).** "
        )
        if cash > 1000 and num_positions < 3:
            msg += (
                f"You should be deploying more — aim for 4-6 positions. "
                f"Size each trade $200-$1,500 based on how strong the mover is. "
                f"Quick example: a $1.00 option x 8 contracts = $800 position.\n\n"
            )
        else:
            msg += "Check your positions — take profits on winners, cut losers, rotate into movers.\n\n"

    if position_changes:
        msg += "**Current positions since last check:**\n"
        for pc in position_changes:
            direction = "+" if pc["pnl"] >= 0 else ""
            msg += (
                f"- {pc['symbol']} ({pc['asset_type']}): {pc['qty']} units @ ${pc['avg_cost']} → "
                f"${pc['current']} ({direction}{pc['pnl_pct']:.1f}%, {direction}${pc['pnl']:.2f})\n"
            )
        msg += "\n"

    if session_phase == "pre_market":
        msg += "The market opens in minutes. Plan your opening trades. Start by reviewing your portfolio."
    elif session_phase == "morning":
        msg += "The market is open. Execute your best trades. Start by reviewing your portfolio."
    elif session_phase == "midday":
        msg += "Review your morning trades. Take profits where appropriate and look for new opportunities."
    elif session_phase == "afternoon":
        msg += "Afternoon session. Evaluate your positions and decide what to hold or sell before the close."
    elif session_phase == "closing":
        msg += "The market closes soon. Make final decisions — close day trades or hold overnight. Be decisive."
    else:
        msg += "Please analyze the current portfolio and market conditions, then make any trades you think are appropriate."

    return msg


def _get_portfolio_data(agent_id: int) -> dict:
    """Get portfolio summary as a plain dict for prompt injection."""
    from portfolio.manager import get_portfolio_summary
    summary = get_portfolio_summary(agent_id)
    return {
        "cash": summary.cash,
        "total_value": summary.total_value,
        "invested_value": summary.invested_value,
        "total_return": summary.total_return,
        "total_return_pct": summary.total_return_pct,
        "daily_return": summary.daily_return,
        "daily_return_pct": summary.daily_return_pct,
        "num_positions": summary.num_positions,
        "positions": [
            {
                "symbol": p.symbol,
                "asset_type": p.asset_type.value,
                "quantity": p.quantity,
                "avg_cost": p.avg_cost,
                "current_price": p.current_price,
                "unrealized_pnl": p.unrealized_pnl,
                "unrealized_pnl_pct": p.unrealized_pnl_pct,
            }
            for p in summary.positions
        ],
    }


def _parse_proposals(text: str) -> list[dict]:
    """Extract trade proposals from the scout's output."""
    match = re.search(r"```proposals\s*\n(.*?)```", text, re.DOTALL)
    if not match:
        return []
    try:
        proposals = json.loads(match.group(1).strip())
        if isinstance(proposals, list):
            return proposals
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _parse_decisions(text: str) -> list[dict]:
    """Extract review decisions from the decision model's output."""
    match = re.search(r"```decisions\s*\n(.*?)```", text, re.DOTALL)
    if not match:
        return []
    try:
        decisions = json.loads(match.group(1).strip())
        if isinstance(decisions, list):
            return decisions
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _maybe_refresh_strategy(agent: dict, decision_model: str, session_phase: str,
                            conversation_log: list) -> str:
    """Check if strategy needs refresh; if so, call the deep model to generate one."""
    agent_id = agent["id"]
    strategy_interval = agent.get("strategy_interval_minutes", 60) or 60
    current_strategy = agent.get("current_strategy", "") or ""
    strategy_updated_at = agent.get("strategy_updated_at", "") or ""

    needs_refresh = False
    if not current_strategy or not strategy_updated_at:
        needs_refresh = True
    else:
        try:
            last_update = datetime.fromisoformat(strategy_updated_at)
            elapsed = (datetime.now() - last_update).total_seconds()
            if elapsed >= strategy_interval * 60:
                needs_refresh = True
        except (ValueError, TypeError):
            needs_refresh = True

    if not needs_refresh:
        logger.info(f"[{agent['name']}] Strategy still fresh, skipping refresh")
        return current_strategy

    logger.info(f"[{agent['name']}] Refreshing strategy with {decision_model}")

    portfolio_data = _get_portfolio_data(agent_id)
    recent_trades = get_trades(agent_id, limit=20)

    provider = get_provider(decision_model)
    try:
        client = create_client(provider)
    except ValueError as e:
        logger.error(f"Cannot create client for strategy: {e}")
        return current_strategy

    strategy_system = build_strategy_prompt(agent_id, portfolio_data, recent_trades)
    messages = [{"role": "user", "content": f"Generate the strategy directive for the {session_phase} session."}]

    try:
        response = call_model(
            client, provider, decision_model,
            strategy_system, [], messages,
        )
    except Exception as e:
        logger.error(f"Strategy model call failed: {e}")
        return current_strategy

    try:
        insert_token_usage(agent_id, response.input_tokens, response.output_tokens, model=decision_model)
    except Exception:
        pass

    strategy_text = "\n".join(response.text_parts) if response.text_parts else current_strategy
    conversation_log.append({"role": "strategy", "content": strategy_text})

    if strategy_text:
        update_agent_strategy(agent_id, strategy_text)
        logger.info(f"[{agent['name']}] Strategy refreshed ({len(strategy_text)} chars)")

    return strategy_text


def _run_scout_phase(agent: dict, scout_model: str, strategy: str,
                     session_phase: str, conversation_log: list) -> list[dict]:
    """Scout phase: fast model gathers data and produces trade proposals."""
    agent_id = agent["id"]
    agent_name = agent["name"]
    check_interval = agent.get("check_interval_minutes", 15) or 15

    provider = get_provider(scout_model)
    try:
        client = create_client(provider)
    except ValueError as e:
        logger.error(str(e))
        return []

    scout_tools = [t for t in TOOL_DEFINITIONS if t["name"] != "place_trade"]
    provider_tools = convert_tools(scout_tools, provider)
    system_prompt = build_scout_prompt(agent_id, strategy, session_phase=session_phase)

    from portfolio.manager import get_portfolio_summary
    from portfolio.database import get_risk_params

    position_changes = get_position_changes(agent_id)
    summary = get_portfolio_summary(agent_id)
    risk = get_risk_params(agent_id)
    allowed = risk.get("allowed_asset_types", [])
    is_options_only = allowed == ["option"]

    user_message = _build_user_message(
        agent_name, session_phase, position_changes,
        cash=summary.cash, num_positions=summary.num_positions,
        is_options_only=is_options_only,
        check_interval=check_interval,
    )

    messages = [{"role": "user", "content": user_message}]
    conversation_log.append({"role": "user", "content": user_message})

    max_iterations = 12
    all_text = []

    for iteration in range(max_iterations):
        logger.info(f"[{agent_name}] Scout iteration {iteration + 1}/{max_iterations}")

        try:
            response = call_model(
                client, provider, scout_model,
                system_prompt, provider_tools, messages,
            )
        except Exception as e:
            logger.error(f"Scout API error: {e}")
            conversation_log.append({"role": "error", "content": str(e)})
            break

        try:
            insert_token_usage(agent_id, response.input_tokens, response.output_tokens, model=scout_model)
        except Exception:
            pass

        append_assistant(messages, provider, response)

        for text in response.text_parts:
            conversation_log.append({"role": "scout", "content": text})
            all_text.append(text)

        tool_results = []
        for tc in response.tool_calls:
            logger.info(f"[{agent_name}] Scout tool: {tc.name}({json.dumps(tc.input)[:200]})")
            conversation_log.append({"role": "tool_call", "tool": tc.name, "input": tc.input})

            result = handle_tool_call(tc.name, tc.input, agent_id)
            conversation_log.append({"role": "tool_result", "tool": tc.name, "result": result[:2000]})

            tool_results.append({"id": tc.id, "name": tc.name, "content": result})

        if response.is_done:
            break

        if tool_results:
            append_tool_results(messages, provider, tool_results)
        else:
            break

    combined_text = "\n".join(all_text)
    proposals = _parse_proposals(combined_text)

    if not proposals:
        logger.info(f"[{agent_name}] Scout produced no proposals")
    else:
        logger.info(f"[{agent_name}] Scout proposed {len(proposals)} trade(s)")

    return proposals


def _run_review_phase(agent: dict, decision_model: str, proposals: list[dict],
                      strategy: str, conversation_log: list) -> list[dict]:
    """Review phase: deep model approves/modifies/rejects each proposal."""
    agent_id = agent["id"]
    agent_name = agent["name"]

    portfolio_data = _get_portfolio_data(agent_id)

    provider = get_provider(decision_model)
    try:
        client = create_client(provider)
    except ValueError as e:
        logger.error(str(e))
        return []

    review_system = build_review_prompt(agent_id, proposals, strategy, portfolio_data)
    messages = [{"role": "user", "content": "Review the proposed trades and provide your decisions."}]

    try:
        response = call_model(
            client, provider, decision_model,
            review_system, [], messages,
        )
    except Exception as e:
        logger.error(f"Review model call failed: {e}")
        return proposals

    try:
        insert_token_usage(agent_id, response.input_tokens, response.output_tokens, model=decision_model)
    except Exception:
        pass

    review_text = "\n".join(response.text_parts) if response.text_parts else ""
    conversation_log.append({"role": "review", "content": review_text})

    decisions = _parse_decisions(review_text)

    if not decisions:
        logger.warning(f"[{agent_name}] Could not parse review decisions, approving all proposals as-is")
        return proposals

    approved = []
    for d in decisions:
        decision = d.get("decision", "APPROVE").upper()
        if decision == "REJECT":
            logger.info(f"[{agent_name}] REJECTED: {d.get('action', '?')} {d.get('symbol', '?')} — {d.get('reasoning', '')[:100]}")
            continue
        approved.append(d)
        status = "APPROVED" if decision == "APPROVE" else "MODIFIED"
        logger.info(f"[{agent_name}] {status}: {d.get('action', '?')} {d.get('quantity', '?')} {d.get('symbol', '?')}")

    return approved


def run_trading_session(agent_id: int, run_type: str = "scheduled", session_phase: str = "morning") -> dict:
    """Run a two-model trading session: strategy → scout → review → execute."""
    agent = get_agent(agent_id)
    if not agent:
        return {"error": f"Agent {agent_id} not found"}

    agent_name = agent["name"]
    decision_model = agent.get("model") or DEFAULT_MODEL
    scout_model = agent.get("scout_model") or DEFAULT_SCOUT_MODEL

    logger.info(
        f"Starting {run_type}/{session_phase} session for '{agent_name}' "
        f"(id={agent_id}, scout={scout_model}, decision={decision_model})"
    )

    conversation_log = []
    trades_made = 0

    # Auto stop-loss before any model runs
    from portfolio.manager import get_portfolio_summary, execute_trade
    from portfolio.models import AssetType, TradeAction
    from portfolio.database import get_risk_params
    from data.market import get_current_price

    summary = get_portfolio_summary(agent_id)
    risk = get_risk_params(agent_id)

    for p in summary.positions:
        if p.quantity <= 0:
            continue
        cost = p.avg_cost * p.quantity * (100 if p.asset_type == AssetType.OPTION else 1)
        if cost > 0 and p.unrealized_pnl / cost < -0.03:
            price = get_current_price(p.symbol) or p.current_price
            ok, msg = execute_trade(
                agent_id, p.symbol, p.asset_type, TradeAction.SELL,
                p.quantity, price, "Auto stop-loss: position down more than 3%"
            )
            if ok:
                logger.info(f"[{agent_name}] AUTO STOP-LOSS: Sold {p.symbol} (down {p.unrealized_pnl/cost*100:.1f}%)")
                trades_made += 1

    # Phase 0: Strategy (deep model, only if stale)
    strategy = _maybe_refresh_strategy(agent, decision_model, session_phase, conversation_log)

    # Phase 1: Scout (fast model with tools)
    proposals = _run_scout_phase(agent, scout_model, strategy, session_phase, conversation_log)

    if not proposals:
        take_intraday_snapshot(agent_id)
        _fire_snapshot_event(agent_id, agent_name)

        summary_text = "No trade proposals from scout this session."
        insert_agent_log(
            agent_id=agent_id,
            run_type=f"{run_type}/{session_phase}",
            summary=summary_text,
            full_log=json.dumps(conversation_log, default=str)[:50000],
            trades_made=trades_made,
        )
        return {
            "agent_id": agent_id,
            "agent_name": agent_name,
            "trades_made": trades_made,
            "summary": summary_text,
            "proposals": 0,
            "session_phase": session_phase,
        }

    # Phase 2: Review (deep model, single call)
    approved = _run_review_phase(agent, decision_model, proposals, strategy, conversation_log)

    # Phase 3: Execute approved trades
    for trade in approved:
        symbol = trade.get("symbol", "")
        asset_type_str = trade.get("asset_type", "stock")
        action_str = trade.get("action", "buy")
        quantity = float(trade.get("quantity", 0))
        reasoning = trade.get("reasoning", "")

        if not symbol or quantity <= 0:
            continue

        try:
            asset_type = AssetType(asset_type_str)
            action = TradeAction(action_str)
        except ValueError:
            logger.warning(f"[{agent_name}] Invalid asset_type/action: {asset_type_str}/{action_str}")
            continue

        price = get_current_price(symbol)
        if price is None:
            logger.warning(f"[{agent_name}] Cannot get price for {symbol}, skipping")
            continue

        ok, msg = execute_trade(
            agent_id=agent_id,
            symbol=symbol,
            asset_type=asset_type,
            action=action,
            quantity=quantity,
            price=price,
            reasoning=reasoning,
        )
        if ok:
            trades_made += 1
            _fire_trade_event(agent_id, agent_name, {
                "success": True,
                "message": msg,
                "symbol": symbol,
                "action": action_str,
                "quantity": quantity,
                "price": price,
            })
            logger.info(f"[{agent_name}] Executed: {action_str} {quantity} {symbol} @ ${price:.2f}")
        else:
            logger.warning(f"[{agent_name}] Trade rejected: {msg}")

    take_intraday_snapshot(agent_id)
    _fire_snapshot_event(agent_id, agent_name)

    summary_parts = [p for p in conversation_log if p.get("role") in ("scout", "review")]
    summary_text = summary_parts[-1]["content"][:1000] if summary_parts else "No summary available"

    insert_agent_log(
        agent_id=agent_id,
        run_type=f"{run_type}/{session_phase}",
        summary=summary_text,
        full_log=json.dumps(conversation_log, default=str)[:50000],
        trades_made=trades_made,
    )

    logger.info(f"[{agent_name}] Session complete ({session_phase}). Proposals: {len(proposals)}, Approved: {len(approved)}, Trades: {trades_made}")

    return {
        "agent_id": agent_id,
        "agent_name": agent_name,
        "trades_made": trades_made,
        "summary": summary_text,
        "proposals": len(proposals),
        "approved": len(approved),
        "session_phase": session_phase,
    }


def run_daily_trading_session(agent_id: int, run_type: str = "scheduled") -> dict:
    return run_trading_session(agent_id, run_type=run_type, session_phase="morning")


def _fire_trade_event(agent_id: int, agent_name: str, trade_data: dict):
    """Push a trade event to the SSE event bus."""
    try:
        from web.events import publish_event
        publish_event({
            "type": "trade_executed",
            "agent_id": agent_id,
            "agent_name": agent_name,
            "trade": trade_data,
        })
    except Exception:
        pass


def _fire_snapshot_event(agent_id: int, agent_name: str):
    """Push a snapshot event to the SSE event bus."""
    try:
        from web.events import publish_event
        publish_event({
            "type": "snapshot_updated",
            "agent_id": agent_id,
            "agent_name": agent_name,
        })
    except Exception:
        pass
