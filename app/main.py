from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app.services.data_fetcher import MarketDataService
from app.services.llm_engine import FinancialAnalyst
from app.services.trading_service import TradingService
import os
import json

app = FastAPI(title="FinSight Agent API", version="1.0.0")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # In production, replace with specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize Services
market_service = MarketDataService()
financial_analyst = FinancialAnalyst()
trading_service = TradingService()

class AnalysisRequest(BaseModel):
    ticker: str
    model: str = "llama3" # Optional override

class TradeRequest(BaseModel):
    ticker: str
    qty: int
    side: str
    strategy: str = "market"

class AgentConfigRequest(BaseModel):
    enabled: bool = None
    model: str = None
    watchlist: list[str] = None
    agent_capital: float = None

@app.get("/")
def serve_dashboard():
    return FileResponse("finsight_dashboard.html")

@app.get("/health")
def health_check():
    return {"status": "healthy", "service": "FinSight Agent"}

@app.get("/account")
def get_account():
    try:
        # Use global trading_service
        trader = trading_service
        if not trader.active:
            return {"status": "inactive", "message": "Alpaca keys missing"}
        
        acc = trader.get_account_info()
        pos = trader.get_positions()
        orders = trader.get_orders()
        
        return {
            "status": "active",
            "mode": trader.mode,
            "equity": acc.equity,
            "buying_power": acc.buying_power,
            "cash": acc.cash,
            "currency": acc.currency,
            "agent_portfolio": getattr(acc, "agent_portfolio", {}),
            "positions": [
                {
                    "symbol": p.symbol,
                    "qty": p.qty,
                    "avg_entry_price": p.avg_entry_price,
                    "current_price": p.current_price,
                    "unrealized_pl": p.unrealized_pl,
                    "unrealized_plpc": getattr(p, "unrealized_plpc", 0),
                    "stop_loss": getattr(p, "stop_loss", 0),
                    "risk_score": getattr(p, "risk_score", 0)
                } for p in pos
            ],
            "pending_orders": orders
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class AllocationRequest(BaseModel):
    amount: float

@app.post("/agent/allocation")
def set_allocation(request: AllocationRequest):
    try:
        trading_service.set_agent_allocation(request.amount)
        return {"status": "success", "allocated": request.amount}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/agent/config")
def get_agent_config():
    try:
        import json
        with open("agent_config.json", "r") as f:
            return json.load(f)
    except:
        return {"autonomous_enabled": True}

@app.get("/market/status/{ticker}")
def get_market_status(ticker: str):
    try:
        data = market_service.get_market_data(ticker)
        return {
            "ticker": ticker,
            "price": data.get("price_data", {}).get("current_price"),
            "change": data.get("price_data", {}).get("change_percent"),
            "indicators": data.get("indicators", {})
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/performance")
def get_performance():
    try:
        import json
        history_file = "performance_history.json"
        if not os.path.exists(history_file):
            return []
        with open(history_file, "r") as f:
            return json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/agent/config")
def update_agent_config(config: AgentConfigRequest):
    try:
        config_path = "agent_config.json"
        
        # 1. Load existing config
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                current_data = json.load(f)
        else:
            current_data = {}

        # 2. Map incoming config to the actual keys used in the file
        # 'enabled' in request maps to 'autonomous_enabled' in file
        if config.enabled is not None:
            current_data["autonomous_enabled"] = config.enabled
        
        if config.model is not None:
            current_data["model"] = config.model
            
        if config.watchlist is not None:
            current_data["watchlist"] = config.watchlist

        if config.agent_capital is not None:
            current_data["agent_capital"] = config.agent_capital

        # 3. Save merged config
        with open(config_path, "w") as f:
            json.dump(current_data, f, indent=4)
            
        return {"status": "success", "config": current_data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/trade")
def execute_trade(request: TradeRequest):
    try:
        print(f"Executing {request.strategy} {request.side} for {request.qty} shares of {request.ticker}...")
        
        # 1. Fetch current market context for strategy logic
        market_data = market_service.get_market_data(request.ticker)
        indicators = market_data.get("indicators", {})
        price = market_data.get("price_data", {}).get("current_price")

        can_trade = True
        reason = "Manual override"

        # 2. Strategy Logic Fallbacks
        if request.strategy == "momentum":
            # RSI > 50 and MACD > 0 for Buy; vice versa for Sell
            rsi = indicators.get("rsi", 50)
            macd = indicators.get("macd", 0)
            if request.side == "buy" and (rsi < 50 or macd < 0):
                can_trade = False
                reason = "Momentum not bullish (RSI/MACD)"
            elif request.side == "sell" and (rsi > 50 or macd > 0):
                can_trade = False
                reason = "Momentum not bearish (RSI/MACD)"
        
        elif request.strategy == "mean_reversion":
            # Buy if Oversold (RSI < 30); Sell if Overbought (RSI > 70)
            rsi = indicators.get("rsi", 50)
            if request.side == "buy" and rsi > 35:
                can_trade = False
                reason = "Not oversold enough for mean reversion"
            elif request.side == "sell" and rsi < 65:
                can_trade = False
                reason = "Not overbought enough for mean reversion"
        
        elif request.strategy == "breakout":
            # Check price vs Bollinger Bands
            bb = indicators.get("bollinger", {})
            if bb:
                if request.side == "buy" and price < bb.get("mid", 0):
                    can_trade = False
                    reason = "Price below BB midline (no breakout)"
                elif request.side == "sell" and price > bb.get("mid", 0):
                    can_trade = False
                    reason = "Price above BB midline (no breakdown)"

        elif request.strategy == "ai_optimized":
            # Rely on the most recent AI analysis
            analysis = financial_analyst.analyze(request.ticker, market_data)
            signal = analysis.get("signal", "HOLD")
            if request.side.upper() != signal:
                can_trade = False
                reason = f"AI Signal is {signal}, not {request.side.upper()}"

        if not can_trade:
            return {"status": "rejected", "reason": reason}

        # 3. Execute Trade
        # from app.services.trading_service import TradingService # Moved to global init
        # trader = TradingService() # Use global trading_service
        result = trading_service.place_order(request.ticker, request.qty, request.side, source="manual")
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/trades")
def get_trades():
    try:
        return trading_service.get_trade_history()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/analyze")
def analyze_ticker(request: AnalysisRequest):
    try:
        # 1. Fetch Data
        print(f"Fetching data for {request.ticker}...")
        market_data = market_service.get_market_data(request.ticker)
        print(f"DEBUG: market_data keys: {market_data.keys()}")
        print(f"DEBUG: price_data type: {type(market_data.get('price_data'))}")
        
        if not market_data.get("price_data"):
             raise HTTPException(status_code=404, detail=f"No data found for ticker {request.ticker}")

        # 2. Analyze with LLM
        print(f"Analyzing {request.ticker} with {request.model}...")
        financial_analyst.model = request.model
        analysis_result = financial_analyst.analyze(request.ticker, market_data)
        
        price_data = market_data.get("price_data", {})
        
        # Merge indicators and history into raw_analysis for the dashboard
        enriched_analysis = {**analysis_result}
        enriched_analysis["indicators"] = market_data.get("indicators", {})
        
        # Extract history as a simple list for the frontend chart
        history_dict = price_data.get("history", {})
        if history_dict:
            # history_dict is {timestamp: {Close: val, ...}}
            # We want just the Close prices in order
            enriched_analysis["price_history"] = [
                float(v["Close"]) for v in history_dict.values()
            ]
        else:
            enriched_analysis["price_history"] = []
            
        return {
            "ticker": request.ticker,
            "signal": analysis_result.get("signal", "UNKNOWN"),
            "reasoning": analysis_result.get("reasoning", "Analysis failed to produce reasoning."),
            "market_data_summary": {
                "price": price_data.get("current_price", "N/A"),
                "change": price_data.get("change_percent", "N/A"),
                "change_abs": price_data.get("change_absolute", "N/A")
            },
            "raw_analysis": enriched_analysis
        }
    except Exception as e:
        print(f"Error in analyze_ticker: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

