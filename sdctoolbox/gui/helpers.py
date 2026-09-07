"""Small GUI primitives shared by otherwise independent screens."""

from __future__ import annotations

from collections.abc import Sized

from PySide6.QtWidgets import QFormLayout, QTableWidget, QWidget

NO_VALUE = "\u2014"


def selected_table_value(table: QTableWidget, column: int) -> str | None:
    """Return one cell from the selected row, if there is one."""
    model = table.selectionModel()
    rows = model.selectedRows() if model else []
    if not rows:
        return None
    item = table.item(rows[0].row(), column)
    return item.text() if item else None


def select_table_row(table: QTableWidget, column: int, value: str) -> None:
    """Select the first row whose cell in ``column`` equals ``value``."""
    for row in range(table.rowCount()):
        item = table.item(row, column)
        if item is not None and item.text() == value:
            table.selectRow(row)
            return


def set_form_row_visible(
    form: QFormLayout,
    field: QWidget,
    *,
    visible: bool,
) -> None:
    """Show or hide a form field and its label together."""
    field.setVisible(visible)
    label = form.labelForField(field)
    if label is not None:
        label.setVisible(visible)


def value_text(value: object | None) -> str:
    """Render a scalar value without conflating ``None`` and valid falsy values."""
    return NO_VALUE if value is None else str(value)


def sample_count_text(samples: Sized) -> str:
    """Summarize a sample block without dumping its contents into a table cell."""
    count = len(samples)
    return f"{count} sample(s)" if count else NO_VALUE
