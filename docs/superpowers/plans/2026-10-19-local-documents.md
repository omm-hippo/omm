# Implementation plan

1. Add bounded extraction, atomic SQLite document/index/history operations, and deterministic local retrieval.
2. Reuse worker serialization and local runtime ownership for source-grounded questions.
3. Add a Documents tab with upload/replace/delete, model selection, persisted answers and exact quotations.
4. Run focused tests, real isolated loopback inference, desktop/mobile browser verification, persistence and deletion checks.
5. Update privacy/usage and release dependency contracts; create a main-target PR with explicit verification limits.
