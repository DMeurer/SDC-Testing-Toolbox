"""Drive the real main window through load phases and measure how responsive it stays.

    perf_gui.py --ip 127.0.0.1 --expect 45 --connect 5 --json-size 800

Phases: idle, discovering ``expect`` providers, opening ``connect`` connection tabs and
watching them, importing a ``json-size`` metric profile and its MDIB export into the
window's own provider. Each phase prints one JSON line with process CPU and event-loop lag:
a 50 ms timer measures how late the GUI thread gets to it, which is what a user feels.
Profile the process from outside (py-spy) while it runs.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from perf_stress import big_device  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402

import run_toolbox  # noqa: E402
from sdctoolbox import config  # noqa: E402
from sdctoolbox.gui.main_window import MainWindow  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402

TICK_MS = 50


class LagProbe:
    """How late a repeating timer fires on the GUI thread."""

    def __init__(self) -> None:
        self.lags: list[float] = []
        self._last = time.perf_counter()
        self._timer = QTimer()
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self) -> None:
        now = time.perf_counter()
        self.lags.append(max(0.0, (now - self._last) * 1000 - TICK_MS))
        self._last = now

    def take(self) -> list[float]:
        lags, self.lags = self.lags, []
        self._last = time.perf_counter()
        return lags


def run_loop(seconds: float) -> None:
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec()


def wait_for(predicate, timeout: float) -> bool:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        run_loop(0.1)
    return bool(predicate())


class Phases:
    def __init__(self, probe: LagProbe) -> None:
        self.probe = probe

    def run(self, name: str, action, **extra) -> object:  # noqa: ANN001
        self.probe.take()
        cpu, wall = time.process_time(), time.perf_counter()
        result = action()
        action_s = time.perf_counter() - wall
        # Work the action queued (signals, deferred deletes, repaints) runs after it returns,
        # so the phase ends when the timer fires on time again.
        settled = len(self.probe.lags)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            run_loop(0.1)
            recent = self.probe.lags[settled:]
            if len(recent) >= 3 and all(lag < 30 for lag in recent[-3:]):
                break
        wall, cpu = time.perf_counter() - wall, time.process_time() - cpu
        lags = self.probe.take()
        row = {"phase": name, "action_s": action_s, "settled_s": wall, "cpu_percent": 100 * cpu / wall,
               "lag_p50_ms": statistics.median(lags) if lags else None,
               "lag_p95_ms": statistics.quantiles(lags, n=20)[-1] if len(lags) >= 20 else max(lags, default=None),
               "lag_max_ms": max(lags, default=None), **extra}
        if isinstance(result, dict):
            row.update(result)
        print(json.dumps({k: round(v, 1) if isinstance(v, float) else v for k, v in row.items()}), flush=True)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ip", default="127.0.0.1")
    parser.add_argument("--expect", type=int, default=0, help="providers the device list should show")
    parser.add_argument("--connect", type=int, default=0, help="connection tabs to open")
    parser.add_argument("--watch", type=float, default=20.0, help="seconds to watch the open connections")
    parser.add_argument("--json-size", type=int, default=0, help="metrics in the imported profile (0: skip)")
    parser.add_argument("--imports", default="json,mdib", help="which imports to run: json, mdib or both")
    parser.add_argument("--idle", type=float, default=10.0)
    args = parser.parse_args()

    app = run_toolbox.create_application(None)
    service = ProviderService(ip=args.ip, instance_name=f"perf-gui-{os.getpid()}")
    service.start()
    window = MainWindow(service)
    window.show()
    probe = LagProbe()
    phases = Phases(probe)
    tabs = window.network_tabs
    try:
        phases.run("idle", lambda: run_loop(args.idle))

        if args.expect:
            pane = tabs.panes[0]
            phases.run("discover", lambda: {"listed": len(pane.devices) if wait_for(
                lambda: len(pane.devices) >= args.expect, 90) else len(pane.devices)}, expected=args.expect)
            phases.run("listed-idle", lambda: run_loop(args.idle), listed=len(pane.devices))

        if args.connect:
            devices = [d for d in tabs.panes[0].devices if d.epr != service.epr.urn][:args.connect]

            def connect_all() -> dict:
                for index, device in enumerate(devices):
                    pane = tabs.panes[0] if index == 0 else tabs.add_connection()
                    if not wait_for(lambda pane=pane, device=device: any(d.epr == device.epr for d in pane.devices), 30):
                        continue
                    pane.device_list.setCurrentRow(next(i for i, d in enumerate(pane.devices) if d.epr == device.epr))
                    pane._on_connect()  # noqa: SLF001 - what the Connect button does
                    wait_for(lambda pane=pane: pane.remote is not None, 30)
                return {"connected": sum(p.remote is not None for p in tabs.panes), "tabs": len(tabs.panes)}

            phases.run("connect", connect_all, requested=len(devices))
            phases.run("watch", lambda: run_loop(args.watch), tabs=len(tabs.panes))

        if args.json_size:
            folder = Path(tempfile.mkdtemp())
            with ProviderService(ip=args.ip, instance_name=f"perf-gui-src-{os.getpid()}") as source:
                source.import_profile(big_device(args.json_size))
                json_path = config.save(source, folder / "big.json")
                xml_path = config.save(source, folder / "big.xml", behaviour=True)
            imports = args.imports.split(",")
            if "json" in imports:
                phases.run("import-json", lambda: {"ok": window.apply_config(config.load_file(json_path), json_path)},
                           metrics=args.json_size)
                phases.run("json-idle", lambda: run_loop(args.idle))
            if "mdib" in imports:
                phases.run("import-mdib", lambda: {"ok": window.apply_config(config.load_file(xml_path), xml_path)},
                           metrics=args.json_size)
                phases.run("mdib-idle", lambda: run_loop(args.idle))
    finally:
        window.close()
        run_loop(0.5)
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
