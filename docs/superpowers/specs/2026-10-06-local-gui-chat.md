# Local GUI conversation

Add a chat action to managed models linked to Ollama or LM Studio. The GUI shows
incremental real responses, sends completed conversation turns as context,
supports cancellation, explicit model release, refresh/restart recovery and
local history deletion. Prompts and answers stay in local `web-chats` records;
no telemetry or remote AI provider is used. Messages do not execute tools.

Keep saved per-model load settings, preserve preloaded models and reject a new
load that would replace other active work. Use the authenticated same-origin
API, bounded text and idempotent requests. Model-management jobs and an active
chat cannot mutate the runtime concurrently. Only release a load owned by this
GUI. A server restart never automatically reloads an old conversation.

Verify real multi-turn response, incremental display, interruption, model
release, persisted history and reopening in an isolated owned runtime. Report
LM Studio fixtures separately from real Ollama/macOS verification.
