"""Read and write ``.sim/pdn``. This does not touch ``.sim/current``."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np

from .circuit import PdnResult


def pdn_dir(board_path: Path) -> Path:
    return Path(board_path).parent / ".sim" / "pdn"


def pdn_em_dir(board_path: Path) -> Path:
    """Field job. Separate from the DC result and from the S-parameter job."""
    return Path(board_path).parent / ".sim" / "pdn-em"


def save_result(directory: Path, result: PdnResult) -> None:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    names = [str(drop["name"]) for drop in result.drops]
    z_names = [str(name) for name in result.impedance]
    nfreq = int(np.asarray(result.freq_hz).size)
    np.savez(
        directory / "result.npz",
        freq=np.asarray(result.freq_hz, dtype=float),
        impedance=_stack(result.impedance, z_names, nfreq),
        impedance_bare=_stack(result.impedance_bare, z_names, nfreq),
        z_names=np.asarray(z_names, dtype="U"),
        drop_names=np.asarray(names, dtype="U"),
        loop_mv=_drop_column(result.drops, "loop_mv"),
        power_mv=_drop_column(result.drops, "power_mv"),
        ground_mv=_drop_column(result.drops, "ground_mv"),
        connected=np.asarray([bool(drop["connected"]) for drop in result.drops], dtype=bool),
        voltages=_cells(result.voltages),
        density=_cells(result.density),
        z_target=np.asarray([np.nan if result.z_target is None else float(result.z_target)]),
        **_field_arrays(result),
    )
    meta = {"ok": bool(result.ok), "message": result.message, "warnings": list(result.warnings)}
    (directory / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def load_result(directory: Path) -> PdnResult | None:
    directory = Path(directory)
    path = directory / "result.npz"
    if not path.is_file():
        return None
    try:
        with np.load(path, allow_pickle=False) as data:
            freq = np.asarray(data["freq"], dtype=float)
            z_names = [str(name) for name in np.asarray(data["z_names"]).reshape(-1)]
            drop_names = [str(name) for name in np.asarray(data["drop_names"]).reshape(-1)]
            impedance = _unstack(data["impedance"], z_names)
            impedance_bare = _unstack(data["impedance_bare"], z_names)
            loop = np.asarray(data["loop_mv"], dtype=float).reshape(-1)
            power = np.asarray(data["power_mv"], dtype=float).reshape(-1)
            ground = np.asarray(data["ground_mv"], dtype=float).reshape(-1)
            connected = np.asarray(data["connected"], dtype=bool).reshape(-1)
            voltages = _cell_list(data["voltages"])
            density = _cell_list(data["density"])
            target_raw = float(np.asarray(data["z_target"]).reshape(-1)[0])
            field_freq, field_z, field_decayed = _field_from(data)
    except (OSError, ValueError, KeyError):
        return None
    meta = {"ok": True, "message": "PDN finished.", "warnings": []}
    meta_path = directory / "meta.json"
    if meta_path.is_file():
        try:
            loaded = json.loads(meta_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass
    drops = []
    for index, name in enumerate(drop_names):
        is_on = bool(connected[index]) if index < connected.size else False
        drops.append(
            {
                "name": name,
                "loop_mv": _finite(loop, index) if is_on else None,
                "power_mv": _finite(power, index) if is_on else None,
                "ground_mv": _finite(ground, index) if is_on else None,
                "connected": is_on,
            }
        )
    target = None if not np.isfinite(target_raw) else target_raw
    return PdnResult(
        ok=bool(meta.get("ok", True)),
        message=str(meta.get("message", "PDN finished.")),
        drops=drops,
        voltages=voltages,
        density=density,
        freq_hz=freq,
        impedance=impedance,
        impedance_bare=impedance_bare,
        z_target=target,
        warnings=[str(item) for item in meta.get("warnings") or []],
        field_freq_hz=field_freq,
        field_impedance=field_z,
        field_decayed=field_decayed,
    )


def write_csv(path: str | Path, result: PdnResult) -> None:
    """Drop table, then every |Z| curve. Checks on the plot do not omit a column."""
    path = Path(path)
    names = [str(name) for name in result.impedance]
    freq = np.asarray(result.freq_hz, dtype=float)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["load", "loop_mV", "power_net_mV", "ground_net_mV", "connected"])
        for drop in result.drops:
            writer.writerow(
                [
                    drop["name"],
                    _csv_number(drop.get("loop_mv")),
                    _csv_number(drop.get("power_mv")),
                    _csv_number(drop.get("ground_mv")),
                    "yes" if drop.get("connected") else "no",
                ]
            )
        writer.writerow([])
        header = ["freq_Hz"]
        for name in names:
            header.append(f"|Z| {name} planes+VRM ohm")
            header.append(f"|Z| {name} with parts ohm")
        header.append("Ztarget_ohm")
        writer.writerow(header)
        target = "" if result.z_target is None else f"{result.z_target:.8g}"
        for index, frequency in enumerate(freq):
            row = [f"{float(frequency):.8g}"]
            for name in names:
                row.append(_csv_abs(result.impedance_bare.get(name), index))
                row.append(_csv_abs(result.impedance.get(name), index))
            row.append(target)
            writer.writerow(row)
        field = result.field_impedance or {}
        field_freq = np.asarray([] if result.field_freq_hz is None else result.field_freq_hz, dtype=float)
        if field and field_freq.size:
            writer.writerow([])
            header = ["field_freq_Hz"]
            header.extend(f"|Z| {name} field ohm" for name in field)
            writer.writerow(header)
            for index, frequency in enumerate(field_freq):
                row = [f"{float(frequency):.8g}"]
                row.extend(_csv_abs(values, index) for values in field.values())
                writer.writerow(row)


def _field_arrays(result: PdnResult) -> dict:
    freq = result.field_freq_hz
    curves = result.field_impedance
    if freq is None or not curves:
        return {}
    names = [str(name) for name in curves]
    count = int(np.asarray(freq).size)
    return {
        "field_freq": np.asarray(freq, dtype=float),
        "field_impedance": _stack(curves, names, count),
        "field_names": np.asarray(names, dtype="U"),
        "field_decayed": np.asarray([bool(result.field_decayed)]),
    }


def _field_from(data) -> tuple[np.ndarray | None, dict[str, np.ndarray] | None, bool]:
    files = set(getattr(data, "files", []))
    if "field_freq" not in files or "field_names" not in files or "field_impedance" not in files:
        return None, None, True
    names = [str(name) for name in np.asarray(data["field_names"]).reshape(-1)]
    decayed = True
    if "field_decayed" in files:
        decayed = bool(np.asarray(data["field_decayed"]).reshape(-1)[0])
    return np.asarray(data["field_freq"], dtype=float), _unstack(data["field_impedance"], names), decayed


def _stack(mapping: dict, names: list[str], nfreq: int) -> np.ndarray:
    if not names:
        return np.zeros((nfreq, 0), dtype=complex)
    columns = []
    for name in names:
        values = np.asarray(mapping.get(name, np.full(nfreq, np.nan)), dtype=complex).reshape(-1)
        if values.size != nfreq:
            values = np.full(nfreq, np.nan, dtype=complex)
        columns.append(values)
    return np.column_stack(columns)


def _unstack(array, names: list[str]) -> dict[str, np.ndarray]:
    values = np.asarray(array, dtype=complex)
    if values.ndim == 1:
        values = values.reshape(-1, 1)
    out = {}
    for index, name in enumerate(names):
        if values.shape[1] <= index:
            out[name] = np.full(values.shape[0], np.nan, dtype=complex)
        else:
            out[name] = values[:, index]
    return out


def _drop_column(drops: list[dict], key: str) -> np.ndarray:
    column = []
    for drop in drops:
        value = drop.get(key)
        column.append(np.nan if value is None else float(value))
    return np.asarray(column, dtype=float)


def _cells(cells) -> np.ndarray:
    if not cells:
        return np.zeros((0, 4), dtype=float)
    return np.asarray(cells, dtype=float).reshape(-1, 4)


def _cell_list(array) -> list[tuple[float, float, float, float]]:
    values = np.asarray(array, dtype=float)
    if values.size == 0:
        return []
    values = values.reshape(-1, 4)
    return [tuple(float(item) for item in row) for row in values]


def _finite(column: np.ndarray, index: int):
    if index >= column.size:
        return None
    value = float(column[index])
    if not np.isfinite(value):
        return None
    return value


def _csv_number(value) -> str:
    if value is None:
        return ""
    return f"{float(value):.8g}"


def _csv_abs(column, index: int) -> str:
    if column is None:
        return ""
    values = np.asarray(column)
    if index >= values.size:
        return ""
    value = values[index]
    if not np.isfinite(value):
        return ""
    return f"{abs(complex(value)):.8g}"
