"""Take the screenshots of the documentation (docs/assets/screenshots) from the real application.

Run it again whenever the GUI changes, so the pictures in the docs stay true:

    .venv\\Scripts\\python.exe helpers\\docs_screenshots.py

It starts two devices on 127.0.0.1 (a patient monitor shown in the window and an infusion
pump to connect to), opens each window and dialog the docs show, saves it as a PNG in light
mode (name.png) and in dark mode (name-dark.png), and closes everything again. A window appears on screen for a short while; do not click it.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from sdctoolbox import config, constants, native_mdib  # noqa: E402
from sdctoolbox.gui.code_picker import CodePickerDialog  # noqa: E402
from sdctoolbox.gui.context_dialog import ContextDialog  # noqa: E402
from sdctoolbox.gui.main_window import MainWindow  # noqa: E402
from sdctoolbox.gui.new_action_dialog import NewActionDialog  # noqa: E402
from sdctoolbox.gui.new_alert_dialog import NewAlertDialog  # noqa: E402
from sdctoolbox.gui.new_metric_dialog import NewMetricDialog  # noqa: E402
from sdctoolbox.gui.startup_dialog import StartupDialog  # noqa: E402
from sdctoolbox.gui.styling import set_dark_mode  # noqa: E402
from sdctoolbox.mdib_io import parse_mdib  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402

OUT = ROOT / "docs" / "assets" / "screenshots"
IP = "127.0.0.1"


def pump(seconds: float = 0.5) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.02)


def wait_for(condition, timeout: float = 30.0) -> bool:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.05)
    return False


def save(widget: QWidget, name: str, redraw=None) -> None:  # noqa: ANN001
    """The widget once in light mode and once in dark mode; the docs show the one that fits.

    `redraw`, when given, runs after each switch, for content that keeps the colours it was
    made with.
    """
    for dark in (False, True):
        set_dark_mode(dark)
        if redraw is not None:
            redraw()
        pump(0.6)
        path = OUT / f"{name}{'-dark' if dark else ''}.png"
        widget.grab().save(str(path))
        print(f"wrote {path.relative_to(ROOT)}")
    set_dark_mode(False)


def dialog(widget: QWidget, name: str) -> None:
    widget.show()
    save(widget, name)
    widget.close()


def preset_device(file: str, name: str) -> ProviderService:
    """A provider started from a shipped preset, as run_toolbox --config does."""
    device_config = config.load_file(constants.PRESET_DIR / file)
    mdib = parse_mdib(device_config.xml)
    device, serial = native_mdib.device_identity(mdib)
    service = ProviderService(ip=IP, instance_name=name, device=device,
                              friendly_name=device.friendly_name or name, serial_number=serial)
    service.start(mdib)
    return service


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    set_dark_mode(False)
    # Screenshots must not change the user's remembered view settings.
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(ROOT / "build" / "docs-settings"))

    dialog(StartupDialog(name="monitor", ip=IP), "startup-dialog")

    service = preset_device("patient-monitor.xml", "docs-monitor")
    peer = preset_device("infusion-pump.xml", "docs-pump")
    window = MainWindow(service)
    try:
        window.resize(1280, 860)
        window.show()
        pane = window.provider_pane
        pump(2.0)
        save(window, "my-device")

        window.widgets_action.trigger()
        save(window, "my-device-tables")
        window.widgets_action.trigger()

        dialog(NewMetricDialog(window), "new-data-source")
        dialog(CodePickerDialog("measurement", window), "code-picker")
        dialog(NewAlertDialog(service.list_metrics(), window), "new-alarm")
        dialog(NewActionDialog(service.list_metrics(), window), "new-action")
        dialog(ContextDialog(service.get_location(), service.get_patient(), window), "patient-and-location")

        # Network: find the pump and open it in a tab.
        window.tabs.setCurrentIndex(1)
        tabs = window.network_tabs
        tabs.current_pane.scan_button.click()
        wait_for(lambda: any(d.epr == peer.epr.urn for d in tabs.current_pane.service.known_devices()))
        device = next(d for d in tabs.current_pane.service.known_devices() if d.epr == peer.epr.urn)
        tabs.current_pane.connect_to(device)  # what Connect does in the idle tab
        wait_for(lambda: any(p.remote is not None for p in tabs.panes))
        pump(3.0)
        save(window, "network")

        window.set_advanced_mode(True)
        pane_net = next(p for p in tabs.panes if p.remote is not None)
        pane_net.messages_section.set_expanded(True)
        pump(1.5)
        save(window, "network-advanced")

        window.tabs.setCurrentIndex(0)
        service.set_metric_fault("m.spo2", validity="Qst")
        service.set_metric_fault("m.ecg_ii", activation="StndBy")
        pane.refresh()
        pane.refresh_faults()
        pane.metrics_section.set_expanded(False)
        pane.alarms_section.set_expanded(True)
        pane.faults_section.set_expanded(True)
        pump(1.0)
        save(window, "my-device-advanced")
        pane.metrics_section.set_expanded(True)
        pane.alarms_section.set_expanded(False)
        pane.faults_section.set_expanded(False)
        window.set_advanced_mode(False)

        check = window.show_mdib_check()
        check.resize(1000, 460)
        save(check, "mdib-check")
        check.close()

        test = window.show_device_test(pane_net)

        def run_test() -> None:
            test.run()
            wait_for(lambda: test.again_button.isEnabled() and test.tree.topLevelItemCount() > 0, 60)

        wait_for(lambda: test.again_button.isEnabled(), 60)
        save(test, "device-test", run_test)
        test.close()

        log = window.show_activity_log()
        save(log, "activity-log")
        log.close()

    finally:
        window.close()
        pump(0.5)
        service.stop()
        peer.stop()
    app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
