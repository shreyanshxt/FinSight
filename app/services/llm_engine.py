import json
import os
from datetime import datetime
from app.services.llm_providers import get_llm_provider

class FinancialAnalyst:
    def __init__(self, model: str = "llama3"):
        self.model_name = model
        self.provider = get_llm_provider(model)

    @property
    def model(self):
        return self.model_name

    @model.setter
    def model(self, value):
        if value != self.model_name:
            self.model_name = value
            self.provider = get_llm_provider(value)

    def analyze(self, ticker: str, market_data: dict) -> dict:
        """
        Sends market data to the LLM and requests a structured analysis.
        """
        from app.services.tools import AVAILABLE_TOOLS, TOOL_MAP

        system_prompt = """You are a seasoned Wall Street technical analyst.
        Analyze the provided stock data. You have access to tools to fetch more context if needed.
        Output a strictly formatted JSON response.
        The JSON must have the following keys:
        1. "signal": One of "BUY", "SELL", or "HOLD".
        2. "risk_score": A number from 1 to 10 (1=Safe, 10=Extreme volatility/danger).
        3. "stop_loss": A suggested price level to exit the trade if it goes against us (based on support levels).
        4. "reasoning": A concise paragraph explaining why based on the indicators, risk profile, and news.
        Do not include any other text."""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Analyze {ticker} based on this data: {json.dumps(market_data)}"}
        ]

        try:
            # First call
            response = self.provider.chat_completion(
                messages=messages,
                tools=AVAILABLE_TOOLS,
                tool_choice="auto"
            )

            assistant_message = response.choices[0].message
            messages.append({"role": "assistant", "content": assistant_message.content or ""})

            # Handle tool calls
            tool_calls = getattr(assistant_message, 'tool_calls', None)
            
            # Simple fallback for tool calls embedded in content (for local models)
            if not tool_calls and assistant_message.content and "{" in assistant_message.content:
                try:
                    start = assistant_message.content.find('{')
                    end = assistant_message.content.rfind('}') + 1
                    possible_tool = json.loads(assistant_message.content[start:end])
                    if "name" in possible_tool and "parameters" in possible_tool:
                        from types import SimpleNamespace
                        tool_calls = [SimpleNamespace(
                            id=f"call_{ticker}_{int(datetime.now().timestamp())}",
                            function=SimpleNamespace(
                                name=possible_tool["name"],
                                arguments=json.dumps(possible_tool["parameters"])
                            )
                        )]
                except:
                    pass

            if tool_calls:
                for tool_call in tool_calls:
                    function_name = tool_call.function.name
                    function_to_call = TOOL_MAP.get(function_name)
                    if not function_to_call: continue

                    function_args = json.loads(tool_call.function.arguments)
                    
                    import inspect
                    sig = inspect.signature(function_to_call)
                    filtered_args = {k: v for k, v in function_args.items() if k in sig.parameters}
                    
                    print(f"Agent decided to call tool: {function_name}({filtered_args})")
                    function_response = function_to_call(**filtered_args)
                    
                    messages.append({
                        "tool_call_id": getattr(tool_call, 'id', 'call_manual'),
                        "role": "tool",
                        "name": function_name,
                        "content": json.dumps(function_response),
                    })
                
                # Second call for final analysis
                second_response = self.provider.chat_completion(messages=messages)
                content = second_response.choices[0].message.content
            else:
                content = assistant_message.content

            if not content or "{" not in content:
                 messages.append({"role": "user", "content": "Analyze and provide a JSON response with 'signal', 'risk_score', 'stop_loss', and 'reasoning' keys."})
                 final_res = self.provider.chat_completion(messages=messages)
                 content = final_res.choices[0].message.content

            # Robust JSON extraction
            try:
                start = content.find('{')
                end = content.rfind('}') + 1
                if start != -1 and end != 0:
                    json_str = content[start:end]
                    parsed = json.loads(json_str)
                    if "signal" in parsed:
                        # Ensure numeric types for risk and SL
                        if "risk_score" in parsed:
                            try: parsed["risk_score"] = float(parsed["risk_score"])
                            except: parsed["risk_score"] = 5
                        if "stop_loss" in parsed:
                            try: parsed["stop_loss"] = float(parsed["stop_loss"])
                            except: parsed["stop_loss"] = 0
                        return parsed
                
                raise ValueError("JSON missing signal key")
            except:
                if "BUY" in content.upper(): return {"signal": "BUY", "reasoning": content[:200]}
                if "SELL" in content.upper(): return {"signal": "SELL", "reasoning": content[:200]}
                return {"signal": "HOLD", "reasoning": content[:200]}
            
        except Exception as e:
            print(f"LLM Analysis Failed: {e}")
            return {
                "signal": "HOLD",
                "reasoning": f"Analysis failed fundamentally: {str(e)}.",
                "error": str(e)
            }
