"""Deterministic offscreen checks for window modes and card reflow."""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gui_test_support import Report, WindowFixture, pump, wait_for
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QSizePolicy
from sdc11073.xml_types import msg_types

from sdctoolbox.gui.consumer_pane import ACTION_BUTTON_TEXT_WIDTH, ACTION_BUTTON_WIDTH
from sdctoolbox.model import MetricKind, MetricSpec, RemoteAction


class ActionRemote:
    def __init__(self, actions: dict[str, RemoteAction]) -> None:
        self._actions = actions
        self.invoked: list[str] = []

    def actions(self) -> dict[str, RemoteAction]:
        return dict(self._actions)

    def run_action(self, handle: str) -> msg_types.InvocationState:
        self.invoked.append(handle)
        return msg_types.InvocationState.FINISHED

    def metrics(self, _handles=None) -> dict[str, object]:
        return {}

    def close(self) -> None:
        pass


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


def remote_action_checks(report: Report, fixture: WindowFixture) -> None:
    window = fixture.window
    assert window is not None
    pane = window.network_pane

    baseline_pane_minimum = pane.minimumSizeHint().width()
    baseline_pane_hint = pane.sizeHint().width()
    baseline_window_minimum = window.minimumSizeHint().width()
    baseline_window_hint = window.sizeHint().width()
    baseline_window_width = window.width()

    label_handle = "action-label"
    type_handle = "action-type"
    handle_fallback = "H" * 10_000
    actions = {
        label_handle: RemoteAction(
            handle=label_handle,
            label="L" * 10_000,
            type_code="unused-label-code",
            enabled=True,
        ),
        type_handle: RemoteAction(
            handle=type_handle,
            type_code="T" * 10_000,
            enabled=True,
        ),
        handle_fallback: RemoteAction(handle=handle_fallback, enabled=True),
    }
    remote = ActionRemote(actions)
    pane.remote = remote
    pane.refresh_actions()
    pump(fixture.app)

    growth_limit = ACTION_BUTTON_WIDTH
    bounded_sizes = (
        pane.minimumSizeHint().width() <= baseline_pane_minimum + growth_limit
        and pane.sizeHint().width() <= baseline_pane_hint + growth_limit
        and window.minimumSizeHint().width() <= baseline_window_minimum + growth_limit
        and window.sizeHint().width() <= baseline_window_hint + growth_limit
        and window.width() <= baseline_window_width + growth_limit
    )
    report.check(
        bounded_sizes,
        "long remote action captions keep pane and window sizes bounded",
        (
            f"pane min/hint {baseline_pane_minimum}/{baseline_pane_hint} -> "
            f"{pane.minimumSizeHint().width()}/{pane.sizeHint().width()}, "
            f"window min/hint/width {baseline_window_minimum}/"
            f"{baseline_window_hint}/{baseline_window_width} -> "
            f"{window.minimumSizeHint().width()}/{window.sizeHint().width()}/"
            f"{window.width()}"
        ),
    )

    for handle, action in actions.items():
        button = pane.action_buttons[handle]
        caption = action.caption
        report.check(
            button.sizePolicy().horizontalPolicy() == QSizePolicy.Fixed
            and button.minimumWidth() == ACTION_BUTTON_WIDTH
            and button.maximumWidth() == ACTION_BUTTON_WIDTH,
            f"the {handle[:20]!r} action button has a bounded non-expanding width",
        )
        report.check(
            button.text() != caption
            and "\u2026" in button.text()
            and button.fontMetrics().horizontalAdvance(button.text())
            <= ACTION_BUTTON_TEXT_WIDTH,
            f"the {handle[:20]!r} action caption is visibly elided",
            f"{len(button.text())} displayed characters",
        )
        report.check(
            button.toolTip() == caption
            and button.accessibleName() == caption
            and button.accessibleDescription() == caption,
            f"the {handle[:20]!r} action retains its full plain-text caption",
        )

    buttons = sorted(
        pane.action_buttons.values(),
        key=lambda button: button.geometry().left(),
    )
    report.check(
        all(
            left.geometry().right() < right.geometry().left()
            for left, right in pairwise(buttons)
        ),
        "multiple action buttons remain separate and usable",
    )
    report.check(
        pane.actions_widget.horizontalScrollBar().maximum() > 0,
        "an overflowing action row can scroll to every button",
    )

    invoked_button = pane.action_buttons[type_handle]
    pane.actions_widget.ensureWidgetVisible(invoked_button)
    pump(fixture.app)
    report.check(
        pane.actions_widget.viewport().rect().intersects(
            invoked_button.geometry().translated(
                -pane.actions_widget.horizontalScrollBar().value(),
                0,
            ),
        ),
        "an overflowing action button can be brought into view",
    )
    QTest.mouseClick(invoked_button, Qt.LeftButton)
    report.check(
        wait_for(fixture.app, lambda: remote.invoked == [type_handle]),
        "an elided action button invokes its original handle",
        repr(remote.invoked),
    )
    report.check(
        wait_for(fixture.app, lambda: all(button.isEnabled() for button in buttons)),
        "action buttons are usable again after invocation",
    )


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

        remote_action_checks(report, fixture)

    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
