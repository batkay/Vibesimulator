"""A ? beside a control. Clicking it opens a short note for that control."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

_POPUP: "InfoPopup | None" = None


class InfoPopup(QFrame):
    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("infoPopup")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAutoFillBackground(True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._anchor: QPushButton | None = None
        self.title = QLabel()
        self.title.setStyleSheet("font-weight: 600;")
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.body.setFixedWidth(240)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)
        layout.addWidget(self.title)
        layout.addWidget(self.body)

    def eventFilter(self, watched: QObject, event) -> bool:
        if self.isVisible() and event.type() == QEvent.Type.MouseButtonPress:
            if _is_info_button(watched):
                return False
            pos = event.globalPosition().toPoint()
            if self.frameGeometry().contains(pos):
                return False
            if _is_info_button(QApplication.widgetAt(pos)):
                return False
            self.hide()
        return False

    def show_for(self, button: QPushButton, title: str, text: str) -> None:
        if self.isVisible() and self._anchor is button:
            self.hide()
            return
        self._anchor = button
        self.title.setText(title)
        self.body.setText(text)
        self.adjustSize()
        origin = button.mapToGlobal(QPoint(button.width() + 6, 0))
        screen = button.screen()
        x, y = origin.x(), origin.y()
        if screen is not None:
            area = screen.availableGeometry()
            if x + self.width() > area.right():
                x = button.mapToGlobal(QPoint(-self.width() - 6, 0)).x()
            if y + self.height() > area.bottom():
                y = max(area.top(), area.bottom() - self.height() - 8)
            x = max(area.left(), x)
            y = max(area.top(), y)
        self.move(x, y)
        self.show()


def _popup() -> InfoPopup:
    global _POPUP
    if _POPUP is None:
        _POPUP = InfoPopup()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(_POPUP)
    return _POPUP


def _is_info_button(widget) -> bool:
    """True when this object, or a parent widget, is a ? button.

    The application event filter also sees native windows. Those are not widgets
    and have no parentWidget.
    """
    while isinstance(widget, QWidget):
        if widget.property("infoTitle"):
            return True
        widget = widget.parentWidget()
    return False


def info_button(title: str, text: str) -> QPushButton:
    button = QPushButton("?")
    button.setObjectName("infoButton")
    button.setProperty("infoTitle", title)
    button.setProperty("infoText", text)
    button.setFixedSize(20, 20)
    button.setFlat(True)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setToolTip(title)
    button.clicked.connect(lambda: _popup().show_for(button, title, text))
    return button


class NoteWithInfo(QWidget):
    """A wrapping note with a ? at the top right. The note keeps its wrapped height."""

    def __init__(self, label: QLabel, button: QPushButton) -> None:
        super().__init__()
        self._label = label
        self._button = button
        label.setParent(self)
        button.setParent(self)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def resizeEvent(self, event) -> None:
        gap = self._button.width() + 4
        self._button.move(max(0, self.width() - self._button.width()), 0)
        self._label.setGeometry(0, 0, max(1, self.width() - gap), self.height())

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        gap = self._button.width() + 4
        return max(self._button.height(), self._label.heightForWidth(max(1, width - gap)))

    def sizeHint(self) -> QSize:
        return QSize(0, self.heightForWidth(240))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self._button.height())


def note_with_info(label: QLabel, title: str, text: str) -> NoteWithInfo:
    return NoteWithInfo(label, info_button(title, text))


def with_info(control: QWidget, title: str, text: str) -> QWidget:
    """The control, with a ? on its right. For a field that can take a new parent."""
    host = QWidget()
    host.setMinimumWidth(0)
    host.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    row = QHBoxLayout(host)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(2)
    row.addWidget(control, 1)
    row.addWidget(info_button(title, text))
    return host
