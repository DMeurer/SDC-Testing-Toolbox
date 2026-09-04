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

LINE_SEPARATORS = "\r\n\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"


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

    label_handle = "action-label"
    type_handle = "action-type"
    disabled_handle = "action-disabled"
    separator_content = (LINE_SEPARATORS * ((10_000 // len(LINE_SEPARATORS)) + 1))[:10_000]
    handle_fallback = f"Handle{separator_content}caption"
    baseline_actions = {
        label_handle: RemoteAction(handle=label_handle, label="Baseline label action", enabled=True),
        type_handle: RemoteAction(handle=type_handle, type_code="baseline.type", enabled=True),
        disabled_handle: RemoteAction(handle=disabled_handle, label="Baseline disabled", enabled=False),
        handle_fallback: RemoteAction(
            handle=handle_fallback,
            label="Baseline handle action",
            enabled=True,
        ),
    }
    pane.remote = ActionRemote(baseline_actions)
    pane.refresh_actions()
    pump(fixture.app)

    baseline_button = next(iter(pane.action_buttons.values()))
    baseline_sizes = {
        "button": (baseline_button.minimumWidth(), baseline_button.minimumHeight()),
        "action row": (
            pane.actions_content.minimumSizeHint().width(),
            pane.actions_content.minimumSizeHint().height(),
        ),
        "action pane": (pane.actions_content.width(), pane.actions_content.height()),
        "action scroll": (
            pane.actions_widget.minimumSizeHint().width(),
            pane.actions_widget.minimumSizeHint().height(),
        ),
        "pane": (pane.minimumSizeHint().width(), pane.minimumSizeHint().height()),
        "window": (window.minimumSizeHint().width(), window.minimumSizeHint().height()),
    }
    baseline_hints = {
        "pane": (pane.sizeHint().width(), pane.sizeHint().height()),
        "window": (window.sizeHint().width(), window.sizeHint().height()),
    }
    baseline_window_size = (window.width(), window.height())

    label_caption = f"Label{separator_content}caption"
    type_caption = f"Type{separator_content}caption"
    actions = {
        label_handle: RemoteAction(
            handle=label_handle,
            label=label_caption,
            type_code="unused-label-code",
            enabled=True,
        ),
        type_handle: RemoteAction(
            handle=type_handle,
            type_code=type_caption,
            enabled=True,
        ),
        disabled_handle: RemoteAction(
            handle=disabled_handle,
            label=f"Disabled{separator_content}caption",
            enabled=False,
        ),
        handle_fallback: RemoteAction(handle=handle_fallback, enabled=True),
    }
    remote = ActionRemote(actions)
    pane.remote = remote
    pane.refresh_actions()
    pump(fixture.app)

    current_sizes = {
        "button": (
            next(iter(pane.action_buttons.values())).minimumWidth(),
            next(iter(pane.action_buttons.values())).minimumHeight(),
        ),
        "action row": (
            pane.actions_content.minimumSizeHint().width(),
            pane.actions_content.minimumSizeHint().height(),
        ),
        "action pane": (pane.actions_content.width(), pane.actions_content.height()),
        "action scroll": (
            pane.actions_widget.minimumSizeHint().width(),
            pane.actions_widget.minimumSizeHint().height(),
        ),
        "pane": (pane.minimumSizeHint().width(), pane.minimumSizeHint().height()),
        "window": (window.minimumSizeHint().width(), window.minimumSizeHint().height()),
    }
    for name, baseline in baseline_sizes.items():
        current = current_sizes[name]
        report.check(
            all(abs(after - before) <= 4 for before, after in zip(baseline, current, strict=True)),
            f"separator-heavy captions keep the {name} minimum width and height near baseline",
            f"{baseline} -> {current}",
        )

    current_hints = {
        "pane": (pane.sizeHint().width(), pane.sizeHint().height()),
        "window": (window.sizeHint().width(), window.sizeHint().height()),
    }
    for name, baseline in baseline_hints.items():
        current = current_hints[name]
        report.check(
            all(abs(after - before) <= 4 for before, after in zip(baseline, current, strict=True)),
            f"separator-heavy captions keep the {name} size hint near baseline",
            f"{baseline} -> {current}",
        )
    report.check(
        abs(window.width() - baseline_window_size[0]) <= 4
        and abs(window.height() - baseline_window_size[1]) <= 4,
        "separator-heavy captions do not resize the window",
        f"{baseline_window_size} -> {(window.width(), window.height())}",
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
            and not any(separator in button.text() for separator in LINE_SEPARATORS)
            and len(button.text()) <= ACTION_BUTTON_TEXT_WIDTH
            and button.fontMetrics().horizontalAdvance(button.text())
            <= ACTION_BUTTON_TEXT_WIDTH,
            f"the {handle[:20]!r} action caption is normalized and visibly bounded",
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
    report.check(
        not pane.action_buttons[disabled_handle].isEnabled(),
        "a disabled remote action remains unavailable in the GUI",
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
    expected_invocations: list[str] = []
    for handle in (type_handle, label_handle, handle_fallback):
        button = pane.action_buttons[handle]
        pane.actions_widget.ensureWidgetVisible(button)
        pump(fixture.app)
        QTest.mouseClick(button, Qt.LeftButton)
        expected_invocations.append(handle)
        report.check(
            wait_for(fixture.app, lambda: remote.invoked == expected_invocations),
            f"the {handle[:20]!r} action button invokes its original handle",
            f"invoked handle lengths: {[len(value) for value in remote.invoked]}",
        )
        report.check(
            wait_for(
                fixture.app,
                lambda: all(
                    candidate.isEnabled() is actions[candidate_handle].enabled
                    for candidate_handle, candidate in pane.action_buttons.items()
                ),
            ),
            "action buttons restore their advertised availability after invocation",
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
