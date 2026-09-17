# Model comparison and measured quality implementation plan

1. Add pure compare resolution/ranking models and tests; wire a read-only CLI
   command with JSON and concise text output.
2. Add a bounded v2 evaluation-pack loader and a Python coding smoke pack.
3. Add Docker/Podman sandbox execution and model-response parsing with security
   regression fixtures.
4. Add `omm evaluate` for installed Ollama models, local evidence output, and no
   upload by default.
5. Add signed quality-artifact loading and exact evidence matching; expose
   `MEASURED FOR` in compare without penalising missing data.
6. Regenerate command docs, update README/privacy documentation, run focused and
   full isolated tests, perform available container/physical checks, then update
   the existing main-target PR with one Korean description.
