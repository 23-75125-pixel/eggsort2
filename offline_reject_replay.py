"""Offline CLI: exported camera observations -> real C++ parser/scheduler -> Python ACK handler.

No app, camera, serial, database, environment configuration, or firmware imports.
All transport is anonymous stdin/stdout to a native PC/WSL executable.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path, PureWindowsPath

from experimental_reject_client import ExperimentalRejectClient
from experimental_reject_export import load_observation_export
from experimental_reject_observer import PassageObservation

ROOT = Path(__file__).resolve().parent
DEFAULT_RUNNER = ROOT / "offline_tests/reject_replay/build/reject_replay"


def demo_events(pair):
    """Clearly synthetic two-egg input; no recorded/physical evidence implied."""
    events = []
    for passage_id, quality in enumerate(pair.split("-"), 1):
        at = 1.01 + (passage_id - 1) * 2.97
        kinds = ["passage_begin", "quality_locked", "passage_exit"]
        if quality != "good":
            kinds.insert(1, "first_defect")
        for kind in kinds:
            events.append(PassageObservation(1, "SYNTHETIC-DEMO", "1", len(events) + 1,
                passage_id, passage_id, kind, at + (0.1 if kind == "passage_exit" else 0), quality,
                at if quality != "good" and kind != "passage_begin" else None))
    return tuple(events)


def wsl_path(path):
    path = PureWindowsPath(str(Path(path).resolve()))
    if not path.drive or len(path.drive) != 2:
        raise ValueError("WSL replay requires a local drive path")
    return f"/mnt/{path.drive[0].lower()}/" + "/".join(path.parts[1:])


def build_runner(output=DEFAULT_RUNNER):
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    sources = [ROOT / "offline_tests/reject_replay/runner.cpp",
               ROOT / "esp32/eggsort_controller/reject_gate_scheduler_tests.cpp"]
    flags = ["-std=c++17", "-O2", "-Wall", "-Wextra", "-pedantic"]
    if os.name == "nt":
        command = ["wsl.exe", "-d", "Ubuntu", "--", "g++", *flags,
                   *(wsl_path(p) for p in sources), "-o", wsl_path(output)]
    else:
        compiler = shutil.which("g++")
        if not compiler:
            raise RuntimeError("Native g++ is required; no installation is performed")
        command = [compiler, *flags, *(str(p) for p in sources), "-o", str(output)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError("Offline C++ compilation failed:\n" + result.stderr)
    return output


@dataclass(frozen=True)
class Assumptions:
    arrival_min_ms: int = 0
    arrival_max_ms: int = 0
    route_clear: bool = False
    clearance_ms: int | None = None
    guard_ms: int = 0
    max_source_age_ms: int = 0

    def config_line(self):
        values = [self.arrival_min_ms, self.arrival_max_ms, self.guard_ms, self.max_source_age_ms]
        if self.clearance_ms is not None:
            values.append(self.clearance_ms)
        if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 60000 for v in values):
            raise ValueError("Simulation timing inputs must be integer milliseconds in 0..60000")
        arrival = self.arrival_min_ms > 0 and self.arrival_max_ms >= self.arrival_min_ms
        return (f"CONFIG {int(arrival)} {int(self.route_clear)} {int(self.clearance_ms is not None)} "
                f"{self.arrival_min_ms} {self.arrival_max_ms} {self.clearance_ms or 0} "
                f"{self.guard_ms} {self.max_source_age_ms}")


class NativeLoopback:
    """Actual C++ replies only. A context manager owns and terminates its child."""
    def __init__(self, runner, assumptions=Assumptions()):
        config = assumptions.config_line()
        command = (["wsl.exe", "-d", "Ubuntu", "--", wsl_path(runner)]
                   if os.name == "nt" else [str(Path(runner).resolve())])
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, bufsize=1)
        self.elapsed = None
        self.process.stdin.write(config + "\n")
        self.process.stdin.flush()

    def exchange(self, elapsed_ms, frame):
        if (isinstance(elapsed_ms, bool) or not isinstance(elapsed_ms, int)
                or elapsed_ms < 0 or (self.elapsed is not None and
                (elapsed_ms < self.elapsed or elapsed_ms - self.elapsed >= 0x80000000))):
            raise ValueError("Simulated time must advance in increments below 2^31 ms")
        if not frame or any(c.isspace() for c in frame) or len(frame) > 80:
            raise ValueError("Invalid loopback command")
        self.elapsed = elapsed_ms
        self.process.stdin.write(f"{elapsed_ms} {frame}\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("Offline C++ runner stopped: " + self.process.stderr.read())
        return json.loads(line)

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.process.stdout.close()
        self.process.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def replay(events, runner, *, assumptions=Assumptions(), latency_ms=0, queue_delay_ms=0,
           tick_ms=1, controller_origin_ms=0, retry_after_ms=None, tail_ms=8000):
    """Replay observed decisions at their actual event times, never at a future final label.

    The controller clock origin is arbitrary. Host intervals map to simulated
    controller elapsed time plus explicitly assumed queue/one-way delay.
    """
    events = tuple(events)
    if (not events or [e.sequence for e in events] != list(range(1, len(events) + 1))
            or any(e.run_id != events[0].run_id for e in events)):
        raise ValueError("A complete ordered single-run observation sequence is required")
    if any(not math.isfinite(e.at) for e in events) or any(b.at < a.at for a, b in zip(events, events[1:])):
        raise ValueError("Observation timestamps must be finite and monotonic")
    for value in (latency_ms, queue_delay_ms, controller_origin_ms, tail_ms):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Simulation clocks/delays must be nonnegative integers")
    if not 1 <= tick_ms <= 1000 or tail_ms > 60000 or controller_origin_ms > 0xFFFFFFFF:
        raise ValueError("Invalid simulation tick, tail, or clock origin")
    if retry_after_ms is not None and not 1 <= retry_after_ms <= 60000:
        raise ValueError("Retry delay must be in 1..60000 ms")

    client = ExperimentalRejectClient(enabled=True, run_id=events[0].run_id)
    report = {"label": "SIMULATION ONLY - NOT PHYSICAL APPROVAL", "assumptions": asdict(assumptions),
              "latency_ms": latency_ms, "queue_delay_ms": queue_delay_ms,
              "tick_ms": tick_ms, "controller_origin_ms": controller_origin_ms,
              "warnings": ["Arrival, movement and Good-route clearance require physical validation",
                           "Good notices are delayed until existing quality lock/exit; prior occupancy is unknown"],
              "passages": {}, "timeline": []}
    jobs = {}
    for e in events:
        passage = report["passages"].setdefault(e.passage_id, {"capture_id": e.capture_id,
                    "run_id": e.run_id, "first_defect_at": None, "final_quality": None})
        if passage["capture_id"] != e.capture_id:
            raise ValueError("Capture ID changed within one observation passage")
        if e.first_defect_at is not None:
            passage["first_defect_at"] = e.first_defect_at
        if e.event == "passage_exit":
            passage["final_quality"] = e.quality
        if e.event == "first_defect" or (e.quality == "good" and e.event in {"quality_locked", "passage_exit"}):
            jobs.setdefault(e.passage_id, e)
    origin = events[0].at
    requests = []
    for passage_id, e in jobs.items():
        source = e.first_defect_at if e.first_defect_at is not None else e.at
        if not math.isfinite(source) or source > e.at or source < origin:
            raise ValueError("Invalid first accepted defect timestamp")
        delivery = controller_origin_ms + round((e.at - origin) * 1000) + queue_delay_ms + latency_ms
        requests.append((delivery, passage_id, False, e, source))
        if retry_after_ms is not None:
            requests.append((delivery + retry_after_ms, passage_id, True, e, source))
    requests.sort(key=lambda item: (item[0], item[1], item[2]))
    if requests and requests[-1][0] - controller_origin_ms > 600000:
        raise ValueError("Replay is bounded to ten minutes per run")

    with NativeLoopback(runner, assumptions) as native:
        hello = native.exchange(controller_origin_ms, client.hello_frame())
        if not client.accept_hello(hello["reply"]):
            raise RuntimeError("Native handshake rejected")
        begin = native.exchange(controller_origin_ms, client.begin_frame())
        if not client.accept_ack(begin["reply"]):
            raise RuntimeError("Native BEGIN rejected")
        report["timeline"].extend([hello, begin])

        def exchange(now, frame, **fields):
            row = native.exchange(now, frame)
            row.update(fields)
            row["input"] = frame
            if row["reply"]:
                row["ack_accepted"] = client.accept_ack(row["reply"])
            if frame != "TICK" or row["reply"] or row["motion"] != "NONE":
                report["timeline"].append(row)
            return row

        next_tick = controller_origin_ms
        for delivery, passage_id, retry, e, source in requests:
            while next_tick < delivery:
                exchange(next_tick, "TICK")
                next_tick += tick_ms
            send_at = e.at + (queue_delay_ms + (retry_after_ms if retry else 0)) / 1000
            try:
                frame = (client.retry_frame(passage_id, now=send_at) if retry else
                         client.request_frame(passage_id, e.quality, detected_at=source, now=send_at))
            except (RuntimeError, ValueError, KeyError) as exc:
                report["timeline"].append({"elapsed_ms": delivery, "passage_id": passage_id,
                                           "host_blocked": str(exc), "retry": retry})
            else:
                exchange(delivery, frame, passage_id=passage_id, capture_id=e.capture_id,
                         first_defect_at=e.first_defect_at, retry=retry,
                         simulated_host_send_at=send_at,
                         source_age_at_accept_ms=(send_at - source) * 1000 + latency_ms)
            # Deliver commands first at this instant, then service simulated scheduler.
            exchange(delivery, "TICK")
            next_tick = delivery + tick_ms
        end = (requests[-1][0] if requests else controller_origin_ms) + tail_ms
        while next_tick <= end:
            exchange(next_tick, "TICK")
            next_tick += tick_ms
        report["final_state"] = exchange(end, "TICK")
    report["client_records"] = {pid: asdict(client.record(pid)) for pid in jobs if pid in client._records}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", nargs="?", type=Path)
    parser.add_argument("--demo", choices=("crack-crack", "crack-rotten", "rotten-crack", "crack-good", "good-crack"))
    parser.add_argument("--runner", type=Path, default=DEFAULT_RUNNER)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--assume-arrival-ms", type=int, nargs=2, default=(0, 0), metavar=("MIN", "MAX"))
    parser.add_argument("--assume-route-clear", action="store_true")
    parser.add_argument("--assume-clearance-ms", type=int)
    parser.add_argument("--guard-ms", type=int, default=0)
    parser.add_argument("--max-source-age-ms", type=int, default=0)
    parser.add_argument("--latency-ms", type=int, default=0)
    parser.add_argument("--queue-delay-ms", type=int, default=0)
    parser.add_argument("--tick-ms", type=int, default=1)
    parser.add_argument("--controller-origin-ms", type=int, default=0)
    parser.add_argument("--retry-after-ms", type=int)
    args = parser.parse_args()
    try:
        runner = args.runner.resolve() if args.no_build else build_runner(args.runner)
        if args.build_only:
            print(f"Offline runner built: {runner}")
            return
        if (args.export is None) == (args.demo is None):
            parser.error("Supply exactly one JSONL export or --demo scenario")
        events = demo_events(args.demo) if args.demo else load_observation_export(args.export)
        report = replay(events, runner, assumptions=Assumptions(*args.assume_arrival_ms,
                        args.assume_route_clear, args.assume_clearance_ms, args.guard_ms, args.max_source_age_ms),
                        latency_ms=args.latency_ms, queue_delay_ms=args.queue_delay_ms, tick_ms=args.tick_ms,
                        controller_origin_ms=args.controller_origin_ms, retry_after_ms=args.retry_after_ms)
        report["input_source"] = "SYNTHETIC DEMO" if args.demo else str(args.export.resolve())
        content = json.dumps(report, indent=2, allow_nan=False)
        if args.output:
            # Never overwrite an export or existing report.
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(content + "\n")
            print(f"SIMULATION ONLY report: {args.output.resolve()}")
        else:
            print(content)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Offline replay failed: {exc}\n")


if __name__ == "__main__":
    main()
