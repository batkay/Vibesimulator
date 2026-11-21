"""Touchstone export of the single-ended S matrix.

The file uses the simulated port impedance. Display Z0 is not written.
"""

from __future__ import annotations

from pathlib import Path


def write_touchstone(ntwk, path: str | Path) -> None:
    ntwk.write_touchstone(str(path))
