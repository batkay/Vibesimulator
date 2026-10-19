"""S, Z, and TDR plots. Renormalizing the display does not rerun the solver."""

from __future__ import annotations

import matplotlib

matplotlib.use("QtAgg")

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from .info_tip import info_button
from ..sim.postprocess import (
    curve_label,
    default_eps_eff,
    differential_s,
    distance_m,
    mixed_mode_networks,
    network,
    renormalize,
    tdr_impedance,
    tdr_view_window,
)


class ResultsView(QWidget):
    display_z0_changed = Signal(float)
    eps_eff_changed = Signal(float)
    export_csv_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw = None
        self._names: list[str] = []
        self._pairs: list = []
        self._excitations: list[dict] = []
        self._decayed = True
        self._show_s: set[tuple[int, int]] = set()
        self._show_diff: set[str] = {"Sdd", "Scc"}
        self.figure = Figure(figsize=(5.2, 4.0), layout="constrained")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.toolbar = NavigationToolbar2QT(self.canvas, self)
        self.tabs = QTabBar()
        for title in ("Magnitude", "Phase", "Smith", "Z", "TDR", "v(t)"):
            self.tabs.addTab(title)
        self.tabs.currentChanged.connect(self.redraw)
        self.port = QComboBox()
        self.port.currentIndexChanged.connect(self._on_port_changed)
        self.z0 = QDoubleSpinBox()
        self.z0.setRange(1.0, 500.0)
        self.z0.setDecimals(1)
        self.z0.setValue(50.0)
        self.z0.setSuffix(" Ω")
        self.z0.setToolTip(
            "Display reference for the single-ended plots. Differential curves use twice this value. "
            "Changing it renormalizes the plots and does not rerun the field solver."
        )
        self.z0.valueChanged.connect(self._on_z0)
        self.eps = QDoubleSpinBox()
        self.eps.setRange(1.0, 30.0)
        self.eps.setDecimals(3)
        self.eps.setValue(default_eps_eff(4.5))
        self.eps.setToolTip("Effective permittivity used only to draw the TDR distance axis. It is a constant-velocity estimate.")
        self.eps.valueChanged.connect(self._on_eps)
        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("color: #8a5a00;")
        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("Port"))
        port_row.addWidget(self.port, 1)
        scale_row = QVBoxLayout()
        scale_row.setContentsMargins(0, 0, 0, 0)
        scale_row.setSpacing(2)
        z_row = QHBoxLayout()
        z_row.setContentsMargins(0, 0, 0, 0)
        z_row.setSpacing(2)
        z_row.addWidget(QLabel("Display Z0"))
        z_row.addWidget(self.z0, 1)
        z_row.addWidget(
            info_button(
                "Display Z0",
                "Redraws the plots from the saved S matrix. Differential curves use twice this value. "
                "It does not rerun the solver.",
            )
        )
        e_row = QHBoxLayout()
        e_row.setContentsMargins(0, 0, 0, 0)
        e_row.setSpacing(2)
        e_row.addWidget(QLabel("εeff"))
        e_row.addWidget(self.eps, 1)
        e_row.addWidget(
            info_button(
                "εeff",
                "Scales the TDR distance axis only. Ohms and S stay as simulated.",
            )
        )
        scale_row.addLayout(z_row)
        scale_row.addLayout(e_row)
        self.terms = QListWidget()
        self.terms.setObjectName("termList")
        self.terms.setMaximumHeight(120)
        self.terms.setMinimumWidth(0)
        self.terms.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.terms.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.terms.itemChanged.connect(self._on_term)
        term_row = QHBoxLayout()
        term_row.setContentsMargins(0, 0, 0, 0)
        all_btn = QPushButton("All")
        all_btn.setToolTip("Show every S parameter.")
        all_btn.clicked.connect(self._check_all)
        refl_btn = QPushButton("Reflections")
        refl_btn.setToolTip("Show S11, S22, and the other reflections.")
        refl_btn.clicked.connect(self._check_reflections)
        csv_btn = QPushButton("CSV")
        csv_btn.setToolTip("Save every S, Z, TDR, and port waveform.")
        csv_btn.clicked.connect(self.export_csv_requested.emit)
        term_row.addWidget(all_btn)
        term_row.addWidget(refl_btn)
        term_row.addWidget(csv_btn)
        term_row.addWidget(
            info_button(
                "Curves",
                "S11 is the reflection at port 1. S21 is the wave leaving port 2 when port 1 is driven. "
                "Reflections start checked. All turns the rest on. Nothing here reruns the solver. "
                "CSV writes every S, Z, TDR, and port waveform.",
            )
        )
        term_row.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.banner)
        layout.addWidget(self.tabs)
        layout.addLayout(port_row)
        layout.addLayout(scale_row)
        layout.addWidget(self.terms)
        layout.addLayout(term_row)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas, 1)
        self._say("Run a simulation to plot S, Z, and TDR.")

    def show_results(self, results: dict, eps_eff: float) -> None:
        self._raw = network(results["freq"], results["S"], results["z0"])
        self._names = list(results["port_names"])
        self._excitations = list(results.get("excitations") or [])
        self._decayed = bool(results.get("decayed", True))
        count = self._raw.s.shape[1]
        self._show_s = {(i, i) for i in range(count)}
        self._show_diff = {"Sdd", "Scc"}
        self._rebuild_choices()
        self._fill_terms()
        self.eps.blockSignals(True)
        self.eps.setValue(float(eps_eff))
        self.eps.blockSignals(False)
        z0 = np.asarray(results["z0"], dtype=float).reshape(-1)
        if z0.size:
            self.z0.blockSignals(True)
            self.z0.setValue(float(np.real(z0[0])))
            self.z0.blockSignals(False)
        if self._decayed:
            self.banner.setText("")
        else:
            self.banner.setText(
                "The field did not decay to the energy limit. The curves are still shown; "
                "the late part of the TDR is unfinished."
            )
        self.redraw()

    def set_pairs(self, pairs) -> None:
        """Pairs are DiffPair values. Adding one does not require a new solve."""
        self._pairs = list(pairs)
        self._rebuild_choices()
        self._fill_terms()
        if self._raw is not None:
            self.redraw()

    def _rebuild_choices(self) -> None:
        previous = self.port.currentData()
        self.port.blockSignals(True)
        self.port.clear()
        if self._names:
            for index, name in enumerate(self._names):
                self.port.addItem(name, ("se", index))
        elif not self._pairs:
            self.port.addItem("P1", ("se", 0))
        for index, pair in enumerate(self._pairs):
            self.port.addItem(f"{pair.name}  {pair.positive}/{pair.negative}", ("diff", index))
        if previous is not None:
            for index in range(self.port.count()):
                if self.port.itemData(index) == previous:
                    self.port.setCurrentIndex(index)
                    break
        self.port.blockSignals(False)

    def _current_choice(self) -> tuple[str, int]:
        data = self.port.currentData()
        if isinstance(data, tuple) and len(data) == 2:
            return str(data[0]), int(data[1])
        return "se", max(self.port.currentIndex(), 0)

    def _resolved_pair(self, index: int):
        if index < 0 or index >= len(self._pairs):
            return None
        pair = self._pairs[index]
        if pair.positive not in self._names or pair.negative not in self._names:
            return None
        return pair, self._names.index(pair.positive), self._names.index(pair.negative)

    def clear(self) -> None:
        self._raw = None
        self._excitations = []
        self._show_s = set()
        self.banner.setText("")
        self.terms.blockSignals(True)
        self.terms.clear()
        self.terms.blockSignals(False)
        self._say("Run a simulation to plot S, Z, and TDR.")

    def _on_port_changed(self, *_args) -> None:
        self._fill_terms()
        self.redraw()

    def _fill_terms(self) -> None:
        self.terms.blockSignals(True)
        self.terms.clear()
        kind, _which = self._current_choice()
        if self._raw is not None and kind == "diff":
            for key, text in (("Sdd", "Sdd  differential reflection"), ("Scc", "Scc  common-mode reflection")):
                self._add_term(text, key, key in self._show_diff)
        elif self._raw is not None:
            count = self._raw.s.shape[1]
            for j in range(count):
                for i in range(count):
                    self._add_term(curve_label(i, j, self._names), (i, j), (i, j) in self._show_s)
        self.terms.blockSignals(False)

    def _add_term(self, text: str, key, checked: bool) -> None:
        item = QListWidgetItem(text)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setData(Qt.UserRole, key)
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        if isinstance(key, tuple):
            into = self._names[key[0]] if key[0] < len(self._names) else f"P{key[0] + 1}"
            driven = self._names[key[1]] if key[1] < len(self._names) else f"P{key[1] + 1}"
            if key[0] == key[1]:
                item.setToolTip(f"S{key[0] + 1}{key[1] + 1} is the reflection at {into}.")
            else:
                item.setToolTip(f"S{key[0] + 1}{key[1] + 1} is the wave leaving {into} when {driven} is driven.")
        self.terms.addItem(item)

    def _on_term(self, item: QListWidgetItem) -> None:
        key = item.data(Qt.UserRole)
        checked = item.checkState() == Qt.Checked
        if isinstance(key, tuple):
            if checked:
                self._show_s.add((int(key[0]), int(key[1])))
            else:
                self._show_s.discard((int(key[0]), int(key[1])))
        elif checked:
            self._show_diff.add(str(key))
        else:
            self._show_diff.discard(str(key))
        self.redraw()

    def _check_all(self) -> None:
        kind, _which = self._current_choice()
        if kind == "diff":
            self._show_diff = {"Sdd", "Scc"}
        elif self._raw is not None:
            count = self._raw.s.shape[1]
            self._show_s = {(i, j) for i in range(count) for j in range(count)}
        self._fill_terms()
        self.redraw()

    def _check_reflections(self) -> None:
        kind, _which = self._current_choice()
        if kind == "diff":
            self._show_diff = {"Sdd"}
        elif self._raw is not None:
            count = self._raw.s.shape[1]
            self._show_s = {(i, i) for i in range(count)}
        self._fill_terms()
        self.redraw()

    def _want(self, i: int, j: int) -> bool:
        return (i, j) in self._show_s

    def _want_diff(self, name: str) -> bool:
        return name in self._show_diff

    def simulated_network(self):
        """Network at the port impedance the solver used. Touchstone export uses this."""
        return self._raw

    def displayed_network(self):
        if self._raw is None:
            return None
        target = float(self.z0.value())
        current = float(np.real(np.asarray(self._raw.z0).reshape(-1)[0]))
        if abs(target - current) < 1e-6:
            return self._raw
        return renormalize(self._raw, target)

    def _on_z0(self, _value: float) -> None:
        self.display_z0_changed.emit(float(self.z0.value()))
        self.redraw()

    def _on_eps(self, _value: float) -> None:
        self.eps_eff_changed.emit(float(self.eps.value()))
        if self.tabs.tabText(self.tabs.currentIndex()) == "TDR":
            self.redraw()

    def redraw(self, *_args) -> None:
        ntwk = self.displayed_network()
        if ntwk is None:
            self._say("Run a simulation to plot S, Z, and TDR.")
            return
        title = self.tabs.tabText(self.tabs.currentIndex())
        kind, which = self._current_choice()
        pair = self._resolved_pair(which) if kind == "diff" else None
        if kind == "diff" and pair is None:
            self._say("This pair's ports are not in the loaded results.")
            return
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        index = which
        freq_ghz = np.asarray(ntwk.f, dtype=float) / 1e9
        try:
            if title == "Magnitude":
                if pair is None:
                    self._magnitude(ax, ntwk, freq_ghz)
                else:
                    self._differential_spectrum(ax, ntwk, freq_ghz, pair, degrees=False)
            elif title == "Phase":
                if pair is None:
                    self._phase(ax, ntwk, freq_ghz)
                else:
                    self._differential_spectrum(ax, ntwk, freq_ghz, pair, degrees=True)
            elif title == "Smith":
                self.figure.clear()
                if pair is None:
                    self._smith(index)
                else:
                    self._smith_differential(pair)
            elif title == "Z":
                if pair is None:
                    self._impedance(ax, ntwk, freq_ghz, index)
                else:
                    self._differential_z(ax, freq_ghz, pair)
            elif title == "TDR":
                if pair is None:
                    self._tdr(ax, ntwk, index)
                else:
                    self._tdr_differential(ax, pair)
            else:
                if pair is None:
                    self._voltage(ax, index)
                else:
                    ax.text(
                        0.5,
                        0.5,
                        "This tab is the single-ended port waveforms.\nDifferential impedance is on the TDR and Z tabs.",
                        ha="center",
                        va="center",
                    )
                    ax.set_axis_off()
        except Exception as exc:
            self.figure.clear()
            ax = self.figure.add_subplot(111)
            ax.text(0.5, 0.5, str(exc), ha="center", va="center", wrap=True)
            ax.set_axis_off()
        self.canvas.draw_idle()

    def _magnitude(self, ax, ntwk, freq_ghz) -> None:
        s = np.asarray(ntwk.s)
        n = s.shape[1]
        for i in range(n):
            for j in range(n):
                if not self._want(i, j):
                    continue
                mag = 20 * np.log10(np.maximum(np.abs(s[:, i, j]), 1e-15))
                ax.plot(freq_ghz, mag, label=f"|S{i + 1}{j + 1}|")
        self._style_or_empty(ax, "Frequency (GHz)", "Magnitude (dB)")

    def _phase(self, ax, ntwk, freq_ghz) -> None:
        s = np.asarray(ntwk.s)
        n = s.shape[1]
        for i in range(n):
            for j in range(n):
                if not self._want(i, j):
                    continue
                ax.plot(freq_ghz, np.angle(s[:, i, j], deg=True), label=f"S{i + 1}{j + 1}")
        self._style_or_empty(ax, "Frequency (GHz)", "Phase (degrees)")

    def _smith(self, _index: int) -> None:
        import skrf.plotting as plotting

        # plotting.smith draws on the axes it is given and returns None.
        ax = self.figure.add_subplot(111)
        plotting.smith(ax=ax, draw_labels=True)
        ntwk = self.displayed_network()
        s = np.asarray(ntwk.s)
        handles = []
        for i in range(s.shape[1]):
            for j in range(s.shape[2]):
                if not self._want(i, j):
                    continue
                curve = s[:, i, j]
                line, = ax.plot(curve.real, curve.imag, label=f"S{i + 1}{j + 1}")
                self._mark_smith_end(ax, curve, line.get_color())
                handles.append(line)
        if handles:
            ax.legend(handles=handles, fontsize=8)

    def _impedance(self, ax, ntwk, freq_ghz, _index: int) -> None:
        z = np.asarray(ntwk.z)
        n = z.shape[1]
        for i in range(n):
            for j in range(n):
                if not self._want(i, j):
                    continue
                ax.plot(freq_ghz, np.real(z[:, i, j]), label=f"Re Z{i + 1}{j + 1}")
                ax.plot(freq_ghz, np.imag(z[:, i, j]), linestyle="--", label=f"Im Z{i + 1}{j + 1}")
        self._style_or_empty(ax, "Frequency (GHz)", "Impedance (Ω)")

    def _tdr(self, ax, ntwk, index: int) -> None:
        time_s, impedance = tdr_impedance(ntwk, index)
        time_ns = np.asarray(time_s) * 1e9
        ax.plot(time_ns, impedance, color="#0b6e4f")
        t0, t1 = tdr_view_window(time_s, impedance)
        ax.set_xlim(t0 * 1e9, t1 * 1e9)
        visible = (np.asarray(time_s) >= t0) & (np.asarray(time_s) <= t1)
        shown = impedance[visible] if np.any(visible) else impedance
        lo = float(np.min(shown))
        hi = float(np.max(shown))
        pad = max(1.0, 0.15 * (hi - lo))
        ax.set_ylim(lo - pad, hi + pad)
        ax.set_xlabel("Time (ns)")
        ax.set_ylabel("Impedance (Ω)")
        ax.grid(True, alpha=0.3)
        eps = float(self.eps.value())

        def to_mm(t_ns):
            return distance_m(np.asarray(t_ns, dtype=float) * 1e-9, eps) * 1e3

        def to_ns(dist_mm):
            velocity = 299_792_458.0 / np.sqrt(eps)
            return np.asarray(dist_mm, dtype=float) * 1e-3 * 2 / velocity * 1e9

        top = ax.secondary_xaxis("top", functions=(to_mm, to_ns))
        top.set_xlabel("Distance (mm), constant-velocity estimate")
        ax.set_title(self._names[index] if index < len(self._names) else "")

    def _style_axes(self, ax, xlabel: str, ylabel: str) -> None:
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.3)
        if ax.get_lines():
            ax.legend(fontsize=8)

    def _style_or_empty(self, ax, xlabel: str, ylabel: str) -> None:
        if not ax.get_lines():
            ax.text(0.5, 0.5, "Check a curve to show it.", ha="center", va="center")
            ax.set_axis_off()
            return
        self._style_axes(ax, xlabel, ylabel)

    def _differential_spectrum(self, ax, ntwk, freq_ghz, resolved, degrees: bool) -> None:
        _pair, positive, negative = resolved
        sdd, scc = differential_s(ntwk.s, positive, negative)
        if degrees:
            if self._want_diff("Sdd"):
                ax.plot(freq_ghz, np.angle(sdd, deg=True), label="Sdd")
            if self._want_diff("Scc"):
                ax.plot(freq_ghz, np.angle(scc, deg=True), label="Scc")
            self._style_or_empty(ax, "Frequency (GHz)", "Phase (degrees)")
        else:
            if self._want_diff("Sdd"):
                ax.plot(freq_ghz, 20 * np.log10(np.maximum(np.abs(sdd), 1e-15)), label="|Sdd|")
            if self._want_diff("Scc"):
                ax.plot(freq_ghz, 20 * np.log10(np.maximum(np.abs(scc), 1e-15)), label="|Scc|")
            self._style_or_empty(ax, "Frequency (GHz)", "Magnitude (dB)")

    def _smith_differential(self, resolved) -> None:
        import skrf.plotting as plotting

        pair, positive, negative = resolved
        ax = self.figure.add_subplot(111)
        plotting.smith(ax=ax, draw_labels=True)
        ntwk = self.displayed_network()
        sdd, scc = differential_s(ntwk.s, positive, negative)
        handles = []
        if self._want_diff("Sdd"):
            line, = ax.plot(np.real(sdd), np.imag(sdd), label=f"Sdd {pair.name}")
            self._mark_smith_end(ax, sdd, line.get_color())
            handles.append(line)
        if self._want_diff("Scc"):
            line, = ax.plot(np.real(scc), np.imag(scc), label=f"Scc {pair.name}")
            self._mark_smith_end(ax, scc, line.get_color())
            handles.append(line)
        if handles:
            ax.legend(handles=handles, fontsize=8)

    def _mark_smith_end(self, ax, curve, color: str) -> None:
        """Dot the last finite frequency so the end of the sweep is visible."""
        values = np.asarray(curve)
        finite = np.isfinite(values.real) & np.isfinite(values.imag)
        if not np.any(finite):
            return
        last = int(np.flatnonzero(finite)[-1])
        ax.plot(values.real[last], values.imag[last], marker="o", color=color, markersize=4, linestyle="none")

    def _differential_z(self, ax, freq_ghz, resolved) -> None:
        _pair, positive, negative = resolved
        diff, common = mixed_mode_networks(self.displayed_network(), positive, negative)
        if self._want_diff("Sdd"):
            z = np.asarray(diff.z[:, 0, 0])
            ax.plot(freq_ghz, np.real(z), label="Re Zdiff")
            ax.plot(freq_ghz, np.imag(z), linestyle="--", label="Im Zdiff")
        if self._want_diff("Scc"):
            zc = np.asarray(common.z[:, 0, 0])
            ax.plot(freq_ghz, np.real(zc), label="Re Zcc")
            ax.plot(freq_ghz, np.imag(zc), linestyle="--", label="Im Zcc")
        self._style_or_empty(ax, "Frequency (GHz)", "Impedance (Ω)")

    def _tdr_differential(self, ax, resolved) -> None:
        pair, positive, negative = resolved
        ntwk = self.displayed_network()
        diff, common = mixed_mode_networks(ntwk, positive, negative)
        time_s, z_diff = tdr_impedance(diff, 0)
        _t_common, z_common = tdr_impedance(common, 0)
        time_ns = np.asarray(time_s) * 1e9
        if self._want_diff("Sdd"):
            ax.plot(time_ns, z_diff, color="#0b6e4f", label="Zdiff")
        if self._want_diff("Scc"):
            ax.plot(time_ns, z_common, color="#1d4e89", label="Zcommon")
        if not ax.get_lines():
            ax.text(0.5, 0.5, "Check a curve to show it.", ha="center", va="center")
            ax.set_axis_off()
            return
        t0, t1 = tdr_view_window(time_s, z_diff)
        ax.set_xlim(t0 * 1e9, t1 * 1e9)
        visible = (np.asarray(time_s) >= t0) & (np.asarray(time_s) <= t1)
        shown = np.concatenate([z_diff[visible], z_common[visible]]) if np.any(visible) else np.concatenate([z_diff, z_common])
        lo = float(np.min(shown))
        hi = float(np.max(shown))
        pad = max(1.0, 0.15 * (hi - lo))
        ax.set_ylim(lo - pad, hi + pad)
        ax.set_xlabel("Time (ns)")
        ax.set_ylabel("Impedance (Ω)")
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=8)
        eps = float(self.eps.value())

        def to_mm(t_ns):
            return distance_m(np.asarray(t_ns, dtype=float) * 1e-9, eps) * 1e3

        def to_ns(dist_mm):
            velocity = 299_792_458.0 / np.sqrt(eps)
            return np.asarray(dist_mm, dtype=float) * 1e-3 * 2 / velocity * 1e9

        top = ax.secondary_xaxis("top", functions=(to_mm, to_ns))
        top.set_xlabel("Distance (mm), constant-velocity estimate")
        ax.set_title(f"{pair.name}  {pair.positive}/{pair.negative}")

    def _voltage(self, ax, index: int) -> None:
        if not self._excitations:
            ax.text(0.5, 0.5, "No time-domain samples in this result.", ha="center", va="center")
            ax.set_axis_off()
            return
        index = min(index, len(self._excitations) - 1)
        sample = self._excitations[index]
        time_ns = np.asarray(sample["t"], dtype=float) * 1e9
        voltage = np.asarray(sample["v"], dtype=float)
        if voltage.ndim == 1:
            ax.plot(time_ns, voltage, label=self._names[index] if index < len(self._names) else "v")
        else:
            for row in range(voltage.shape[0]):
                name = self._names[row] if row < len(self._names) else f"P{row + 1}"
                ax.plot(time_ns[: voltage.shape[1]], voltage[row], label=name)
        ax.set_xlabel("Time (ns)")
        ax.set_ylabel("Voltage (V)")
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=8)
        source = self._names[index] if index < len(self._names) else str(index + 1)
        ax.set_title(f"Excitation at {source}")

    def _say(self, text: str) -> None:
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.text(0.5, 0.5, text, ha="center", va="center")
        ax.set_axis_off()
        self.canvas.draw_idle()
