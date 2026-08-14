# Local runtime compatibility verification

`omm verify MODEL [--engine ollama|lmstudio] [--keep-loaded]` verifies that an
OMM-managed model is visible to a running local server, can be loaded, and can
return non-empty text for a short deterministic probe.

The verifier uses only loopback origins:

- Ollama: `http://127.0.0.1:11434`
- LM Studio native v1 API: `http://127.0.0.1:1234`

It does not start either server or GUI. LM Studio authentication is read from
the `LM_API_TOKEN` environment variable. Tokens, prompts, and generated text
are not written to configuration, the model registry, or logs.

## Lifecycle rules

1. Confirm server health and model visibility.
2. Preserve a model that was already loaded.
3. Ask before loading an unloaded model unless `--yes` was supplied.
4. Generate at most eight tokens with a bounded timeout.
5. Release only a model loaded by this verifier, unless `--keep-loaded` was
   explicitly supplied.
6. Store only the bounded result fields in `~/.omm/models.json`.

Failed verification never removes or unlinks the downloaded GGUF. A cleanup
failure is itself recorded as `unload_failed` rather than being hidden behind
an earlier generation error.

Regular interactive installs apply the same consent rule after linking. With
Ollama, the existing local speed benchmark doubles as the non-empty generation
proof and preserves a model that was already resident. When LM Studio is the
configured runtime, its native local probe is used instead. Scripts must pass
`--verify-runtime` to grant load consent or `--no-verify-runtime` to skip the
check without prompting.

## Stored result

```json
{
  "compatibility": {
    "ollama": {
      "status": "passed",
      "checked_at": "2026-07-31T12:00:00+00:00",
      "probe_version": 1,
      "runtime_version": "0.12.6",
      "failure_reason": null
    }
  }
}
```

Failure reasons are limited to `server_unavailable`, `model_not_visible`,
`load_failed`, `out_of_memory`, `generation_timeout`, `empty_response`,
`unload_failed`, `unsupported_runtime`, and `unknown`.
