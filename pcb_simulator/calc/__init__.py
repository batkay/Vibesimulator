"""Calculated results. One file per quantity.

This is the math, not a directory a run writes into. Solved arrays stay in
``.sim/current/results.npz``. Touchstone and CSV stay user-chosen paths.

- ``s_parameters.py`` — S from the port waves
- ``z_parameters.py`` — Z from S
- ``tdr.py`` — step-response impedance
- ``differential.py`` — Sdd, Scc, Zdiff, Zcommon
- ``waveforms.py`` — port v(t) and i(t)
- ``touchstone.py`` — Touchstone export
- ``csv_export.py`` — CSV of every calculated result
- ``network.py`` — saved S as a scikit-rf Network
- ``load.py`` — read ``results.npz``
"""
