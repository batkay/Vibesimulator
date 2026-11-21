"""Left-to-right layout that wraps. Used by the top shelf so a row of controls does not grow a horizontal scrollbar."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QSizePolicy, QWidget


class FlowLayout(QLayout):
    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._place(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._place(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _place(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        space = self.spacing()
        x = rect.x() + margins.left()
        y = rect.y() + margins.top()
        line_height = 0
        right = rect.right() - margins.right()
        for item in self._items:
            hint = item.sizeHint()
            width = hint.width()
            next_x = x + width
            if next_x > right and line_height > 0:
                x = rect.x() + margins.left()
                y = y + line_height + space
                next_x = x + width
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x + space
            line_height = max(line_height, hint.height())
        return y + line_height + margins.bottom() - rect.y()


class FlowHost(QWidget):
    """Widget whose height follows the wrapped rows of its flow layout."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setLayout(FlowLayout())
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self.layout().heightForWidth(width)

    def sizeHint(self) -> QSize:
        return QSize(0, self.heightForWidth(max(self.width(), 240)))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.layout().minimumSize().height())
