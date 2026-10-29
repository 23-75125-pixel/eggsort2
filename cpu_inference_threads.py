"""Opt-in CPU thread policy for this inference process; no vendor files edited."""
import logging
from threading import local


def initialize_cpu_threads(threads):
    """Set once before model construction; return read-only predictor verifiers."""
    import torch
    from ultralytics.utils import torch_utils

    # Installed select_device() calls set_num_threads(NUM_THREADS) for CPU.
    # Keep its initialization reset aligned, including any predictor recreation.
    torch_utils.NUM_THREADS = threads
    torch.set_num_threads(threads)
    actual = torch.get_num_threads()
    if actual != threads:
        raise RuntimeError(f"CPU thread initialization failed: requested {threads}, actual {actual}")
    log = logging.getLogger(__name__)
    log.info("CPU inference intra-op limit initialized: requested=%s actual=%s", threads, actual)
    verified_worker = local()

    def verify(predictor):
        device = getattr(predictor, "device", None)
        if getattr(device, "type", str(device)) != "cpu":
            return
        actual = torch.get_num_threads()
        if actual != threads:
            # Do not change scheduling on every frame or conceal an override.
            log.error("CPU inference thread limit overridden: requested=%s actual=%s", threads, actual)
            raise RuntimeError(f"CPU inference thread limit overridden: requested {threads}, actual {actual}. Restart required.")
        if not getattr(verified_worker, "logged", False):
            verified_worker.logged = True
            log.info("CPU predictor thread limit verified on inference worker: requested=%s actual=%s", threads, actual)

    return verify
