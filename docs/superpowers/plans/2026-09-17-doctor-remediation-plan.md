# Doctor remediation and disk-space wording plan

1. Add a frozen remediation value object to `doctor.py`, expose it through
   actionable checks, and deduplicate report actions.
2. Attach safe source-aware, registry, Ollama, install-recovery, and runtime
   cleanup guidance without changing doctor exit codes or read-only behavior.
3. Render a final `How to fix` section and include structured remediation in
   `--json` output.
4. Split onboarding's OMM home path from disk availability and calculate
   decimal GB instead of mislabelled GiB.
5. Add focused tests for JSON compatibility, command quoting, deduplication,
   read-only output, diagnostic mappings, and decimal disk units.
6. Update README and generated command documentation, then run focused and
   full cross-feature validation before creating a main-target PR.
