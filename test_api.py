import requests
import os
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv('OPENROUTER_API_KEY')
print(f'API Key: {api_key}')

# Try minimal headers first
headers = {
    'Authorization': f'Bearer {api_key}',
}

payload = {
    'model': 'openai/gpt-3.5-turbo',
    'messages': [{'role': 'user', 'content': 'Hello'}],
}

print('Sending test request with minimal headers...')
try:
    response = requests.post(
        'https://openrouter.io/api/v1/chat/completions',
        json=payload,
        headers=headers,
        timeout=10
    )
    print(f'Status: {response.status_code}')
    print(f'Headers: {dict(response.headers)}')
    print(f'Response text: {response.text}')
    print(f'JSON: {response.json() if response.text else "empty"}')
except Exception as e:
    print(f'Error: {e}')
    import traceback
    traceback.print_exc()
