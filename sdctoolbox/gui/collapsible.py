"""A section with a tinted header that folds its content away.

The header stays when the section is collapsed, so the chips (counts, status) remain
readable. An expanded section asks for the remaining height; a collapsed one only for its
header, so a vertical layout gives the free space to whatever is open.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from .styling import mute

EXPANDED_CHEVRON = "⌄"
COLLAPSED_CHEVRON = "›"


def chip(text: str, role: str = "chip") -> QLabel:
    """A small pill label; the role picks its colours from the theme style sheet."""
    label = QLabel(text)
    label.setProperty("role", role)
    # A pill is one line high whatever room its row has.
    label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    return label


class _Header(QFrame):
    clicked = Signal()

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt naming
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt naming
        if event.key() in (Qt.Key_Space, Qt.Key_Return, Qt.Key_Enter):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class CollapsibleSection(QWidget):
    """Header (chevron, title, chips, optional hint) above a content widget."""

    toggled = Signal(bool)

    def __init__(self, title: str, content: QWidget, *, expanded: bool = True,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.content = content
        self.header = _Header()
        self.header.setProperty("role", "section-header")
        self.header.setFocusPolicy(Qt.StrongFocus)
        self.header.setCursor(Qt.PointingHandCursor)
        self.header.setAccessibleName(f"{title} section")
        self.header.clicked.connect(self.toggle)
        self.chevron = QLabel()
        self.chevron.setProperty("role", "chevron")
        self.chevron.setFixedWidth(14)
        self.title = QLabel(title)
        self.title.setProperty("role", "section-title")
        self.hint = QLabel("")
        mute(self.hint)
        self.hint.hide()
        self._chips = QHBoxLayout()
        self._chips.setSpacing(4)
        self._chips.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout(self.header)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(6)
        row.addWidget(self.chevron)
        row.addWidget(self.title)
        row.addLayout(self._chips)
        row.addWidget(self.hint)
        row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.header)
        layout.addWidget(content, 1)
        self._expanded = not expanded
        self.set_expanded(expanded)

    @property
    def expanded(self) -> bool:
        """Whether the content is shown."""
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:  # noqa: FBT001 - matches the signal
        """Show or fold away the content; the header stays."""
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self.content.setVisible(expanded)
        self.chevron.setText(EXPANDED_CHEVRON if expanded else COLLAPSED_CHEVRON)
        self.hint.setVisible(bool(self.hint.text()) and not expanded)
        vertical = QSizePolicy.Expanding if expanded else QSizePolicy.Maximum
        self.setSizePolicy(QSizePolicy.Preferred, vertical)
        self.toggled.emit(expanded)

    def toggle(self) -> None:
        """Flip between expanded and collapsed."""
        self.set_expanded(not self._expanded)

    def set_hint(self, text: str) -> None:
        """A muted note shown in the header while the section is collapsed."""
        self.hint.setText(text)
        self.hint.setVisible(bool(text) and not self._expanded)

    def set_chips(self, texts: list[str]) -> None:
        """Replace the chips in the header."""
        if texts == self.chip_texts():
            return
        while self._chips.count():
            widget = self._chips.takeAt(0).widget()
            widget.setParent(None)  # off screen now, not at the next event loop turn
            widget.deleteLater()
        for text in texts:
            self._chips.addWidget(chip(text))

    def chip_texts(self) -> list[str]:
        """The texts of the header's chips, in order."""
        return [self._chips.itemAt(i).widget().text() for i in range(self._chips.count())]
