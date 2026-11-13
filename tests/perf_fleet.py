"""Many providers across hosts: run one role per container on a shared network.

    providers --count 45 --prefix c3 [--metrics 10] [--rate 1]
        starts ``count`` providers on this host's address, prints READY, changes one value
        on each provider ``rate`` times a second, and stops them all on SIGTERM.
    consumer --expect 450 [--connect 20] [--window 10]
        discovers every provider, subscribes to ``connect`` of them, and counts the value
        reports that arrive within ``window`` seconds.

Each role prints JSON lines, as tests/perf_stress.py does.
"""

from __future__ import annotations

import argparse
import json
import random
import signal
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from perf_stress import big_device  # noqa: E402

from sdctoolbox.consumer_service import ConsumerService  # noqa: E402
from sdctoolbox.model import MetricKind  # noqa: E402
from sdctoolbox.provider_service import ProviderService  # noqa: E402


def emit(**row: object) -> None:
    print(json.dumps({k: round(v, 3) if isinstance(v, float) else v for k, v in row.items()}), flush=True)


def rss_mb() -> float | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


def run_providers(args: argparse.Namespace, ip: str) -> None:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    services = [ProviderService(ip=ip, instance_name=f"{args.prefix}-{i:02d}") for i in range(args.count)]
    started, failed = [], []
    start = time.perf_counter()
    for service in services:
        try:
            service.start()
            if args.metrics:
                service.import_profile(big_device(args.metrics))
            started.append(service)
        except Exception as exc:  # noqa: BLE001 - one failed provider is a result
            failed.append(f"{service.instance_name}: {type(exc).__name__}: {exc}")
    emit(role="providers", ip=ip, started=len(started), failed=failed[:3], start_s=time.perf_counter() - start,
         threads=threading.active_count(), rss_mb=rss_mb())
    print("READY", flush=True)

    targets = [(s, next((h for h, spec in s.list_metrics().items() if spec.kind is MetricKind.NUMBER), None))
               for s in started]
    targets = [(s, h) for s, h in targets if h is not None]
    sent, late = 0, 0
    interval = 1 / args.rate if args.rate else None
    next_tick = time.monotonic()
    while not stop.is_set():
        if interval is None:
            stop.wait(1)
            continue
        for service, handle in targets:
            service.set_value(handle, Decimal(sent % 900))
        sent += 1
        next_tick += interval
        delay = next_tick - time.monotonic()
        if delay < 0:
            late += 1
            next_tick = time.monotonic()
        stop.wait(max(delay, 0))
    emit(role="providers", ip=ip, rounds=sent, late_rounds=late, rss_mb=rss_mb())

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(len(started), 1)) as pool:
        list(pool.map(lambda s: s.stop(), started))
    emit(role="providers", ip=ip, stop_all_s=time.perf_counter() - start)


def run_consumer(args: argparse.Namespace, ip: str) -> None:
    consumer = ConsumerService(ip=ip)
    consumer.start()
    remotes = []
    try:
        start = time.perf_counter()
        found = consumer.scan(timeout=args.timeout, expected=args.expect)
        emit(role="consumer", found=len(found), expected=args.expect, discover_s=time.perf_counter() - start,
             hosts=len({d.x_addrs[0].split("/")[2].split(":")[0] for d in found if d.x_addrs}))

        sample = random.Random(1).sample(found, min(args.connect, len(found)))
        counts: dict[str, int] = {}
        lock = threading.Lock()
        connect_times, errors = [], []
        for device in sample:
            t = time.perf_counter()
            try:
                remote = consumer.connect(device)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{device.epr}: {type(exc).__name__}: {exc}")
                continue
            connect_times.append(time.perf_counter() - t)

            def on_values(changed, epr=device.epr) -> None:  # noqa: ANN001
                with lock:
                    counts[epr] = counts.get(epr, 0) + len(changed or ())

            remote.bind(metrics_by_handle=on_values)
            remotes.append(remote)
        emit(role="consumer", connected=len(remotes), connect_avg_ms=1000 * sum(connect_times) / max(len(connect_times), 1),
             connect_max_ms=1000 * max(connect_times, default=0), errors=errors[:3])

        with lock:
            counts.clear()
        time.sleep(args.window)
        with lock:
            per_device = [counts.get(r.epr, 0) for r in remotes]
        emit(role="consumer", window_s=args.window, reports=sum(per_device),
             per_device_min=min(per_device, default=0), per_device_max=max(per_device, default=0),
             silent_devices=sum(1 for n in per_device if n == 0), rss_mb=rss_mb())
    finally:
        for remote in remotes:
            remote.close()
        consumer.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("role", choices=("providers", "consumer"))
    parser.add_argument("--ip", help="address to bind; default: this host's name resolves to it")
    parser.add_argument("--count", type=int, default=45)
    parser.add_argument("--prefix", default=socket.gethostname())
    parser.add_argument("--metrics", type=int, default=10, help="metrics per provider (0: bootstrap only)")
    parser.add_argument("--rate", type=float, default=1.0, help="value changes per provider per second")
    parser.add_argument("--expect", type=int, default=1)
    parser.add_argument("--connect", type=int, default=20)
    parser.add_argument("--window", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    ip = args.ip or socket.gethostbyname(socket.gethostname())
    (run_providers if args.role == "providers" else run_consumer)(args, ip)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
