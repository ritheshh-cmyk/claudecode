# Lessons learned

- **Gemini Free Tier Model Quota**: Some preview/older model names (e.g., `gemini-2.0-flash-lite-001`) have their Free Tier quota disabled (`limit: 0`) in Google AI Studio, yielding immediate HTTP 429 responses. Remap these to stable versions (e.g., `gemini-2.5-flash` or `gemini-2.0-flash`) which support active free quotas.
- **Proxy Retry Hangs**: When a provider returns a 429, the proxy's built-in retry logic with exponential backoff will attempt multiple retries, causing the client/server connection to hang. If a connection is hanging, inspect `server.log` to see if retries are active.
- **OpenCode Model Naming**: OpenCode Zen's API endpoint expects direct model IDs (e.g. `deepseek-v4-flash-free`) without the `opencode/` slug prefix. The proxy handles stripping the prefix internally, but direct API tests should omit the prefix.
- **OpenCode Credits limits**: Paid models on OpenCode Zen return `401 CreditsError` if no payment method/billing info is set. Fallback to free models (e.g. `deepseek-v4-flash-free`) to test without credits.
