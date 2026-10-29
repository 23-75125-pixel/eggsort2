"""Best-effort, low-frequency resource sampling on the summary thread only."""
import math
import sys
from time import monotonic


def safe(call):
    try:
        return call()
    except Exception:
        return None


def number(value):
    return value if isinstance(value, (int, float)) and math.isfinite(value) else None


class ResourceSampler:
    def __init__(self):
        self.psutil = self.process = None
        self.previous_faults = None
        self.previous_at = monotonic()
        try:
            import psutil  # Optional, already installed on the prototype; no installation.
            self.psutil = psutil
            self.process = psutil.Process()
            # Prime nonblocking CPU counters in this same summary thread.
            safe(lambda: self.process.cpu_percent(interval=None))
            safe(lambda: psutil.cpu_percent(interval=None))
        except Exception:
            pass

    def sample(self):
        at = monotonic()
        result = {"monotonic_s": at, "available": self.psutil is not None and self.process is not None,
                  "temperature_c": None, "effective_clock_mhz": None}
        if not result["available"]:
            result["reason"] = "psutil unavailable or process access denied"
            return result
        psutil, process = self.psutil, self.process
        cpus = number(safe(lambda: psutil.cpu_count()))
        cpu = number(safe(lambda: process.cpu_percent(interval=None)))
        memory = safe(process.memory_info)
        virtual = safe(psutil.virtual_memory)
        swap = safe(psutil.swap_memory)
        freq = safe(lambda: psutil.cpu_freq())
        faults = number(getattr(memory, "num_page_faults", None))
        interval = at - self.previous_at
        rate = ((faults - self.previous_faults) / interval
                if faults is not None and self.previous_faults is not None
                and faults >= self.previous_faults and interval > 0 else None)
        self.previous_faults, self.previous_at = faults, at
        result.update(
            logical_cpus=cpus, sample_interval_s=interval,
            process_cpu_percent_one_core=cpu,
            process_cpu_percent_machine=cpu / cpus if cpu is not None and cpus else None,
            system_cpu_percent=number(safe(lambda: psutil.cpu_percent(interval=None))),
            process_private_bytes=number(getattr(memory, "private", None)),
            process_rss_bytes=number(getattr(memory, "rss", None)),
            process_threads=number(safe(process.num_threads)),
            system_available_bytes=number(getattr(virtual, "available", None)),
            system_memory_percent=number(getattr(virtual, "percent", None)),
            swap_used_bytes=number(getattr(swap, "used", None)),
            swap_percent=number(getattr(swap, "percent", None)),
            swap_in_bytes=number(getattr(swap, "sin", None)) if sys.platform != "win32" else None,
            swap_out_bytes=number(getattr(swap, "sout", None)) if sys.platform != "win32" else None,
            process_page_faults=faults, process_page_faults_per_s=rate,
            reported_clock_mhz=number(getattr(freq, "current", None)),
            reported_max_clock_mhz=number(getattr(freq, "max", None)),
            clock_kind="reported_nominal_not_effective" if sys.platform != "linux" else "reported_current",
        )
        return result
