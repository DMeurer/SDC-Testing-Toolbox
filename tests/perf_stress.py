"""Stress and timing harness: many providers, large JSON and MDIB imports, many subscribers.

Not a pass/fail suite. Each scenario runs at growing sizes and prints one JSON line per size,
so rounds can be compared. ``--profile DIR`` writes a cProfile per size for hotspot hunting.

    .venv/Scripts/python.exe tests/perf_stress.py json --sizes 50,200,800 --profile out/
"""

from __future__ import annotations

import argparse
import cProfile
import json
import pstats
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from script_support import wait_until  # noqa: E402

from sdctoolbox import config  # noqa: E402
from sdctoolbox.consumer_service import ConsumerService  # noqa: E402
from sdctoolbox.model import AlertSpec, MetricKind, MetricSpec  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402

IP = "127.0.0.1"


def big_device(size: int) -> config.DeviceConfig:
    """``size`` metrics over ten sections: numbers (every other one settable), strings, waveforms; an alarm per fourth."""
    metrics, alerts = [], []
    for i in range(size):
        section = f"Section {i % 10}"
        if i % 10 == 9:
            spec = MetricSpec(f"Wave {i}", MetricKind.WAVEFORM, section=section,
                              minimum=Decimal("0"), maximum=Decimal("10"))
        elif i % 10 == 8:
            spec = MetricSpec(f"Text {i}", MetricKind.TEXT, section=section, initial_value="x")
        else:
            spec = MetricSpec(f"Value {i}", MetricKind.NUMBER, section=section, controllable=i % 2 == 0,
                              minimum=Decimal("0"), maximum=Decimal("1000"), initial_value=Decimal("50"))
        metrics.append(spec)
    handles = config._resolve_metric_handles(metrics, set())  # noqa: SLF001 - same handles an import assigns
    for i, spec in enumerate(metrics):
        if i % 4 == 0 and spec.kind is MetricKind.NUMBER:
            alerts.append(AlertSpec(f"Alarm {i}", spec.handle, upper_limit=Decimal("900")))
    assert len(handles) == size
    return config.DeviceConfig(metrics=metrics, alerts=alerts)


@contextmanager
def profiled(directory: Path | None, name: str):
    if directory is None:
        yield
        return
    profile = cProfile.Profile()
    profile.enable()
    try:
        yield
    finally:
        profile.disable()
        directory.mkdir(parents=True, exist_ok=True)
        profile.dump_stats(directory / f"{name}.prof")
        with (directory / f"{name}.txt").open("w") as out:
            pstats.Stats(profile, stream=out).sort_stats("cumulative").print_stats(40)


def timed(fn):  # noqa: ANN001, ANN201
    start = time.perf_counter()
    result = fn()
    return time.perf_counter() - start, result


def emit(**row: object) -> None:
    print(json.dumps({k: round(v, 3) if isinstance(v, float) else v for k, v in row.items()}), flush=True)


def scenario_json(size: int, prof: Path | None) -> None:
    device = big_device(size)
    with ProviderService(ip=IP, instance_name=f"perf-json-{size}") as service:
        with profiled(prof, f"json-{size}"):
            first, _ = timed(lambda: service.import_profile(device))
            again, _ = timed(lambda: service.import_profile(big_device(size)))
        saved, path = timed(lambda: config.save(service, Path(tempfile.mkdtemp()) / "big.json"))
        parsed, _ = timed(lambda: config.load_file(path))
        emit(scenario="json", size=size, alerts=len(device.alerts), import_s=first, reset_s=again,
             save_s=saved, parse_s=parsed, file_kb=path.stat().st_size // 1024)


def scenario_mdib(size: int, prof: Path | None) -> None:
    directory = Path(tempfile.mkdtemp())
    with ProviderService(ip=IP, instance_name=f"perf-mdib-{size}") as service:
        service.import_profile(big_device(size))
        path = config.save(service, directory / "big.xml", behaviour=True)
        with profiled(prof, f"mdib-{size}"):
            parsed, native = timed(lambda: config.load_file(path))
            replaced, _ = timed(lambda: service.replace_mdib(native))
            again, _ = timed(lambda: service.replace_mdib(native))
        edit, _ = timed(lambda: service.add_metric(MetricSpec("Late", MetricKind.NUMBER, initial_value=Decimal("1"))))
        emit(scenario="mdib", size=size, file_kb=path.stat().st_size // 1024, parse_s=parsed,
             replace_s=replaced, replace_again_s=again, add_metric_after_s=edit)


def scenario_providers(size: int, prof: Path | None) -> None:
    services = [ProviderService(ip=IP, instance_name=f"perf-p{size}-{i}") for i in range(size)]
    threads_before = threading.active_count()
    consumer = ConsumerService(ip=IP)
    started = []
    try:
        with profiled(prof, f"providers-{size}"):
            start = time.perf_counter()
            for service in services:
                service.start()
                started.append(service)
            start_s = time.perf_counter() - start
        threads = threading.active_count() - threads_before
        consumer.start()
        eprs = {service.epr.urn for service in services}
        found: set[str] = set()

        def all_found() -> bool:
            found.update(d.epr for d in consumer.scan(timeout=2, expected=size) if d.epr in eprs)
            return found >= eprs

        scan_s, ok = timed(lambda: wait_until(all_found, timeout=60, interval=0.1))
        remote = consumer.connect(next(d for d in consumer.known_devices() if d.epr == services[-1].epr.urn))
        remote.close()
    finally:
        consumer.stop()
        stop_s, _ = timed(lambda: [service.stop() for service in started])
    emit(scenario="providers", size=size, start_s=start_s, start_each_ms=start_s / size * 1000,
         threads=threads, found=len(found), scan_s=scan_s, all_found=ok, stop_s=stop_s)


def scenario_subscribers(size: int, prof: Path | None, updates: int = 50) -> None:
    consumers = [ConsumerService(ip=IP) for _ in range(min(size, 4))]
    remotes = []
    with ProviderService(ip=IP, instance_name=f"perf-sub-{size}") as service:
        service.import_profile(big_device(50))
        handle = next(h for h, s in service.list_metrics().items() if s.kind is MetricKind.NUMBER)
        try:
            for consumer in consumers:
                consumer.start()
            device = next(d for d in consumers[0].scan(timeout=10, expected=1) if d.epr == service.epr.urn)
            connect_s, _ = timed(lambda: remotes.extend(consumers[i % len(consumers)].connect(device)
                                                        for i in range(size)))
            with profiled(prof, f"subscribers-{size}"):
                start = time.perf_counter()
                for value in range(updates):
                    service.set_value(handle, Decimal(value))
                publish_s = time.perf_counter() - start
                last = Decimal(updates - 1)
                ok = wait_until(lambda: all(r.metrics([handle])[handle].value == last for r in remotes), timeout=60)
                deliver_s = time.perf_counter() - start
        finally:
            for remote in remotes:
                remote.close()
            for consumer in consumers:
                consumer.stop()
    emit(scenario="subscribers", size=size, connect_s=connect_s, connect_each_ms=connect_s / size * 1000,
         updates=updates, publish_s=publish_s, set_value_ms=publish_s / updates * 1000,
         all_delivered_s=deliver_s, delivered=ok)


SCENARIOS = {"json": scenario_json, "mdib": scenario_mdib, "providers": scenario_providers,
             "subscribers": scenario_subscribers}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario", choices=SCENARIOS)
    parser.add_argument("--sizes", default="10,50", help="comma separated sizes")
    parser.add_argument("--profile", type=Path, help="write cProfile output per size to this directory")
    args = parser.parse_args()
    for size in (int(s) for s in args.sizes.split(",")):
        try:
            SCENARIOS[args.scenario](size, args.profile)
        except Exception as exc:  # noqa: BLE001 - a failure at one size is a result, not the end
            emit(scenario=args.scenario, size=size, error=f"{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
