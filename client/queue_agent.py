#!/usr/bin/env python3
"""Safe, recoverable queue worker for a YOLO training machine (stdlib only)."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


OOM_RE = re.compile(r"cuda out of memory|cudnn_status_alloc_failed|outofmemoryerror", re.I)
DISK_RE = re.compile(r"no space left on device|disk quota exceeded", re.I)
NONFINITE_RE = re.compile(r"\b(?:nan|inf|infinity)\b", re.I)
MONITOR_ANOMALY_PREFIX = "[YOLO monitor anomaly]"
QUEUE_CONTROL_PREFIX = "[YOLO queue control]"
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OUTPUT_DIR_RE = re.compile(r"Logging results to\s+(.+?)\s*$", re.I | re.M)


class Agent:
    def __init__(self, config: dict):
        self.config = config
        self.server = str(config["server_url"]).rstrip("/")
        self.token = str(config["api_token"])
        self.agent_id = str(config.get("agent_id") or socket.gethostname())
        self.hostname = socket.gethostname()
        self.poll_seconds = max(2, int(config.get("poll_seconds", 10)))
        self.allowed_roots = [Path(value).expanduser().resolve() for value in config.get("allowed_roots", [])]
        raw_scan_patterns = config.get(
            "script_scan_patterns",
            ["train_dronevehicle*.py", "train_flir*.py"],
        )
        if isinstance(raw_scan_patterns, str):
            raw_scan_patterns = [raw_scan_patterns]
        self.script_scan_patterns = []
        if isinstance(raw_scan_patterns, (list, tuple)):
            for value in raw_scan_patterns:
                pattern = str(value).strip()
                if not pattern or Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                    continue
                if pattern not in self.script_scan_patterns:
                    self.script_scan_patterns.append(pattern)
        self.training_python = str(config.get("training_python") or sys.executable)
        self.allowed_executables = set(config.get("allowed_executables", ["python", "python3"]))
        self.allowed_executables.add(Path(self.training_python).name)
        self.idle_utilization = float(config.get("gpu_idle_utilization_percent", 5))
        self.idle_memory_used = float(config.get("gpu_idle_memory_used_mb", 3000))
        self.idle_since: dict[int, float] = {}
        self.opener = build_opener(ProxyHandler({}))
        self.stop_requested = False
        self.current_job: dict | None = None
        self.current_process: subprocess.Popen | None = None
        self.current_pid = 0
        self.cancelled_by_server = False
        self.pause_marker: dict | None = None
        self.detected_anomaly: tuple[str, str] | None = None
        self.log_offset = 0
        self.log_dir = Path(config.get("log_directory", "~/.local/state/yolo-monitor-agent")).expanduser()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = Path(config.get("state_file") or (self.log_dir / "agent-state.json")).expanduser()
        self.catalog: list[dict] = []
        self.catalog_at = 0.0
        self._load_state()

    def request(self, path: str, payload: dict) -> dict:
        request = Request(
            self.server + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json", "User-Agent": "yolo-monitor-queue-agent/2.0"},
            method="POST",
        )
        with self.opener.open(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def _write_state(self):
        data = {}
        if self.current_job and self.current_pid:
            data = {
                "job": self.current_job,
                "pid": self.current_pid,
                "log_path": str(self.log_path(self.current_job)),
                "control_path": str(self.control_path(self.current_job)),
                "log_offset": self.log_offset,
                "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)

    def _load_state(self):
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            job = data.get("job") if isinstance(data, dict) else None
            pid = int(data.get("pid") or 0) if isinstance(data, dict) else 0
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return
        if job and pid and self.process_matches(pid, str(job.get("id") or "")):
            self.current_job, self.current_pid = job, pid
            self.log_offset = max(0, int(data.get("log_offset") or 0))
            print(f"[queue agent] recovered running job {job.get('name')} (PID {pid})", flush=True)
        elif job:
            self.current_job, self.current_pid = job, 0
            print(f"[queue agent] recovered stale lease for {job.get('name')}", flush=True)

    def log_path(self, job: dict) -> Path:
        return self.log_dir / f"{job['id']}.log"

    def control_path(self, job: dict) -> Path:
        return self.log_dir / f"{job['id']}.control.json"

    @staticmethod
    def process_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        try:
            stat_tail = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].strip()
            state = stat_tail.split(maxsplit=1)[0]
            if state in {"Z", "X"}:
                return False
        except (OSError, IndexError):
            pass
        return True

    def process_matches(self, pid: int, job_id: str) -> bool:
        if not self.process_alive(pid):
            return False
        try:
            values = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            return f"YOLO_QUEUE_JOB_ID={job_id}".encode() in values
        except OSError:
            return False

    def gpu_status(self) -> list[dict]:
        query = "index,name,utilization.gpu,memory.used,memory.free,memory.total,temperature.gpu"
        try:
            result = subprocess.run(["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=4, check=False)
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode:
            return []
        now = time.monotonic()
        gpus = []
        for line in result.stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 7:
                continue
            try:
                index, utilization, used, free, total, temperature = int(parts[0]), float(parts[2]), float(parts[3]), float(parts[4]), float(parts[5]), float(parts[6])
            except ValueError:
                continue
            idle = utilization <= self.idle_utilization and used <= self.idle_memory_used
            self.idle_since.setdefault(index, now) if idle else self.idle_since.pop(index, None)
            gpus.append({"index": index, "name": parts[1], "utilization_percent": utilization, "memory_used_mb": used, "memory_free_mb": free, "memory_total_mb": total, "temperature_c": temperature, "idle_for_seconds": round(now - self.idle_since[index], 1) if index in self.idle_since else 0})
        return gpus

    @staticmethod
    def _literal(node):
        try:
            return ast.literal_eval(node)
        except (ValueError, TypeError):
            return None

    def _path_expression(self, node, root: Path) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = Path(node.value).expanduser()
            return str(value if value.is_absolute() else (root / value).resolve())
        if isinstance(node, ast.Call) and node.args:
            name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            if name in {"str", "Path"}:
                return self._path_expression(node.args[0], root)
            if name == "repo_path":
                parts = [self._literal(arg) for arg in node.args]
                if all(isinstance(part, str) for part in parts):
                    return str(root.joinpath(*parts).resolve())
        return ""

    def queue_runtime_default_batch(self, root: Path):
        helper = root / "tools" / "queue_runtime.py"
        try:
            tree = ast.parse(helper.read_text(encoding="utf-8"), filename=str(helper))
        except (OSError, UnicodeDecodeError, SyntaxError):
            return self.config.get("default_batch")
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "resolve_queue_runtime":
                positional = node.args.posonlyargs + node.args.args
                offset = len(positional) - len(node.args.defaults)
                defaults = {arg.arg: self._literal(value) for arg, value in zip(positional[offset:], node.args.defaults)}
                keyword_defaults = {arg.arg: self._literal(value) for arg, value in zip(node.args.kwonlyargs, node.args.kw_defaults) if value is not None}
                value = {**defaults, **keyword_defaults}.get("default_batch")
                return value if isinstance(value, int) and value > 0 else self.config.get("default_batch")
        return self.config.get("default_batch")

    @staticmethod
    def _leading_comments(source: str, limit: int = 80) -> list[str]:
        comments = []
        for line in source.splitlines()[:limit]:
            stripped = line.strip()
            if stripped.startswith("#"):
                comments.append(stripped[1:].strip())
            elif stripped and comments:
                break
        return comments

    @staticmethod
    def _resolve_project_path(value: str, root: Path, relative_to: Path | None = None) -> Path:
        path = Path(value.strip().strip("`'\"")).expanduser()
        if path.is_absolute():
            return path.resolve()
        candidates = []
        if relative_to is not None:
            candidates.append((relative_to / path).resolve())
        candidates.append((root / path).resolve())
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]

    def inspect_model_yaml(self, model_yaml: Path, root: Path) -> dict:
        result = {
            "model_yaml": str(model_yaml),
            "model_yaml_name": model_yaml.name,
            "father_yaml": "",
            "father_yaml_name": "",
            "change_summary": "",
            "lineage_relation": "",
            "lineage_confidence": "",
        }
        try:
            source = model_yaml.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return result
        comments = self._leading_comments(source)
        relation_index = -1
        relation_match = None
        pattern = re.compile(
            r"本文件以\s*[`'\"]?([^`'\"，,\s]+\.ya?ml)[`'\"]?\s*为(父实验|对照)",
            re.I,
        )
        for index, comment in enumerate(comments):
            match = pattern.search(comment)
            if match:
                relation_index, relation_match = index, match
                break
        if relation_match is None:
            return result
        father = self._resolve_project_path(relation_match.group(1), root, model_yaml.parent)
        relation = "father" if relation_match.group(2) == "父实验" else "reference"
        result.update({
            "father_yaml": str(father),
            "father_yaml_name": father.name,
            "lineage_relation": relation,
            "lineage_confidence": "exact" if relation == "father" else "reference",
        })
        if relation != "father":
            return result
        summary_lines = []
        for comment in comments[relation_index + 1:]:
            text = comment.strip()
            if not text:
                if summary_lines:
                    break
                continue
            summary_lines.append(text)
            if text.endswith("。"):
                break
        result["change_summary"] = "\n".join(summary_lines)[:8000]
        return result

    def inspect_script(self, script: Path, root: Path) -> dict:
        try:
            source = script.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(script))
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            return {"error": str(exc)}
        assignments = {}
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        assignments[target.id] = node.value
        experiment = ""
        for key in ("EXPERIMENT_NAME", "experiment_name", "NAME"):
            value = self._literal(assignments.get(key))
            if isinstance(value, str):
                experiment = value
                break
        checkpoint = ""
        for key in ("CHECKPOINT", "checkpoint", "MODEL", "model_path"):
            if key in assignments:
                checkpoint = self._path_expression(assignments[key], root)
                if checkpoint:
                    break
        batch = None
        default_device = ""
        data_files = []
        model_files = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func_name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            for keyword in node.keywords:
                if keyword.arg == "batch" and batch is None:
                    value = self._literal(keyword.value)
                    if isinstance(value, int):
                        batch = value
                if keyword.arg == "default_batch" and batch is None:
                    value = self._literal(keyword.value)
                    if isinstance(value, int):
                        batch = value
                if keyword.arg == "default_device":
                    value = self._literal(keyword.value)
                    if isinstance(value, str):
                        default_device = value
                if keyword.arg in {"data", "cfg", "model"}:
                    value = self._path_expression(keyword.value, root)
                    if value:
                        (data_files if keyword.arg == "data" else model_files).append(value)
            if func_name == "YoloExperimentMonitor":
                for keyword in node.keywords:
                    if keyword.arg == "experiment_name":
                        value = self._literal(keyword.value)
                        if isinstance(value, str):
                            experiment = value
                        elif isinstance(keyword.value, ast.Name):
                            value = self._literal(assignments.get(keyword.value.id))
                            if isinstance(value, str):
                                experiment = value
        if batch is None:
            value = self.queue_runtime_default_batch(root)
            batch = int(value) if isinstance(value, int) and value > 0 else None
        model_yaml_info = {}
        yaml_match = re.search(r"^\s*#\s*对应模型\s*YAML\s*[：:]\s*(\S+\.ya?ml)\s*$", source, re.I | re.M)
        if yaml_match:
            model_yaml = self._resolve_project_path(yaml_match.group(1), root)
            model_yaml_info = self.inspect_model_yaml(model_yaml, root)
        script_id = hashlib.sha256(str(script).encode()).hexdigest()[:20]
        return {
            "id": script_id,
            "name": script.name,
            "path": str(script),
            "working_directory": str(root),
            "python": self.training_python,
            "command": shlex.join([self.training_python, str(script)]),
            "task_name": experiment or script.stem,
            "default_batch": batch,
            "checkpoint": checkpoint,
            "data_files": list(dict.fromkeys(data_files)),
            "model_files": list(dict.fromkeys(model_files)),
            "default_device": default_device,
            "default_gpu_count": max(1, len([part for part in default_device.split(",") if part.strip()])),
            **model_yaml_info,
        }

    def scan_catalog(self) -> list[dict]:
        items = []
        limit = max(1, min(1000, int(self.config.get("script_scan_limit", 300))))
        for root in self.allowed_roots:
            scripts = set()
            for pattern in self.script_scan_patterns:
                try:
                    scripts.update(path.resolve() for path in root.glob(pattern) if path.is_file())
                except (OSError, ValueError):
                    continue
            scripts = sorted(scripts)
            for script in scripts:
                if len(items) >= limit:
                    return items
                try:
                    script.relative_to(root)
                except ValueError:
                    continue
                info = self.inspect_script(script, root)
                if not info.get("error"):
                    items.append(info)
        return items

    def refresh_catalog(self):
        interval = max(30, int(self.config.get("script_scan_seconds", 300)))
        if not self.catalog or time.monotonic() - self.catalog_at >= interval:
            self.catalog = self.scan_catalog()
            self.catalog_at = time.monotonic()

    def heartbeat(self, gpus: list[dict]) -> dict:
        self.refresh_catalog()
        response = self.request("/api/v1/agents/heartbeat", {
            "agent_id": self.agent_id, "hostname": self.hostname,
            "state": "running" if self.current_job and self.current_pid else "idle",
            "current_job_id": (self.current_job or {}).get("id", ""), "gpus": gpus,
            "idle_reminder_seconds": int(self.config.get("idle_reminder_seconds", 1800)),
            "idle_reminder_gpu_count": int(self.config.get("idle_reminder_gpu_count", 1)),
            "catalog": self.catalog,
            "capabilities": {"preflight": True, "safe_pause": True, "restart_recovery": True, "version": "2.0", "idle_utilization_percent": self.idle_utilization, "idle_memory_used_mb": self.idle_memory_used},
        })
        current = response.get("current_job") or {}
        if self.current_job and current.get("id") == self.current_job.get("id"):
            before = str(self.current_job.get("output_dir") or "")
            self.current_job.update(current)
            if str(self.current_job.get("output_dir") or "") != before:
                self._write_state()
        if self.current_pid and current.get("status") == "cancelled":
            print("[queue agent] server requested cancellation", flush=True)
            self.cancelled_by_server = True
            self.terminate_process()
        elif self.current_pid and current.get("pause_requested"):
            self.request_safe_pause()
        return response

    def claim(self, gpus: list[dict]) -> dict | None:
        return self.request("/api/v1/agents/claim", {"agent_id": self.agent_id, "gpus": gpus}).get("job")

    def update_job(self, job: dict, status: str, **extra) -> dict:
        return self.request(f"/api/v1/agents/jobs/{job['id']}/status", {"agent_id": self.agent_id, "status": status, **extra}).get("job") or {}

    def output_binding_from_log(self, job: dict, text: str) -> dict:
        clean = ANSI_ESCAPE_RE.sub("", text)
        matches = OUTPUT_DIR_RE.findall(clean)
        if not matches:
            return {}
        raw = matches[-1].strip().strip("'\"")
        output_dir = Path(raw).expanduser()
        if not output_dir.is_absolute():
            output_dir = (Path(job["working_directory"]).expanduser() / output_dir).resolve()
        else:
            output_dir = output_dir.resolve()
        if not self.within_allowed_root(output_dir):
            return {}
        return {
            "save_dir": str(output_dir),
            "last_model": str(output_dir / "weights" / "last.pt"),
            "best_model": str(output_dir / "weights" / "best.pt"),
            "results_csv": str(output_dir / "results.csv"),
        }

    def bind_output(self, job: dict, binding: dict) -> dict:
        if not binding:
            return job
        current_dir = str(job.get("output_dir") or "")
        if current_dir == str(binding.get("save_dir") or ""):
            return job
        response = self.request(
            f"/api/v1/agents/jobs/{job['id']}/binding",
            {"agent_id": self.agent_id, "output_binding": binding},
        )
        updated = response.get("job") or {}
        if updated:
            job.update(updated)
            if self.current_job is job:
                self._write_state()
        print(f"[queue agent] bound output directory: {binding.get('save_dir')}", flush=True)
        return job

    def anomaly(self, job: dict, kind: str, message: str, data=None):
        print(f"[queue agent anomaly] {kind}: {message}", flush=True)
        try:
            self.request(f"/api/v1/agents/jobs/{job['id']}/anomaly", {"agent_id": self.agent_id, "kind": kind, "message": message[-4000:], "data": data or {}})
        except Exception as exc:
            print(f"[queue agent] failed to report anomaly: {exc}", flush=True)

    def within_allowed_root(self, path: Path) -> bool:
        for root in self.allowed_roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                pass
        return False

    @staticmethod
    def command_arguments(job: dict) -> list[str]:
        arguments = shlex.split(job["command_text"])
        batch, resume = job.get("batch_size"), job.get("resume_checkpoint") or ""
        result, index = [], 0
        while index < len(arguments):
            value = arguments[index].replace("{batch}", str(batch or "")).replace("{resume}", resume)
            if batch and value.startswith("batch="):
                value = "batch=" + str(batch)
            result.append(value)
            if batch and value == "--batch" and index + 1 < len(arguments):
                index += 1
                result.append(str(batch))
            index += 1
        return [value for value in result if value]

    def validate_job(self, job: dict):
        cwd = Path(job["working_directory"]).expanduser().resolve()
        if not cwd.is_dir() or not self.within_allowed_root(cwd):
            raise ValueError(f"working directory is outside allowed_roots: {cwd}")
        command = self.command_arguments(job)
        if not command:
            raise ValueError("empty command")
        executable_path = Path(command[0]).expanduser()
        executable = executable_path.name
        if executable not in self.allowed_executables:
            raise ValueError(f"executable is not allowlisted: {executable}")
        resolved_executable = executable_path.resolve() if executable_path.is_absolute() else Path(shutil.which(command[0]) or "")
        if not resolved_executable.is_file():
            raise ValueError(f"python executable does not exist: {command[0]}")
        scripts = []
        for argument in command[1:]:
            if argument.endswith(".py"):
                script = (cwd / argument).resolve() if not Path(argument).is_absolute() else Path(argument).resolve()
                if not script.is_file() or not self.within_allowed_root(script):
                    raise ValueError(f"script is outside allowed_roots: {script}")
                scripts.append(script)
        if not scripts:
            raise ValueError("training command must contain an allowlisted .py script")
        checkpoint = job.get("resume_checkpoint") or ""
        if checkpoint and (not Path(checkpoint).expanduser().is_file() or not self.within_allowed_root(Path(checkpoint).expanduser().resolve())):
            raise ValueError(f"resume checkpoint does not exist or is outside allowed_roots: {checkpoint}")
        free_gb = shutil.disk_usage(cwd).free / 1024 ** 3
        minimum_gb = float(self.config.get("min_disk_free_gb", 10))
        if free_gb < minimum_gb:
            raise RuntimeError(f"disk_free:{free_gb:.1f} GiB available, {minimum_gb:.1f} GiB required")
        return cwd, command, scripts, free_gb

    def preflight(self, job: dict):
        checks = []
        try:
            cwd, command, scripts, free_gb = self.validate_job(job)
            checks.extend([
                {"name": "working_directory", "ok": True, "detail": str(cwd)},
                {"name": "python", "ok": True, "detail": command[0]},
                {"name": "script", "ok": True, "detail": ", ".join(map(str, scripts))},
                {"name": "disk", "ok": True, "detail": f"{free_gb:.1f} GiB free"},
            ])
            catalog_item = next((item for item in self.catalog if item.get("id") == job.get("script_id") or item.get("path") in map(str, scripts)), None)
            for key in ("checkpoint",):
                value = (catalog_item or {}).get(key)
                if value:
                    ok = Path(value).is_file()
                    checks.append({"name": "base_checkpoint", "ok": ok, "detail": value})
                    if not ok:
                        raise ValueError(f"script checkpoint does not exist: {value}")
            for value in (catalog_item or {}).get("data_files", []) + (catalog_item or {}).get("model_files", []):
                ok = Path(value).is_file()
                checks.append({"name": "referenced_file", "ok": ok, "detail": value})
                if not ok:
                    raise ValueError(f"referenced data/model file does not exist: {value}")
            env = os.environ.copy()
            env["CUDA_VISIBLE_DEVICES"] = ""
            result = subprocess.run([command[0], "-c", "import torch, ultralytics; print(torch.__version__, ultralytics.__version__)"], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=max(10, int(self.config.get("preflight_import_timeout_seconds", 45))), check=False)
            detail = (result.stdout + result.stderr).strip()[-1000:]
            checks.append({"name": "imports", "ok": result.returncode == 0, "detail": detail})
            if result.returncode:
                raise RuntimeError("PyTorch/Ultralytics import failed: " + detail)
            status, message = "passed", "Python、脚本、引用文件、磁盘和依赖导入均正常"
        except Exception as exc:
            status, message = "failed", str(exc)
            checks.append({"name": "failure", "ok": False, "detail": message})
        self.request(f"/api/v1/agents/jobs/{job['id']}/preflight", {"agent_id": self.agent_id, "status": status, "checks": checks, "message": message})
        print(f"[queue agent] preflight {status}: {job.get('name')} - {message}", flush=True)

    def request_safe_pause(self):
        if not self.current_job:
            return
        path = self.control_path(self.current_job)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps({"pause_after_epoch": True, "requested_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}), encoding="utf-8")
        temporary.replace(path)
        print("[queue agent] safe pause armed; waiting for current epoch to finish", flush=True)

    def runtime_environment(self, job: dict, assigned: list[str]) -> dict:
        env = os.environ.copy()
        env.update({
            "CUDA_VISIBLE_DEVICES": ",".join(assigned), "YOLO_MONITOR_URL": self.server,
            "YOLO_MONITOR_TOKEN": self.token, "YOLO_MONITOR_RUN_ID": job["run_id"],
            "YOLO_MONITOR_ADOPT_ENV_RUN_ID": "true", "YOLO_EXPERIMENT_NAME": job["name"],
            "YOLO_QUEUE_JOB_ID": job["id"], "YOLO_QUEUE_BATCH": str(job.get("batch_size") or ""),
            "YOLO_QUEUE_RESUME_CHECKPOINT": job.get("resume_checkpoint") or "",
            "YOLO_BASELINE_RUN_ID": job.get("baseline_run_id") or "",
            "YOLO_QUEUE_CONTROL_FILE": str(self.control_path(job)),
        })
        return env

    def start_job(self, job: dict):
        self.current_job, self.cancelled_by_server, self.pause_marker, self.detected_anomaly = job, False, None, None
        try:
            self.log_offset = self.log_path(job).stat().st_size
        except OSError:
            self.log_offset = 0
        try:
            cwd, command, _, _ = self.validate_job(job)
        except RuntimeError as exc:
            self.anomaly(job, "disk_low", str(exc)); self.retry_or_finish(job, "disk_low", str(exc)); self.clear_runtime(); return
        except (ValueError, OSError) as exc:
            self.anomaly(job, "unsafe_job", str(exc)); self.update_job(job, "failed", error=str(exc)); self.clear_runtime(); return
        assigned = [str(index) for index in job.get("assigned_gpus") or []]
        self.control_path(job).unlink(missing_ok=True)
        log_path = self.log_path(job)
        print(f"[queue agent] starting {job['name']} on GPU {','.join(assigned)}: {shlex.join(command)}", flush=True)
        log = log_path.open("ab", buffering=0)
        try:
            self.current_process = subprocess.Popen(command, cwd=str(cwd), env=self.runtime_environment(job, assigned), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            log.close()
        self.current_pid = self.current_process.pid
        self._write_state()
        self.update_job(job, "running", message=f"started on GPU {','.join(assigned)}", runtime_pid=self.current_pid)
        self.monitor_current_process()

    def read_new_log(self):
        if not self.current_job:
            return
        path = self.log_path(self.current_job)
        try:
            with path.open("rb") as stream:
                stream.seek(self.log_offset)
                data = stream.read(1024 * 1024)
                self.log_offset = stream.tell()
        except OSError:
            return
        if not data:
            return
        text = data.decode("utf-8", errors="replace")
        sys.stdout.write(text)
        if not self.current_job.get("output_dir"):
            binding = self.output_binding_from_log(self.current_job, text)
            if binding:
                try:
                    self.bind_output(self.current_job, binding)
                except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                    print(f"[queue agent] output binding failed: {exc}", flush=True)
        for line in text.splitlines():
            if line.startswith(QUEUE_CONTROL_PREFIX):
                try:
                    self.pause_marker = json.loads(line[len(QUEUE_CONTROL_PREFIX):].strip())
                except json.JSONDecodeError:
                    self.pause_marker = {"checkpoint": ""}
            anomaly = self.detect_anomaly(line)
            if anomaly and self.detected_anomaly is None:
                self.detected_anomaly = anomaly
                self.anomaly(self.current_job, anomaly[0], anomaly[1])
                if self.current_job["anomaly_policy"] != "notify":
                    self.terminate_process()
        self._write_state()

    def monitor_current_process(self):
        job = self.current_job
        if not job:
            return
        last_heartbeat = 0.0
        while self.current_pid and self.process_alive(self.current_pid) and not self.stop_requested:
            self.read_new_log()
            if time.monotonic() - last_heartbeat >= self.poll_seconds:
                last_heartbeat = time.monotonic()
                try:
                    self.heartbeat(self.gpu_status())
                except Exception as exc:
                    print(f"[queue agent] heartbeat failed: {exc}", flush=True)
            time.sleep(1)
        self.read_new_log()
        if self.stop_requested and self.current_pid and self.process_alive(self.current_pid):
            self._write_state()
            print(f"[queue agent] Agent stopping; training PID {self.current_pid} remains attached for recovery", flush=True)
            return
        exit_code = self.current_process.poll() if self.current_process else None
        self.finish_process(job, exit_code)

    def finish_process(self, job: dict, exit_code):
        checkpoint = str((self.pause_marker or {}).get("checkpoint") or self.discover_checkpoint(job))
        if self.cancelled_by_server:
            self.update_job(job, "cancelled", message="cancelled by server", last_checkpoint=checkpoint)
        elif self.pause_marker is not None:
            self.update_job(job, "paused", message="paused safely after current epoch", resume_checkpoint=checkpoint, last_checkpoint=checkpoint)
        elif self.current_process is None and exit_code is None:
            message = "Agent 重启后发现原训练进程已经消失"
            self.anomaly(job, "recovery_process_missing", message)
            if checkpoint:
                self.update_job(job, "paused", error=message, resume_checkpoint=checkpoint, last_checkpoint=checkpoint)
            else:
                self.update_job(job, "failed", error=message)
        elif exit_code == 0 and self.detected_anomaly is None:
            self.update_job(job, "completed", message="process exited successfully", last_checkpoint=checkpoint)
        else:
            kind, message = self.detected_anomaly or ("process_exit", f"training process exited with code {exit_code}")
            if self.detected_anomaly is None:
                self.anomaly(job, kind, message, {"exit_code": exit_code})
            self.retry_or_finish(job, kind, message)
        self.clear_runtime()

    def clear_runtime(self):
        self.current_job, self.current_process, self.current_pid = None, None, 0
        self.cancelled_by_server, self.pause_marker, self.detected_anomaly = False, None, None
        self.log_offset = 0
        self._write_state()

    @staticmethod
    def detect_anomaly(line: str) -> tuple[str, str] | None:
        if OOM_RE.search(line): return "cuda_oom", line.strip()
        if DISK_RE.search(line): return "disk_full", line.strip()
        if line.startswith(MONITOR_ANOMALY_PREFIX):
            try:
                data = json.loads(line[len(MONITOR_ANOMALY_PREFIX):].strip())
                return str(data.get("kind") or "monitor"), str(data.get("message") or line.strip())
            except json.JSONDecodeError:
                return "monitor", line.strip()
        if ("loss" in line.lower() or "epoch" in line.lower()) and NONFINITE_RE.search(line):
            return "nonfinite_loss", line.strip()
        return None

    def retry_or_finish(self, job: dict, kind: str, message: str):
        policy = job["anomaly_policy"]
        attempts_left = int(job.get("attempt_count") or 0) <= int(job.get("retry_limit") or 0)
        resume_checkpoint = job.get("resume_checkpoint") or self.discover_checkpoint(job)
        if policy == "retry_lower_batch" and attempts_left and job.get("batch_size"):
            old_batch = int(job["batch_size"]); new_batch = max(int(job.get("min_batch_size") or 1), old_batch // 2)
            if new_batch < old_batch:
                not_before = (datetime.now(timezone.utc) + timedelta(seconds=int(job.get("retry_delay_seconds") or 60))).isoformat(timespec="seconds")
                self.update_job(job, "queued", error=message, batch_size=new_batch, resume_checkpoint=resume_checkpoint, last_checkpoint=resume_checkpoint, not_before=not_before, message=f"batch {old_batch} -> {new_batch}")
                return
        if policy == "retry_when_memory" and attempts_left:
            not_before = (datetime.now(timezone.utc) + timedelta(seconds=int(job.get("retry_delay_seconds") or 60))).isoformat(timespec="seconds")
            self.update_job(job, "waiting_memory", error=message, resume_checkpoint=resume_checkpoint, last_checkpoint=resume_checkpoint, not_before=not_before)
            return
        if policy != "stop" and attempts_left:
            not_before = (datetime.now(timezone.utc) + timedelta(seconds=int(job.get("retry_delay_seconds") or 60))).isoformat(timespec="seconds")
            self.update_job(job, "queued", error=message, resume_checkpoint=resume_checkpoint, last_checkpoint=resume_checkpoint, not_before=not_before)
            return
        self.update_job(job, "failed", error=f"{kind}: {message}", last_checkpoint=resume_checkpoint)

    def discover_checkpoint(self, job: dict) -> str:
        root = Path(job["working_directory"]).expanduser().resolve()
        output_dir_text = str(job.get("output_dir") or "").strip()
        output_last_text = str(job.get("output_last_checkpoint") or "").strip()
        if output_dir_text or output_last_text:
            exact_candidates = []
            if output_last_text:
                output_last = Path(output_last_text).expanduser()
                exact_candidates.append(output_last if output_last.is_absolute() else root / output_last)
            if output_dir_text:
                output_dir = Path(output_dir_text).expanduser()
                output_dir = output_dir if output_dir.is_absolute() else root / output_dir
                exact_candidates.append(output_dir / "weights" / "last.pt")
            for path in exact_candidates:
                try:
                    resolved = path.resolve()
                except OSError:
                    continue
                if resolved.is_file() and self.within_allowed_root(resolved):
                    return str(resolved)
            # Once an output directory is bound, never fall back to another run.
            return ""
        candidates = []
        try:
            for path in root.glob("**/weights/last.pt"):
                if path.is_file() and self.within_allowed_root(path.resolve()):
                    candidates.append(path)
                if len(candidates) >= 500:
                    break
        except OSError:
            return ""
        if not candidates:
            return ""
        try:
            return str(max(candidates, key=lambda path: path.stat().st_mtime))
        except OSError:
            return ""

    def terminate_process(self):
        pid = self.current_pid
        if not pid or not self.process_alive(pid):
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and self.process_alive(pid):
            time.sleep(0.5)
        if self.process_alive(pid):
            try: os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError: pass

    def run_forever(self):
        print(f"[queue agent] {self.agent_id} connected to {self.server}", flush=True)
        while not self.stop_requested:
            try:
                if self.current_job:
                    if self.current_pid and self.process_matches(self.current_pid, str(self.current_job.get("id"))):
                        self.monitor_current_process()
                        continue
                    self.heartbeat(self.gpu_status())
                    self.finish_process(self.current_job, None)
                    continue
                gpus = self.gpu_status()
                response = self.heartbeat(gpus)
                preflights = response.get("preflight_jobs") or []
                if preflights:
                    self.preflight(preflights[0])
                    continue
                job = self.claim(gpus)
                if job:
                    self.start_job(job)
                    continue
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
                print(f"[queue agent] loop error: {exc}", flush=True)
            time.sleep(self.poll_seconds)


def load_config(path: str) -> dict:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if not all(config.get(key) for key in ("server_url", "api_token", "allowed_roots")):
        raise ValueError("server_url, api_token and allowed_roots are required")
    if not str(config["server_url"]).startswith("https://"):
        raise ValueError("server_url must use HTTPS")
    return config


def worker_configs(config: dict) -> list[dict]:
    """Expand one host config into isolated worker slots.

    Slot 1 intentionally keeps the historical base agent/state names. Additional
    slots use ``-slot-N`` and ``.slot-N.json`` so upgrades can recover jobs that
    were already started by an earlier multi-slot Agent.
    """
    count = max(1, min(32, int(config.get("worker_slots") or 1)))
    base_agent_id = str(config.get("agent_id") or socket.gethostname())
    base_state = Path(
        config.get("state_file")
        or (Path(config.get("log_directory", "~/.local/state/yolo-monitor-agent")).expanduser() / "agent-state.json")
    ).expanduser()
    configs = []
    for slot in range(1, count + 1):
        worker = dict(config)
        worker["worker_slot"] = slot
        if slot == 1:
            worker["agent_id"] = base_agent_id
            worker["state_file"] = str(base_state)
        else:
            worker["agent_id"] = f"{base_agent_id}-slot-{slot}"
            worker["state_file"] = str(base_state.with_name(f"{base_state.stem}.slot-{slot}{base_state.suffix}"))
        configs.append(worker)
    return configs


def publish_runtime_bindings(agents: list[Agent]) -> int:
    published = 0
    for agent in agents:
        if not agent.current_job or not agent.current_pid:
            continue
        path = agent.log_path(agent.current_job)
        # A recovered task may have reused an existing log whose startup line is
        # several MiB in. This is a one-shot maintenance command, so scan a
        # generous bounded prefix without changing the live reader offset.
        with path.open("rb") as stream:
            text = stream.read(64 * 1024 * 1024).decode("utf-8", errors="replace")
        binding = agent.output_binding_from_log(agent.current_job, text)
        if not binding:
            print(f"[queue agent] {agent.agent_id}: training log has no output directory", flush=True)
            continue
        agent.bind_output(agent.current_job, binding)
        print(
            f"[queue agent] {agent.agent_id}: published runtime binding for {agent.current_job['id']}",
            flush=True,
        )
        published += 1
    return published


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/yolo-monitor-agent.json")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--publish-catalog", action="store_true")
    parser.add_argument("--publish-runtime-binding", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    agents = [Agent(worker) for worker in worker_configs(config)]
    if args.publish_catalog:
        agent = agents[0]
        catalog = agent.scan_catalog()
        response = agent.request("/api/v1/agents/catalog", {
            "agent_id": agent.agent_id,
            "catalog": catalog,
        })
        print(f"[queue agent] published {len(catalog)} catalog entries", flush=True)
        if not response.get("agent"):
            raise RuntimeError("catalog publish was not acknowledged")
        return
    if args.publish_runtime_binding:
        if not publish_runtime_bindings(agents):
            raise RuntimeError("no recoverable running job was found")
        return
    if args.once:
        agent = agents[0]
        gpus = agent.gpu_status(); response = agent.heartbeat(gpus)
        if response.get("preflight_jobs"): agent.preflight(response["preflight_jobs"][0])
        elif not agent.current_job:
            job = agent.claim(gpus)
            if job: agent.start_job(job)
        return
    def stop_agent(signum, frame):
        for agent in agents:
            agent.stop_requested = True
        print(f"[queue agent] received signal {signum}; preserving child process for recovery", flush=True)
    signal.signal(signal.SIGTERM, stop_agent)
    signal.signal(signal.SIGINT, stop_agent)
    if len(agents) == 1:
        agents[0].run_forever()
        return
    threads = [
        threading.Thread(target=agent.run_forever, name=f"queue-agent-slot-{index}", daemon=False)
        for index, agent in enumerate(agents, start=1)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
