"""Machine-readable output, independent of terminal styling and width."""
from __future__ import annotations

import json
import sys


def write_document(data: object) -> None:
    # Serialize first: failures must not leave half a JSON document on stdout.
    # ASCII escapes also preserve Korean model names on cp949/cp1252 stdout.
    document = json.dumps(data, ensure_ascii=True, allow_nan=False, indent=2)
    sys.stdout.write(document + "\n")
