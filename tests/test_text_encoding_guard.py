"""Make the CI text-encoding gate part of ordinary local pytest runs too."""
from pathlib import Path
import subprocess
import sys


def test_repository_text_encoding_contract():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "check_text_encoding.py")],
        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
