"""Dialog sections that say who an option belongs to: the MDIB or the toolbox.

Each section is a heading (bold title, an MDIB or Toolbox chip, an optional muted note) and a
form, inside a frame with a coloured bar down its left side. The colours come from the theme
style sheet, so they follow the mode.
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout, QLabel, QVBoxLayout

from .collapsible import chip
from .styling import mute, set_button_kind

KIND_CHIPS = {"mdib": "MDIB", "toolbox": "Toolbox"}


def kind_section(title: str, kind: str, note: str = "") -> tuple[QFrame, QFormLayout]:
    """A framed section of `kind` ("mdib" or "toolbox") and the form to fill it with."""
    frame = QFrame()
    frame.setProperty("role", f"sect-{kind}")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(10, 2, 0, 2)
    heading = QHBoxLayout()
    heading.setSpacing(6)
    caption = QLabel(title)
    font = caption.font()
    font.setBold(True)
    caption.setFont(font)
    heading.addWidget(caption)
    heading.addWidget(chip(KIND_CHIPS[kind], f"chip-{kind}"))
    if note:
        remark = QLabel(note)
        mute(remark)
        heading.addWidget(remark)
    heading.addStretch(1)
    layout.addLayout(heading)
    form = QFormLayout()
    layout.addLayout(form)
    # Spare height (rows hidden after the dialog was sized) goes below the form, not into the heading.
    layout.addStretch(1)
    return frame, form


def style_dialog_buttons(buttons: QDialogButtonBox) -> None:
    """OK (or Start) is the key action of a dialog; Cancel sits next to it, outlined."""
    for role, kind in ((QDialogButtonBox.Ok, "highlight"), (QDialogButtonBox.Cancel, "highlight-outline")):
        button = buttons.button(role)
        if button is not None:
            set_button_kind(button, kind)
