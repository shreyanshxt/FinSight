import yfinance as yf
from alpha_vantage.timeseries import TimeSeries
from alpha_vantage.techindicators import TechIndicators
import os
import pandas as pd
from datetime import datetime, timedelta

class MarketDataService:
    def __init__(self):
        self.av_key = os.getenv("ALPHAVANTAGE_API_KEY")
        
    def get_market_data(self, ticker: str):
        """
        Aggregates data from multiple sources.
        """
        data = {
            "ticker": ticker,
            "timestamp": datetime.now().isoformat(),
            "price_data": self._get_yfinance_data(ticker),
            "indicators": {}
        }
        
        # Try to get simplified AlphaVantage data if key is present
        if self.av_key and self.av_key != "your_key_here":
            try:
                av_data = self._get_alpha_vantage_data(ticker)
                data["indicators"].update(av_data)
            except Exception as e:
                if "rate limit" in str(e).lower() or "thank you for using alpha vantage" in str(e).lower():
                    print(f"INFO: AlphaVantage rate limit reached for {ticker}. Using local indicator fallbacks.")
                else:
                    print(f"AlphaVantage Error for {ticker}: {e}")
                data["errors"] = [str(e)]
                
        # Calculate local indicators if AV failed or as supplement
        local_indicators = self._calculate_basic_indicators(data["price_data"].get("_raw_df"))
        data["indicators"].update(local_indicators)
        # Remove raw df before returning
        if "_raw_df" in data["price_data"]:
            del data["price_data"]["_raw_df"]
             
        return data

    def _get_yfinance_data(self, ticker: str) -> dict:
        """
        Fetches OHLCV data from yfinance with retries and error handling.
        """
        import time
        max_retries = 3
        for attempt in range(max_retries):
            try:
                print(f"DEBUG: YF Fetching {ticker} (Attempt {attempt+1})")
                stock = yf.Ticker(ticker)
                # Get 1 year of history
                df = stock.history(period="1y")
                
                if df.empty:
                    print(f"DEBUG: YF df is empty for {ticker}")
                    if attempt < max_retries - 1:
                        time.sleep(1)
                        continue
                    return {}

                # Check if we have enough data
                if len(df) < 2:
                    if attempt < max_retries - 1:
                        time.sleep(1)
                        continue
                    return {}

                current_price = float(df['Close'].iloc[-1])
                prev_close = float(df['Close'].iloc[-2])
                change_abs = current_price - prev_close
                change_pct = (change_abs / prev_close) * 100 if prev_close != 0 else 0
                
                return {
                    "current_price": current_price,
                    "change_absolute": change_abs,
                    "change_percent": change_pct,
                    "volume": int(df['Volume'].iloc[-1]),
                    "history": {str(k): v for k, v in df.tail(30).to_dict(orient="index").items()}, # Last 30 days for context
                    "_raw_df": df # Temporary for indicator calculation
                }
            except Exception as e:
                print(f"ERROR in _get_yfinance_data for {ticker}: {e}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                return {}
        return {}

    def _get_alpha_vantage_data(self, ticker: str) -> dict:
        """
        Fetches technical indicators from AlphaVantage.
        """
        ti = TechIndicators(key=self.av_key, output_format='json')
        
        # RSI
        rsi_data, _ = ti.get_rsi(symbol=ticker, interval='daily', time_period=14, series_type='close')
        latest_date = list(rsi_data.keys())[0]
        current_rsi = float(rsi_data[latest_date]['RSI'])
        
        # MACD
        macd_data, _ = ti.get_macd(symbol=ticker, interval='daily', series_type='close')
        latest_macd = macd_data[list(macd_data.keys())[0]]
        
        return {
            "rsi": current_rsi,
            "macd": latest_macd
        }

    def _calculate_basic_indicators(self, df: pd.DataFrame) -> dict:
        """
        Calculates RSI, MACD, SMA, and EMA using pandas.
        """
        if df is None or df.empty or len(df) < 26:
            return {"note": "insufficient_data", "rsi": 50.0, "macd": 0.0, "sma_20": 0.0, "ema_20": 0.0}

        # Basic RSI Calculation
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        # Basic MACD Calculation
        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        macd = exp1 - exp2
        
        # SMA and EMA
        sma_20 = df['Close'].rolling(window=20).mean()
        ema_20 = df['Close'].ewm(span=20, adjust=False).mean()
        sma_50 = df['Close'].rolling(window=50).mean()
        
        return {
            "note": "calculated_locally",
            "rsi": float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0,
            "macd": float(macd.iloc[-1]) if not pd.isna(macd.iloc[-1]) else 0.0,
            "sma_20": float(sma_20.iloc[-1]) if not pd.isna(sma_20.iloc[-1]) else 0.0,
            "ema_20": float(ema_20.iloc[-1]) if not pd.isna(ema_20.iloc[-1]) else 0.0,
            "sma_50": float(sma_50.iloc[-1]) if not pd.isna(sma_50.iloc[-1]) else 0.0
        }
