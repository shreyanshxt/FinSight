import os
import json
import fcntl
import threading
import alpaca_trade_api as tradeapi
from datetime import datetime
from contextlib import contextmanager
from dotenv import load_dotenv

SIMULATED_PORTFOLIO = "simulated_portfolio.json"
PERFORMANCE_HISTORY = "performance_history.json"
TRADE_HISTORY = "trade_history.json"

PRICE_REFRESH_INTERVAL = 60  # seconds

class TradingService:
    def __init__(self):
        load_dotenv()
        self.api_key = os.getenv("ALPACA_API_KEY")
        self.secret_key = os.getenv("ALPACA_SECRET_KEY")
        self.base_url = "https://paper-api.alpaca.markets" # Paper trading by default
        self._last_price_refresh = None  # throttle tracker
        
        if self.api_key and self.secret_key:
            self.api = tradeapi.REST(self.api_key, self.secret_key, self.base_url, api_version='v2')
            self.active = True
            self.mode = "ALPACA"
        else:
            print("ALPACA_API_KEY/SECRET not found. Switching to LOCAL SIMULATION.")
            self.api = None
            self.active = True # Trading is "active" but in simulation mode
            self.mode = "SIMULATION"
            self._ensure_sim_portfolio()

    def _ensure_sim_portfolio(self):
        if not os.path.exists(SIMULATED_PORTFOLIO):
            initial_state = {
                "equity": 100000.0,
                "buying_power": 100000.0,
                "cash": 100000.0,
                "currency": "USD",
                "positions": {},
                "agent_portfolio": {
                    "allocated": 0.0,
                    "cash": 0.0,
                    "equity": 0.0,
                    "positions": {}
                }
            }
            with open(SIMULATED_PORTFOLIO, "w") as f:
                json.dump(initial_state, f)
        else:
            # Migration for existing files
            with open(SIMULATED_PORTFOLIO, "r") as f:
                try:
                    data = json.load(f)
                except:
                    data = {}
            
            if "agent_portfolio" not in data:
                data["agent_portfolio"] = {
                    "allocated": 0.0,
                    "cash": 0.0,
                    "equity": 0.0,
                    "positions": {}
                }
                with open(SIMULATED_PORTFOLIO, "w") as f:
                    json.dump(data, f)
    
    def set_agent_allocation(self, amount: float):
        """Allocates trading capital to the AI agent."""
        with self._locked_portfolio("r+") as state:
            # We treat this as setting a "virtual" budget. 
            # It doesn't physically move cash from the main account unless we wanted to enforce strict partition.
            # For now, we'll just set the agent's starting cash.
            # Logic: If we re-allocate, we reset the agent's cash to that amount.
            # Ideally, we should handle this carefully, but valid for a prototype.
            state["agent_portfolio"]["allocated"] = amount
            state["agent_portfolio"]["cash"] = amount
            # Recalculate equity
            agent_pos_val = sum(p["qty"] * p.get("current_price", 0) for p in state["agent_portfolio"]["positions"].values())
            state["agent_portfolio"]["equity"] = amount + agent_pos_val
            return state["agent_portfolio"]

    @contextmanager
    def _locked_portfolio(self, mode="r+"):
        """Context manager to handle portfolio file locking."""
        self._ensure_sim_portfolio()
        with open(SIMULATED_PORTFOLIO, mode) as f:
            try:
                # Acquire exclusive lock
                fcntl.flock(f, fcntl.LOCK_EX)
                if "r" in mode:
                    state = json.load(f)
                else:
                    state = {}
                
                yield state
                
                # If we modified, seek to start and truncate
                if "w" in mode or "+" in mode:
                    f.seek(0)
                    json.dump(state, f)
                    f.truncate()
            finally:
                # Release lock
                fcntl.flock(f, fcntl.LOCK_UN)

    def _maybe_refresh_prices(self):
        """Kick off a background price refresh at most once per PRICE_REFRESH_INTERVAL seconds.
        Returns immediately — never blocks the caller."""
        now = datetime.now()
        if (
            self._last_price_refresh is None
            or (now - self._last_price_refresh).total_seconds() >= PRICE_REFRESH_INTERVAL
        ):
            # Mark the time now so concurrent requests don't all spawn threads
            self._last_price_refresh = now
            t = threading.Thread(target=self._refresh_sim_prices, daemon=True)
            t.start()

    def get_account_info(self):
        self._ensure_sim_portfolio() # Ensure we can at least read agent metadata

        # Throttled price refresh — at most once per 60s
        self._maybe_refresh_prices()

        with self._locked_portfolio("r") as state:
            agent_portfolio = state.get("agent_portfolio", {})

        if self.mode == "ALPACA":
            alp_acc = self.api.get_account()
            # Wrap in a namespace that mimics the expected structure but with agent_portfolio added
            acc = type('SimpleNamespace', (object,), {
                "equity": float(alp_acc.equity),
                "buying_power": float(alp_acc.buying_power),
                "cash": float(alp_acc.cash),
                "currency": alp_acc.currency,
                "agent_portfolio": agent_portfolio
            })
            return acc
        
        with self._locked_portfolio("r") as state:
            acc = type('SimpleNamespace', (object,), state)
            # Attach agent info
            acc.agent_portfolio = agent_portfolio
            return acc

    def get_positions(self):
        if self.mode == "ALPACA":
            alp_positions = self.api.list_positions()
            # Normalize for frontend consistency
            normalized = []
            
            # Get agent metadata for risk info
            with self._locked_portfolio("r") as state:
                ag_positions = state.get("agent_portfolio", {}).get("positions", {})

            for p in alp_positions:
                ag_meta = ag_positions.get(p.symbol, {})
                normalized.append(type('SimpleNamespace', (object,), {
                    "symbol": p.symbol,
                    "qty": int(p.qty),
                    "avg_entry_price": float(p.avg_entry_price),
                    "current_price": float(p.current_price),
                    "unrealized_pl": float(p.unrealized_pl),
                    "unrealized_plpc": float(p.unrealized_plpc),
                    "stop_loss": ag_meta.get("stop_loss", 0),
                    "risk_score": ag_meta.get("risk_score", 0)
                }))
            return normalized
        
        self._refresh_sim_prices()
        with self._locked_portfolio("r") as state:
            ag_positions = state.get("agent_portfolio", {}).get("positions", {})
            results = []
            for k, v in state["positions"].items():
                ag_meta = ag_positions.get(k, {})
                results.append(type('SimpleNamespace', (object,), {
                    **v, 
                    "symbol": k,
                    "stop_loss": v.get("stop_loss", ag_meta.get("stop_loss", 0)),
                    "risk_score": v.get("risk_score", ag_meta.get("risk_score", 0))
                }))
            return results

    def get_orders(self):
        """Returns pending orders."""
        if self.mode == "ALPACA":
            try:
                orders = self.api.list_orders(status='open')
                return [{
                    "id": o.id,
                    "symbol": o.symbol,
                    "qty": o.qty,
                    "side": o.side,
                    "status": o.status,
                    "submitted_at": o.submitted_at.isoformat() if hasattr(o.submitted_at, 'isoformat') else str(o.submitted_at)
                } for o in orders]
            except:
                return []
        return []

    def place_order(self, symbol: str, qty: int, side: str, order_type: str = 'market', time_in_force: str = 'gtc', source="manual", stop_loss=0, risk_score=0):
        """
        Places an order on Alpaca or Local Simulation.
        """
        if self.mode == "ALPACA":
            try:
                # Alpaca expects qty as float or string for fractional? No, REST v2 usually int or string for whole.
                order = self.api.submit_order(
                    symbol=symbol,
                    qty=qty,
                    side=side,
                    type=order_type,
                    time_in_force=time_in_force
                )
                
                # Fetch current price for logging (Alpaca doesn't return it yet for market orders)
                from app.services.data_fetcher import MarketDataService
                mkt = MarketDataService()
                price_data = mkt.get_market_data(symbol)
                current_price = price_data.get("price_data", {}).get("current_price", 0)

                # Manual logging for Alpaca trades so they show in UI
                trade_entry = {
                    "timestamp": datetime.now().isoformat(),
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "price": current_price,
                    "source": source,
                    "mode": "alpaca"
                }
                self._log_trade(trade_entry)

                # --- SYNC AGENT METADATA IF ALPACA AGENT TRADE ---
                # This is now handled by _execute_sim_trade which is called at the end
                
                # Fetch account to log performance
                acc = self.api.get_account()
                self._log_performance(float(acc.equity))

                return {"status": "success", "mode": "alpaca", "result": order._asset if hasattr(order, '_asset') else str(order)}
            except Exception as e:
                return {"error": str(e)}
        
        # Local Simulation Logic
        return self._execute_sim_trade(symbol, qty, side, source=source, stop_loss=stop_loss, risk_score=risk_score)

    def _execute_sim_trade(self, symbol, qty, side, source="manual", stop_loss=0, risk_score=0):
        from app.services.data_fetcher import MarketDataService
        mkt = MarketDataService()
        data = mkt.get_market_data(symbol)
        price = data.get("price_data", {}).get("current_price", 0)
        
        if price == 0:
            return {"error": f"Could not fetch price for {symbol}"}

        with self._locked_portfolio("r+") as state:
            cost = price * qty
            
            # --- AGENT CAPITAL CHECK ---
            if source == "agent":
                agent_cash = state.get("agent_portfolio", {}).get("cash", 0)
                if side.lower() == "buy" and agent_cash < cost:
                    return {"error": f"Agent insufficient funds. Cash: ${agent_cash:.2f}, Cost: ${cost:.2f}"}

            # --- MAIN ACCOUNT ---
            if side.lower() == "buy":
                if state["cash"] < cost:
                    return {"error": "Insufficient buying power (Simulated)"}
                
                state["cash"] -= cost
                pos = state["positions"].get(symbol, {"qty": 0, "avg_entry_price": 0})
                new_qty = pos["qty"] + qty
                new_avg = ((pos["qty"] * pos["avg_entry_price"]) + cost) / new_qty
                state["positions"][symbol] = {
                    "qty": new_qty,
                    "avg_entry_price": new_avg,
                    "current_price": price,
                    "unrealized_pl": (price - new_avg) * new_qty,
                    "unrealized_plpc": (price / new_avg) - 1 if new_avg else 0
                }
            else: # sell
                pos = state["positions"].get(symbol)
                if not pos or pos["qty"] < qty:
                    return {"error": f"Not enough shares of {symbol} to sell (Simulated)"}
                
                state["cash"] += cost
                pos["qty"] -= qty
                if pos["qty"] == 0:
                    del state["positions"][symbol]
                else:
                    pos["current_price"] = price
                    pos["unrealized_pl"] = (price - pos["avg_entry_price"]) * pos["qty"]
                    pos["unrealized_plpc"] = (price / pos["avg_entry_price"]) - 1
            
            # --- AGENT SUB-ACCOUNT UPDATE ---
            if source == "agent":
                ag_port = state.get("agent_portfolio")
                if ag_port:
                    if side.lower() == "buy":
                        ag_port["cash"] -= cost
                        p = ag_port["positions"].get(symbol, {"qty": 0, "avg_entry_price": 0})
                        n_q = p["qty"] + qty
                        n_av = ((p["qty"] * p["avg_entry_price"]) + cost) / n_q
                        ag_port["positions"][symbol] = {
                            "qty": n_q,
                            "avg_entry_price": n_av,
                            "current_price": price,
                            "unrealized_pl": (price - n_av) * n_q,
                            "stop_loss": stop_loss,
                            "risk_score": risk_score
                        }
                    else: # sell
                        # Assuming agent only sells what it bought. 
                        # Ideally should check agent pos existence, but relying on main check for simplicity for now?
                        # No, we must check agent pos to avoid negative agent qty.
                        p = ag_port["positions"].get(symbol)
                        if p and p["qty"] >= qty:
                            ag_port["cash"] += cost
                            p["qty"] -= qty
                            if p["qty"] == 0:
                                del ag_port["positions"][symbol]
                            else:
                                p["current_price"] = price
                                p["unrealized_pl"] = (price - p["avg_entry_price"]) * p["qty"]
                        else:
                            # If we are here, it means main acct had shares but agent didn't?
                            # This is an edge case if agent logic is flawed.
                            # For safety, we just don't update agent cash if it didn't hold the pos.
                            pass

            # Update Equity (Main)
            total_pos_value = sum(p["qty"] * p.get("current_price", 0) for p in state["positions"].values())
            state["equity"] = state["cash"] + total_pos_value
            state["buying_power"] = state["cash"]
            
            # Update Equity (Agent)
            if "agent_portfolio" in state:
                ap = state["agent_portfolio"]
                ap_val = sum(p["qty"] * p.get("current_price", 0) for p in ap["positions"].values())
                ap["equity"] = ap["cash"] + ap_val

            # Performance logging is separate from portfolio file lock but uses equity
            equity_to_log = state["equity"]

        self._log_performance(equity_to_log)
        
        trade_entry = {
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": price,
            "source": source
        }
        self._log_trade(trade_entry)
        
        return {"status": "success", "mode": "simulation", "symbol": symbol, "side": side, "qty": qty, "price": price, "source": source}

    def _refresh_sim_prices(self):
        """Updates simulated position prices with real market data."""
        from app.services.data_fetcher import MarketDataService
        mkt = MarketDataService()
        
        with self._locked_portfolio("r+") as state:
            updated = False
            
            # 1. Refresh Main Positions
            if state.get("positions"):
                for symbol, pos in state["positions"].items():
                    try:
                        data = mkt.get_market_data(symbol)
                        price = data.get("price_data", {}).get("current_price")
                        if price:
                            pos["current_price"] = price
                            pos["unrealized_pl"] = (price - pos["avg_entry_price"]) * pos["qty"]
                            pos["unrealized_plpc"] = (price / pos["avg_entry_price"]) - 1 if pos["avg_entry_price"] else 0
                            updated = True
                    except Exception as e:
                        print(f"Error refreshing main price for {symbol}: {e}")

            # 2. Refresh Agent Positions
            ag_port = state.get("agent_portfolio")
            if ag_port and ag_port.get("positions"):
                for symbol, pos in ag_port["positions"].items():
                    try:
                        data = mkt.get_market_data(symbol)
                        price = data.get("price_data", {}).get("current_price")
                        if price:
                            pos["current_price"] = price
                            pos["unrealized_pl"] = (price - pos["avg_entry_price"]) * pos["qty"]
                            # Note: agent position dict might not have unrealized_plpc, but we should update pl
                            updated = True
                    except Exception as e:
                        print(f"Error refreshing agent price for {symbol}: {e}")

            if updated:
                # Update Main Equity
                main_pos_val = sum(p["qty"] * p.get("current_price", 0) for p in state["positions"].values())
                state["equity"] = state["cash"] + main_pos_val
                
                # Update Agent Equity
                if ag_port:
                    ag_pos_val = sum(p["qty"] * p.get("current_price", 0) for p in ag_port["positions"].values())
                    ag_port["equity"] = ag_port["cash"] + ag_pos_val
                
                equity_to_log = state["equity"]
            else:
                equity_to_log = None

        if equity_to_log is not None:
            self._log_performance(equity_to_log)

    def _log_performance(self, equity):
        """Logs current equity to performance history."""
        history = []
        if os.path.exists(PERFORMANCE_HISTORY):
            try:
                with open(PERFORMANCE_HISTORY, "r") as f:
                    history = json.load(f)
            except:
                pass

        # Only log if last entry is different or certain time passed (e.g., 5 mins)
        # Log if equity changed OR if it's been more than 60 seconds since the last log
        timestamp_dt = datetime.now()
        timestamp = timestamp_dt.isoformat()
        
        should_log = False
        if not history:
            should_log = True
        else:
            last_entry = history[-1]
            last_timestamp = datetime.fromisoformat(last_entry["timestamp"])
            time_diff = (timestamp_dt - last_timestamp).total_seconds()
            
            if last_entry["equity"] != equity or time_diff >= 60:
                should_log = True

        if should_log:
            history.append({"timestamp": timestamp, "equity": equity})
            
            # Keep last 1000 points
            if len(history) > 1000:
                history = history[-1000:]
                
            with open(PERFORMANCE_HISTORY, "w") as f:
                json.dump(history, f)
    def _log_trade(self, entry):
        """Logs a completed trade to trade history."""
        history = []
        if os.path.exists(TRADE_HISTORY):
            try:
                with open(TRADE_HISTORY, "r") as f:
                    history = json.load(f)
            except:
                pass
        
        history.append(entry)
        # Keep last 500 trades
        if len(history) > 500:
            history = history[-500:]
            
        with open(TRADE_HISTORY, "w") as f:
            json.dump(history, f)

    def get_trade_history(self):
        """Returns the logged trade history."""
        if os.path.exists(TRADE_HISTORY):
            try:
                with open(TRADE_HISTORY, "r") as f:
                    return json.load(f)
            except:
                return []
        return []
