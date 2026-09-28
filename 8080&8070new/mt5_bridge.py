"""
===============================================================================
🏛️ GOLDFLOW // INSTITUTIONAL METATRADER 5 (MT5) EXECUTION BRIDGE
===============================================================================
Features:
- Connects directly to running MetaTrader 5 terminal (Account 1189847 / Equiti)
- Auto-detects supported filling mode (FOK / IOC / RETURN) for zero reject orders
- Precise Lot sizing, Stop Loss, and Take Profit execution
- Magic Number tagging per port (e.g. 807001 for Port 8070, 809501 for Port 8095)
- Position query and auto-close synchronization
===============================================================================
"""

import MetaTrader5 as mt5
import time
import logging
import threading

logger = logging.getLogger("MT5_BRIDGE")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - [MT5_BRIDGE] - %(message)s")

_mt5_lock = threading.Lock()
_is_initialized = False

def _detect_gold_symbol():
    for c in ["XAUUSD.sd", "XAUUSD", "XAUUSD.pr", "GOLD"]:
        try:
            s = mt5.symbol_info(c)
            if s is not None and s.visible:
                return c
            elif s is not None:
                mt5.symbol_select(c, True)
                return c
        except Exception:
            pass
    return "XAUUSD.sd"

DEFAULT_SYMBOL = "XAUUSD.sd"
DEFAULT_LOTS = 0.01
MAX_SLIPPAGE_POINTS = 30  # 30 points = $0.30 slippage guard


def init_mt5():
    """Initialize connection to MetaTrader 5 and ensure gold symbol is selected."""
    global _is_initialized, DEFAULT_SYMBOL
    with _mt5_lock:
        if _is_initialized:
            return True

        if not mt5.initialize():
            logger.error(f"MT5 initialize failed: {mt5.last_error()}")
            return False

        acc = mt5.account_info()
        if acc is None:
            logger.error(f"Failed to get MT5 account info: {mt5.last_error()}")
            return False

        logger.info(f"✅ MT5 Connected Successfully | Account: {acc.login} | Server: {acc.server} | Balance: ${acc.balance:.2f}")

        # Auto-detect broker gold symbol
        DEFAULT_SYMBOL = _detect_gold_symbol()
        if not mt5.symbol_select(DEFAULT_SYMBOL, True):
            logger.warning(f"Could not select {DEFAULT_SYMBOL} in Market Watch: {mt5.last_error()}")
        else:
            logger.info(f"✅ Selected Gold Symbol: {DEFAULT_SYMBOL}")

        _is_initialized = True
        return True


def get_account_status():
    """Return live MT5 account statistics."""
    if not init_mt5():
        return None
    with _mt5_lock:
        acc = mt5.account_info()
        if acc:
            return {
                "login": acc.login,
                "server": acc.server,
                "balance": round(acc.balance, 2),
                "equity": round(acc.equity, 2),
                "margin": round(acc.margin, 2),
                "free_margin": round(acc.margin_free, 2),
                "profit": round(acc.profit, 2),
                "currency": acc.currency
            }
        return None


# Filling mode bitmask constants (safe cross-version integers)
_FILL_FOK    = 1   # Fill-or-Kill
_FILL_IOC    = 2   # Immediate-or-Cancel
_FILL_RETURN = 4   # Return remaining volume

def get_filling_mode(symbol=DEFAULT_SYMBOL):
    """Detect the filling mode accepted by the broker for the symbol.
    Uses raw integer bitmask values to avoid AttributeError on older MT5 builds.
    """
    try:
        sym_info = mt5.symbol_info(symbol)
        if sym_info is None:
            return mt5.ORDER_FILLING_IOC
        modes = sym_info.filling_mode
        if modes & _FILL_FOK:
            return mt5.ORDER_FILLING_FOK
        elif modes & _FILL_IOC:
            return mt5.ORDER_FILLING_IOC
        else:
            return mt5.ORDER_FILLING_RETURN
    except Exception:
        # Absolute fallback: IOC is universally accepted by most brokers
        return mt5.ORDER_FILLING_IOC


def send_order(direction, lots=DEFAULT_LOTS, sl_points=2.0, tp_points=4.0,
               sl_price=None, tp_price=None,
               symbol=DEFAULT_SYMBOL, magic=807001, comment="GOLDFLOW"):
    """
    Execute a market order on MT5.
    direction: 'BUY' or 'SELL'
    sl_points: Distance in USD points from entry (used if sl_price is None)
    tp_points: Distance in USD points from entry (used if tp_price is None)
    sl_price: Exact target SL price
    tp_price: Exact target TP price
    """
    # Log order submission with port identification
    logger.info(f"📤 [MT5_BRIDGE] Submitting {direction.upper()} order for Magic {magic} (Port {magic // 100}) | SL: {sl_price} | TP: {tp_price}")

    if not init_mt5():
        return {"success": False, "error": "MT5 Not Connected"}

    if not symbol or symbol == "XAUUSD.pr":
        symbol = DEFAULT_SYMBOL

    with _mt5_lock:
        sym_info = mt5.symbol_info(symbol)
        if sym_info is None:
            return {"success": False, "error": f"Symbol {symbol} not found"}

        if not sym_info.visible:
            mt5.symbol_select(symbol, True)
            sym_info = mt5.symbol_info(symbol)

        is_buy = (direction.upper() == "BUY")
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        price = sym_info.ask if is_buy else sym_info.bid

        # Calculate exact SL and TP prices
        if sl_price is None:
            sl_price = round(price - sl_points, 2) if is_buy else round(price + sl_points, 2)
        else:
            sl_price = round(float(sl_price), 2)

        if tp_price is None:
            tp_price = round(price + tp_points, 2) if is_buy else round(price - tp_points, 2)
        else:
            tp_price = round(float(tp_price), 2)

        filling = get_filling_mode(symbol)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(lots),
            "type": order_type,
            "price": price,
            "sl": sl_price,
            "tp": tp_price,
            "deviation": MAX_SLIPPAGE_POINTS,
            "magic": int(magic),
            "comment": comment[:31],  # MT5 max comment length
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

        logger.info(f"🚀 Sending MT5 Order: {direction} {lots} {symbol} @ {price:.2f} | SL: {sl_price:.2f} | TP: {tp_price:.2f} | Magic: {magic}")
        result = mt5.order_send(request)

        if result is None:
            err = mt5.last_error()
            logger.error(f"❌ MT5 Order Send failed, result is None: {err}")
            return {"success": False, "error": str(err)}

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"❌ MT5 Order Rejected: retcode={result.retcode} | {result.comment}")
            return {
                "success": False,
                "retcode": result.retcode,
                "error": result.comment
            }

        logger.info(f"✅ MT5 Order Executed! Ticket: #{result.order} | Fill Price: ${result.price:.2f} | Deal: #{result.deal}")
        return {
            "success": True,
            "ticket": result.order,
            "deal": result.deal,
            "price": result.price,
            "volume": result.volume,
            "sl": sl_price,
            "tp": tp_price,
            "direction": direction.upper(),
            "symbol": symbol,
            "comment": comment
        }


def close_position_by_ticket(ticket):
    """Close an open position by its ticket number."""
    if not init_mt5():
        return False

    with _mt5_lock:
        positions = mt5.positions_get(ticket=int(ticket))
        if not positions or len(positions) == 0:
            logger.warning(f"Position ticket #{ticket} not found or already closed.")
            return True

        pos = positions[0]
        symbol = pos.symbol
        is_buy = (pos.type == mt5.ORDER_TYPE_BUY)
        close_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
        sym_info = mt5.symbol_info(symbol)
        price = sym_info.bid if is_buy else sym_info.ask
        filling = get_filling_mode(symbol)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": pos.volume,
            "type": close_type,
            "position": pos.ticket,
            "price": price,
            "deviation": MAX_SLIPPAGE_POINTS,
            "magic": pos.magic,
            "comment": "GOLDFLOW CLOSE",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"✅ Closed MT5 Position #{ticket} @ ${result.price:.2f}")
            return True
        else:
            logger.error(f"❌ Failed to close MT5 Position #{ticket}: {result.comment if result else mt5.last_error()}")
            return False


def modify_position_sl_tp(ticket, new_sl, new_tp=None):
    """Modify SL (and optionally TP) for an open MT5 position (e.g. for trailing SL)."""
    if not init_mt5():
        return False
    with _mt5_lock:
        positions = mt5.positions_get(ticket=int(ticket))
        if not positions or len(positions) == 0:
            return False
        pos = positions[0]
        tp_val = float(new_tp) if new_tp is not None else float(pos.tp)
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": int(pos.ticket),
            "symbol": pos.symbol,
            "sl": float(round(new_sl, 2)),
            "tp": tp_val,
        }
        res = mt5.order_send(request)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"✅ MT5 Trailing SL Modified #{ticket} -> ${new_sl:.2f}")
            return True
        else:
            err_msg = res.comment if res else mt5.last_error()
            logger.warning(f"⚠️ Could not modify MT5 SL #{ticket}: {err_msg}")
            return False


def is_position_open(ticket):
    """Check if position is still open in MT5."""
    if not init_mt5():
        return False
    with _mt5_lock:
        positions = mt5.positions_get(ticket=int(ticket))
        return bool(positions and len(positions) > 0)


def get_open_positions_by_magic(magic=None):
    """Retrieve all open positions filtered optionally by magic number."""
    if not init_mt5():
        return []

    with _mt5_lock:
        if magic:
            positions = mt5.positions_get(symbol=DEFAULT_SYMBOL)
            if positions:
                return [p for p in positions if p.magic == int(magic)]
            return []
        else:
            positions = mt5.positions_get(symbol=DEFAULT_SYMBOL)
            return list(positions) if positions else []


def shutdown_mt5():
    """Cleanly close MT5 connection."""
    global _is_initialized
    with _mt5_lock:
        if _is_initialized:
            mt5.shutdown()
            _is_initialized = False
            logger.info("MT5 connection shutdown.")
