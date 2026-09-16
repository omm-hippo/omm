# Security, privacy, and network controls implementation plan

1. Add a network-policy module, persisted setting, global `--offline`
   override, request guard, and explicit Git/package-manager/upload gates.
   Preserve loopback traffic and add cache/fallback guidance.
2. Add secure credential storage and auth commands. Wire Hugging Face and LM
   Studio requests to environment-first token lookup without logging secrets.
3. Add listener/process inspection and OMM-owned Ollama receipts. Add the
   read-only security view and the narrowly owned local-only repair flow.
4. Add the support-report allow-list builder, preview/save CLI, and docs.
5. Add install plan/result cards while reusing provider metadata and enforcing
   known sizes without changing contribution/non-interactive output.
6. Add focused tests for network modes, secure-backend rejection and token
   precedence, listener ownership, support-report exclusions, source-card
   wording, cached offline install, and legacy security invariants.
7. Run focused tests, the broader relevant suite, encoding/diff checks, then
   review the exact branch diff against current `origin/main` before signing
   a conventional commit and opening a Korean four-section PR.
