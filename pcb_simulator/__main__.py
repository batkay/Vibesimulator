"""Launch the desktop app: python -m pcb_simulator [board.kicad_pcb | ipc-2581 | odb archive]"""

from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    import sys

    from PySide6.QtWidgets import QApplication

    from .app import MainWindow

    args = list(sys.argv[1:] if argv is None else argv)
    app = QApplication.instance() or QApplication(sys.argv if argv is None else [sys.argv[0], *args])
    window = MainWindow()
    if args:
        path = args[0]
        if path.endswith(".pcbsim.json"):
            window.open_project(path)
        else:
            window.open_board(path)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
