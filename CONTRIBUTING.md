# Contributing to omm

Thanks for taking the time to contribute. This guide covers how to get set up,
what to check before opening a PR, and how the project is organized.

## Getting started

```sh
git clone https://github.com/minigu5/Omm.git
cd Omm
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Run the test suite:

```sh
pytest -q
```

Verify the CLI entry point works:

```sh
omm --help
```

## Project layout

- `src/omm/` — the `omm` CLI package (installer, search, hardware detection,
  linking into LM Studio/Ollama/etc.)
- `src/localfit_server/` — the optional telemetry/benchmark collection server
- `scripts/` — training, migration, and maintenance scripts
- `tests/` — pytest suite; mirrors the structure of `src/`
- `docs/` — design notes and validation evidence
- `published/` — the currently published recommendation model artifact

## Before opening a pull request

1. **Add or update tests** for any behavior change. This repo treats test
   coverage as part of the change, not an afterthought.
2. **Run the full suite locally**: `pytest -q`. CI also runs on Ubuntu,
   Windows, and macOS, plus a Docker build and a bare-install check (no dev
   extras) — a change that only works in your local venv may still fail CI.
3. **Don't touch `published/recommend-model.json` by hand.** That artifact is
   produced by the training pipeline (`scripts/train_model.py`) and gated by
   the quality checks described in the README; hand edits will be
   overwritten or rejected.
4. **Keep changes scoped.** Prefer a focused PR over a large one that mixes
   unrelated fixes, refactors, and features — it's easier to review and to
   revert if something breaks.
5. **Match existing style.** No enforced formatter/linter is currently wired
   into CI, so follow the conventions already present in the file you're
   editing.

## Commit messages

Keep the subject line short and focused on *why* the change was made, not a
restatement of the diff. Conventional prefixes (`feat:`, `fix:`, `docs:`,
`refactor:`, `test:`) are used throughout the history and are appreciated but
not strictly required.

## Reporting bugs / requesting features

Open a GitHub issue using the provided templates. For bugs, include your OS,
Python version, `omm` version (`omm --version` or the version line from bare
`omm`), and the exact command that failed.

## Security issues

Please do not open a public issue for a suspected security vulnerability.
See [SECURITY.md](SECURITY.md) for how to report it privately.

## License

By contributing, you agree that your contributions will be licensed under the
project's [MIT License](LICENSE).
