import asyncio
import json
from app.services.data_fetcher import MarketDataService
from app.services.llm_engine import FinancialAnalyst
from app.services.trading_service import TradingService
from app.services.notifier import Notifier

class AutonomousAgent:
    def __init__(self, model: str = None):
        # Load config
        self.config = self._load_config()
        self.model = model or self.config.get("model", "llama3.1")
        
        self.market_service = MarketDataService()
        self.analyst = FinancialAnalyst(model=self.model)
        self.trading_service = TradingService()
        self.notifier = Notifier()
        
        # Priority: Config > Defaults
        self.watchlist = self.config.get("watchlist", ["AAPL", "NVDA", "BTC-USD", "TSLA"])
        # interval_minutes in config converts to seconds
        self.interval = self.config.get("interval_minutes", 5) * 60 

    def _load_config(self):
        try:
            with open("agent_config.json", "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Config read error: {e}. Using defaults.")
            return {}

    async def run_once(self, ticker: str):
        print(f"--- Autonomous Cycle for {ticker} ---")
        
        # 1. Fetch Data
        data = self.market_service.get_market_data(ticker)
        if not data.get("price_data"):
            print(f"Failed to fetch data for {ticker}")
            return

        # 1.5 Fetch News (Proactive fallback for models without tool-calling)
        from app.services.tools import get_market_news
        print(f"Fetching latest news for {ticker}...")
        data["news"] = get_market_news(ticker)

        # 2. Analyze
        print(f"Analyzing {ticker}...")
        analysis = self.analyst.analyze(ticker, data)
        signal = analysis.get("signal", "HOLD")
        reasoning = analysis.get("reasoning", "")
        risk_score = analysis.get("risk_score", 5) # Default to medium risk
        stop_loss = analysis.get("stop_loss", 0)
        
        self.notifier.notify_analysis(ticker, signal, f"Risk: {risk_score}/10 | SL: {stop_loss} | {reasoning}")

        # 2.1 Proactive Stop-Loss Check (if we have a position)
        try:
            account = self.trading_service.get_account_info()
            if isinstance(account, dict):
                ag_port = account.get("agent_portfolio", {})
            else:
                ag_port = getattr(account, "agent_portfolio", {})
            
            pos = ag_port.get("positions", {}).get(ticker, {})
            if pos and pos.get("qty", 0) > 0:
                current_price = data.get("price_data", {}).get("current_price", 0)
                stored_sl = pos.get("stop_loss", 0)
                
                # Use the tighter/safer stop loss between current analysis and stored SL
                # If stored_sl is 0, we use analysis stop_loss.
                active_sl = max(stop_loss, stored_sl) if stop_loss > 0 and stored_sl > 0 else (stop_loss or stored_sl)

                if active_sl > 0 and current_price <= active_sl:
                    print(f"⚠️ STOP LOSS TRIGGERED for {ticker}: Price {current_price} <= SL {active_sl} (Analysis SL: {stop_loss}, Stored SL: {stored_sl})")
                    self.notifier.notify(f"PANIC SELL: Stop-loss breached for {ticker} at {current_price} (SL: {active_sl})", level="error")
                    self.trading_service.place_order(ticker, pos["qty"], "sell", source="agent")
                    return analysis
        except Exception as e:
            print(f"Error in stop-loss check: {e}")

        # 3. Execute Trade (if signal Buy/Sell and logic permits)
        if signal in ["BUY", "SELL"]:
            if not self.config.get("autonomous_enabled", True):
                print(f"Skipping autonomous trade for {ticker}: Disabled in config.")
                return analysis

            side = signal.lower()
            print(f"Autonomous decision: {signal} {ticker} (Risk: {risk_score}/10)")
            # Calculate quantity based on Agent's available capital
            try:
                account = self.trading_service.get_account_info()
                if isinstance(account, dict):
                    ag_port = account.get("agent_portfolio", {})
                else:
                    ag_port = getattr(account, "agent_portfolio", {})
                
                agent_cash = ag_port.get("cash", 0)
                current_price = data.get("price_data", {}).get("current_price", 0)
                
                if current_price > 0:
                    if side == "buy":
                        # RISK-AWARE SIZING: 
                        # Base is 20% of cash. We scale it by (11 - risk_score) / 10.
                        # High risk (10) -> (11-10)/10 = 0.1x multiplier (2% of cash)
                        # Low risk (1) -> (11-1)/10 = 1.0x multiplier (20% of cash)
                        risk_multiplier = max(0.1, (11 - risk_score) / 10)
                        base_allocation = agent_cash * 0.20
                        allocation_per_trade = base_allocation * risk_multiplier
                        
                        qty = int(allocation_per_trade / current_price)
                        
                        if qty < 1 and agent_cash >= current_price:
                            qty = 1
                        
                        if qty < 1:
                            print(f"Insufficient agent funds for {ticker}. Cash: ${agent_cash:.2f}, Allocation: ${allocation_per_trade:.2f}")
                            return analysis
                    else: # sell
                        # Get held quantity for this ticker
                        ag_pos = ag_port.get("positions", {}).get(ticker, {})
                        qty = ag_pos.get("qty", 0)
                        
                        if qty <= 0:
                            print(f"Skipping SELL for {ticker}: Agent has no shares to sell.")
                            return analysis
                else:
                    print(f"Invalid price for {ticker}: {current_price}")
                    return analysis

            except Exception as e:
                print(f"Error calculating quantity for {ticker}: {e}. Defaulting to 1.")
                qty = 1 
            
            print(f"Agent Attempting to {side.upper()} {qty} shares of {ticker} with SL {stop_loss} and Risk {risk_score}...")
            # We pass the stop_loss to place_order so it can be persisted
            result = self.trading_service.place_order(ticker, qty, side, source="agent", stop_loss=stop_loss, risk_score=risk_score)
            
            if "error" in result:
                print(f"Trade failed/rejected for {ticker}: {result['error']}")
            else:
                mode_str = " (SIMULATED)" if result.get("mode") == "simulation" else ""
                self.notifier.notify_trade(ticker, side, qty, f"Risk {risk_score}/10 | SL {stop_loss} | {reasoning}{mode_str}")
                print(f"Trade Successful for {ticker}: {result}")
        
        return analysis

    async def start_monitoring(self):
        print(f"Starting background monitor for: {self.watchlist}")
        while True:
            for ticker in self.watchlist:
                try:
                    # Reload config on every ticker cycle to pick up changes
                    self.config = self._load_config()
                    # Sync analyst model with current config
                    new_model = self.config.get("model", self.model)
                    if new_model != self.analyst.model:
                        print(f"Switching analyst model to: {new_model}")
                        self.analyst.model = new_model
                    
                    await self.run_once(ticker)
                except Exception as e:
                    print(f"Error in monitor cycle for {ticker}: {e}")
            
            print(f"Cycle complete. Waiting {self.interval}s...")
            await asyncio.sleep(self.interval)

if __name__ == "__main__":
    # For standalone testing
    agent = AutonomousAgent()
    asyncio.run(agent.start_monitoring())
