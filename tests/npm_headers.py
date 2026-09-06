"""Synthetic native-executable header builders shared by the npm packaging tests.

Used by both `tests/test_npm_package.py` and `tests/test_npm_binary.py` so the
two suites exercise the exact same fake Mach-O/ELF/PE bytes against the shared
`npm_package.binary_architecture` / `validate_binary_format` logic.
"""

from __future__ import annotations


def _macho(cputype: int, byteorder: str = "little") -> bytes:
    magic = bytes.fromhex("cffaedfe" if byteorder == "little" else "feedfacf")
    return magic + cputype.to_bytes(4, byteorder) + b" OMM Mach-O"


def _elf(machine: int, *, bits: int = 2) -> bytes:
    header = bytearray(64)
    header[0:4] = bytes.fromhex("7f454c46")
    header[4] = bits
    header[5] = 1
    header[16:18] = (2).to_bytes(2, "little")
    header[18:20] = machine.to_bytes(2, "little")
    return bytes(header)


def _pe(machine: int) -> bytes:
    header = bytearray(0x40)
    header[0:2] = b"MZ"
    header[0x3C:0x40] = (0x40).to_bytes(4, "little")
    return bytes(header) + bytes.fromhex("50450000") + machine.to_bytes(2, "little")


def _binary(target: str) -> bytes:
    """A minimal but honest executable header for one npm target."""
    return {
        "darwin-arm64": _macho(0x0100000C),
        "darwin-x64": _macho(0x01000007),
        "linux-arm64-gnu": _elf(0xB7),
        "linux-x64-gnu": _elf(0x3E),
        "win32-x64": _pe(0x8664),
    }[target]
