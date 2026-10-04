"""
mt5_exec.py - reusable, DEMO-GUARDED MT5 execution helper.
============================================================================
Same proven order logic as the 9060/9080 dashboards, packaged so Port 9900
(and anything else) can place demo orders too. Every order/modify/close first
checks that the connected MT5 account is a DEMO account (trade_mode == DEMO);
if it is ever a REAL account, the action is BLOCKED. So even if the MT5
terminal is switched to a live account, this will not place real-money trades.

Attaches to an MT5 terminal the user already opened + logged in; no
credentials pass through here. Symbol/lot/enable come from env by default.
"""
import os

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False


class MT5Exec:
    def __init__(self, logger, symbol=None, lot=None, enabled=None, name="mt5"):
        self.logger = logger
        self.symbol = symbol or os.getenv("GOLDFLOW_MT5_SYMBOL", "XAUUSD.sd")
        self.lot = lot if lot is not None else float(os.getenv("GOLDFLOW_MT5_LOT", "0.01"))
        self.enabled = enabled if enabled is not None else (os.getenv("GOLDFLOW_MT5_TRADING_ENABLED", "0") == "1")
        self.name = name
        self.ready = False

    def _is_demo(self):
        acc = mt5.account_info()
        return acc is not None and acc.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO

    def init(self):
        if not self.enabled:
            self.logger.info(f"[MT5-{self.name}] trading DISABLED (master switch off) - paper only.")
            return
        if not MT5_AVAILABLE:
            self.logger.warning(f"[MT5-{self.name}] MetaTrader5 package not installed - disabled.")
            return
        if not mt5.initialize():
            self.logger.warning(f"[MT5-{self.name}] terminal not reachable ({mt5.last_error()}). "
                                f"Open MT5 + log into the DEMO account, then restart.")
            return
        acc = mt5.account_info()
        if acc is None:
            self.logger.warning(f"[MT5-{self.name}] terminal up but no account logged in - disabled.")
            return
        if acc.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
            self.logger.warning(f"[MT5-{self.name}] account #{acc.login} is NOT demo "
                                f"(trade_mode={acc.trade_mode}) - DISABLED (demo-only safety).")
            return
        if mt5.symbol_info(self.symbol) is None:
            self.logger.warning(f"[MT5-{self.name}] symbol '{self.symbol}' not found in Market Watch - disabled.")
            return
        mt5.symbol_select(self.symbol, True)
        self.ready = True
        self.logger.info(f"[MT5-{self.name}] CONNECTED (DEMO): #{acc.login} '{acc.server}' "
                         f"(${acc.balance:.2f}) - ARMED {self.symbol}, {self.lot} lot.")

    def place(self, direction, sl, tp, magic, comment):
        if not self.enabled or not self.ready:
            return None
        try:
            if not self._is_demo():
                self.logger.error(f"[MT5-{self.name}] account NOT demo - order BLOCKED (demo-only safety).")
                return None
            tick = mt5.symbol_info_tick(self.symbol)
            if not tick:
                self.logger.error(f"[MT5-{self.name}] no live tick, order skipped.")
                return None
            otype = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
            price = tick.ask if direction == "BUY" else tick.bid
            result = None
            for filling in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
                req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": self.symbol, "volume": self.lot,
                       "type": otype, "price": price, "sl": float(sl) if sl else 0.0,
                       "tp": float(tp) if tp else 0.0, "deviation": 20, "magic": int(magic),
                       "comment": comment, "type_time": mt5.ORDER_TIME_GTC, "type_filling": filling}
                result = mt5.order_send(req)
                if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
                    self.logger.info(f"[MT5-{self.name} DEMO] {direction} {self.lot} {self.symbol} @ {price} "
                                     f"SL={sl} TP={tp} magic={magic} ticket=#{result.order} ({comment})")
                    return result.order
            self.logger.error(f"[MT5-{self.name}] order failed retcode={getattr(result,'retcode','?')} "
                              f"{getattr(result,'comment','?')}")
            return None
        except Exception:
            self.logger.exception(f"[MT5-{self.name}] order error")
            return None

    def modify(self, ticket, sl, tp):
        if not self.enabled or not self.ready or not ticket:
            return False
        try:
            if not self._is_demo():
                self.logger.error(f"[MT5-{self.name}] not demo - modify BLOCKED.")
                return False
            req = {"action": mt5.TRADE_ACTION_SLTP, "position": int(ticket), "symbol": self.symbol,
                   "sl": float(sl) if sl else 0.0, "tp": float(tp) if tp else 0.0}
            r = mt5.order_send(req)
            return r is not None and r.retcode == mt5.TRADE_RETCODE_DONE
        except Exception:
            self.logger.exception(f"[MT5-{self.name}] modify error")
            return False

    def close(self, ticket, side):
        if not self.enabled or not self.ready or not ticket:
            return False
        try:
            if not self._is_demo():
                self.logger.error(f"[MT5-{self.name}] not demo - close BLOCKED.")
                return False
            pos = mt5.positions_get(ticket=int(ticket))
            if not pos:
                return False
            p = pos[0]
            tick = mt5.symbol_info_tick(self.symbol)
            otype = mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY
            price = tick.bid if p.type == 0 else tick.ask
            for filling in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
                req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": self.symbol, "volume": p.volume,
                       "type": otype, "position": int(ticket), "price": price, "deviation": 20,
                       "magic": p.magic, "comment": "GF-close", "type_time": mt5.ORDER_TIME_GTC,
                       "type_filling": filling}
                r = mt5.order_send(req)
                if r is not None and r.retcode == mt5.TRADE_RETCODE_DONE:
                    return True
            return False
        except Exception:
            self.logger.exception(f"[MT5-{self.name}] close error")
            return False
