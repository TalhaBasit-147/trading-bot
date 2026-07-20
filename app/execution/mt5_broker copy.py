"""MetaTrader 5 broker. Windows-only.

Install the terminal and the Python package separately on the VPS:
    pip install MetaTrader5==5.0.45

All calls are wrapped with reconnection logic. On any failure, we attempt a
single reconnect; repeated failures trigger an upstream pause via the engine.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import List, Optional

from loguru import logger

from app.config import settings
from app.core.types import Bar, Position, Side
from app.execution.base import Broker


try:
    import MetaTrader5 as mt5   # type: ignore
    MT5_AVAILABLE = True
except Exception:  # ImportError on non-Windows
    mt5 = None  # type: ignore
    MT5_AVAILABLE = False


# mapping our timeframe strings to MT5 constants
def _tf(tf: str):
    if not MT5_AVAILABLE:
        return None
    return {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }[tf]


class MT5Broker(Broker):
    def __init__(self):
        if not MT5_AVAILABLE:
            raise RuntimeError("MetaTrader5 package not available. Install on Windows VPS.")
        self._connected = False
        self._magic = settings.MT5_MAGIC

    # ---- connection ----

    def connect(self) -> bool:
        if self._connected:
            return True
        kwargs = {}
        if settings.MT5_PATH:
            kwargs["path"] = settings.MT5_PATH
        if not mt5.initialize(**kwargs):  # type: ignore[attr-defined]
            logger.error(f"mt5.initialize failed: {mt5.last_error()}")  # type: ignore
            return False
        if settings.MT5_LOGIN and settings.MT5_PASSWORD and settings.MT5_SERVER:
            ok = mt5.login(  # type: ignore
                login=int(settings.MT5_LOGIN),
                password=settings.MT5_PASSWORD,
                server=settings.MT5_SERVER,
            )
            if not ok:
                logger.error(f"mt5.login failed: {mt5.last_error()}")  # type: ignore
                return False
        self._connected = True
        acc = mt5.account_info()  # type: ignore
        logger.info(f"MT5 connected. account={getattr(acc, 'login', '?')} server={getattr(acc, 'server', '?')} equity={getattr(acc, 'equity', '?')}")
        return True

    def disconnect(self) -> None:
        if MT5_AVAILABLE and self._connected:
            mt5.shutdown()  # type: ignore
            self._connected = False

    def _reconnect(self) -> None:
        logger.warning("MT5 reconnecting...")
        try:
            self.disconnect()
        except Exception:
            pass
        time.sleep(1.0)
        self.connect()

    # ---- account ----

    def account_equity(self) -> float:
        info = mt5.account_info()  # type: ignore
        return float(info.equity) if info else 0.0

    def account_balance(self) -> float:
        info = mt5.account_info()  # type: ignore
        return float(info.balance) if info else 0.0

    # ---- symbols ----

    def symbol_info(self, symbol: str) -> dict:
        info = mt5.symbol_info(symbol)  # type: ignore
        if info is None:
            mt5.symbol_select(symbol, True)  # type: ignore
            info = mt5.symbol_info(symbol)  # type: ignore
        if info is None:
            raise RuntimeError(f"symbol_info({symbol}) failed")
        tick = mt5.symbol_info_tick(symbol)  # type: ignore
        spread_points = (tick.ask - tick.bid) / info.point if tick else float(info.spread)
        return {
            "point": info.point,
            "digits": info.digits,
            "contract_size": info.trade_contract_size,
            "tick_value": info.trade_tick_value,
            "spread_points": float(spread_points),
            "min_lot": info.volume_min,
            "max_lot": info.volume_max,
            "lot_step": info.volume_step,
        }

    # ---- data ----

    def get_bars(self, symbol: str, timeframe: str, n: int) -> List[Bar]:
        tf = _tf(timeframe)
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, n)  # type: ignore
        if rates is None or len(rates) == 0:
            self._reconnect()
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, n)  # type: ignore
        if rates is None:
            return []
        out: List[Bar] = []
        for r in rates:
            ts = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc)
            out.append(Bar(ts=ts, open=float(r["open"]), high=float(r["high"]),
                           low=float(r["low"]), close=float(r["close"]),
                           volume=float(r["tick_volume"])))
        return out

    def tick(self, symbol: str) -> dict:
        t = mt5.symbol_info_tick(symbol)  # type: ignore
        if t is None:
            self._reconnect()
            t = mt5.symbol_info_tick(symbol)  # type: ignore
        if t is None:
            raise RuntimeError(f"tick({symbol}) failed")
        return {
            "bid": float(t.bid),
            "ask": float(t.ask),
            "time": datetime.fromtimestamp(int(t.time), tz=timezone.utc),
        }

    # ---- orders ----

    # def place_market(self, symbol: str, side: Side, lots: float, sl: float, tp: float, comment: str = "") -> Optional[Position]:
    #     info = self.symbol_info(symbol)
    #     tick_ = mt5.symbol_info_tick(symbol)  # type: ignore
    #     order_type = mt5.ORDER_TYPE_BUY if side == Side.BUY else mt5.ORDER_TYPE_SELL  # type: ignore
    #     price = tick_.ask if side == Side.BUY else tick_.bid
    #     request = {
    #         "action": mt5.TRADE_ACTION_DEAL,  # type: ignore
    #         "symbol": symbol,
    #         "volume": float(lots),
    #         "type": order_type,
    #         "price": float(price),
    #         "sl": float(sl),
    #         "tp": float(tp),
    #         "deviation": 20,
    #         "magic": self._magic,
    #         "comment": comment[:30],
    #         "type_time": mt5.ORDER_TIME_GTC,  # type: ignore
    #         "type_filling": mt5.ORDER_FILLING_IOC,  # type: ignore
    #     }
    #     result = mt5.order_send(request)  # type: ignore
    #     if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:  # type: ignore
    #         logger.error(f"order_send failed: {getattr(result, 'retcode', None)} {getattr(result, 'comment', '')}")
    #         return None
    #     logger.info(f"[MT5] OPEN #{result.order} {side.value} {lots} {symbol} @ {result.price} sl={sl} tp={tp}")
    #     return Position(
    #         symbol=symbol, side=side, qty=lots, entry=result.price,
    #         sl=sl, tp=tp, open_ts=datetime.now(timezone.utc),
    #         ticket=result.order, comment=comment,
    #     )

    def place_market(self, symbol: str, side: Side, lots: float, sl: float, tp: float, comment: str = "") -> Optional[Position]:
        info_raw = mt5.symbol_info(symbol)  # type: ignore
        if info_raw is None:
            mt5.symbol_select(symbol, True)  # type: ignore
            info_raw = mt5.symbol_info(symbol)  # type: ignore
        tick_ = mt5.symbol_info_tick(symbol)  # type: ignore
        point = info_raw.point
        digits = info_raw.digits

        order_type = mt5.ORDER_TYPE_BUY if side == Side.BUY else mt5.ORDER_TYPE_SELL  # type: ignore
        price = tick_.ask if side == Side.BUY else tick_.bid

        # --- Minimum stop distance fix ---
        # Broker requires SL/TP to be at least `trade_stops_level` points away
        # from the current price. If our stops are too close (because price moved
        # past our intended entry), push them out to the minimum allowed distance.
        stops_level = getattr(info_raw, "trade_stops_level", 0) or 0
        freeze_level = getattr(info_raw, "trade_freeze_level", 0) or 0
        min_dist = max(stops_level, freeze_level) * point
        # add a small safety buffer (5 points) on top of the broker minimum
        min_dist += 5 * point

        sl = float(sl)
        tp = float(tp)

        if side == Side.BUY:
            # SL must be below price by at least min_dist; TP above by at least min_dist
            max_sl = price - min_dist
            if sl > max_sl:
                logger.warning(f"[MT5] BUY SL {sl:.{digits}f} too close to price {price:.{digits}f}, "
                               f"adjusting to {max_sl:.{digits}f} (min_dist={min_dist})")
                sl = max_sl
            min_tp = price + min_dist
            if tp < min_tp:
                tp = min_tp
        else:
            # SELL: SL must be above price by at least min_dist; TP below
            min_sl = price + min_dist
            if sl < min_sl:
                logger.warning(f"[MT5] SELL SL {sl:.{digits}f} too close to price {price:.{digits}f}, "
                               f"adjusting to {min_sl:.{digits}f} (min_dist={min_dist})")
                sl = min_sl
            max_tp = price - min_dist
            if tp > max_tp:
                tp = max_tp

        # Round to digits
        sl = round(sl, digits)
        tp = round(tp, digits)
        price = round(float(price), digits)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,  # type: ignore
            "symbol": symbol,
            "volume": float(lots),
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 30,
            "magic": self._magic,
            "comment": comment[:30],
            "type_time": mt5.ORDER_TIME_GTC,  # type: ignore
            "type_filling": mt5.ORDER_FILLING_IOC,  # type: ignore
        }
        result = mt5.order_send(request)  # type: ignore

        # If filling mode rejected, retry with FOK then RETURN
        if result is not None and result.retcode == 10030:  # unsupported filling mode
            for fill_mode in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):  # type: ignore
                request["type_filling"] = fill_mode
                result = mt5.order_send(request)  # type: ignore
                if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:  # type: ignore
                    break

        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:  # type: ignore
            logger.error(f"order_send failed: {getattr(result, 'retcode', None)} {getattr(result, 'comment', '')} "
                         f"(price={price} sl={sl} tp={tp} min_dist={min_dist})")
            return None
        logger.info(f"[MT5] OPEN #{result.order} {side.value} {lots} {symbol} @ {result.price} sl={sl} tp={tp}")
        return Position(
            symbol=symbol, side=side, qty=lots, entry=result.price,
            sl=sl, tp=tp, open_ts=datetime.now(timezone.utc),
            ticket=result.order, comment=comment,
        )

    def modify_sl_tp(self, ticket: int, sl: float, tp: float) -> bool:
        """Modify SL/TP of an open position. Returns True on success.
        Used to re-anchor TP to the actual fill price after slippage."""
        positions = mt5.positions_get(ticket=ticket)  # type: ignore
        if not positions:
            logger.warning(f"[MT5] modify_sl_tp: position #{ticket} not found")
            return False
        p = positions[0]
        info_raw = mt5.symbol_info(p.symbol)  # type: ignore
        digits = info_raw.digits if info_raw else 2
        request = {
            "action": mt5.TRADE_ACTION_SLTP,  # type: ignore
            "symbol": p.symbol,
            "position": int(ticket),
            "sl": round(float(sl), digits),
            "tp": round(float(tp), digits),
            "magic": self._magic,
        }
        result = mt5.order_send(request)  # type: ignore
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:  # type: ignore
            logger.warning(f"[MT5] modify_sl_tp failed for #{ticket}: "
                           f"{getattr(result, 'retcode', None)} {getattr(result, 'comment', '')}")
            return False
        logger.info(f"[MT5] MODIFY #{ticket} sl={request['sl']} tp={request['tp']}")
        return True

    def close(self, ticket: int) -> float:
        positions = mt5.positions_get(ticket=ticket)  # type: ignore
        if not positions:
            return 0.0
        p = positions[0]
        tick_ = mt5.symbol_info_tick(p.symbol)  # type: ignore
        side_close = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY  # type: ignore
        price = tick_.bid if p.type == mt5.ORDER_TYPE_BUY else tick_.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,  # type: ignore
            "symbol": p.symbol,
            "volume": float(p.volume),
            "type": side_close,
            "position": int(ticket),
            "price": float(price),
            "deviation": 20,
            "magic": self._magic,
            "comment": "smc-close",
            "type_time": mt5.ORDER_TIME_GTC,  # type: ignore
            "type_filling": mt5.ORDER_FILLING_IOC,  # type: ignore
        }
        result = mt5.order_send(request)  # type: ignore
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:  # type: ignore
            logger.error(f"close failed: {getattr(result, 'retcode', None)}")
            return 0.0
        # realized pnl: ask broker for deal history
        return float(p.profit)

    def open_positions(self, symbol: Optional[str] = None) -> List[Position]:
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()  # type: ignore
        out: List[Position] = []
        if not positions:
            return out
        for p in positions:
            if p.magic != self._magic:
                continue
            side = Side.BUY if p.type == mt5.ORDER_TYPE_BUY else Side.SELL  # type: ignore
            out.append(Position(
                symbol=p.symbol, side=side, qty=p.volume, entry=p.price_open,
                sl=p.sl, tp=p.tp,
                open_ts=datetime.fromtimestamp(int(p.time), tz=timezone.utc),
                ticket=int(p.ticket), comment=p.comment,
            ))
        return out
