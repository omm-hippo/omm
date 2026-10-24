"""Stable PyInstaller entry point for npm standalone packages."""

import multiprocessing

from omm.cli import main


if __name__ == "__main__":
    # `omm contribute` benchmarks in a spawned worker process
    # (quality.evaluate_model_isolated). A frozen build starts that worker
    # by re-running this executable with `--multiprocessing-fork ...`;
    # without this call the worker runs the omm CLI on those arguments,
    # fails with "No such option", and every benchmark fails.
    multiprocessing.freeze_support()
    main()
