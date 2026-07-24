"""Ultralytics YOLO monitoring callback with no third-party dependencies.

The monitor never raises network errors into the training loop.  For
Ultralytics auto-DDP, use ``RemoteMonitorTrainerMixin`` in an importable
Trainer subclass and call ``monitor.run(tracked_train, ...)`` in the parent.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_QUEUE_CONTROL_PREFIX = "[YOLO queue control]"


def _queue_pause_requested():
    path = os.getenv("YOLO_QUEUE_CONTROL_FILE", "").strip()
    if not path:
        return False
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return bool(payload.get("pause_after_epoch"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _queue_pause_at_epoch_end(trainer):
    """Cooperatively stop every DDP rank at the same epoch boundary."""
    if not _queue_pause_requested():
        return
    trainer.stop = True
    trainer._yolo_queue_paused = True
    local_rank = int(os.getenv("LOCAL_RANK", os.getenv("RANK", "0")) or 0)
    if local_rank in {-1, 0} and not getattr(trainer, "_yolo_queue_pause_announced", False):
        trainer._yolo_queue_pause_announced = True
        checkpoint = str(getattr(trainer, "last", "") or "")
        print(_QUEUE_CONTROL_PREFIX + " " + json.dumps({"action": "paused", "checkpoint": checkpoint}, ensure_ascii=False), flush=True)


def install_queue_control_callback(owner):
    if getattr(owner, "_yolo_queue_control_installed", False):
        return
    if not os.getenv("YOLO_QUEUE_CONTROL_FILE", "").strip():
        return
    owner._yolo_queue_control_installed = True
    owner.add_callback("on_train_epoch_end", _queue_pause_at_epoch_end)


class _TailBuffer:
    def __init__(self, max_chars=24000):
        self.max_chars = max_chars
        self.value = ""
        self.lock = threading.Lock()

    def append(self, value):
        text = _ANSI_RE.sub("", str(value)).replace("\r", "\n")
        with self.lock:
            self.value = (self.value + text)[-self.max_chars :]

    def tail(self, max_chars=12000):
        with self.lock:
            return self.value[-max_chars:]


class _TeeWriter:
    """Preserve terminal output while retaining a bounded text tail."""

    def __init__(self, original, tail_buffer):
        self.original = original
        self.tail_buffer = tail_buffer

    def write(self, value):
        result = self.original.write(value)
        self.tail_buffer.append(value)
        return result

    def flush(self):
        return self.original.flush()

    def __getattr__(self, name):
        return getattr(self.original, name)


def _plain(value):
    """Convert tensors, Paths and config objects to compact JSON values."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    try:
        scalar = value.item()
        return _plain(scalar)
    except (AttributeError, ValueError, TypeError, RuntimeError):
        pass
    return str(value)


def _public_train_args(args):
    if args is None:
        return {}
    try:
        values = vars(args)
    except TypeError:
        return {}
    # Do not transmit secrets or an unbounded custom object.
    blocked = {"api_key", "token", "password", "secret"}
    return {
        str(k): _plain(v)
        for k, v in values.items()
        if not any(word in str(k).lower() for word in blocked)
    }


def _short_command(arguments):
    try:
        result = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _file_identity(value):
    if not isinstance(value, (str, Path)) or not str(value):
        return {}
    path = Path(value).expanduser()
    result = {"path": str(path)}
    try:
        stat = path.stat()
    except OSError:
        return result
    result.update({"size_bytes": stat.st_size, "modified_unix": round(stat.st_mtime, 3)})
    if path.is_file() and stat.st_size <= 5 * 1024 * 1024 and path.suffix.lower() in {
        ".yaml", ".yml", ".json", ".toml", ".py"
    }:
        try:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
            result["sha256"] = digest.hexdigest()
        except OSError:
            pass
        if path.suffix.lower() in {".yaml", ".yml"} and stat.st_size <= 100 * 1024:
            try:
                result["content"] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
    return result


def _reproducibility(train_args):
    args = train_args if isinstance(train_args, dict) else _public_train_args(train_args)
    git_commit = _short_command(["git", "rev-parse", "HEAD"])
    git_branch = _short_command(["git", "branch", "--show-current"])
    git_status = _short_command(["git", "status", "--porcelain"])
    result = {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "entrypoint": str(Path(sys.argv[0]).name),
        "working_directory": str(Path.cwd()),
    }
    if git_commit:
        result["git"] = {
            "commit": git_commit,
            "branch": git_branch or "detached",
            "dirty": bool(git_status),
        }
    libraries = {}
    try:
        import ultralytics

        libraries["ultralytics"] = getattr(ultralytics, "__version__", "unknown")
    except (ImportError, AttributeError):
        pass
    try:
        import torch

        libraries.update(
            {
                "torch": getattr(torch, "__version__", "unknown"),
                "cuda_runtime": getattr(getattr(torch, "version", None), "cuda", None),
                "cudnn": torch.backends.cudnn.version() if hasattr(torch.backends, "cudnn") else None,
            }
        )
    except (ImportError, AttributeError, RuntimeError):
        pass
    result["libraries"] = {key: value for key, value in libraries.items() if value is not None}
    files = {}
    for key in ("model", "data", "cfg"):
        identity = _file_identity(args.get(key))
        if identity:
            files[key] = identity
    if files:
        result["files"] = files
    return result


def _model_configuration(owner):
    model = getattr(owner, "model", None)
    value = getattr(model, "yaml", None)
    if isinstance(value, dict):
        return _plain(value)
    return {}


class YoloExperimentMonitor:
    def __init__(
        self,
        server_url: str | None = None,
        api_token: str | None = None,
        experiment_name: str | None = None,
        run_id: str | None = None,
        started_at_unix: float | None = None,
        use_proxy: bool | None = None,
        batch_update_seconds: float = 15.0,
        request_timeout: float = 3.0,
    ):
        self.server_url = (server_url or os.getenv("YOLO_MONITOR_URL", "")).rstrip("/")
        self.api_token = api_token or os.getenv("YOLO_MONITOR_TOKEN", "")
        self.experiment_name = experiment_name or os.getenv("YOLO_EXPERIMENT_NAME", "")
        self.batch_update_seconds = max(5.0, float(batch_update_seconds))
        self.request_timeout = max(0.5, float(request_timeout))
        if use_proxy is None:
            use_proxy = os.getenv("YOLO_MONITOR_USE_PROXY", "false").lower() in {"1", "true", "yes", "on"}
        self.use_proxy = bool(use_proxy)
        # Use a dedicated opener.  By default the monitor connects directly and
        # ignores unrelated/dead HTTP(S)_PROXY settings on training servers.
        self._opener = build_opener() if self.use_proxy else build_opener(ProxyHandler({}))
        adopt_environment_run = os.getenv("YOLO_MONITOR_ADOPT_ENV_RUN_ID", "").lower() in {"1", "true", "yes", "on"}
        self.run_id = run_id or (os.getenv("YOLO_MONITOR_RUN_ID", "") if adopt_environment_run else "") or uuid.uuid4().hex
        if started_at_unix is None:
            try:
                started_at_unix = float(os.getenv("YOLO_MONITOR_STARTED_AT", ""))
            except ValueError:
                started_at_unix = None
        self.started_at = started_at_unix
        self.last_batch_update = 0.0
        self.finished = False
        self.started = False
        self.last_metrics = {}
        self.last_trainer = None
        self._warned = False
        self._tail_buffer = _TailBuffer()
        self._capture_installed = False
        self._original_stdout = None
        self._original_stderr = None
        self._anomalies_reported = set()
        self._loss_reference = None
        self._extra_parameters = {}

    @staticmethod
    def is_primary_process():
        """Return True only for the parent/single process or global DDP rank 0."""
        rank = os.getenv("RANK", os.getenv("LOCAL_RANK", "-1"))
        try:
            return int(rank) in {-1, 0}
        except ValueError:
            return rank in {"", "-1", "0"}

    def _request(self, path: str, payload: dict, attempts: int = 1):
        if not self.is_primary_process():
            return None
        if not self.server_url or not self.api_token:
            if not self._warned:
                print("[YOLO monitor] URL/token missing; remote monitoring is disabled.")
                self._warned = True
            return None
        body = json.dumps(_plain(payload), ensure_ascii=False).encode("utf-8")
        request = Request(
            self.server_url + path,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
                "User-Agent": "yolo-experiment-monitor/1.0",
            },
            method="POST",
        )
        for attempt in range(max(1, attempts)):
            try:
                with self._opener.open(request, timeout=self.request_timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (HTTPError, URLError, TimeoutError, socket.timeout, OSError, json.JSONDecodeError) as exc:
                if attempt + 1 < attempts:
                    time.sleep(0.5)
                else:
                    # A monitoring failure is reported locally and never raised into training.
                    print(f"[YOLO monitor] update skipped: {exc}")
        return None

    def install_console_capture(self):
        if self._capture_installed or not self.is_primary_process():
            return
        self._original_stdout, self._original_stderr = sys.stdout, sys.stderr
        sys.stdout = _TeeWriter(sys.stdout, self._tail_buffer)
        sys.stderr = _TeeWriter(sys.stderr, self._tail_buffer)
        self._capture_installed = True

    def restore_console_capture(self):
        if not self._capture_installed:
            return
        if isinstance(sys.stdout, _TeeWriter) and sys.stdout.tail_buffer is self._tail_buffer:
            sys.stdout = self._original_stdout
        if isinstance(sys.stderr, _TeeWriter) and sys.stderr.tail_buffer is self._tail_buffer:
            sys.stderr = self._original_stderr
        self._capture_installed = False

    def log_tail(self):
        return self._tail_buffer.tail()

    @staticmethod
    def _memory_status():
        values = {}
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as handle:
                for line in handle:
                    key, raw = line.split(":", 1)
                    values[key] = int(raw.strip().split()[0])
        except (OSError, ValueError, IndexError):
            return {}
        total_kb = values.get("MemTotal", 0)
        available_kb = values.get("MemAvailable", values.get("MemFree", 0))
        used_kb = max(0, total_kb - available_kb)
        return {
            "total_mb": round(total_kb / 1024),
            "used_mb": round(used_kb / 1024),
            "available_mb": round(available_kb / 1024),
            "used_percent": round(100 * used_kb / total_kb, 1) if total_kb else None,
        }

    @staticmethod
    def _selected_gpu_indices(trainer):
        raw = os.getenv("CUDA_VISIBLE_DEVICES", "")
        if not raw:
            raw = str(getattr(getattr(trainer, "args", None), "device", "") or "")
        values = {part.strip() for part in raw.replace("cuda:", "").split(",")}
        return {value for value in values if value.isdigit()}

    @staticmethod
    def _gpu_status(trainer):
        query = (
            "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu"
        )
        try:
            completed = subprocess.run(
                ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if completed.returncode:
            return []
        selected = YoloExperimentMonitor._selected_gpu_indices(trainer)
        result = []
        for line in completed.stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 6 or (selected and parts[0] not in selected):
                continue
            try:
                result.append(
                    {
                        "index": int(parts[0]),
                        "name": parts[1],
                        "utilization_percent": float(parts[2]),
                        "memory_used_mb": float(parts[3]),
                        "memory_total_mb": float(parts[4]),
                        "temperature_c": float(parts[5]),
                    }
                )
            except ValueError:
                continue
        return result

    @staticmethod
    def _host_status(trainer):
        cpu_count = os.cpu_count() or 1
        try:
            load1, load5, load15 = os.getloadavg()
        except (AttributeError, OSError):
            load1 = load5 = load15 = 0.0
        return {
            "hostname": socket.gethostname(),
            "sampled_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
            "cpu_count": cpu_count,
            "load": {
                "load1": round(load1, 2),
                "load5": round(load5, 2),
                "load15": round(load15, 2),
                "per_cpu_percent": round(min(999.9, load1 / cpu_count * 100), 1),
            },
            "memory": YoloExperimentMonitor._memory_status(),
            "gpus": YoloExperimentMonitor._gpu_status(trainer),
        }

    def _start(self, name: str, total_epochs: int, parameters: dict):
        if self.started_at is None:
            self.started_at = time.time()
        public_parameters = {"host": socket.gethostname(), **parameters, **self._extra_parameters}
        public_parameters["reproducibility"] = _reproducibility(
            parameters.get("train_args") or {}
        )
        self._request(
            "/api/v1/runs/start",
            {
                "run_id": self.run_id,
                "name": name,
                "total_epochs": total_epochs,
                "parameters": public_parameters,
                "baseline_run_id": os.getenv("YOLO_BASELINE_RUN_ID", ""),
            },
            attempts=2,
        )
        self.started = True

    def start(self, total_epochs: int = 0, parameters=None, experiment_name=None):
        """Create the remote run now and export its identity to DDP children."""
        name = experiment_name or self.experiment_name or f"YOLO-{self.run_id[:8]}"
        self.experiment_name = name
        if self.started_at is None:
            self.started_at = time.time()

        # Ultralytics' generated DDP script inherits these environment variables.
        os.environ["YOLO_MONITOR_URL"] = self.server_url
        os.environ["YOLO_MONITOR_TOKEN"] = self.api_token
        os.environ["YOLO_MONITOR_RUN_ID"] = self.run_id
        os.environ["YOLO_EXPERIMENT_NAME"] = name
        os.environ["YOLO_MONITOR_STARTED_AT"] = str(self.started_at)
        os.environ["YOLO_MONITOR_USE_PROXY"] = "true" if self.use_proxy else "false"

        self._start(name, int(total_epochs or 0), {"train_args": _plain(parameters or {})})
        return self

    @staticmethod
    def _total_batches(trainer):
        try:
            return len(trainer.train_loader)
        except (AttributeError, TypeError):
            return None

    @staticmethod
    def _loss_metrics(trainer):
        result = {}
        losses = getattr(trainer, "tloss", None)
        names = getattr(trainer, "loss_names", None)
        if losses is None:
            return result
        try:
            values = losses.detach().cpu().tolist()
        except AttributeError:
            values = _plain(losses)
        if not isinstance(values, list):
            values = [values]
        if isinstance(names, str):
            names = [names]
        if not names:
            names = [f"loss_{index + 1}" for index in range(len(values))]
        for name, value in zip(names, values):
            result[f"train/{name}"] = _plain(value)
        return result

    @staticmethod
    def _metrics(trainer):
        metrics = {}
        for source in (getattr(trainer, "metrics", None), getattr(trainer, "lr", None)):
            if isinstance(source, dict):
                metrics.update({str(k): _plain(v) for k, v in source.items()})
        metrics.update(YoloExperimentMonitor._loss_metrics(trainer))
        return {k: v for k, v in metrics.items() if isinstance(v, (int, float)) and v is not None}

    @staticmethod
    def _output_binding(trainer):
        save_dir = getattr(trainer, "save_dir", None)
        if save_dir is None:
            return {}
        save_dir = Path(save_dir).expanduser()
        last_model = getattr(trainer, "last", None) or (save_dir / "weights" / "last.pt")
        best_model = getattr(trainer, "best", None) or (save_dir / "weights" / "best.pt")
        return {
            "save_dir": str(save_dir),
            "last_model": str(last_model),
            "best_model": str(best_model),
            "results_csv": str(save_dir / "results.csv"),
        }

    def _progress_payload(self, trainer, phase: str, include_metrics: bool):
        now = time.time()
        elapsed = now - self.started_at if self.started_at else 0
        epoch = int(getattr(trainer, "epoch", 0)) + 1
        total_epochs = int(getattr(trainer, "epochs", 0) or getattr(getattr(trainer, "args", None), "epochs", 0) or 0)
        batch_i = getattr(trainer, "batch_i", None)
        batch = int(batch_i) + 1 if batch_i is not None else None
        total_batches = self._total_batches(trainer)
        fraction = float(epoch - 1)
        if phase == "batch" and batch and total_batches:
            fraction += batch / total_batches
        elif phase == "epoch":
            fraction = float(epoch)
        eta = (elapsed / fraction) * max(0.0, total_epochs - fraction) if fraction > 0 and total_epochs else None
        metrics = self._metrics(trainer) if include_metrics else {}
        if metrics:
            self.last_metrics = metrics
        if include_metrics:
            self._detect_loss_anomaly(trainer, metrics)
        return {
            "epoch": epoch,
            "batch": batch,
            "total_batches": total_batches,
            "phase": phase,
            "elapsed_seconds": round(elapsed, 1),
            "eta_seconds": round(eta, 1) if eta is not None else None,
            "metrics": metrics,
            "log_tail": self.log_tail(),
            "host_status": self._host_status(trainer),
            "output_binding": self._output_binding(trainer),
        }

    def _detect_loss_anomaly(self, trainer, metrics):
        values = []
        losses = getattr(trainer, "tloss", None)
        try:
            raw_values = losses.detach().cpu().tolist()
        except AttributeError:
            raw_values = _plain(losses)
        if not isinstance(raw_values, list):
            raw_values = [raw_values]
        for value in raw_values:
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                pass
        if any(not math.isfinite(value) for value in values):
            self._emit_anomaly("nonfinite_loss", "loss 出现 NaN 或 Inf", {"values": values})
            return
        positive = [abs(value) for value in values if math.isfinite(value) and value != 0]
        if not positive:
            return
        current = max(positive)
        if self._loss_reference is None:
            self._loss_reference = current
            return
        multiplier = max(2.0, float(os.getenv("YOLO_ANOMALY_LOSS_MULTIPLIER", "50")))
        if current > self._loss_reference * multiplier and current > 10:
            self._emit_anomaly(
                "loss_explosion",
                f"loss {current:.6g} 超过参考值 {self._loss_reference:.6g} 的 {multiplier:.1f} 倍",
                {"current": current, "reference": self._loss_reference, "metrics": metrics},
            )
        self._loss_reference = min(self._loss_reference, current)

    def _emit_anomaly(self, kind, message, data):
        if kind in self._anomalies_reported:
            return
        self._anomalies_reported.add(kind)
        print("[YOLO monitor anomaly] " + json.dumps({"kind": kind, "message": message, "data": _plain(data)}, ensure_ascii=False), flush=True)

    def on_train_start(self, trainer):
        self.last_trainer = trainer
        args = getattr(trainer, "args", None)
        total_epochs = int(getattr(trainer, "epochs", 0) or getattr(args, "epochs", 0) or 0)
        name = self.experiment_name or str(getattr(args, "name", "") or f"YOLO-{self.run_id[:8]}")
        # Re-sending start is safe and enriches a pre-start record with Trainer args.
        self._start(name, total_epochs, {"train_args": _public_train_args(args)})
        binding = self._output_binding(trainer)
        if binding:
            self._request(
                f"/api/v1/runs/{self.run_id}/progress",
                {
                    "epoch": int(getattr(trainer, "epoch", 0)) + 1,
                    "phase": "binding",
                    "output_binding": binding,
                },
            )

    def on_train_batch_end(self, trainer):
        self.last_trainer = trainer
        now = time.monotonic()
        if now - self.last_batch_update < self.batch_update_seconds:
            return
        self.last_batch_update = now
        self._request(
            f"/api/v1/runs/{self.run_id}/progress",
            self._progress_payload(trainer, "batch", include_metrics=False),
        )

    def on_fit_epoch_end(self, trainer):
        self.last_trainer = trainer
        self._request(
            f"/api/v1/runs/{self.run_id}/progress",
            self._progress_payload(trainer, "epoch", include_metrics=True),
        )

    def on_train_end(self, trainer):
        self.last_trainer = trainer
        result = {
            "save_dir": _plain(getattr(trainer, "save_dir", None)),
            "best_model": _plain(getattr(trainer, "best", None)),
            "last_model": _plain(getattr(trainer, "last", None)),
            "best_fitness": _plain(getattr(trainer, "best_fitness", None)),
        }
        try:
            status = "paused" if getattr(trainer, "_yolo_queue_paused", False) or _queue_pause_requested() else "completed"
            self.finish(status, metrics=self._metrics(trainer), result=result)
        finally:
            self.restore_console_capture()

    def finish(self, status: str, metrics=None, result=None, error="", log_tail=""):
        if self.finished:
            return
        elapsed = time.time() - self.started_at if self.started_at else None
        self._request(
            f"/api/v1/runs/{self.run_id}/finish",
            {
                "status": status,
                "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
                "metrics": metrics or self.last_metrics,
                "result": result or {},
                "error": error,
                "log_tail": (self.log_tail() + "\n" + log_tail)[-12000:],
            },
            attempts=3,
        )
        self.finished = True

    def attach(self, model):
        return self.attach_to_callback_owner(model)

    def attach_to_callback_owner(self, owner):
        self.install_console_capture()
        install_queue_control_callback(owner)
        owner.add_callback("on_train_start", self.on_train_start)
        owner.add_callback("on_train_batch_end", self.on_train_batch_end)
        # Official Ultralytics docs state that validation metrics are available here.
        owner.add_callback("on_fit_epoch_end", self.on_fit_epoch_end)
        owner.add_callback("on_train_end", self.on_train_end)
        return self

    def train(self, model, **train_args):
        """Attach callbacks, train, and report exceptions before re-raising them."""
        self.attach(model)
        self._extra_parameters["model_config"] = _model_configuration(model)
        # Create the run before Ultralytics initializes datasets/devices so even an
        # early configuration error can be reported as a failed experiment.
        name = self.experiment_name or str(train_args.get("name") or f"YOLO-{self.run_id[:8]}")
        self.start(int(train_args.get("epochs") or 0), train_args, name)
        try:
            return model.train(**train_args)
        except BaseException as exc:
            trace = traceback.format_exc()
            try:
                self.finish("failed", error=f"{type(exc).__name__}: {exc}", log_tail=trace)
            finally:
                self.restore_console_capture()
            raise

    def run(self, train_callable, *args, **train_args):
        """Run a custom training wrapper with immediate start and failure reporting.

        The Trainer used by ``train_callable`` must inherit
        ``RemoteMonitorTrainerMixin`` so the generated DDP children attach the
        callbacks using the shared run ID.
        """
        if args:
            self._extra_parameters["model_config"] = _model_configuration(args[0])
        name = self.experiment_name or str(train_args.get("name") or f"YOLO-{self.run_id[:8]}")
        self.start(int(train_args.get("epochs") or 0), train_args, name)
        try:
            return train_callable(*args, **train_args)
        except BaseException as exc:
            trace = traceback.format_exc()
            self.finish("failed", error=f"{type(exc).__name__}: {exc}", log_tail=trace)
            raise


def monitor_from_ddp_environment():
    """Build the rank-0 monitor recreated inside an Ultralytics DDP child."""
    if not YoloExperimentMonitor.is_primary_process():
        return None
    run_id = os.getenv("YOLO_MONITOR_RUN_ID", "")
    if not run_id:
        print("[YOLO monitor] DDP run ID missing; Trainer monitoring is disabled.")
        return None
    started_value = os.getenv("YOLO_MONITOR_STARTED_AT", "")
    try:
        started_at = float(started_value)
    except ValueError:
        started_at = None
    return YoloExperimentMonitor(run_id=run_id, started_at_unix=started_at)


def attach_monitor_to_trainer(trainer):
    """Attach the inherited remote run to a real Trainer on rank 0 only."""
    install_queue_control_callback(trainer)
    if hasattr(trainer, "_remote_experiment_monitor"):
        return trainer._remote_experiment_monitor
    monitor = monitor_from_ddp_environment()
    trainer._remote_experiment_monitor = monitor
    if monitor is not None:
        monitor.attach_to_callback_owner(trainer)
        print(f"[YOLO monitor] rank 0 attached to run {monitor.run_id[:8]}.")
    return monitor


class RemoteMonitorTrainerMixin:
    """Mixin for an importable Trainer class reconstructed by Ultralytics DDP."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        attach_monitor_to_trainer(self)


def monitored_train(model, experiment_name=None, **train_args):
    """Convenience entry point using YOLO_MONITOR_URL/TOKEN environment variables."""
    monitor = YoloExperimentMonitor(experiment_name=experiment_name)
    return monitor.train(model, **train_args)
