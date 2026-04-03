import requests
import json
import os
from typing import Optional, Callable, Any, List

class OpenRouterError(Exception):
    pass

class OpenRouterCompletion:
    BASE_URL = "https://openrouter.io/api/v1"
    
    @staticmethod
    def create(model: str, messages: List[dict], temperature: float = 0.7, stream: bool = False, functions: Optional[List] = None, function_call: Optional[dict] = None):
        """
        Create a chat completion using OpenRouter API
        """
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise OpenRouterError("OPENROUTER_API_KEY environment variable not set")
        
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/smol-ai/developer",
            "X-Title": "smol-developer"
        }
        
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        
        # Note: OpenRouter has limited function calling support
        # We'll ignore functions for now and just use the messages
        if functions:
            payload["functions"] = functions
        if function_call:
            payload["function_call"] = function_call
        
        url = f"{OpenRouterCompletion.BASE_URL}/chat/completions"
        
        if stream:
            response = requests.post(url, json=payload, headers=headers, stream=True)
            response.raise_for_status()
            return OpenRouterStreamingCompletion(response)
        else:
            response = requests.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()

class OpenRouterStreamingCompletion:
    def __init__(self, response):
        self.response = response
    
    def __aiter__(self):
        return self
    
    async def __anext__(self):
        line = self.response.iter_lines()
        for line_data in line:
            if line_data:
                line_str = line_data.decode('utf-8')
                if line_str.startswith('data: '):
                    try:
                        chunk = json.loads(line_str[6:])
                        yield chunk
                    except json.JSONDecodeError:
                        continue
        raise StopAsyncIteration
    
    def __iter__(self):
        for line_data in self.response.iter_lines():
            if line_data:
                line_str = line_data.decode('utf-8')
                if line_str.startswith('data: '):
                    try:
                        chunk = json.loads(line_str[6:])
                        yield chunk
                    except json.JSONDecodeError:
                        continue
