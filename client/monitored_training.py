"""Optional monitor adapter for existing Ultralytics training entry points.

The caller keeps its original model, Trainer class, and training arguments.
The queue agent supplies credentials and an assigned GPU mask through the
environment; manually launched training remains unchanged without them.
"""

from __future__ import annotations

import os


def agent_device(requested: str | None, *, single_gpu: bool = False) -> str | None:
    """Use the Agent's physical GPU assignment without changing manual runs."""
    if not os.getenv("YOLO_QUEUE_JOB_ID") or requested in {"cpu", "mps", "mps:0"}:
        return requested
    assigned = [part.strip() for part in os.getenv("CUDA_VISIBLE_DEVICES", "").split(",")]
    if not assigned or any(not part.isdecimal() for part in assigned):
        raise RuntimeError("queue job has no valid CUDA_VISIBLE_DEVICES assignment")
    return assigned[0] if single_gpu else ",".join(assigned)


def train_with_monitor(model, *, trainer=None, **train_args):
    """Keep the exact Trainer path; attach only the monitor's callbacks."""
    kwargs = {**train_args, **({"trainer": trainer} if trainer is not None else {})}
    if not os.getenv("YOLO_MONITOR_URL") or not os.getenv("YOLO_MONITOR_TOKEN"):
        if os.getenv("YOLO_QUEUE_JOB_ID"):
            raise RuntimeError("queue job is missing YOLO_MONITOR_URL or YOLO_MONITOR_TOKEN")
        return model.train(**kwargs)

    from yolo_monitor import YoloExperimentMonitor

    name = os.getenv("YOLO_EXPERIMENT_NAME") or str(train_args.get("name") or "")
    monitor = YoloExperimentMonitor(experiment_name=name or None)
    device = str(train_args.get("device") or "")
    if "," in device:
        print("[YOLO monitor] DDP callbacks require an importable monitored Trainer; "
              "live Epoch updates may be unavailable for this entry point", flush=True)
    return monitor.train(model, **kwargs)
