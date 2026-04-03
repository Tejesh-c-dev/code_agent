# OpenRouter API Issues

## Errors Encountered:
1. **405 Method Not Allowed** on `/api/v1/chat/completions` endpoint
2. **Models endpoint redirecting** to landing page instead of returning API data
3. **Empty response bodies** from API calls

## Possible Causes:
- OpenRouter API key may have expired or is invalid
- Free trial credits may be depleted
- API key may not have proper permissions
- OpenRouter service might be down or have changed their API structure

## Troubleshooting Steps:

### Option 1: Verify/Regenerate API Key
1. Go to https://openrouter.ai/keys
2. Check if your key is still valid
3. Regenerate a new key if needed
4. Update it in `.env` file

### Option 2: Check Account Status
1. Visit https://openrouter.ai
2. Log in and check your account status
3. Verify you have credits remaining
4. Check if there are any usage limits or restrictions

### Option 3: Try Different Model
The default model `openai/gpt-3.5-turbo` might not be available.
Try:
- `openai/gpt-4-turbo`
- `openai/gpt-4`
- `anthropic/claude-3-opus`

### Option 4: Contact OpenRouter Support
If the key is valid, there might be a service issue.

## Current Setup:
- OpenRouter configured for requests library
- API key location: `.env` file
- Base URL: `https://openrouter.io/api/v1`
- Default model: `openai/gpt-4-turbo-preview`
