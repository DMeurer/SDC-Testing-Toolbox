"""Deterministic offscreen checks for window modes and card reflow."""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gui_test_support import Report, WindowFixture, pump
from PySide6.QtCore import Qt

from sdctoolbox.model import MetricKind, MetricSpec


def occupied_columns(board) -> int:
    return len(
        {
            board._grid.getItemPosition(index)[1]
            for index in range(board._grid.count())
        },
    )


def stretched_empty_columns(board) -> list[int]:
    used = {
        board._grid.getItemPosition(index)[1]
        for index in range(board._grid.count())
    }
    return [
        column
        for column in range(board._grid.columnCount())
        if column not in used and board._grid.columnStretch(column) > 0
    ]


def main() -> int:
    report = Report()
    print("GUI layout (offscreen)")
    with WindowFixture("gui-layout") as fixture:
        app = fixture.app
        service = fixture.service
        window = fixture.window
        assert window is not None

        report.check(window.splitter.count() == 2, "split view contains both panels")
        report.check(window.splitter.orientation() == Qt.Horizontal, "split panels are side by side")
        report.check(not window.splitter.childrenCollapsible(), "neither split panel can collapse")

        window.set_split_view(False)
        pump(app)
        report.check(window.tabs.count() == 2, "tab view contains both panels")
        report.check(
            [window.tabs.tabText(index) for index in range(window.tabs.count())]
            == ["My device", "Network"],
            "tab view names both panels",
        )
        report.check(
            window.provider_panel.isVisible() != window.network_panel.isVisible(),
            "tab view displays exactly one panel",
        )

        for index in range(10):
            service.add_metric(
                MetricSpec(
                    label=f"Layout metric {index}",
                    kind=MetricKind.NUMBER,
                    controllable=True,
                    initial_value=Decimal(index),
                ),
            )
        window.set_use_widgets(True)
        window.tabs.setCurrentIndex(0)
        window.resize(1500, 640)
        pump(app, 0.5)
        board = window.provider_pane.board
        wide_columns = occupied_columns(board)
        report.check(wide_columns > 1, "a wide board uses multiple card columns", str(wide_columns))
        report.check(not stretched_empty_columns(board), "a wide board has no stretched empty columns")

        window.resize(600, 640)
        pump(app, 0.5)
        narrow_columns = occupied_columns(board)
        report.check(
            narrow_columns < wide_columns,
            "narrowing the window reduces card columns",
            f"{wide_columns} -> {narrow_columns}",
        )
        report.check(not stretched_empty_columns(board), "reflow leaves no stretched empty columns")

        window.set_split_view(True)
        pump(app)
        report.check(window.splitter.count() == 2 and window.tabs.count() == 0, "split view can be restored")

    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
