import os
import json
from abc import ABC, abstractmethod
from openai import OpenAI
import google.generativeai as genai

class BaseLLMProvider(ABC):
    @abstractmethod
    def chat_completion(self, messages: list, tools: list = None, tool_choice: str = "auto") -> dict:
        pass

class OllamaProvider(BaseLLMProvider):
    def __init__(self, model: str = "llama3"):
        self.model = model
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        # Ensure base_url ends with /v1 for OpenAI client compatibility
        if base_url.endswith("/"):
            base_url = base_url.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
            
        self.client = OpenAI(base_url=base_url, api_key="ollama")

    def chat_completion(self, messages: list, tools: list = None, tool_choice: str = "auto") -> dict:
        try:
            params = {
                "model": self.model,
                "messages": messages
            }
            if tools:
                params["tools"] = tools
                params["tool_choice"] = tool_choice
                
            response = self.client.chat.completions.create(**params)
            return response
        except Exception as e:
            # Fallback if tools not supported or 400 Bad Request (often caused by tools/tool_choice on some versions)
            err_str = str(e).lower()
            if "support tools" in err_str or "tool_choice" in err_str or "400" in err_str:
                return self.client.chat.completions.create(model=self.model, messages=messages)
            raise e

class GeminiProvider(BaseLLMProvider):
    def __init__(self, model: str = "gemini-2.0-flash"):
        self.model_name = model
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not found in environment")
        genai.configure(api_key=api_key)
        self.model = genai.GenerativeModel(model)

    def chat_completion(self, messages: list, tools: list = None, tool_choice: str = "auto") -> dict:
        # Convert OpenAI-style messages to Gemini
        contents = []
        system_instruction = None
        for m in messages:
            if m["role"] == "system":
                system_instruction = m["content"]
            elif m["role"] == "user":
                contents.append({"role": "user", "parts": [m["content"]]})
            elif m["role"] == "assistant":
                contents.append({"role": "model", "parts": [m["content"]]})
            elif m["role"] == "tool":
                # Gemini tool handling is different, but for now we focus on basic chat
                contents.append({"role": "user", "parts": [f"Tool response ({m.get('name')}): {m['content']}"]})

        if system_instruction:
            self.model = genai.GenerativeModel(self.model_name, system_instruction=system_instruction)

        # Basic generation (simplified tool support for now)
        response = self.model.generate_content(contents)
        
        # Mocking OpenAI response structure for compatibility
        from types import SimpleNamespace
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content=response.text,
                    role="assistant",
                    tool_calls=None
                )
            )]
        )

def get_llm_provider(model: str) -> BaseLLMProvider:
    if "gemini" in model.lower():
        return GeminiProvider(model)
    else:
        return OllamaProvider(model)
