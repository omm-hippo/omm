"""Drawer for the poster art worker. Runs in the poster venv, never imports omm.

    <venv>/bin/python -I poster_draw.py --model-path SNAPSHOT --model flux2-klein-4b --quantize 8 --steps 4

Loads the model and prints {"ready": true, "seconds": ...}. Then it reads ONE
stdin line {"prompt", "width", "height", "seed", "out", "quality"}, draws that
picture, writes it to `out` as JPEG, prints {"ok": true, "seconds": ...} or
{"ok": false, "error": <exception class name>}, and exits. The error never
carries the exception message, which can quote the prompt.

One picture per process: like mflux's own CLI, the text encoder (~9 GB) is
dropped once the prompt is encoded, so the peak is 13.7 GB instead of 23.1 GB
with everything kept (Node1, 992x736, 2026-11-07), but a second prompt would
need it back. Starting fresh costs about 4 s per picture.
"""

from __future__ import annotations

import argparse
import json
import sys
import time


def _say(reply: dict) -> None:
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--quantize", type=int, required=True)
    parser.add_argument("--steps", type=int, required=True)
    args = parser.parse_args()

    started = time.monotonic()
    from mflux.callbacks.instances.memory_saver import MemorySaver
    from mflux.models.common.config.model_config import ModelConfig
    from mflux.models.flux2.variants import Flux2Klein

    model = Flux2Klein(model_config=ModelConfig.from_name(args.model), quantize=args.quantize,
                       model_path=args.model_path)
    # What mflux's own CLI does: drop the text encoder once the prompt is encoded.
    model.callbacks.register(MemorySaver(model=model, keep_transformer=False, cache_limit_bytes=None))
    _say({"ready": True, "seconds": round(time.monotonic() - started, 2)})

    line = sys.stdin.readline()
    if line:
        try:
            request = json.loads(line)
            started = time.monotonic()
            generated = model.generate_image(
                seed=int(request["seed"]), prompt=str(request["prompt"]),
                width=int(request["width"]), height=int(request["height"]),
                num_inference_steps=args.steps, guidance=1.0, scheduler="flow_match_euler_discrete")
            generated.image.convert("RGB").save(request["out"], format="JPEG", quality=int(request["quality"]))
            _say({"ok": True, "seconds": round(time.monotonic() - started, 2)})
        except Exception as error:
            _say({"ok": False, "error": type(error).__name__})
    return 0


if __name__ == "__main__":
    sys.exit(main())
