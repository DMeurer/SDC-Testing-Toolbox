"""The "Pick an MDC code" dialog: find an IEEE 11073-10101 term by name instead of by number.

It searches sdctoolbox.nomenclature, the common codes that ship with the toolbox, and the
user's own codes.csv export if one was loaded ("Load codes…"). The file's path is
remembered, so it loads again in the next session the first time the dialog opens.
Picking a unit for a known measure offers only that measure's usual units, unless unticked:
the toolbox's own pick, then any a loaded codes.csv names.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import constants, nomenclature
from .kind_sections import kind_section, style_dialog_buttons
from .no_wheel import NoWheelComboBox
from .styling import apply_row_selection_style, mute, set_button_kind

# Caption -> nomenclature kind; None searches everything.
LOOK_FOR: list[tuple[str, str | None]] = [
    ("What is measured", "measurement"),
    ("Units", "unit"),
    ("Alarm codes", "alert"),
    ("Everything", None),
]
# shortcut: a full codes.csv has thousands of rows; the table shows the best ones only. Page it if users ask.
MAX_ROWS = 300
SETTINGS_KEY = "nomenclature/codes_csv"

_remembered_tried = False


def settings() -> QSettings:
    """The toolbox's remembered choices: an ini file per user, so tests can point it elsewhere."""
    return QSettings(QSettings.IniFormat, QSettings.UserScope, constants.APP_NAME, constants.APP_NAME)


def load_codes(parent: QWidget | None, path: str) -> bool:
    """Load a codes.csv and remember it, or say why not and leave the codes as they were."""
    try:
        nomenclature.load_csv(path)
    except nomenclature.NomenclatureFileError as exc:
        QMessageBox.warning(parent, "Could not load codes", str(exc))
        return False
    settings().setValue(SETTINGS_KEY, path)
    return True


def load_remembered_codes(parent: QWidget | None) -> None:
    """Load the codes.csv of an earlier session, once per session."""
    global _remembered_tried  # noqa: PLW0603 - once per process
    if _remembered_tried or nomenclature.loaded_from is not None:
        return
    _remembered_tried = True
    path = settings().value(SETTINGS_KEY)
    if path and not load_codes(parent, str(path)):
        settings().remove(SETTINGS_KEY)


class CodePickerDialog(QDialog):
    """Pick one MDC term; `term()` is the choice after OK."""

    def __init__(
        self, kind: str | None = "measurement", parent: QWidget | None = None,
        measure: nomenclature.Term | None = None,
    ) -> None:
        """`measure`, when given, is what the metric measures: units can be narrowed to its own."""
        super().__init__(parent)
        self.setWindowTitle("Pick an MDC code")
        self.setMinimumSize(560, 360)
        self._terms: list[nomenclature.Term] = []
        load_remembered_codes(self)

        self.kind_box = NoWheelComboBox()
        for caption, value in LOOK_FOR:
            self.kind_box.addItem(caption, value)
        self.kind_box.setCurrentIndex([value for _, value in LOOK_FOR].index(kind))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("a name, reference ID or code, e.g. pulse rate")
        self.search_edit.setClearButtonEnabled(True)
        self._measure = measure
        self._units: set[str] = set(nomenclature.units_for(measure.code)) if measure else set()
        self.only_units_box = QCheckBox(
            f"Only units for {measure.term} ({measure.ref_id})" if measure and measure.term
            else f"Only units for {measure.ref_id}" if measure else "",
        )
        self.only_units_box.setChecked(bool(self._units))

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Code", "Reference ID", "Term"])
        self.table.verticalHeader().hide()
        self.table.setWordWrap(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        apply_row_selection_style(self.table)

        self.source_label = QLabel()
        self.source_label.setWordWrap(True)
        mute(self.source_label)
        self.load_button = QPushButton("Load codes…")
        set_button_kind(self.load_button, "design-outline")
        self.load_button.setToolTip(
            "A codes.csv made by helpers/Coding Systems/export_11073_tables.py from your own copy\n"
            "of IEEE 11073-10101, or by rtmms_to_codes_csv.py from a NIST RTMMS download, which\n"
            "also knows each measure's units. It is remembered and loads again next time.",
        )
        source = QWidget()
        source_row = QHBoxLayout(source)
        source_row.setContentsMargins(0, 0, 0, 0)
        source_row.addWidget(self.source_label, 1)
        source_row.addWidget(self.load_button, 0, Qt.AlignTop)

        section, form = kind_section("IEEE 11073 nomenclature", "mdib")
        self._form = form
        form.addRow("Look for", self.kind_box)
        form.addRow("Search", self.search_edit)
        form.addRow("", self.only_units_box)
        self._only_units_row = form.rowCount() - 1
        # Below the form, not in it: a form row never grows, so a taller window would only add
        # empty space. The table takes it instead, never shrinking below its usual height.
        box = section.layout()
        box.takeAt(box.count() - 1)  # kind_section's trailing stretch
        self.table.setMinimumHeight(self.table.sizeHint().height())
        box.addWidget(self.table, 1)
        box.addWidget(source)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        style_dialog_buttons(self.buttons)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.addWidget(section, 1)
        layout.addWidget(self.buttons)

        self.kind_box.currentIndexChanged.connect(self._refresh)
        self.search_edit.textChanged.connect(self._refresh)
        self.only_units_box.toggled.connect(self._refresh)
        self.table.itemSelectionChanged.connect(self._update_ok)
        self.table.cellDoubleClicked.connect(lambda *_: self.accept())
        self.load_button.clicked.connect(self._on_load)
        self._refresh()
        self.search_edit.setFocus()

    def term(self) -> nomenclature.Term | None:
        """The selected term."""
        rows = self.table.selectionModel().selectedRows()
        return self._terms[rows[0].row()] if rows else None

    def _refresh(self) -> None:
        kind = self.kind_box.currentData()
        narrow = bool(self._units) and kind == "unit"
        self._form.setRowVisible(self._only_units_row, narrow)
        if narrow and self.only_units_box.isChecked():
            hits = nomenclature.search(self.search_edit.text(), kind)
            self._terms = [term for term in hits if term.code in self._units]
            if not self.search_edit.text().strip():  # the measure's own order: its usual unit first
                order = nomenclature.units_for(self._measure.code)
                self._terms.sort(key=lambda term: order.index(term.code))
        else:
            self._terms = nomenclature.search(self.search_edit.text(), kind, MAX_ROWS)
        # Empty first: replacing cells one by one makes the size-to-content columns measure every
        # row again for each cell, 1.7 s for 300 rows; filling an empty table takes 20 ms.
        self.table.setRowCount(0)
        self.table.setRowCount(len(self._terms))
        for row, term in enumerate(self._terms):
            for column, text in enumerate((term.code, term.ref_id, term.display)):
                self.table.setItem(row, column, QTableWidgetItem(text))
        if self._terms:
            self.table.selectRow(0)
        self._update_ok()
        self._update_source()

    def _update_ok(self) -> None:
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(self.term() is not None)

    def _update_source(self) -> None:
        text = f"{len(nomenclature.SHIPPED)} common codes ship with the toolbox."
        extra = len(nomenclature.terms()) - len(nomenclature.SHIPPED)
        if nomenclature.loaded_from is not None and nomenclature.has_loaded_units():
            text = (f"{len(nomenclature.SHIPPED)} common codes ship with the toolbox; {nomenclature.loaded_from.name}"
                    " adds the rest and which units each measure allows.")
        elif nomenclature.loaded_from is not None:
            text += f" {extra} more come from {nomenclature.loaded_from.name}."
        else:
            text += " Your own export of IEEE 11073-10101 adds the rest."
        self.source_label.setText(text)

    def _on_load(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose codes.csv", "", "CSV files (*.csv);;All files (*)")
        if path and load_codes(self, str(Path(path))):
            if self._measure is not None:  # the file may name units the measure had none of
                had_units = bool(self._units)
                self._units = set(nomenclature.units_for(self._measure.code))
                if not had_units:
                    self.only_units_box.setChecked(bool(self._units))
            self._refresh()

    def accept(self) -> None:
        if self.term() is not None:
            super().accept()
