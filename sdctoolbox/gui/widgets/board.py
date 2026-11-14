"""A board of metric controls.

Each metric gets a card: its name, its control, its unit and handle. The cards reflow into
as many columns as the width allows, so the board is usable both in a half-window panel and
across the whole thing.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...model import MetricKind
from ..decimal_input import DecimalInputError, parse_decimal_input
from ..kind_sections import kind_section
from ..no_wheel import NoWheelComboBox
from ..styling import constrain_dynamic_label, mute, set_button_kind
from .base import CardSettings
from .controls import SampleArrayWidget, SliderWidget, StepperWidget, TextWidget
from .factory import build_widget, chosen_control, matching_controls

if TYPE_CHECKING:
    from .base import MetricWidget, WidgetSpec

PEN_PIXELS = 17
POPUP_WIDTH = 340

# Narrowest a card may be before the board drops to fewer columns.
MIN_CARD_WIDTH = 260


class MetricCard(QFrame):
    """One metric: heading, control, and the details underneath.

    A deletable card carries a bin in its top right. That is the only way to remove a
    metric while the board is showing, because there is no row to select.
    """

    # The bin was clicked. Carries the handle.
    delete_requested = Signal(str)
    # The pen was clicked. Carries the handle.
    edit_requested = Signal(str)

    def __init__(
        self,
        spec: WidgetSpec,
        parent: QWidget | None = None,
        *,
        deletable: bool = False,
        settings: CardSettings | None = None,
    ) -> None:
        super().__init__(parent)
        self.spec = spec
        self.settings = settings or CardSettings()
        self.setFrameShape(QFrame.StyledPanel)

        self.heading = QLabel(spec.caption)
        constrain_dynamic_label(self.heading, max_lines=2)
        font = self.heading.font()
        font.setBold(True)
        self.heading.setFont(font)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(4)
        title_row.addWidget(self.heading, 1)

        # Every card can be edited: your own opens the data source, a peer's only how the
        # card looks. Only your own can be removed.
        self.edit_button = QToolButton()
        self.edit_button.setText("\u270e")
        font = self.edit_button.font()
        font.setPixelSize(PEN_PIXELS)
        self.edit_button.setFont(font)
        self.edit_button.setAutoRaise(True)
        self.edit_button.setToolTip(f"Edit {spec.handle} while it runs" if deletable else "Card settings")
        self.edit_button.setAccessibleName(f"Edit {spec.handle}")
        self.edit_button.clicked.connect(lambda: self.edit_requested.emit(spec.handle))
        title_row.addWidget(self.edit_button, 0, Qt.AlignTop)
        self.delete_button: QToolButton | None = None
        if deletable:
            self.delete_button = QToolButton()
            self.delete_button.setIcon(
                self.style().standardIcon(QStyle.StandardPixmap.SP_TitleBarCloseButton),
            )
            self.delete_button.setAutoRaise(True)
            self.delete_button.setToolTip(f"Remove {spec.handle}")
            self.delete_button.setAccessibleName(f"Remove {spec.handle}")
            self.delete_button.clicked.connect(lambda: self.delete_requested.emit(spec.handle))
            title_row.addWidget(self.delete_button, 0, Qt.AlignTop)

        self.control: MetricWidget = build_widget(spec, self, settings=self.settings)

        footer_parts = [spec.handle]
        if spec.unit:
            footer_parts.append(spec.unit)
        if spec.domain:
            # A distribution's domain is what its x axis means, so it belongs on the card
            # rather than only in the table.
            footer_parts.append(f"over {spec.domain}")
        if spec.sample_period:
            footer_parts.append(f"{spec.sample_period}s/sample")
        if spec.note:
            footer_parts.append(spec.note)
        footer_parts.extend(self.control.footer_extras())
        if not spec.active:
            footer_parts.append(f"switched {spec.activation}" if spec.activation == "Off" else spec.activation)
        self.footer = QLabel("  \u00b7  ".join(footer_parts))
        constrain_dynamic_label(self.footer, max_lines=3)
        mute(self.footer)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        layout.addLayout(title_row)
        layout.addWidget(self.control)
        layout.addWidget(self.footer)
        # A metric that is not On has no value to rely on, so it is the one that looks greyed.
        self.control.setEnabled(spec.active)


class WidgetBoard(QScrollArea):
    """Shows a control per metric, reflowing into columns as the width allows."""

    # handle, value. Forwarded from whichever card asked.
    value_requested = Signal(str, object)
    # handle. A card's bin was clicked.
    delete_requested = Signal(str)
    edit_requested = Signal(str)
    # handle. A card was rebuilt with new settings and needs its value again.
    card_rebuilt = Signal(str)

    def __init__(self, parent: QWidget | None = None, *, deletable: bool = False) -> None:
        super().__init__(parent)
        # Whether cards carry a bin. False for a peer's metrics, which are not ours to
        # delete.
        self.deletable = deletable
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)

        self._canvas = QWidget()
        self._grid = QGridLayout(self._canvas)
        self._grid.setContentsMargins(4, 4, 4, 4)
        self._grid.setSpacing(8)
        self.setWidget(self._canvas)

        self._empty = QLabel("Nothing to show yet")
        self._empty.setAlignment(Qt.AlignCenter)
        mute(self._empty)

        self._cards: dict[str, MetricCard] = {}
        self._order: list[str] = []
        # How each card looks, by handle. Kept for the session; a rebuilt card keeps it.
        self._settings: dict[str, CardSettings] = {}
        self.settings_popup: CardSettingsPopup | None = None
        self._columns = 0
        # Highest column and row index we have ever stretched, so they can all be undone.
        self._stretched_columns = 0
        self._stretched_rows = 0

    # -- content -------------------------------------------------------------------

    @property
    def handles(self) -> list[str]:
        """Handles currently on the board, in display order."""
        return list(self._order)

    def card(self, handle: str) -> MetricCard | None:
        """The card for a handle, or None."""
        return self._cards.get(handle)

    def set_metrics(self, specs: list[WidgetSpec]) -> None:
        """Rebuild the board.

        Cards are recreated when the metric's definition changes, and kept otherwise, so a
        refresh does not interrupt somebody using a control.
        """
        wanted = {spec.handle: spec for spec in specs}

        for handle in list(self._cards):
            if handle not in wanted or self._cards[handle].spec != wanted[handle]:
                card = self._cards.pop(handle)
                card.setParent(None)
                card.deleteLater()

        for spec in specs:
            if spec.handle not in self._cards:
                self._cards[spec.handle] = self._make_card(spec)

        self._order = [spec.handle for spec in specs]
        self._relayout(force=True)

    def _make_card(self, spec: WidgetSpec) -> MetricCard:
        card = MetricCard(spec, self._canvas, deletable=self.deletable, settings=self._settings.get(spec.handle))
        card.control.value_requested.connect(self.value_requested)
        card.delete_requested.connect(self.delete_requested)
        card.edit_requested.connect(self.edit_requested if self.deletable else self.open_card_settings)
        return card

    def card_settings(self, handle: str) -> CardSettings:
        """How the card for this handle looks."""
        return self._settings.get(handle, CardSettings())

    def set_card_settings(self, handle: str, settings: CardSettings) -> None:
        """Change how a card looks; the card is rebuilt with its current value kept."""
        self._settings[handle] = settings
        old = self._cards.get(handle)
        if old is None or old.settings == settings:
            return
        card = self._make_card(old.spec)
        self._cards[handle] = card
        old.setParent(None)
        old.deleteLater()
        self._relayout(force=True)
        # Ask for the value again: whoever fills the board answers through show_values.
        self.card_rebuilt.emit(handle)

    def open_card_settings(self, handle: str) -> None:
        """The pen on a peer's card: a popup with how this card looks, nothing else."""
        card = self._cards.get(handle)
        if card is None:
            return
        if self.settings_popup is not None:
            self.settings_popup.close()
            self.settings_popup.deleteLater()
        self.settings_popup = CardSettingsPopup(card.spec, self.card_settings(handle), self)
        self.settings_popup.editor.changed.connect(lambda settings: self.set_card_settings(handle, settings))
        anchor = card.edit_button.mapToGlobal(QPoint(0, card.edit_button.height()))
        self.settings_popup.move(anchor - QPoint(self.settings_popup.sizeHint().width() - card.edit_button.width(), 0))
        self.settings_popup.show()

    def show_values(self, values: dict[str, Any]) -> None:
        """Push values in, skipping any control the user is currently working with."""
        for handle, value in values.items():
            card = self._cards.get(handle)
            if card is not None and not card.control.busy_editing():
                card.control.show_value(value)

    def append_samples(self, blocks: dict[str, Any]) -> None:
        """Hand each named card a block of new samples.

        Deliberately keyed by handle and called only with the handles a report actually
        carried: pushing every card on every report is what made traces jagged.
        """
        for handle, samples in blocks.items():
            card = self._cards.get(handle)
            if card is not None:
                card.control.append_samples(samples)

    def clear(self) -> None:
        """Remove everything."""
        self.set_metrics([])

    # -- layout --------------------------------------------------------------------

    def _column_count(self) -> int:
        available = self.viewport().width() - 8
        return max(1, available // MIN_CARD_WIDTH)

    def _clear_grid(self) -> None:
        """Empty the grid and undo every stretch we have ever set on it.

        QGridLayout never shrinks: its columnCount and rowCount only grow, and a stretch
        set on a column stays there after the column is empty. Without this, going from
        four columns down to two leaves columns three and four still stretching, so the
        cards bunch up on the left and the rest of the panel stays blank.
        """
        while self._grid.count():
            self._grid.takeAt(0)

        for column in range(max(self._grid.columnCount(), self._stretched_columns)):
            self._grid.setColumnStretch(column, 0)
            self._grid.setColumnMinimumWidth(column, 0)
        for row in range(max(self._grid.rowCount(), self._stretched_rows)):
            self._grid.setRowStretch(row, 0)

    def _relayout(self, *, force: bool = False) -> None:
        columns = self._column_count()
        if not force and columns == self._columns:
            return
        self._columns = columns

        self._clear_grid()

        if not self._order:
            self._grid.addWidget(self._empty, 0, 0)
            self._empty.setVisible(True)
            return
        self._empty.setVisible(False)

        rows = 0
        for index, handle in enumerate(self._order):
            card = self._cards.get(handle)
            if card is None:
                continue
            row, column = divmod(index, columns)
            self._grid.addWidget(card, row, column)
            card.setVisible(True)
            rows = row + 1

        for column in range(columns):
            self._grid.setColumnStretch(column, 1)
        # Keep the cards packed at the top rather than spread down the panel.
        self._grid.setRowStretch(rows, 1)

        self._stretched_columns = max(self._stretched_columns, columns)
        self._stretched_rows = max(self._stretched_rows, rows + 1)

    def resizeEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt naming
        super().resizeEvent(event)
        self._relayout()


def _check(caption: str, checked: bool, tooltip: str = "") -> QCheckBox:  # noqa: FBT001
    box = QCheckBox(caption)
    box.setChecked(checked)
    box.setToolTip(tooltip)
    return box


def _note(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    mute(label)
    return label


class CardSettingsEditor(QWidget):
    """Card type and the options of that type, for one metric. Toolbox only.

    Shown in a peer card's popup and in the Card section of Edit data source. Every change
    is announced at once through `changed`, so a card can follow while the user clicks.
    """

    changed = Signal(object)

    def __init__(self, spec: WidgetSpec, settings: CardSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.spec = spec
        self._settings = settings
        self._options = matching_controls(spec)
        self.type_box = NoWheelComboBox()
        for control in self._options:
            self.type_box.addItem(card_caption(control, spec))
        self.type_box.setCurrentIndex(self._options.index(chosen_control(spec, settings)))
        self.others = _note("")

        self.small_box = _check("Show \u22121 / +1 buttons", settings.small_steps)
        self.large_box = _check("Show \u221210 / +10 buttons", settings.large_steps)
        self.release_button = QPushButton("Release")
        self.change_button = QPushButton("Change")
        self._update_group = QButtonGroup(self)
        for button in (self.release_button, self.change_button):
            button.setCheckable(True)
            set_button_kind(button, "design-outline")
            self._update_group.addButton(button)
        (self.change_button if settings.update_on == "change" else self.release_button).setChecked(True)
        self.ranges_box = _check("Show technical and allowed range", settings.show_ranges)
        self.legend_box = _check("Show legend (min, max and 0)", settings.legend)
        self.speed_edit = QLineEdit(str(settings.seconds_per_width))
        self.speed_edit.setMaximumWidth(60)
        self.split_box = _check("Split current and desired", settings.split)
        self.multiline_box = _check("Allow multiline", settings.multiline)

        update_row = QHBoxLayout()
        update_row.setContentsMargins(0, 0, 0, 0)
        update_row.setSpacing(0)
        update_row.addWidget(self.release_button)
        update_row.addWidget(self.change_button)
        update_row.addStretch(1)
        speed_row = QHBoxLayout()
        speed_row.setContentsMargins(0, 0, 0, 0)
        speed_row.addWidget(self.speed_edit)
        speed_row.addWidget(QLabel("s per width"))
        speed_row.addStretch(1)

        self.form = QFormLayout()
        self.form.addRow("Card type", self.type_box)
        self.form.addRow("", self.others)
        self._pages: dict[type, list[QWidget]] = {StepperWidget: [], SliderWidget: [],
                                                  SampleArrayWidget: [], TextWidget: []}
        self._add(StepperWidget, self.small_box)
        self._add(StepperWidget, self.large_box)
        update_widget = QWidget()
        update_widget.setLayout(update_row)
        self._add(SliderWidget, update_widget, "Update on")
        self._add(SliderWidget, _note("Release sends when you let go of the knob. Change sends while you drag."))
        self._add(SliderWidget, self.ranges_box)
        self._add(SliderWidget, _note("The track spans the technical range; values outside the allowed range "
                                      "are marked and cannot be picked."))
        self._add(SampleArrayWidget, self.legend_box)
        if spec.kind is MetricKind.WAVEFORM:
            speed_widget = QWidget()
            speed_widget.setLayout(speed_row)
            self._add(SampleArrayWidget, speed_widget, "Playback speed")
            self._add(SampleArrayWidget, _note("Seconds of signal across the plot. Resizing the card does not "
                                               "change the speed."))
        self._add(TextWidget, self.split_box)
        self.split_note = _note("Shows the device value above the input. Only offered when remote control "
                                "is enabled.")
        self._add(TextWidget, self.split_note)
        self._add(TextWidget, self.multiline_box)
        self._add(TextWidget, _note("The field turns amber while it holds changes that are not sent yet. "
                                    "This is always on."))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.form)

        self.type_box.currentIndexChanged.connect(self._emit)
        for box in (self.small_box, self.large_box, self.ranges_box, self.legend_box, self.split_box,
                    self.multiline_box):
            box.toggled.connect(self._emit)
        self._update_group.buttonClicked.connect(self._emit)
        self.speed_edit.editingFinished.connect(self._emit)
        self._show_page()

    def _add(self, control: type, widget: QWidget, caption: str = "") -> None:
        self.form.addRow(caption, widget)
        self._pages[control].append(widget)

    def _show_page(self) -> None:
        chosen = self._options[self.type_box.currentIndex()]
        for control, widgets in self._pages.items():
            for widget in widgets:
                visible = control is chosen
                if widget in (self.split_box, self.split_note):
                    # Splitting only means something when the value can be set from here.
                    visible = visible and self.spec.editable
                self.form.setRowVisible(widget, visible)
        others = [card_caption(control, self.spec) for control in self._options if control is not chosen]
        self.others.setText(f"Also available: {', '.join(others)}" if others else "No other card type fits")

    def settings(self) -> CardSettings:
        """The settings as currently entered."""
        try:
            speed = parse_decimal_input(self.speed_edit.text(), "playback speed", positive=True)
        except DecimalInputError:
            speed = self._settings.seconds_per_width
        return replace(
            self._settings,
            control=self._options[self.type_box.currentIndex()].__name__,
            small_steps=self.small_box.isChecked(),
            large_steps=self.large_box.isChecked(),
            update_on="change" if self.change_button.isChecked() else "release",
            show_ranges=self.ranges_box.isChecked(),
            legend=self.legend_box.isChecked(),
            seconds_per_width=speed if isinstance(speed, Decimal) else Decimal(str(speed)),
            split=self.split_box.isChecked(),
            multiline=self.multiline_box.isChecked(),
        )

    def _emit(self, *_args: object) -> None:
        self._show_page()
        self._settings = self.settings()
        self.changed.emit(self._settings)


def card_caption(control: type, spec: WidgetSpec) -> str:
    if control is SampleArrayWidget:
        return "Distribution" if spec.kind is MetricKind.DISTRIBUTION else "Waveform"
    return control.caption or control.__name__


class CardSettingsPopup(QFrame):
    """The pen on a peer's card: how this one card looks, anchored to the card."""

    def __init__(self, spec: WidgetSpec, settings: CardSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.Popup)
        self.setObjectName("cardSettingsPopup")
        self.setStyleSheet("#cardSettingsPopup { border: 1px solid palette(mid); border-radius: 8px; }")
        self.editor = CardSettingsEditor(spec, settings)
        section, form = kind_section("Card settings", "toolbox")
        form.addRow(_note(f"{spec.caption} ({spec.handle}). Only changes this card."))
        form.addRow(self.editor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(section)
        self.setFixedWidth(POPUP_WIDTH)
        self.editor.changed.connect(lambda _settings: self._fit())  # each type's options differ in height
        self._fit()

    def _fit(self) -> None:
        # Wrapped notes only know their height for a given width, so ask for exactly that.
        self.setFixedHeight(max(self.layout().heightForWidth(POPUP_WIDTH), self.layout().minimumSize().height()))
