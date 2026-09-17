#!/usr/bin/env python3
"""Live Docker/Podman smoke verification for the coding sandbox."""

from __future__ import annotations

from omm import coding_eval


def main() -> None:
    pack = coding_eval.load_pack()
    runtime = coding_eval.find_runtime()
    coding_eval.ensure_runtime_available(runtime)
    coding_eval.ensure_image_available(runtime, pack.image)
    task = next(task for task in pack.tasks if task.id == "generation_chunks")
    valid = """def chunks(values, size):
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("size")
    return [list(values[index:index + size]) for index in range(0, len(values), size)]
"""
    result = coding_eval.run_in_sandbox(valid, task, pack, runtime=runtime)
    if result.outcome != "completed" or result.tests_passed != task.test_count:
        raise SystemExit(f"valid sandbox fixture failed: {result.outcome} {result.output[-500:]}")

    network_attempt = """import socket
socket.create_connection(("1.1.1.1", 53), timeout=1)
def chunks(values, size):
    return []
"""
    blocked = coding_eval.run_in_sandbox(network_attempt, task, pack, runtime=runtime)
    if blocked.outcome == "completed":
        raise SystemExit("network-capable generated code unexpectedly passed")
    print("coding sandbox OK: valid code passed and outbound network attempt failed")


if __name__ == "__main__":
    main()
