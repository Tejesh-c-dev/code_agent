import requests

# Check if we can reach OpenRouter at all
print("Checking OpenRouter API status...")
try:
    response = requests.get('https://openrouter.ai/api/v1/models', timeout=10)
    print(f'Models endpoint - Status: {response.status_code}')
    if response.text:
        print(f'Models response (first 300 chars): {response.text[:300]}')
except Exception as e:
    print(f'Error accessing models endpoint: {e}')

# Try POST with correct headers
print('\nTrying chat completions with correct .ai domain...')
api_key = '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'
headers = {
    'Authorization': f'Bearer {api_key}',
    'Content-Type': 'application/json'
}
payload = {
    'model': 'openai/gpt-4o-mini', 
    'messages': [{'role': 'user', 'content': 'Say hello'}]
}

try:
    response = requests.post(
        'https://openrouter.ai/api/v1/chat/completions', 
        json=payload, 
        headers=headers, 
        timeout=10
    )
    print(f'Chat completions - Status: {response.status_code}')
    print(f'Response: {response.text}')
    if response.status_code == 200:
        print("✅ SUCCESS! API is working!")
        result = response.json()
        print(f"Message: {result['choices'][0]['message']['content']}")
except Exception as e:
    print(f'Error: {e}')
    import traceback
    traceback.print_exc()
