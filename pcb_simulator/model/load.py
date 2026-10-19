"""Pick a board importer from a path.

KiCad `.kicad_pcb` is read directly. Altium and Xpedition are not: open the
ODB++ job or IPC-2581 file those tools export. An ODB++ job may be a folder
or a .zip, .tar, .tgz, or .tar.gz archive.
"""

from __future__ import annotations

import os
import tarfile
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from .board import Board
from .ipc2581 import load_ipc2581
from .kicad import load_kicad_pcb
from .odb import find_job_root, load_odb

NATIVE_MESSAGE = (
    "Altium .PcbDoc and Xpedition .pcb files are not read directly. "
    "Export ODB++ or IPC-2581 and open that instead. "
    "In Altium: File → Fabrication Outputs → ODB++ or IPC-2581. "
    "In Xpedition: File → Export → ODB++ or IPC-2581."
)

BOARD_FILTER = (
    "Boards (*.kicad_pcb *.xml *.cvg *.ipc2581 *.zip *.tgz *.tar *.tar.gz *.PcbDoc *.pcb);;"
    "KiCad (*.kicad_pcb);;"
    "IPC-2581 (*.xml *.cvg *.ipc2581);;"
    "ODB++ archive (*.zip *.tgz *.tar *.tar.gz);;"
    "Altium/Xpedition native (*.PcbDoc *.pcb);;"
    "All files (*.*)"
)


def load_board(path: str | Path) -> Board:
    path = Path(path)
    if path.is_dir():
        return load_odb(path)
    if not path.exists():
        raise FileNotFoundError(path)
    name = path.name.lower()
    if name.endswith(".kicad_pcb"):
        return load_kicad_pcb(path)
    if name.endswith(".pcbdoc"):
        raise ValueError(NATIVE_MESSAGE)
    if name.endswith(".pcb"):
        raise ValueError(NATIVE_MESSAGE)
    if name.endswith((".xml", ".cvg", ".ipc2581")):
        return load_ipc2581(path)
    if name.endswith((".zip", ".tgz", ".tar", ".tar.gz")):
        return _load_archive(path)
    if name.endswith(".gz"):
        return _load_gzip(path)
    return _sniff(path)


def _sniff(path: Path) -> Board:
    data = path.read_bytes()[:8000]
    if data.startswith(b"PK"):
        return _load_archive(path)
    head = data.decode("utf-8", "replace")
    if "kicad_pcb" in head:
        return load_kicad_pcb(path)
    if "IPC-2581" in head or "IPC2581" in head:
        return load_ipc2581(path)
    if tarfile.is_tarfile(path):
        return _load_archive(path)
    raise ValueError(
        "Unrecognized board file. Open a KiCad board, an IPC-2581 file, "
        "or an ODB++ folder or archive."
    )


def _load_gzip(path: Path) -> Board:
    import gzip

    if tarfile.is_tarfile(path):
        return _load_archive(path)
    data = gzip.decompress(path.read_bytes())
    head = data[:8000].decode("utf-8", "replace")
    suffix = ".kicad_pcb" if "kicad_pcb" in head else ".xml"
    with TemporaryDirectory(prefix="pcb-sim-") as tmp:
        dest = Path(tmp) / f"board{suffix}"
        dest.write_bytes(data)
        if suffix == ".kicad_pcb":
            return load_kicad_pcb(dest)
        return load_ipc2581(dest)


def _load_archive(path: Path) -> Board:
    with TemporaryDirectory(prefix="pcb-sim-") as tmp:
        dest = Path(tmp)
        _extract(path, dest)
        root = find_job_root(dest)
        if root is not None:
            return load_odb(root)
        ipc = _find_ipc(dest)
        if ipc is not None:
            return load_ipc2581(ipc)
    raise ValueError("Archive does not contain an ODB++ job (matrix/matrix) or an IPC-2581 file.")


def _find_ipc(dest: Path) -> Path | None:
    for directory, _dirnames, filenames in os.walk(dest):
        for name in filenames:
            if not name.lower().endswith((".xml", ".cvg", ".ipc2581")):
                continue
            candidate = Path(directory) / name
            head = candidate.read_text(encoding="utf-8", errors="replace")[:4000]
            if "IPC-2581" in head or "IPC2581" in head:
                return candidate
    return None


def _extract(path: Path, dest: Path) -> None:
    name = path.name.lower()
    if name.endswith(".zip") or zipfile.is_zipfile(path):
        if not zipfile.is_zipfile(path):
            raise ValueError(f"Not a zip archive: {path.name}")
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                _reject(dest, info.filename)
            archive.extractall(dest)
        return
    if not tarfile.is_tarfile(path):
        raise ValueError(f"Not an ODB++ archive: {path.name}")
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            if member.issym() or member.islnk():
                raise ValueError(f"Archive entry escapes the extract folder: {member.name}")
            _reject(dest, member.name)
        archive.extractall(dest)


def _reject(dest: Path, name: str) -> None:
    pure = Path(name)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"Archive entry escapes the extract folder: {name}")
    target = (dest / name).resolve()
    try:
        target.relative_to(dest.resolve())
    except ValueError as exc:
        raise ValueError(f"Archive entry escapes the extract folder: {name}") from exc
