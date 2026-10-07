#!/usr/bin/env python3
"""Lightweight experiment monitor for a small Ubuntu server.

Only Python's standard library is used.  The service is intended to listen on
127.0.0.1 behind Nginx, while training clients push updates over HTTPS.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import hmac
import html
import json
import math
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import threading
import time
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    bind = os.getenv("MONITOR_BIND", "127.0.0.1")
    port = int(os.getenv("MONITOR_PORT", "8765"))
    db_path = os.getenv("MONITOR_DB", "/opt/experiment-monitor/data/monitor.db")
    api_token = os.getenv("MONITOR_API_TOKEN", "")
    admin_password = os.getenv("MONITOR_ADMIN_PASSWORD", "")
    session_secret = os.getenv("MONITOR_SESSION_SECRET", "")
    public_url = os.getenv("MONITOR_PUBLIC_URL", "http://localhost:8765").rstrip("/")
    download_dir = os.getenv("MONITOR_DOWNLOAD_DIR", "/opt/experiment-monitor/downloads")
    ai_config_path = os.getenv("MONITOR_AI_CONFIG", "/opt/experiment-monitor/data/ai_config.json")
    cookie_secure = env_bool("MONITOR_COOKIE_SECURE", True)
    smtp_host = os.getenv("QQ_SMTP_HOST", "smtp.qq.com")
    smtp_port = int(os.getenv("QQ_SMTP_PORT", "465"))
    smtp_user = os.getenv("QQ_SMTP_USER", "")
    smtp_auth_code = os.getenv("QQ_SMTP_AUTH_CODE", "")
    mail_to = os.getenv("MONITOR_MAIL_TO", "")
    stale_seconds = max(60, int(os.getenv("MONITOR_STALE_SECONDS", "600")))
    watchdog_seconds = max(15, int(os.getenv("MONITOR_WATCHDOG_SECONDS", "60")))


CFG = Config()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_json(value, default):
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


AI_PROVIDER_PRESETS = (
    {"id": "deepseek", "name": "DeepSeek", "type": "deepseek",
     "base_url": "https://api.deepseek.com", "models": ["deepseek-v4-flash", "deepseek-v4-pro"]},
    {"id": "openai", "name": "OpenAI", "type": "openai_responses",
     "base_url": "https://api.openai.com/v1", "models": ["gpt-5.6-terra", "gpt-5.6-sol"]},
    {"id": "qwen", "name": "通义千问 / DashScope", "type": "openai_compatible",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "models": ["qwen-plus", "qwen-max"]},
    {"id": "moonshot", "name": "Moonshot / Kimi", "type": "openai_compatible",
     "base_url": "https://api.moonshot.cn/v1", "models": ["moonshot-v1-32k"]},
    {"id": "zhipu", "name": "智谱 GLM", "type": "openai_compatible",
     "base_url": "https://open.bigmodel.cn/api/paas/v4", "models": ["glm-4-plus"]},
    {"id": "anthropic", "name": "Anthropic Claude", "type": "anthropic",
     "base_url": "https://api.anthropic.com/v1", "models": ["claude-sonnet-4-5"]},
    {"id": "gemini", "name": "Google Gemini", "type": "gemini",
     "base_url": "https://generativelanguage.googleapis.com/v1beta", "models": ["gemini-2.5-pro"]},
    {"id": "custom", "name": "自定义 OpenAI-compatible", "type": "openai_compatible",
     "base_url": "https://api.example.com/v1", "models": []},
)


def default_ai_config() -> dict:
    return {
        "enabled": False,
        "auto_on_completed": True,
        "email_report": True,
        "apply_metadata": True,
        "overwrite_metadata": False,
        "selected_models": [],
        "synthesis_model": "",
        "max_output_tokens": 5000,
        "providers": [{**provider, "enabled": False, "api_key": ""} for provider in AI_PROVIDER_PRESETS],
    }


class AiConfigStore:
    """Keep provider API keys in a mode-0600 JSON file, never in SQLite."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.lock = threading.RLock()

    def load(self) -> dict:
        with self.lock:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                return default_ai_config()
        return self.normalize(value)

    @staticmethod
    def normalize(value: dict) -> dict:
        defaults = default_ai_config()
        if not isinstance(value, dict):
            return defaults
        result = {**defaults}
        for field in ("enabled", "auto_on_completed", "email_report", "apply_metadata", "overwrite_metadata"):
            if field in value:
                result[field] = bool(value[field])
        result["max_output_tokens"] = max(500, min(16000, int(value.get("max_output_tokens") or 5000)))
        providers = []
        for item in (value.get("providers") or [])[:20]:
            if not isinstance(item, dict):
                continue
            provider_id = re.sub(r"[^a-z0-9_-]", "", str(item.get("id") or "").lower())[:40]
            provider_type = str(item.get("type") or "openai_compatible")
            base_url = str(item.get("base_url") or "").strip().rstrip("/")
            raw_models = item.get("models") or []
            if isinstance(raw_models, str):
                raw_models = raw_models.replace("，", ",").replace("\n", ",").split(",")
            raw_models = [str(model).strip() for model in raw_models if str(model).strip()]
            if provider_id == "deepseek":
                provider_type = "deepseek"
                if base_url in {"https://api.deepseek.com/v1", "https://api.deepseek.com/v1/"}:
                    base_url = "https://api.deepseek.com"
                if not raw_models or set(raw_models).issubset({"deepseek-chat", "deepseek-reasoner"}):
                    raw_models = ["deepseek-v4-flash", "deepseek-v4-pro"]
            parsed = urlparse(base_url)
            if (
                not provider_id
                or provider_type not in {"deepseek", "openai_compatible", "openai_responses", "anthropic", "gemini"}
                or parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username
                or parsed.password
            ):
                continue
            models = []
            for model in raw_models:
                model = str(model).strip()[:120]
                if model and model not in models:
                    models.append(model)
                if len(models) >= 30:
                    break
            providers.append({
                "id": provider_id,
                "name": str(item.get("name") or provider_id).strip()[:80],
                "type": provider_type,
                "base_url": base_url,
                "models": models,
                "enabled": bool(item.get("enabled")),
                "api_key": str(item.get("api_key") or "").strip()[:1000],
            })
        result["providers"] = providers or defaults["providers"]
        available = {
            f"{provider['id']}:{model}"
            for provider in result["providers"] if provider["enabled"] and provider["api_key"]
            for model in provider["models"]
        }
        selected = []
        deepseek_aliases = {
            "deepseek:deepseek-chat": "deepseek:deepseek-v4-flash",
            "deepseek:deepseek-reasoner": "deepseek:deepseek-v4-pro",
        }
        for reference in value.get("selected_models") or []:
            reference = deepseek_aliases.get(str(reference).strip(), str(reference).strip())
            if reference in available and reference not in selected:
                selected.append(reference)
        result["selected_models"] = selected[:12]
        synthesis = deepseek_aliases.get(
            str(value.get("synthesis_model") or "").strip(),
            str(value.get("synthesis_model") or "").strip(),
        )
        result["synthesis_model"] = synthesis if synthesis in available else ""
        return result

    def save(self, value: dict) -> dict:
        existing = {provider["id"]: provider for provider in self.load().get("providers", [])}
        incoming = dict(value) if isinstance(value, dict) else {}
        providers = []
        for item in incoming.get("providers") or []:
            if not isinstance(item, dict):
                continue
            current = existing.get(str(item.get("id") or ""), {})
            candidate = dict(item)
            if candidate.pop("clear_api_key", False):
                candidate["api_key"] = ""
            elif not str(candidate.get("api_key") or "").strip():
                candidate["api_key"] = current.get("api_key", "")
            providers.append(candidate)
        incoming["providers"] = providers
        normalized = self.normalize(incoming)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}.tmp")
        with self.lock:
            try:
                with open(temp_path, "x", encoding="utf-8") as handle:
                    os.chmod(temp_path, 0o600)
                    json.dump(normalized, handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_path, self.path)
                os.chmod(self.path, 0o600)
            finally:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass
        return normalized

    def public(self) -> dict:
        value = self.load()
        value["providers"] = [
            {
                **{key: item[key] for key in ("id", "name", "type", "base_url", "models", "enabled")},
                "has_api_key": bool(item.get("api_key")),
            }
            for item in value["providers"]
        ]
        return value


class Store:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total_epochs INTEGER NOT NULL DEFAULT 0,
                    current_epoch INTEGER NOT NULL DEFAULT 0,
                    current_batch INTEGER,
                    total_batches INTEGER,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    ended_at TEXT,
                    elapsed_seconds REAL,
                    eta_seconds REAL,
                    parameters_json TEXT NOT NULL DEFAULT '{}',
                    final_metrics_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    host_status_json TEXT NOT NULL DEFAULT '{}',
                    group_name TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    favorite INTEGER NOT NULL DEFAULT 0,
                    metadata_revision INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    log_tail TEXT
                );
                CREATE TABLE IF NOT EXISTS metric_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    epoch INTEGER NOT NULL DEFAULT 0,
                    batch INTEGER,
                    phase TEXT NOT NULL DEFAULT 'train',
                    created_at TEXT NOT NULL,
                    metrics_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_metric_run_epoch
                    ON metric_events(run_id, epoch, id);
                CREATE TABLE IF NOT EXISTS queue_jobs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    command_text TEXT NOT NULL,
                    working_directory TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    required_gpu_count INTEGER NOT NULL DEFAULT 1,
                    gpu_candidates_json TEXT NOT NULL DEFAULT '[]',
                    min_free_memory_mb INTEGER NOT NULL DEFAULT 2048,
                    max_gpu_utilization REAL NOT NULL DEFAULT 10,
                    idle_seconds INTEGER NOT NULL DEFAULT 60,
                    retry_limit INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    retry_delay_seconds INTEGER NOT NULL DEFAULT 60,
                    anomaly_policy TEXT NOT NULL DEFAULT 'notify',
                    batch_size INTEGER,
                    min_batch_size INTEGER NOT NULL DEFAULT 1,
                    resume_checkpoint TEXT NOT NULL DEFAULT '',
                    baseline_run_id TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL DEFAULT '',
                    assigned_gpus_json TEXT NOT NULL DEFAULT '[]',
                    agent_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT,
                    not_before TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    preflight_status TEXT NOT NULL DEFAULT 'passed',
                    preflight_json TEXT NOT NULL DEFAULT '{}',
                    script_id TEXT NOT NULL DEFAULT '',
                    script_path TEXT NOT NULL DEFAULT '',
                    source_job_id TEXT NOT NULL DEFAULT '',
                    sweep_group_id TEXT NOT NULL DEFAULT '',
                    pause_requested INTEGER NOT NULL DEFAULT 0,
                    last_checkpoint TEXT NOT NULL DEFAULT '',
                    runtime_pid INTEGER NOT NULL DEFAULT 0,
                    fault_suggestion TEXT NOT NULL DEFAULT '',
                    output_dir TEXT NOT NULL DEFAULT '',
                    output_last_checkpoint TEXT NOT NULL DEFAULT '',
                    output_best_checkpoint TEXT NOT NULL DEFAULT '',
                    output_results_csv TEXT NOT NULL DEFAULT '',
                    output_bound_at TEXT,
                    reconcile_requested_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_queue_status_priority
                    ON queue_jobs(status, priority DESC, created_at ASC);
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    hostname TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'idle',
                    gpus_json TEXT NOT NULL DEFAULT '[]',
                    current_job_id TEXT NOT NULL DEFAULT '',
                    idle_reminder_seconds INTEGER NOT NULL DEFAULT 1800,
                    idle_reminder_gpu_count INTEGER NOT NULL DEFAULT 1,
                    idle_notified INTEGER NOT NULL DEFAULT 0,
                    catalog_json TEXT NOT NULL DEFAULT '[]',
                    capabilities_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES queue_jobs(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    data_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
            migrations = {
                "host_status_json": "TEXT NOT NULL DEFAULT '{}'",
                "group_name": "TEXT NOT NULL DEFAULT ''",
                "tags_json": "TEXT NOT NULL DEFAULT '[]'",
                "favorite": "INTEGER NOT NULL DEFAULT 0",
                "metadata_revision": "INTEGER NOT NULL DEFAULT 0",
                "hypothesis": "TEXT NOT NULL DEFAULT ''",
                "change_notes": "TEXT NOT NULL DEFAULT ''",
                "result_notes": "TEXT NOT NULL DEFAULT ''",
                "conclusion": "TEXT NOT NULL DEFAULT ''",
                "next_step": "TEXT NOT NULL DEFAULT ''",
                "baseline_run_id": "TEXT NOT NULL DEFAULT ''",
                "peak_gpu_memory_mb": "REAL NOT NULL DEFAULT 0",
                "ai_status": "TEXT NOT NULL DEFAULT 'not_requested'",
                "ai_report": "TEXT NOT NULL DEFAULT ''",
                "ai_models_json": "TEXT NOT NULL DEFAULT '[]'",
                "ai_requested_at": "TEXT",
                "ai_generated_at": "TEXT",
                "ai_error": "TEXT NOT NULL DEFAULT ''",
                "ai_email_status": "TEXT NOT NULL DEFAULT 'not_requested'",
                "ai_email_sent_at": "TEXT",
                "ai_email_error": "TEXT NOT NULL DEFAULT ''",
            }
            for column, definition in migrations.items():
                if column not in columns:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {column} {definition}")
            queue_columns = {row[1] for row in conn.execute("PRAGMA table_info(queue_jobs)")}
            queue_migrations = {
                "preflight_status": "TEXT NOT NULL DEFAULT 'passed'",
                "preflight_json": "TEXT NOT NULL DEFAULT '{}'",
                "script_id": "TEXT NOT NULL DEFAULT ''",
                "script_path": "TEXT NOT NULL DEFAULT ''",
                "source_job_id": "TEXT NOT NULL DEFAULT ''",
                "sweep_group_id": "TEXT NOT NULL DEFAULT ''",
                "pause_requested": "INTEGER NOT NULL DEFAULT 0",
                "last_checkpoint": "TEXT NOT NULL DEFAULT ''",
                "runtime_pid": "INTEGER NOT NULL DEFAULT 0",
                "fault_suggestion": "TEXT NOT NULL DEFAULT ''",
                "output_dir": "TEXT NOT NULL DEFAULT ''",
                "output_last_checkpoint": "TEXT NOT NULL DEFAULT ''",
                "output_best_checkpoint": "TEXT NOT NULL DEFAULT ''",
                "output_results_csv": "TEXT NOT NULL DEFAULT ''",
                "output_bound_at": "TEXT",
                "reconcile_requested_at": "TEXT",
            }
            for column, definition in queue_migrations.items():
                if column not in queue_columns:
                    conn.execute(f"ALTER TABLE queue_jobs ADD COLUMN {column} {definition}")
            agent_columns = {row[1] for row in conn.execute("PRAGMA table_info(agents)")}
            agent_migrations = {
                "catalog_json": "TEXT NOT NULL DEFAULT '[]'",
                "capabilities_json": "TEXT NOT NULL DEFAULT '{}'",
            }
            for column, definition in agent_migrations.items():
                if column not in agent_columns:
                    conn.execute(f"ALTER TABLE agents ADD COLUMN {column} {definition}")
            conn.execute(
                """UPDATE queue_jobs SET fault_suggestion=''
                WHERE status IN ('queued','leased','running','completed')
                AND TRIM(fault_suggestion)!=''"""
            )
            conn.execute(
                """UPDATE runs SET ai_status='failed',
                ai_error='AI 分析因服务重启而中断，请手动重新生成。',
                ai_email_status=CASE
                    WHEN ai_email_status='pending' THEN 'failed'
                    ELSE ai_email_status
                END,
                ai_email_error=CASE
                    WHEN ai_email_status='pending' THEN '报告生成中断，邮件未发送。'
                    ELSE ai_email_error
                END
                WHERE ai_status='running'"""
            )

    def start_run(self, payload: dict) -> dict:
        run_id = str(payload.get("run_id") or secrets.token_hex(12))[:80]
        name = str(payload.get("name") or f"YOLO run {run_id[:8]}")[:200]
        now = utc_now()
        total_epochs = max(0, int(payload.get("total_epochs") or 0))
        params = json.dumps(payload.get("parameters") or {}, ensure_ascii=False)
        host_status = json.dumps(payload.get("host_status") or {}, ensure_ascii=False)
        baseline_run_id = str(payload.get("baseline_run_id") or "")[:80]
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO runs
                (id, name, status, total_epochs, started_at, updated_at,
                 parameters_json, host_status_json, baseline_run_id)
                VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, status='running', total_epochs=excluded.total_epochs,
                    updated_at=excluded.updated_at, parameters_json=excluded.parameters_json,
                    host_status_json=excluded.host_status_json,
                    baseline_run_id=CASE WHEN excluded.baseline_run_id!='' THEN excluded.baseline_run_id ELSE runs.baseline_run_id END""",
                (run_id, name, total_epochs, now, now, params, host_status, baseline_run_id),
            )
        self.reconcile_catalog_lineage()
        self.enrich_run_metadata(run_id)
        return self.get_run(run_id)

    @staticmethod
    def normalize_output_binding(payload: dict | None) -> dict:
        if not isinstance(payload, dict):
            return {}
        source = payload.get("output_binding") if isinstance(payload.get("output_binding"), dict) else payload
        aliases = {
            "save_dir": ("save_dir", "output_dir"),
            "last_model": ("last_model", "last_checkpoint", "output_last_checkpoint"),
            "best_model": ("best_model", "best_checkpoint", "output_best_checkpoint"),
            "results_csv": ("results_csv", "output_results_csv"),
        }
        binding = {}
        for target, names in aliases.items():
            value = next((source.get(name) for name in names if source.get(name)), "")
            value = str(value or "").strip()[:1000]
            if value:
                binding[target] = value
        return binding

    def bind_run_output(self, run_id: str, payload: dict | None, confirmed_last: bool = False) -> dict:
        binding = self.normalize_output_binding(payload)
        if not binding:
            return {}
        now = utc_now()
        with self.connect() as conn:
            run_row = conn.execute("SELECT result_json FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run_row:
                return {}
            result = safe_json(run_row["result_json"], {})
            if not isinstance(result, dict):
                result = {}
            result_changed = any(result.get(key) != value for key, value in binding.items())
            if result_changed:
                result.update(binding)
                conn.execute(
                    "UPDATE runs SET result_json=? WHERE id=?",
                    (json.dumps(result, ensure_ascii=False), run_id),
                )
            job = conn.execute("SELECT * FROM queue_jobs WHERE run_id=? ORDER BY created_at DESC LIMIT 1", (run_id,)).fetchone()
            if job:
                changed = any((
                    bool(binding.get("save_dir")) and binding["save_dir"] != (job["output_dir"] or ""),
                    bool(binding.get("last_model")) and binding["last_model"] != (job["output_last_checkpoint"] or ""),
                    bool(binding.get("best_model")) and binding["best_model"] != (job["output_best_checkpoint"] or ""),
                    bool(binding.get("results_csv")) and binding["results_csv"] != (job["output_results_csv"] or ""),
                ))
                if changed or (confirmed_last and binding.get("last_model")):
                    conn.execute(
                        """UPDATE queue_jobs SET
                        output_dir=CASE WHEN ?!='' THEN ? ELSE output_dir END,
                        output_last_checkpoint=CASE WHEN ?!='' THEN ? ELSE output_last_checkpoint END,
                        output_best_checkpoint=CASE WHEN ?!='' THEN ? ELSE output_best_checkpoint END,
                        output_results_csv=CASE WHEN ?!='' THEN ? ELSE output_results_csv END,
                        output_bound_at=CASE WHEN ?!='' THEN ? ELSE output_bound_at END,
                        last_checkpoint=CASE WHEN ?=1 AND ?!='' THEN ? ELSE last_checkpoint END
                        WHERE id=?""",
                        (
                            binding.get("save_dir", ""), binding.get("save_dir", ""),
                            binding.get("last_model", ""), binding.get("last_model", ""),
                            binding.get("best_model", ""), binding.get("best_model", ""),
                            binding.get("results_csv", ""), binding.get("results_csv", ""),
                            binding.get("save_dir", ""), now,
                            1 if confirmed_last else 0, binding.get("last_model", ""), binding.get("last_model", ""),
                            job["id"],
                        ),
                    )
                if changed and binding.get("save_dir"):
                    conn.execute(
                        "INSERT INTO job_events(job_id,created_at,kind,message,data_json) VALUES (?,?,?,?,?)",
                        (
                            job["id"], now, "output_bound",
                            f"实验输出目录已绑定：{binding['save_dir']}",
                            json.dumps(binding, ensure_ascii=False),
                        ),
                    )
        return binding

    def progress(self, run_id: str, payload: dict) -> dict | None:
        epoch = max(0, int(payload.get("epoch") or 0))
        batch = payload.get("batch")
        total_batches = payload.get("total_batches")
        batch = int(batch) if batch is not None else None
        total_batches = int(total_batches) if total_batches is not None else None
        elapsed = payload.get("elapsed_seconds")
        eta = payload.get("eta_seconds")
        phase = str(payload.get("phase") or "train")[:30]
        metrics = payload.get("metrics") or {}
        log_tail = str(payload.get("log_tail") or "")[-12000:] or None
        host_payload = payload.get("host_status")
        host_status = (
            json.dumps(host_payload, ensure_ascii=False)
            if isinstance(host_payload, dict) and host_payload
            else None
        )
        peak_gpu_memory = max(
            [float(gpu.get("memory_used_mb") or 0) for gpu in (host_payload or {}).get("gpus", []) if isinstance(gpu, dict)]
            or [0.0]
        )
        now = utc_now()
        with self.connect() as conn:
            exists = conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone()
            if not exists:
                return None
            conn.execute(
                """UPDATE runs SET status='running', current_epoch=?, current_batch=?,
                total_batches=?, elapsed_seconds=?, eta_seconds=?, updated_at=?,
                log_tail=COALESCE(?, log_tail),
                host_status_json=COALESCE(?, host_status_json),
                peak_gpu_memory_mb=MAX(peak_gpu_memory_mb, ?) WHERE id=?""",
                (epoch, batch, total_batches, elapsed, eta, now, log_tail, host_status, peak_gpu_memory, run_id),
            )
            conn.execute(
                """INSERT INTO metric_events
                (run_id, epoch, batch, phase, created_at, metrics_json)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, epoch, batch, phase, now, json.dumps(metrics, ensure_ascii=False)),
            )
        self.bind_run_output(run_id, payload.get("output_binding"))
        return self.get_run(run_id)

    def finish(self, run_id: str, payload: dict) -> tuple[dict | None, bool]:
        status = str(payload.get("status") or "completed")
        if status not in {"completed", "failed", "paused"}:
            status = "failed"
        now = utc_now()
        with self.connect() as conn:
            previous = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if not previous:
                return None, False
            first_finish = previous["status"] in {"running", "stalled"}
            conn.execute(
                """UPDATE runs SET status=?, ended_at=?, updated_at=?, elapsed_seconds=?,
                eta_seconds=0, final_metrics_json=?, result_json=?, error_message=?, log_tail=?
                WHERE id=?""",
                (
                    status,
                    now,
                    now,
                    payload.get("elapsed_seconds"),
                    json.dumps(payload.get("metrics") or {}, ensure_ascii=False),
                    json.dumps(payload.get("result") or {}, ensure_ascii=False),
                    str(payload.get("error") or "")[:4000] or None,
                    str(payload.get("log_tail") or "")[-12000:] or None,
                    run_id,
                ),
            )
            conn.execute(
                """UPDATE queue_jobs SET status=?, ended_at=?, updated_at=?, last_error=?
                WHERE run_id=? AND status IN ('leased','running','waiting_memory')""",
                (status, now, now, str(payload.get("error") or "")[:4000], run_id),
            )
            if status == "completed":
                conn.execute(
                    "UPDATE queue_jobs SET fault_suggestion='',last_error='' WHERE run_id=?",
                    (run_id,),
                )
        self.bind_run_output(run_id, payload.get("result"), confirmed_last=True)
        self.reconcile_catalog_lineage()
        self.enrich_run_metadata(run_id)
        return self.get_run(run_id), first_finish

    def get_run(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self.row_to_run(row) if row else None

    def list_runs(self, limit: int = 100) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY favorite DESC, started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self.row_to_run(row) for row in rows]

    def best_map50_95_by_run(self, run_ids: set[str]) -> dict[str, float]:
        """Read the best observed detection mAP50-95, not the final Epoch value."""
        if not run_ids:
            return {}
        ids = sorted(run_ids)
        result = {}
        with self.connect() as conn:
            for start in range(0, len(ids), 400):
                batch = ids[start:start + 400]
                placeholders = ",".join("?" for _ in batch)
                rows = conn.execute(
                    f"""SELECT run_id, MAX(COALESCE(
                        json_extract(metrics_json, '$."metrics/mAP50-95(B)"'),
                        json_extract(metrics_json, '$."metrics/mAP50-95(M)"'),
                        json_extract(metrics_json, '$."metrics/mAP50-95"')
                    )) AS best_value
                    FROM metric_events WHERE phase='epoch' AND run_id IN ({placeholders})
                    GROUP BY run_id""",
                    batch,
                )
                result.update({row["run_id"]: float(row["best_value"]) for row in rows if row["best_value"] is not None})
        return result

    def metric_events(self, run_id: str) -> list[dict]:
        events = []
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT epoch, batch, phase, created_at, metrics_json
                FROM metric_events WHERE run_id=? AND phase='epoch'
                ORDER BY epoch ASC, id ASC""",
                (run_id,),
            )
            for row in rows:
                if not events or events[-1]["epoch"] != row["epoch"]:
                    if len(events) >= 1000:
                        break
                    events.append({
                        "epoch": row["epoch"],
                        "batch": row["batch"],
                        "phase": row["phase"],
                        "created_at": row["created_at"],
                        "metrics": {},
                    })
                event = events[-1]
                if row["batch"] is not None:
                    event["batch"] = row["batch"]
                event["created_at"] = row["created_at"]
                metrics = safe_json(row["metrics_json"], {})
                if isinstance(metrics, dict):
                    event["metrics"].update(metrics)
        return events

    def delete_run(self, run_id: str) -> bool:
        """Delete one run and its metric events in a single transaction."""
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
        return cursor.rowcount == 1

    def update_metadata(self, run_id: str, payload: dict) -> dict | None:
        run = self.get_run(run_id)
        if not run:
            return None
        group_name = str(payload.get("group_name", run.get("group_name") or "")).strip()[:80]
        raw_tags = payload.get("tags", run.get("tags") or [])
        if isinstance(raw_tags, str):
            raw_tags = raw_tags.replace("，", ",").split(",")
        if not isinstance(raw_tags, list):
            raise ValueError("tags must be a list or comma-separated string")
        tags = []
        for value in raw_tags:
            tag = str(value).strip()[:40]
            if tag and tag not in tags:
                tags.append(tag)
            if len(tags) >= 20:
                break
        favorite_value = payload.get("favorite", bool(run.get("favorite")))
        favorite = 1 if favorite_value is True or str(favorite_value).lower() in {"1", "true", "yes", "on"} else 0
        note_values = {
            "hypothesis": str(payload.get("hypothesis", run.get("hypothesis") or ""))[:8000],
            "change_notes": str(payload.get("change_notes", run.get("change_notes") or ""))[:8000],
            "result_notes": str(payload.get("result_notes", run.get("result_notes") or ""))[:8000],
            "conclusion": str(payload.get("conclusion", run.get("conclusion") or ""))[:8000],
            "next_step": str(payload.get("next_step", run.get("next_step") or ""))[:8000],
            "baseline_run_id": str(payload.get("baseline_run_id", run.get("baseline_run_id") or ""))[:80],
        }
        with self.connect() as conn:
            conn.execute(
                """UPDATE runs SET group_name=?, tags_json=?, favorite=?,
                hypothesis=?, change_notes=?, result_notes=?, conclusion=?, next_step=?, baseline_run_id=?,
                metadata_revision=metadata_revision+1 WHERE id=?""",
                (group_name, json.dumps(tags, ensure_ascii=False), favorite,
                 note_values["hypothesis"], note_values["change_notes"], note_values["result_notes"],
                 note_values["conclusion"], note_values["next_step"], note_values["baseline_run_id"], run_id),
            )
        return self.get_run(run_id)

    def begin_ai_analysis(self, run_id: str, send_email: bool = False) -> bool:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """UPDATE runs SET ai_status='running',ai_error='',ai_requested_at=?,
                ai_email_status=?,ai_email_sent_at=NULL,ai_email_error=''
                WHERE id=? AND ai_status!='running'""",
                (now, "pending" if send_email else "not_requested", run_id),
            )
        return cursor.rowcount == 1

    def fail_ai_analysis(self, run_id: str, error: str):
        with self.connect() as conn:
            conn.execute(
                """UPDATE runs SET ai_status='failed',ai_error=?,
                ai_email_status=CASE
                    WHEN ai_email_status='pending' THEN 'failed'
                    ELSE ai_email_status
                END,
                ai_email_error=CASE
                    WHEN ai_email_status='pending' THEN '报告生成失败，邮件未发送。'
                    ELSE ai_email_error
                END
                WHERE id=?""",
                (str(error)[:4000], run_id),
            )

    def complete_ai_email(self, run_id: str, sent: bool, error: str = ""):
        with self.connect() as conn:
            conn.execute(
                """UPDATE runs SET ai_email_status=?,ai_email_sent_at=?,
                ai_email_error=? WHERE id=?""",
                (
                    "sent" if sent else "skipped",
                    utc_now() if sent else None,
                    str(error)[:4000],
                    run_id,
                ),
            )

    def fail_ai_email(self, run_id: str, error: str):
        with self.connect() as conn:
            conn.execute(
                """UPDATE runs SET ai_email_status='failed',ai_email_error=?
                WHERE id=?""",
                (str(error)[:4000], run_id),
            )

    def apply_ai_analysis(
        self,
        run_id: str,
        analysis: dict,
        model_references: list[str],
        apply_metadata: bool,
        overwrite: bool,
    ) -> dict | None:
        run = self.get_run(run_id)
        if not run:
            return None
        group_name = str(analysis.get("group_name") or "").strip()[:80]
        raw_tags = analysis.get("tags") or []
        if isinstance(raw_tags, str):
            raw_tags = raw_tags.replace("，", ",").split(",")
        ai_tags = []
        for value in raw_tags if isinstance(raw_tags, list) else []:
            tag = str(value).strip()[:40]
            if tag and tag not in ai_tags:
                ai_tags.append(tag)
            if len(ai_tags) >= 20:
                break
        tags = list(run.get("tags") or [])
        for tag in ai_tags:
            if tag not in tags and len(tags) < 20:
                tags.append(tag)
        favorite = bool(run.get("favorite")) or analysis.get("favorite") is True
        human_edited = int(run.get("metadata_revision") or 0) > 0
        system_prefixes = {
            "hypothesis": ("评估「", "验证以下 YAML 修改相对父实验的影响："),
            "result_notes": ("当前最佳 ",),
            "conclusion": ("按最佳 ", "实验已完成；"),
            "next_step": ("绑定明确的 baseline Run ID", "复核运行日志并重复实验", "先定位失败或中断原因"),
        }
        notes = {}
        for field in ("hypothesis", "change_notes", "result_notes", "conclusion", "next_step"):
            generated = str(analysis.get(field) or "").strip()[:8000]
            current = str(run.get(field) or "")
            system_generated = any(
                current.startswith(prefix) for prefix in system_prefixes.get(field, ())
            )
            notes[field] = (
                generated
                if generated and (overwrite or not human_edited or not current.strip() or system_generated)
                else current
            )
        if not apply_metadata:
            group_name = str(run.get("group_name") or "")
            tags = list(run.get("tags") or [])
            favorite = bool(run.get("favorite"))
            notes = {field: str(run.get(field) or "") for field in notes}
        elif not overwrite and human_edited and str(run.get("group_name") or "").strip():
            group_name = str(run.get("group_name") or "")
        report = str(analysis.get("detailed_report") or analysis.get("conclusion") or "").strip()[:30000]
        with self.connect() as conn:
            conn.execute(
                """UPDATE runs SET group_name=?,tags_json=?,favorite=?,
                hypothesis=?,change_notes=?,result_notes=?,conclusion=?,next_step=?,
                ai_status='completed',ai_report=?,ai_models_json=?,ai_generated_at=?,ai_error=''
                WHERE id=?""",
                (
                    group_name, json.dumps(tags, ensure_ascii=False), 1 if favorite else 0,
                    notes["hypothesis"], notes["change_notes"], notes["result_notes"],
                    notes["conclusion"], notes["next_step"], report,
                    json.dumps(model_references, ensure_ascii=False), utc_now(), run_id,
                ),
            )
        return self.get_run(run_id)

    @staticmethod
    def _queue_row(row: sqlite3.Row, include_command: bool = True) -> dict:
        result = dict(row)
        result["gpu_candidates"] = safe_json(result.pop("gpu_candidates_json"), [])
        result["assigned_gpus"] = safe_json(result.pop("assigned_gpus_json"), [])
        result["preflight"] = safe_json(result.pop("preflight_json"), {})
        result["pause_requested"] = bool(result.get("pause_requested"))
        if not include_command:
            result.pop("command_text", None)
            result.pop("working_directory", None)
            result.pop("resume_checkpoint", None)
            result.pop("script_path", None)
        return result

    def create_queue_job(self, payload: dict) -> dict:
        name = str(payload.get("name") or "").strip()[:200]
        command = str(payload.get("command") or "").strip()[:8000]
        working_directory = str(payload.get("working_directory") or "").strip()[:1000]
        if not name or not command or not working_directory:
            raise ValueError("name, command and working_directory are required")
        raw_candidates = payload.get("gpu_candidates") or []
        if isinstance(raw_candidates, str):
            raw_candidates = raw_candidates.replace("，", ",").split(",")
        candidates = []
        for value in raw_candidates:
            try:
                index = int(str(value).strip())
            except ValueError:
                continue
            if index >= 0 and index not in candidates:
                candidates.append(index)
        policy = str(payload.get("anomaly_policy") or "notify")
        if policy not in {"notify", "stop", "retry_lower_batch", "retry_when_memory"}:
            raise ValueError("invalid anomaly_policy")
        batch_size = payload.get("batch_size")
        batch_size = max(1, int(batch_size)) if batch_size not in (None, "") else None
        if policy == "retry_lower_batch" and batch_size is None:
            raise ValueError("batch_size is required for retry_lower_batch")
        baseline_run_id = str(payload.get("baseline_run_id") or "")[:80]
        if not baseline_run_id:
            lineage = self.lineage_for_script(str(payload.get("script_id") or "")) or {}
            baseline_run_id = str(lineage.get("father_run_id") or "")[:80]
        now = utc_now()
        job_id = secrets.token_hex(12)
        run_id = secrets.token_hex(12)
        preflight_status = "pending"
        values = (
            job_id, name, command, working_directory,
            max(-1000, min(1000, int(payload.get("priority") or 0))),
            max(1, min(16, int(payload.get("required_gpu_count") or 1))),
            json.dumps(candidates),
            max(0, int(payload.get("min_free_memory_mb") or 2048)),
            max(0.0, min(100.0, float(payload.get("max_gpu_utilization") or 10))),
            max(0, int(payload.get("idle_seconds") or 60)),
            max(0, min(20, int(payload.get("retry_limit") or 0))),
            max(5, int(payload.get("retry_delay_seconds") or 60)), policy, batch_size,
            max(1, int(payload.get("min_batch_size") or 1)),
            str(payload.get("resume_checkpoint") or "")[:1000],
            baseline_run_id,
            str(payload.get("notes") or "")[:8000], run_id, now, now,
            preflight_status,
            str(payload.get("script_id") or "")[:160],
            str(payload.get("script_path") or "")[:1000],
            str(payload.get("source_job_id") or "")[:80],
            str(payload.get("sweep_group_id") or "")[:80],
        )
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO queue_jobs
                (id,name,status,command_text,working_directory,priority,required_gpu_count,
                 gpu_candidates_json,min_free_memory_mb,max_gpu_utilization,idle_seconds,
                 retry_limit,retry_delay_seconds,anomaly_policy,batch_size,min_batch_size,
                 resume_checkpoint,baseline_run_id,notes,run_id,created_at,updated_at,
                 preflight_status,script_id,script_path,source_job_id,sweep_group_id)
                 VALUES (?,?,'queued',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                values,
            )
            conn.execute(
                "INSERT INTO job_events(job_id,created_at,kind,message) VALUES (?,?,?,?)",
                (job_id, now, "created", "任务已加入实验队列"),
            )
        return self.get_queue_job(job_id)

    def get_queue_job(self, job_id: str, include_command: bool = True) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM queue_jobs WHERE id=?", (job_id,)).fetchone()
        return self._queue_row(row, include_command) if row else None

    def get_queue_job_by_run(self, run_id: str, include_command: bool = True) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM queue_jobs WHERE run_id=? ORDER BY created_at DESC LIMIT 1", (run_id,)).fetchone()
        return self._queue_row(row, include_command) if row else None

    def delete_queue_job(self, job_id: str) -> bool:
        """Delete a removable queue record without touching its run or output files."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT q.status,q.run_id,COALESCE(r.current_epoch,0) AS run_current_epoch
                FROM queue_jobs q LEFT JOIN runs r ON r.id=q.run_id WHERE q.id=?""",
                (job_id,),
            ).fetchone()
            if not row:
                return False
            status = str(row["status"] or "")
            run_current_epoch = int(row["run_current_epoch"] or 0)
            if status != "cancelled" and not (status == "failed" and run_current_epoch < 3):
                raise RuntimeError("only a cancelled job or a failed job below 3 epochs can be deleted")
            cursor = conn.execute(
                "DELETE FROM queue_jobs WHERE id=? AND status=?",
                (job_id, status),
            )
        return cursor.rowcount == 1

    @staticmethod
    def queue_job_editable(job) -> bool:
        return bool(
            job
            and job.get("status") in {"queued", "waiting_memory"}
            and int(job.get("attempt_count") or 0) == 0
            and not job.get("started_at")
            and not job.get("agent_id")
        )

    def update_queue_job(self, job_id: str, payload: dict) -> dict | None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM queue_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            current = self._queue_row(row, True)
            if not self.queue_job_editable(current):
                raise RuntimeError("queue job has already started and is no longer editable")

            name = str(payload.get("name", current["name"]) or "").strip()[:200]
            command = str(payload.get("command", current["command_text"]) or "").strip()[:8000]
            working_directory = str(payload.get("working_directory", current["working_directory"]) or "").strip()[:1000]
            if not name or not command or not working_directory:
                raise ValueError("name, command and working_directory are required")

            raw_candidates = payload.get("gpu_candidates", current["gpu_candidates"])
            if isinstance(raw_candidates, str):
                raw_candidates = raw_candidates.replace("，", ",").split(",")
            candidates = []
            for value in raw_candidates or []:
                try:
                    index = int(str(value).strip())
                except ValueError:
                    continue
                if index >= 0 and index not in candidates:
                    candidates.append(index)

            policy = str(payload.get("anomaly_policy", current["anomaly_policy"]) or "notify")
            if policy not in {"notify", "stop", "retry_lower_batch", "retry_when_memory"}:
                raise ValueError("invalid anomaly_policy")
            batch_value = payload.get("batch_size", current["batch_size"])
            batch_size = max(1, int(batch_value)) if batch_value not in (None, "") else None
            if policy == "retry_lower_batch" and batch_size is None:
                raise ValueError("batch_size is required for retry_lower_batch")
            values = (
                name, command, working_directory,
                max(-1000, min(1000, int(payload.get("priority", current["priority"]) or 0))),
                max(1, min(16, int(payload.get("required_gpu_count", current["required_gpu_count"]) or 1))),
                json.dumps(candidates),
                max(0, int(payload.get("min_free_memory_mb", current["min_free_memory_mb"]) or 0)),
                max(0.0, min(100.0, float(payload.get("max_gpu_utilization", current["max_gpu_utilization"]) or 0))),
                max(0, int(payload.get("idle_seconds", current["idle_seconds"]) or 0)),
                max(0, min(20, int(payload.get("retry_limit", current["retry_limit"]) or 0))),
                max(5, int(payload.get("retry_delay_seconds", current["retry_delay_seconds"]) or 5)),
                policy, batch_size,
                max(1, int(payload.get("min_batch_size", current["min_batch_size"]) or 1)),
                str(payload.get("resume_checkpoint", current["resume_checkpoint"]) or "")[:1000],
                str(payload.get("baseline_run_id", current["baseline_run_id"]) or "")[:80],
                str(payload.get("notes", current["notes"]) or "")[:8000], now, job_id,
            )
            cursor = conn.execute(
                """UPDATE queue_jobs SET name=?,command_text=?,working_directory=?,priority=?,
                required_gpu_count=?,gpu_candidates_json=?,min_free_memory_mb=?,max_gpu_utilization=?,
                idle_seconds=?,retry_limit=?,retry_delay_seconds=?,anomaly_policy=?,batch_size=?,
                min_batch_size=?,resume_checkpoint=?,baseline_run_id=?,notes=?,updated_at=?,
                preflight_status='pending',preflight_json='{}',fault_suggestion=''
                WHERE id=? AND status IN ('queued','waiting_memory') AND attempt_count=0
                AND started_at IS NULL AND agent_id=''""",
                values,
            )
            if cursor.rowcount != 1:
                raise RuntimeError("queue job was claimed while it was being edited")
            conn.execute(
                "INSERT INTO job_events(job_id,created_at,kind,message) VALUES (?,?,?,?)",
                (job_id, now, "configuration_updated", "未开始任务的队列配置已修改"),
            )
            updated = conn.execute("SELECT * FROM queue_jobs WHERE id=?", (job_id,)).fetchone()
        return self._queue_row(updated, True)

    def list_queue_jobs(self, include_command: bool = False, limit: int = 200) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT q.*,COALESCE(r.current_epoch,0) AS run_current_epoch
                FROM queue_jobs q LEFT JOIN runs r ON r.id=q.run_id
                ORDER BY CASE q.status WHEN 'running' THEN 0 WHEN 'leased' THEN 1 WHEN 'queued' THEN 2
                WHEN 'waiting_memory' THEN 3 ELSE 4 END, q.priority DESC, q.created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [self._queue_row(row, include_command) for row in rows]

    def list_queue_jobs_with_reasons(self, include_command: bool = False, limit: int = 200) -> list[dict]:
        jobs = self.list_queue_jobs(include_command, limit)
        agents = self.list_agents()
        host_by_agent = {str(agent.get("id") or ""): str(agent.get("hostname") or "") for agent in agents}
        reservations_by_host: dict[str, dict[int, str]] = {}
        for active_job in jobs:
            if active_job.get("status") not in {"leased", "running"}:
                continue
            host = host_by_agent.get(str(active_job.get("agent_id") or ""))
            if not host:
                continue
            reservations = reservations_by_host.setdefault(host, {})
            for index in active_job.get("assigned_gpus") or []:
                try:
                    reservations[int(index)] = str(active_job.get("name") or active_job.get("id") or "")
                except (TypeError, ValueError):
                    continue
        now = datetime.now(timezone.utc)
        for job in jobs:
            reasons = []
            if job.get("status") not in {"queued", "waiting_memory"}:
                job["queue_reasons"] = reasons
                continue
            if job.get("preflight_status") == "pending":
                reasons.append("正在等待 Agent 完成入队预检")
            elif job.get("preflight_status") == "failed":
                reasons.append("预检未通过：" + str((job.get("preflight") or {}).get("message") or job.get("last_error") or "请查看预检详情"))
            not_before = job.get("not_before")
            if not_before:
                try:
                    remaining = max(0, int((datetime.fromisoformat(not_before) - now).total_seconds()))
                    if remaining:
                        reasons.append(f"重试冷却中，还需 {remaining} 秒")
                except ValueError:
                    pass
            if job.get("preflight_status") != "passed" or reasons:
                job["queue_reasons"] = reasons
                continue
            if not agents:
                reasons.append("没有在线训练 Agent")
            candidates = set(job.get("gpu_candidates") or [])
            best_ready = 0
            best_details = []
            for agent in agents:
                ready = 0
                details = []
                reservations = reservations_by_host.get(str(agent.get("hostname") or ""), {})
                for gpu in agent.get("gpus") or []:
                    index = int(gpu.get("index", -1))
                    if candidates and index not in candidates:
                        continue
                    if gpu.get("cuda_usable") is False:
                        details.append(f"GPU {index}：CUDA 初始化不可用，已隔离")
                        continue
                    if gpu.get("telemetry_usable") is False:
                        details.append(f"GPU {index}：显卡利用率/显存遥测不可用，已隔离")
                        continue
                    if index in reservations:
                        details.append(f"GPU {index}：已被任务 {reservations[index]} 预留")
                        continue
                    free_mb = float(gpu.get("memory_free_mb") or 0)
                    utilization = float(gpu.get("utilization_percent") or 0)
                    idle_for = float(gpu.get("idle_for_seconds") or 0)
                    issues = []
                    if free_mb < job["min_free_memory_mb"]:
                        issues.append(f"可用显存 {free_mb:.0f}<{job['min_free_memory_mb']} MiB")
                    if utilization > job["max_gpu_utilization"]:
                        issues.append(f"利用率 {utilization:.0f}%>{job['max_gpu_utilization']:g}%")
                    if idle_for < job["idle_seconds"]:
                        issues.append(f"空闲还需 {max(0, int(job['idle_seconds']-idle_for))} 秒")
                    if issues:
                        details.append(f"GPU {index}：" + "，".join(issues))
                    else:
                        ready += 1
                if ready > best_ready or not best_details:
                    best_ready, best_details = ready, details
            required = int(job.get("required_gpu_count") or 1)
            if best_ready < required:
                reasons.append(f"还缺 {required-best_ready} 张满足条件的 GPU（当前 {best_ready}/{required}）")
                reasons.extend(best_details[:8])
            job["queue_reasons"] = reasons or ["条件已满足，等待 Agent 下一次领取"]
        return jobs

    def queue_action(self, job_id: str, action: str) -> dict | None:
        if action not in {"cancel", "requeue", "requeue_resume", "pause", "resume"}:
            raise ValueError("invalid queue action")
        now = utc_now()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM queue_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            current = self._queue_row(row, True)
            checkpoint = ""
            if action == "pause":
                if current["status"] not in {"leased", "running"}:
                    raise RuntimeError("only a running job can be paused")
                conn.execute("UPDATE queue_jobs SET pause_requested=1,updated_at=? WHERE id=?", (now, job_id))
            elif action == "resume":
                if current["status"] != "paused":
                    raise RuntimeError("only a paused job can be resumed")
                checkpoint = current.get("last_checkpoint") or current.get("resume_checkpoint") or ""
                if not checkpoint:
                    raise RuntimeError("no checkpoint is available for resume")
                conn.execute(
                    """UPDATE queue_jobs SET status='queued',pause_requested=0,updated_at=?,ended_at=NULL,
                    assigned_gpus_json='[]',agent_id='',runtime_pid=0,not_before=NULL,last_error='',
                    fault_suggestion='',resume_checkpoint=?,preflight_status='pending',preflight_json='{}',run_id=? WHERE id=?""",
                    (now, checkpoint, secrets.token_hex(12), job_id),
                )
            elif action == "requeue_resume":
                if current["status"] not in {"failed", "cancelled"}:
                    raise RuntimeError("only a failed or cancelled job can be resumed from checkpoint")
                checkpoint = current.get("last_checkpoint") or current.get("resume_checkpoint") or ""
                if not checkpoint:
                    raise RuntimeError("no checkpoint is available for resume")
                conn.execute(
                    """UPDATE queue_jobs SET status='queued',pause_requested=0,updated_at=?,ended_at=NULL,
                    assigned_gpus_json='[]',agent_id='',runtime_pid=0,not_before=NULL,last_error='',
                    fault_suggestion='',attempt_count=0,resume_checkpoint=?,preflight_status='pending',preflight_json='{}',
                    run_id=? WHERE id=?""",
                    (now, checkpoint, secrets.token_hex(12), job_id),
                )
            elif action == "requeue":
                conn.execute(
                    """UPDATE queue_jobs SET status='queued',pause_requested=0,updated_at=?,ended_at=NULL,
                    assigned_gpus_json='[]',agent_id='',runtime_pid=0,not_before=NULL,last_error='',
                    fault_suggestion='',attempt_count=0,resume_checkpoint='',preflight_status='pending',preflight_json='{}',
                    last_checkpoint='',output_dir='',output_last_checkpoint='',
                    output_best_checkpoint='',output_results_csv='',output_bound_at=NULL,
                    run_id=? WHERE id=?""",
                    (now, secrets.token_hex(12), job_id),
                )
            else:
                conn.execute("UPDATE queue_jobs SET status='cancelled',pause_requested=0,updated_at=?,ended_at=? WHERE id=?", (now, now, job_id))
            conn.execute(
                "INSERT INTO job_events(job_id,created_at,kind,message) VALUES (?,?,?,?)",
                (job_id, now, action, {
                    "cancel": "任务已取消",
                    "requeue": "任务已从头重新排队",
                    "requeue_resume": f"任务已从 checkpoint 续训：{checkpoint}",
                    "pause": "已请求在当前 Epoch 结束后暂停",
                    "resume": f"暂停任务已从 checkpoint 恢复：{checkpoint}",
                }[action]),
            )
        return self.get_queue_job(job_id, False)

    @staticmethod
    def merge_catalog(existing: list, incoming: list) -> list[dict]:
        """Keep lineage metadata when a still-running older Agent sends its legacy catalog."""
        lineage_fields = {
            "model_yaml", "model_yaml_name", "father_yaml", "father_yaml_name",
            "change_summary", "lineage_relation", "lineage_confidence",
            "father_task_name", "father_run_id", "father_run_name", "lineage_status",
        }
        old_by_key = {}
        for item in existing or []:
            if isinstance(item, dict):
                key = str(item.get("id") or item.get("path") or "")
                if key:
                    old_by_key[key] = item
        merged = []
        for item in (incoming or [])[:500]:
            if not isinstance(item, dict):
                continue
            current = dict(item)
            key = str(current.get("id") or current.get("path") or "")
            previous = old_by_key.get(key, {})
            for field in lineage_fields:
                if current.get(field) in (None, "") and previous.get(field) not in (None, ""):
                    current[field] = previous[field]
            merged.append(current)
        return merged

    def publish_agent_catalog(self, payload: dict) -> dict:
        """Update only script metadata; never claims work or changes the Agent's runtime state."""
        agent_id = str(payload.get("agent_id") or "").strip()[:120]
        if not agent_id:
            raise ValueError("agent_id is required")
        incoming = payload.get("catalog") if isinstance(payload.get("catalog"), list) else []
        with self.connect() as conn:
            row = conn.execute("SELECT catalog_json FROM agents WHERE id=?", (agent_id,)).fetchone()
            if not row:
                raise ValueError("agent must heartbeat before publishing catalog")
            catalog = self.merge_catalog(safe_json(row["catalog_json"], []), incoming)
            conn.execute(
                "UPDATE agents SET catalog_json=?, updated_at=? WHERE id=?",
                (json.dumps(catalog, ensure_ascii=False), utc_now(), agent_id),
            )
        self.reconcile_catalog_lineage()
        return self.get_agent(agent_id)

    @staticmethod
    def _find_run_for_catalog_item(conn, item: dict):
        """Resolve a catalog item to a run without guessing from a merely similar name."""
        script_id = str(item.get("id") or "")
        if script_id:
            row = conn.execute(
                """SELECT r.id,r.name FROM queue_jobs q JOIN runs r ON r.id=q.run_id
                WHERE q.script_id=? ORDER BY CASE r.status WHEN 'completed' THEN 0 ELSE 1 END,
                r.started_at DESC LIMIT 1""",
                (script_id,),
            ).fetchone()
            if row:
                return row
        task_name = str(item.get("task_name") or "").strip()
        if task_name:
            return conn.execute(
                """SELECT id,name FROM runs WHERE name=? COLLATE NOCASE
                ORDER BY CASE status WHEN 'completed' THEN 0 ELSE 1 END, started_at DESC LIMIT 1""",
                (task_name,),
            ).fetchone()
        return None

    def reconcile_catalog_lineage(self):
        """Close explicit YAML father links whenever either catalogs or runs change."""
        with self.connect() as conn:
            agent_rows = conn.execute("SELECT id,catalog_json FROM agents").fetchall()
            catalogs = {
                row["id"]: [item for item in safe_json(row["catalog_json"], []) if isinstance(item, dict)]
                for row in agent_rows
            }
            all_items = [item for catalog in catalogs.values() for item in catalog]
            by_yaml_name = {}
            for item in all_items:
                yaml_name = str(item.get("model_yaml_name") or "").strip().casefold()
                if yaml_name:
                    by_yaml_name.setdefault(yaml_name, []).append(item)

            for agent_id, catalog in catalogs.items():
                changed = False
                for item in catalog:
                    relation = str(item.get("lineage_relation") or "")
                    father_name = str(item.get("father_yaml_name") or "").strip()
                    resolved = {
                        "father_task_name": "", "father_run_id": "", "father_run_name": "",
                        "lineage_status": "none",
                    }
                    if relation == "reference":
                        resolved["lineage_status"] = "reference_only"
                    elif relation == "father" and father_name:
                        candidates = by_yaml_name.get(father_name.casefold(), [])
                        if candidates:
                            same_root = [candidate for candidate in candidates if candidate.get("working_directory") == item.get("working_directory")]
                            father_item = (same_root or candidates)[0]
                            father_task_name = str(father_item.get("task_name") or "").strip()
                            resolved["father_task_name"] = father_task_name
                            if father_task_name:
                                run_row = self._find_run_for_catalog_item(conn, father_item)
                                if run_row:
                                    resolved.update({
                                        "father_run_id": run_row["id"],
                                        "father_run_name": run_row["name"],
                                        "lineage_status": "matched",
                                    })
                                else:
                                    resolved["lineage_status"] = "run_missing"
                            else:
                                resolved["lineage_status"] = "parent_name_missing"
                        else:
                            resolved["lineage_status"] = "parent_script_missing"
                    for field, value in resolved.items():
                        if item.get(field) != value:
                            item[field] = value
                            changed = True

                    child_run = self._find_run_for_catalog_item(conn, item)
                    if relation == "father" and child_run:
                        summary = str(item.get("change_summary") or "")[:8000]
                        father_run_id = str(resolved.get("father_run_id") or "")[:80]
                        hypothesis = (
                            f"验证以下 YAML 修改相对父实验的影响：{summary.splitlines()[0]}"
                            if summary else ""
                        )[:8000]
                        conn.execute(
                            """UPDATE runs SET
                            hypothesis=CASE WHEN TRIM(hypothesis)='' AND ?!='' THEN ? ELSE hypothesis END,
                            change_notes=CASE WHEN TRIM(change_notes)='' AND ?!='' THEN ? ELSE change_notes END,
                            baseline_run_id=CASE WHEN TRIM(baseline_run_id)='' AND ?!='' THEN ? ELSE baseline_run_id END
                            WHERE id=? AND
                            ((TRIM(hypothesis)='' AND ?!='') OR (TRIM(change_notes)='' AND ?!='')
                             OR (TRIM(baseline_run_id)='' AND ?!=''))""",
                            (
                                hypothesis, hypothesis, summary, summary, father_run_id, father_run_id,
                                child_run["id"], hypothesis, summary, father_run_id,
                            ),
                        )
                if changed:
                    conn.execute(
                        "UPDATE agents SET catalog_json=? WHERE id=?",
                        (json.dumps(catalog, ensure_ascii=False), agent_id),
                    )

    @staticmethod
    def _best_metric_from_conn(conn, run_id: str) -> tuple[str, float, int | None] | None:
        rows = conn.execute(
            """SELECT epoch,metrics_json FROM metric_events
            WHERE run_id=? AND phase='epoch' ORDER BY id""",
            (run_id,),
        ).fetchall()
        priorities = ("map50-95", "map50", "fitness", "precision", "recall")
        parsed = [(row["epoch"], safe_json(row["metrics_json"], {})) for row in rows]
        keys = []
        for _, metrics in parsed:
            for key, value in metrics.items():
                if isinstance(value, (int, float)) and key not in keys:
                    keys.append(key)
        metric_key = next(
            (key for priority in priorities for key in keys if priority in key.lower().replace("_", "-")),
            None,
        )
        candidates = [
            (float(metrics[metric_key]), epoch)
            for epoch, metrics in parsed
            if metric_key and isinstance(metrics.get(metric_key), (int, float))
        ]
        if not candidates:
            return None
        value, epoch = max(candidates, key=lambda pair: pair[0])
        return metric_key, value, epoch

    def enrich_run_metadata(self, run_id: str = ""):
        """Fill blank factual experiment notes; never overwrite human-authored text."""
        with self.connect() as conn:
            if run_id:
                rows = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM runs").fetchall()
            for row in rows:
                name = str(row["name"] or row["id"])
                best = self._best_metric_from_conn(conn, row["id"])
                hypothesis = (
                    f"评估「{name}」在当前数据与训练配置下的效果、稳定性和资源开销。"
                    if not str(row["hypothesis"] or "").strip() else ""
                )
                result_notes = ""
                if best and not str(row["result_notes"] or "").strip():
                    result_notes = f"当前最佳 {best[0]}={best[1]:.6g}"
                    if best[2] is not None:
                        result_notes += f"（Epoch {best[2]}）"
                    result_notes += "。"
                conclusion = ""
                next_step = ""
                baseline_id = str(row["baseline_run_id"] or "")
                if row["status"] == "completed" and not str(row["conclusion"] or "").strip():
                    if baseline_id and best:
                        baseline_best = self._best_metric_from_conn(conn, baseline_id)
                        if baseline_best and baseline_best[0] == best[0]:
                            delta = best[1] - baseline_best[1]
                            direction = "高于" if delta > 0 else "低于" if delta < 0 else "等于"
                            conclusion = (
                                f"按最佳 {best[0]} 计，本实验{direction}基线 "
                                f"{abs(delta):.6g}；仍需结合重复实验判断稳定性。"
                            )
                        else:
                            conclusion = "实验已完成；基线缺少同名可比指标，暂不做优劣判断。"
                    else:
                        conclusion = "实验已完成；尚未绑定可用 baseline，暂不做优劣判断。"
                if not str(row["next_step"] or "").strip():
                    if not baseline_id:
                        next_step = "绑定明确的 baseline Run ID，再按同一指标完成对比。"
                    elif row["status"] == "completed":
                        next_step = "复核运行日志并重复实验，确认指标差异具有稳定性。"
                    elif row["status"] in {"failed", "stalled"}:
                        next_step = "先定位失败或中断原因，修复后从可用 checkpoint 复现。"
                conn.execute(
                    """UPDATE runs SET
                    hypothesis=CASE WHEN TRIM(hypothesis)='' AND ?!='' THEN ? ELSE hypothesis END,
                    result_notes=CASE WHEN TRIM(result_notes)='' AND ?!='' THEN ? ELSE result_notes END,
                    conclusion=CASE WHEN TRIM(conclusion)='' AND ?!='' THEN ? ELSE conclusion END,
                    next_step=CASE WHEN TRIM(next_step)='' AND ?!='' THEN ? ELSE next_step END
                    WHERE id=?""",
                    (
                        hypothesis, hypothesis, result_notes, result_notes,
                        conclusion, conclusion, next_step, next_step, row["id"],
                    ),
                )

    def lineage_for_script(self, script_id: str) -> dict | None:
        if not script_id:
            return None
        with self.connect() as conn:
            rows = conn.execute("SELECT catalog_json FROM agents ORDER BY updated_at DESC").fetchall()
        for row in rows:
            for item in safe_json(row["catalog_json"], []):
                if isinstance(item, dict) and str(item.get("id") or "") == script_id:
                    return item
        return None

    def lineage_for_run(self, run: dict) -> dict | None:
        run_id = str((run or {}).get("id") or "")
        if not run_id:
            return None
        with self.connect() as conn:
            rows = conn.execute("SELECT catalog_json FROM agents ORDER BY updated_at DESC").fetchall()
            for row in rows:
                for item in safe_json(row["catalog_json"], []):
                    if not isinstance(item, dict) or not item.get("model_yaml_name"):
                        continue
                    matched = self._find_run_for_catalog_item(conn, item)
                    if matched and matched["id"] == run_id:
                        return item
        return None

    def heartbeat_agent(self, payload: dict) -> tuple[dict, bool]:
        agent_id = str(payload.get("agent_id") or "").strip()[:120]
        if not agent_id:
            raise ValueError("agent_id is required")
        hostname = str(payload.get("hostname") or agent_id)[:200]
        gpus = payload.get("gpus") if isinstance(payload.get("gpus"), list) else []
        state = str(payload.get("state") or "idle")[:30]
        current_job_id = str(payload.get("current_job_id") or "")[:80]
        reminder_seconds = max(60, int(payload.get("idle_reminder_seconds") or 1800))
        reminder_count = max(1, min(16, int(payload.get("idle_reminder_gpu_count") or 1)))
        catalog = payload.get("catalog") if isinstance(payload.get("catalog"), list) else []
        catalog = catalog[:500]
        capabilities = payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else {}
        idle_count = sum(
            1 for gpu in gpus if isinstance(gpu, dict) and gpu.get("cuda_usable") is not False
            and gpu.get("telemetry_usable") is not False
            and float(gpu.get("idle_for_seconds") or 0) >= reminder_seconds
        )
        now = utc_now()
        with self.connect() as conn:
            previous = conn.execute("SELECT idle_notified,catalog_json FROM agents WHERE id=?", (agent_id,)).fetchone()
            was_notified = bool(previous and previous["idle_notified"])
            catalog = self.merge_catalog(safe_json(previous["catalog_json"], []) if previous else [], catalog)
            should_notify = state == "idle" and idle_count >= reminder_count and not was_notified
            notified = 1 if state == "idle" and idle_count >= reminder_count else 0
            conn.execute(
                """INSERT INTO agents
                (id,hostname,updated_at,state,gpus_json,current_job_id,idle_reminder_seconds,idle_reminder_gpu_count,idle_notified,catalog_json,capabilities_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                hostname=excluded.hostname,updated_at=excluded.updated_at,state=excluded.state,
                gpus_json=excluded.gpus_json,current_job_id=excluded.current_job_id,
                idle_reminder_seconds=excluded.idle_reminder_seconds,
                idle_reminder_gpu_count=excluded.idle_reminder_gpu_count,
                idle_notified=excluded.idle_notified,catalog_json=excluded.catalog_json,
                capabilities_json=excluded.capabilities_json""",
                (agent_id, hostname, now, state, json.dumps(gpus, ensure_ascii=False), current_job_id,
                 reminder_seconds, reminder_count, notified, json.dumps(catalog, ensure_ascii=False),
                 json.dumps(capabilities, ensure_ascii=False)),
            )
        self.reconcile_catalog_lineage()
        return self.get_agent(agent_id), should_notify

    def get_agent(self, agent_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["gpus"] = safe_json(result.pop("gpus_json"), [])
        result["catalog"] = safe_json(result.pop("catalog_json"), [])
        result["capabilities"] = safe_json(result.pop("capabilities_json"), {})
        result["idle_notified"] = bool(result["idle_notified"])
        return result

    def list_agents(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT id FROM agents ORDER BY updated_at DESC").fetchall()
        return [agent for row in rows if (agent := self.get_agent(row["id"]))]

    def pending_preflights(self, limit: int = 3) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM queue_jobs WHERE status='queued' AND preflight_status='pending'
                ORDER BY priority DESC, created_at ASC LIMIT ?""",
                (max(1, min(20, limit)),),
            ).fetchall()
        return [self._queue_row(row, True) for row in rows]

    def complete_preflight(self, job_id: str, payload: dict) -> dict | None:
        agent_id = str(payload.get("agent_id") or "")[:120]
        if not self.get_agent(agent_id):
            raise PermissionError("agent must heartbeat before preflight")
        status = str(payload.get("status") or "failed")
        if status not in {"passed", "failed"}:
            raise ValueError("invalid preflight status")
        checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
        message = str(payload.get("message") or "")[:4000]
        result = {"agent_id": agent_id, "checks": checks[:50], "message": message, "checked_at": utc_now()}
        with self.connect() as conn:
            exists = conn.execute("SELECT 1 FROM queue_jobs WHERE id=?", (job_id,)).fetchone()
            if not exists:
                return None
            conn.execute(
                "UPDATE queue_jobs SET preflight_status=?,preflight_json=?,updated_at=?,last_error=CASE WHEN ?='failed' THEN ? ELSE '' END WHERE id=? AND status='queued'",
                (status, json.dumps(result, ensure_ascii=False), utc_now(), status, message, job_id),
            )
            conn.execute(
                "INSERT INTO job_events(job_id,created_at,kind,message,data_json) VALUES (?,?,?,?,?)",
                (job_id, utc_now(), "preflight:" + status, message, json.dumps(result, ensure_ascii=False)),
            )
        return self.get_queue_job(job_id, False)

    def claim_queue_job(self, agent_id: str, gpus: list[dict]) -> dict | None:
        if not agent_id:
            raise PermissionError("agent must heartbeat before claiming jobs")
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            agent = conn.execute("SELECT hostname FROM agents WHERE id=?", (agent_id,)).fetchone()
            if not agent:
                raise PermissionError("agent must heartbeat before claiming jobs")
            # GPU indices are local to a training host. Reserve every GPU already
            # leased/running on the same hostname inside this write transaction,
            # so concurrent worker slots cannot claim the same physical card from
            # identical nvidia-smi snapshots.
            reserved_gpus = set()
            reserved_rows = conn.execute(
                """SELECT q.assigned_gpus_json FROM queue_jobs q
                JOIN agents a ON a.id=q.agent_id
                WHERE a.hostname=? AND q.status IN ('leased','running')""",
                (agent["hostname"],),
            ).fetchall()
            for reserved_row in reserved_rows:
                for index in safe_json(reserved_row["assigned_gpus_json"], []):
                    try:
                        reserved_gpus.add(int(index))
                    except (TypeError, ValueError):
                        continue
            rows = conn.execute(
                """SELECT * FROM queue_jobs WHERE status IN ('queued','waiting_memory') AND preflight_status='passed'
                AND (not_before IS NULL OR not_before<=?) ORDER BY priority DESC, created_at ASC""",
                (now,),
            ).fetchall()
            for row in rows:
                job = self._queue_row(row, True)
                candidates = set(job["gpu_candidates"])
                available = []
                for gpu in gpus:
                    if not isinstance(gpu, dict):
                        continue
                    if gpu.get("cuda_usable") is False:
                        continue
                    if gpu.get("telemetry_usable") is False:
                        continue
                    index = int(gpu.get("index", -1))
                    if index in reserved_gpus:
                        continue
                    if candidates and index not in candidates:
                        continue
                    free_mb = float(gpu.get("memory_free_mb") or 0)
                    utilization = float(gpu.get("utilization_percent") or 0)
                    idle_for = float(gpu.get("idle_for_seconds") or 0)
                    if free_mb >= job["min_free_memory_mb"] and utilization <= job["max_gpu_utilization"] and idle_for >= job["idle_seconds"]:
                        available.append(index)
                if len(available) < job["required_gpu_count"]:
                    continue
                assigned = available[: job["required_gpu_count"]]
                cursor = conn.execute(
                    """UPDATE queue_jobs SET status='leased',agent_id=?,assigned_gpus_json=?,
                    attempt_count=attempt_count+1,updated_at=?,started_at=COALESCE(started_at,?)
                    WHERE id=? AND status IN ('queued','waiting_memory')""",
                    (agent_id, json.dumps(assigned), now, now, job["id"]),
                )
                if cursor.rowcount == 1:
                    conn.execute(
                        "INSERT INTO job_events(job_id,created_at,kind,message,data_json) VALUES (?,?,?,?,?)",
                        (job["id"], now, "claimed", f"Agent {agent_id} 已领取任务", json.dumps({"gpus": assigned})),
                    )
                    leased = conn.execute("SELECT * FROM queue_jobs WHERE id=?", (job["id"],)).fetchone()
                    return self._queue_row(leased, True)
        return None

    def agent_job_update(self, job_id: str, payload: dict) -> dict | None:
        job = self.get_queue_job(job_id, True)
        if not job:
            return None
        agent_id = str(payload.get("agent_id") or "")[:120]
        if not job.get("agent_id") or agent_id != job["agent_id"]:
            raise PermissionError("job lease belongs to another agent")
        status = str(payload.get("status") or "running")
        if status not in {"running", "completed", "failed", "queued", "waiting_memory", "cancelled", "paused"}:
            raise ValueError("invalid job status")
        error = str(payload.get("error") or "")[:4000]
        batch_size = payload.get("batch_size", job.get("batch_size"))
        resume_checkpoint = str(payload.get("resume_checkpoint", job.get("resume_checkpoint") or ""))[:1000]
        last_checkpoint = str(payload.get("last_checkpoint", job.get("last_checkpoint") or ""))[:1000]
        runtime_pid = max(0, int(payload.get("runtime_pid") or 0))
        not_before = payload.get("not_before")
        now = utc_now()
        ended = now if status in {"completed", "failed", "cancelled", "paused"} else None
        with self.connect() as conn:
            conn.execute(
                """UPDATE queue_jobs SET status=?,updated_at=?,ended_at=?,last_error=?,
                batch_size=?,resume_checkpoint=?,last_checkpoint=?,runtime_pid=?,not_before=?,
                fault_suggestion=CASE WHEN ? IN ('running','completed','queued') THEN '' ELSE fault_suggestion END,
                pause_requested=CASE WHEN ?='paused' THEN 0 ELSE pause_requested END,
                assigned_gpus_json=CASE WHEN ? IN ('queued','waiting_memory') THEN '[]' ELSE assigned_gpus_json END,
                agent_id=CASE WHEN ? IN ('queued','waiting_memory','completed','failed','cancelled','paused') THEN '' ELSE agent_id END
                WHERE id=?""",
                (status, now, ended, error, batch_size, resume_checkpoint, last_checkpoint, runtime_pid,
                 not_before, status, status, status, status, job_id),
            )
            conn.execute(
                "INSERT INTO job_events(job_id,created_at,kind,message,data_json) VALUES (?,?,?,?,?)",
                (job_id, now, status, error or str(payload.get("message") or "")[:2000], json.dumps(payload.get("data") or {}, ensure_ascii=False)),
            )
        return self.get_queue_job(job_id, True)

    def bind_agent_job_output(self, job_id: str, payload: dict) -> dict | None:
        job = self.get_queue_job(job_id, True)
        if not job:
            return None
        agent_id = str(payload.get("agent_id") or "")[:120]
        if not job.get("agent_id") or agent_id != job["agent_id"]:
            raise PermissionError("job lease belongs to another agent")
        binding = self.normalize_output_binding(payload)
        if not binding:
            raise ValueError("output binding is empty")
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """UPDATE queue_jobs SET
                output_dir=CASE WHEN ?!='' THEN ? ELSE output_dir END,
                output_last_checkpoint=CASE WHEN ?!='' THEN ? ELSE output_last_checkpoint END,
                output_best_checkpoint=CASE WHEN ?!='' THEN ? ELSE output_best_checkpoint END,
                output_results_csv=CASE WHEN ?!='' THEN ? ELSE output_results_csv END,
                output_bound_at=?,updated_at=? WHERE id=?""",
                (
                    binding.get("save_dir", ""), binding.get("save_dir", ""),
                    binding.get("last_model", ""), binding.get("last_model", ""),
                    binding.get("best_model", ""), binding.get("best_model", ""),
                    binding.get("results_csv", ""), binding.get("results_csv", ""),
                    now, now, job_id,
                ),
            )
            conn.execute(
                """INSERT INTO job_events(job_id,created_at,kind,message,data_json)
                VALUES (?,?,?,?,?)""",
                (
                    job_id, now, "output_bound",
                    f"Agent 已绑定实验输出目录：{binding.get('save_dir') or job.get('output_dir') or '—'}",
                    json.dumps(binding, ensure_ascii=False),
                ),
            )
        if job.get("run_id"):
            self.bind_run_output(job["run_id"], binding)
        return self.get_queue_job(job_id, True)

    def reconciliation_jobs(self, agent_id: str) -> list[dict]:
        """Ask the base worker to verify bound files after a service/Agent restart.

        The request timestamp is persisted so a failed verification is retried,
        but a busy worker is not asked to reread the same CSV every heartbeat.
        """
        if "-slot-" in agent_id:
            return []
        cutoff = datetime.fromtimestamp(time.time() - 600, timezone.utc).isoformat(timespec="seconds")
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT q.id,q.run_id,q.output_dir,q.output_results_csv,
                q.output_last_checkpoint,q.output_best_checkpoint,q.runtime_pid,
                r.total_epochs FROM queue_jobs q JOIN runs r ON r.id=q.run_id
                WHERE q.output_dir!='' AND q.status!='cancelled'
                AND (q.reconcile_requested_at IS NULL OR q.reconcile_requested_at<?)
                AND (r.status IN ('stalled','failed') OR
                     (q.status='failed' AND r.status='completed') OR
                     (SELECT COUNT(DISTINCT e.epoch) FROM metric_events e
                      WHERE e.run_id=r.id AND e.phase='epoch')<r.current_epoch)
                ORDER BY q.updated_at DESC LIMIT 1""", (cutoff,),
            ).fetchall()
            for row in rows:
                conn.execute("UPDATE queue_jobs SET reconcile_requested_at=? WHERE id=?", (utc_now(), row["id"]))
        return [dict(row) for row in rows]

    def reconcile_job(self, job_id: str, payload: dict) -> dict | None:
        agent_id = str(payload.get("agent_id") or "")[:120]
        if not self.get_agent(agent_id):
            raise PermissionError("agent must heartbeat before reconciliation")
        rows = payload.get("epochs") or []
        if not isinstance(rows, list) or len(rows) > 50:
            raise ValueError("invalid epoch batch")
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute("SELECT * FROM queue_jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                return None
            run = conn.execute("SELECT * FROM runs WHERE id=?", (job["run_id"],)).fetchone()
            if not run or not job["output_dir"] or str(payload.get("output_dir") or "") != job["output_dir"]:
                raise ValueError("output binding mismatch")
            recovered = 0
            last_metrics = {}
            max_epoch = 0
            for item in rows:
                if not isinstance(item, dict):
                    continue
                epoch = int(item.get("epoch") or 0)
                metrics = item.get("metrics")
                if not 1 <= epoch <= max(10000, int(run["total_epochs"])) or not isinstance(metrics, dict):
                    continue
                clean = {}
                for key, value in list(metrics.items())[:300]:
                    try:
                        number = float(value)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(key, str) and len(key) <= 120 and math.isfinite(number):
                        clean[key] = number
                if not clean:
                    continue
                existing = conn.execute(
                    "SELECT id,metrics_json FROM metric_events WHERE run_id=? AND phase='epoch' AND epoch=? ORDER BY id DESC LIMIT 1",
                    (run["id"], epoch),
                ).fetchone()
                if existing:
                    old = safe_json(existing["metrics_json"], {})
                    if len(clean) > len(old):
                        conn.execute("UPDATE metric_events SET metrics_json=? WHERE id=?", (json.dumps(clean, ensure_ascii=False), existing["id"]))
                        recovered += 1
                else:
                    conn.execute("INSERT INTO metric_events(run_id,epoch,batch,phase,created_at,metrics_json) VALUES (?,?,NULL,'epoch',?,?)",
                                 (run["id"], epoch, now, json.dumps(clean, ensure_ascii=False)))
                    recovered += 1
                if epoch >= max_epoch:
                    max_epoch, last_metrics = epoch, clean
            if max_epoch:
                conn.execute("UPDATE runs SET current_epoch=MAX(current_epoch,?),updated_at=? WHERE id=?",
                             (max_epoch, now, run["id"]))
            verified_epoch = max(int(evidence.get("csv_max_epoch") or 0), max_epoch)
            complete = (not evidence.get("process_alive") and evidence.get("completed_log") is True
                        and evidence.get("checkpoint_exists") is True
                        and verified_epoch >= int(run["total_epochs"]) > 0)
            if complete and job["status"] in {"failed", "running", "completed"}:
                conn.execute("UPDATE runs SET status='completed',current_epoch=MAX(current_epoch,?),ended_at=COALESCE(ended_at,?),updated_at=?,eta_seconds=0,error_message=NULL,final_metrics_json=? WHERE id=?",
                             (verified_epoch, now, now, json.dumps(last_metrics, ensure_ascii=False), run["id"]))
                if job["status"] != "completed":
                    conn.execute("UPDATE queue_jobs SET status='completed',ended_at=COALESCE(ended_at,?),updated_at=?,last_error='',fault_suggestion='',agent_id='' WHERE id=?",
                                 (now, now, job_id))
                    conn.execute("INSERT INTO job_events(job_id,created_at,kind,message,data_json) VALUES (?,?,?,?,?)",
                                 (job_id, now, "reconciled_completed", "已根据绑定输出目录的完整 CSV、训练完成日志及 checkpoint 二次验证成功", json.dumps(evidence, ensure_ascii=False)))
            elif (evidence.get("process_alive") is True and job["status"] == "running"
                  and evidence.get("log_age_seconds") is not None
                  and float(evidence["log_age_seconds"]) < CFG.stale_seconds):
                conn.execute("UPDATE runs SET status='running',updated_at=? WHERE id=?", (now, run["id"]))
            elif recovered:
                conn.execute("INSERT INTO job_events(job_id,created_at,kind,message,data_json) VALUES (?,?,?,?,?)",
                             (job_id, now, "metrics_recovered", f"从绑定的 results.csv 补齐 {recovered} 个 Epoch 指标", "{}"))
        return self.get_queue_job(job_id, False)

    def record_job_anomaly(self, job_id: str, payload: dict) -> dict | None:
        job = self.get_queue_job(job_id, True)
        if not job:
            return None
        if not job.get("agent_id") or str(payload.get("agent_id") or "") != job["agent_id"]:
            raise PermissionError("job lease belongs to another agent")
        now = utc_now()
        kind = str(payload.get("kind") or "unknown")[:80]
        message = str(payload.get("message") or "")[:4000]
        suggestion = fault_suggestion(kind, job, payload.get("data") or {})
        with self.connect() as conn:
            conn.execute("UPDATE queue_jobs SET fault_suggestion=?,updated_at=? WHERE id=?", (suggestion, now, job_id))
            conn.execute(
                "INSERT INTO job_events(job_id,created_at,kind,message,data_json) VALUES (?,?,?,?,?)",
                (job_id, now, "anomaly:" + kind, message, json.dumps({"details": payload.get("data") or {}, "suggestion": suggestion}, ensure_ascii=False)),
            )
        return self.get_queue_job(job_id, True)

    def mark_stalled(self, stale_seconds: int) -> list[dict]:
        """Move silent running jobs to stalled once per silent episode."""
        cutoff = datetime.fromtimestamp(
            time.time() - max(1, stale_seconds), timezone.utc
        ).isoformat(timespec="seconds")
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM runs WHERE status='running' AND updated_at < ?", (cutoff,)
            ).fetchall()
            ids = []
            for row in rows:
                live_agents = conn.execute(
                    """SELECT a.updated_at,a.capabilities_json FROM queue_jobs q
                    JOIN agents a ON a.id=q.agent_id WHERE q.run_id=?
                    AND q.status IN ('leased','running') AND a.state='running'
                    AND a.current_job_id=q.id AND a.updated_at>=?""",
                    (row["id"], cutoff),
                ).fetchall()
                log_is_fresh = any(
                    (age := safe_json(agent["capabilities_json"], {}).get("current_log_age_seconds")) is not None
                    and float(age) < stale_seconds for agent in live_agents
                )
                if not log_is_fresh:
                    ids.append(row["id"])
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE runs SET status='stalled' WHERE id IN ({placeholders})",
                    ids,
                )
        return [run for run_id in ids if (run := self.get_run(run_id))]

    @staticmethod
    def row_to_run(row: sqlite3.Row) -> dict:
        result = dict(row)
        for key in ("parameters_json", "final_metrics_json", "result_json", "host_status_json"):
            result[key.removesuffix("_json")] = safe_json(result.pop(key), {})
        result["tags"] = safe_json(result.pop("tags_json"), [])
        result["ai_models"] = safe_json(result.pop("ai_models_json", "[]"), [])
        result["favorite"] = bool(result.get("favorite"))
        return result


STORE = Store(CFG.db_path)
AI_CONFIG = AiConfigStore(CFG.ai_config_path)


def human_duration(value) -> str:
    if value is None:
        return "—"
    seconds = max(0, int(float(value)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}小时 {minutes}分"
    if minutes:
        return f"{minutes}分 {secs}秒"
    return f"{secs}秒"


def report_summary(run: dict, events: list[dict]) -> dict:
    best = best_epoch_info(events)
    metrics = best.get("metrics") or latest_metrics_for(run, events)
    lowered = {str(key).lower().replace("_", "-"): (key, value) for key, value in metrics.items()}

    def find_metric(*needles):
        for needle in needles:
            for normalized, pair in lowered.items():
                if needle in normalized and isinstance(pair[1], (int, float)):
                    return {"key": pair[0], "value": float(pair[1])}
        return {}

    baseline = STORE.get_run(run.get("baseline_run_id") or "") if run.get("baseline_run_id") else None
    baseline_events = STORE.metric_events(baseline["id"]) if baseline else []
    baseline_best = best_epoch_info(baseline_events) if baseline else {}
    def model_content(item):
        if isinstance(item, dict) and isinstance((item.get("parameters") or {}).get("model_config"), dict):
            return json.dumps(item["parameters"]["model_config"], ensure_ascii=False, indent=2, sort_keys=True)
        try:
            return item["parameters"]["reproducibility"]["files"]["model"].get("content", "")
        except (KeyError, TypeError, AttributeError):
            return ""
    current_config = model_content(run)
    baseline_config = model_content(baseline) if baseline else ""
    config_diff = ""
    if current_config and baseline_config:
        config_diff = "\n".join(list(difflib.unified_diff(
            baseline_config.splitlines(), current_config.splitlines(),
            fromfile=f"baseline/{baseline['name']}", tofile=f"current/{run['name']}", lineterm="",
        ))[:500])
    delta = None
    if best and baseline_best and best.get("metric_key") == baseline_best.get("metric_key"):
        delta = float(best["value"]) - float(baseline_best["value"])
    return {
        "best": best,
        "map50_95": find_metric("map50-95", "map50(b)"),
        "map50": find_metric("map50(b)", "map50"),
        "precision": find_metric("precision"),
        "recall": find_metric("recall"),
        "peak_gpu_memory_mb": float(run.get("peak_gpu_memory_mb") or 0),
        "baseline": {"id": baseline["id"], "name": baseline["name"]} if baseline else {},
        "baseline_delta": delta,
        "config_diff": config_diff,
    }


AI_REQUIRED_FIELDS = (
    "group_name", "tags", "favorite", "hypothesis", "change_notes",
    "result_notes", "conclusion", "next_step", "detailed_report",
)


def ai_http_json(url: str, payload: dict, headers: dict, timeout: int = 120) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    last_error = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read(2_000_001)
                if len(data) > 2_000_000:
                    raise ValueError("AI response exceeded 2 MB")
                value = json.loads(data.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("AI response was not a JSON object")
                return value
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", "replace")
            reason = {
                400: "请求格式错误",
                401: "认证失败，请检查 API Key",
                402: "账户余额不足",
                422: "请求参数错误",
                429: "请求速率达到上限",
                500: "供应商服务器故障",
                503: "供应商服务器繁忙",
            }.get(exc.code, "请求失败")
            last_error = RuntimeError(f"AI HTTP {exc.code}（{reason}）：{detail[:500]}")
            if exc.code not in {408, 409, 429, 500, 502, 503, 504}:
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            last_error = RuntimeError(f"AI request failed: {exc}")
        if attempt == 0:
            time.sleep(2)
    raise last_error or RuntimeError("AI request failed")


def extract_ai_text(provider_type: str, response: dict) -> str:
    if provider_type == "openai_responses":
        if isinstance(response.get("output_text"), str):
            return response["output_text"]
        parts = []
        for item in response.get("output") or []:
            for content in item.get("content") or [] if isinstance(item, dict) else []:
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    parts.append(content["text"])
        return "\n".join(parts)
    if provider_type == "anthropic":
        return "\n".join(
            str(item.get("text") or "")
            for item in response.get("content") or []
            if isinstance(item, dict) and item.get("type") == "text"
        )
    if provider_type == "gemini":
        try:
            return "\n".join(
                str(part.get("text") or "")
                for part in response["candidates"][0]["content"]["parts"]
                if isinstance(part, dict)
            )
        except (KeyError, IndexError, TypeError):
            return ""
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or "") for item in content if isinstance(item, dict)
        )
    return str(content or "")


def parse_ai_analysis(text: str) -> dict:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("模型没有返回可解析的 JSON")
        value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("模型输出必须是 JSON 对象")
    result = {field: value.get(field) for field in AI_REQUIRED_FIELDS}
    result["group_name"] = str(result.get("group_name") or "").strip()[:80]
    tags = result.get("tags") or []
    if isinstance(tags, str):
        tags = tags.replace("，", ",").split(",")
    result["tags"] = [str(tag).strip()[:40] for tag in tags if str(tag).strip()][:20]
    result["favorite"] = result.get("favorite") is True
    for field in ("hypothesis", "change_notes", "result_notes", "conclusion", "next_step"):
        result[field] = str(result.get(field) or "").strip()[:8000]
    result["detailed_report"] = str(result.get("detailed_report") or "").strip()[:30000]
    if not result["detailed_report"] or not result["conclusion"]:
        raise ValueError("模型输出缺少 detailed_report 或 conclusion")
    return result


def call_ai_model(provider: dict, model: str, prompt: str, max_output_tokens: int) -> dict:
    provider_type = provider["type"]
    base_url = provider["base_url"].rstrip("/")
    key = provider["api_key"]
    system = (
        "你是一名严谨的机器学习实验审计员。只依据给定证据作结论，"
        "明确区分事实、推断和待验证项；不得虚构未提供的指标或因果关系。"
    )
    content_attempts = 2 if provider_type == "deepseek" else 1
    last_content_error = None
    for content_attempt in range(content_attempts):
        effective_prompt = prompt
        if content_attempt:
            effective_prompt += (
                "\n上一次返回了空内容或无效 JSON。请务必直接返回完整、合法、非空的 JSON 对象。"
            )
        if provider_type == "openai_responses":
            response = ai_http_json(
                f"{base_url}/responses",
                {"model": model, "instructions": system, "input": effective_prompt, "max_output_tokens": max_output_tokens},
                {"Authorization": f"Bearer {key}"},
            )
        elif provider_type == "anthropic":
            response = ai_http_json(
                f"{base_url}/messages",
                {
                    "model": model, "system": system, "max_tokens": max_output_tokens,
                    "messages": [{"role": "user", "content": effective_prompt}],
                },
                {"x-api-key": key, "anthropic-version": "2023-06-01"},
            )
        elif provider_type == "gemini":
            response = ai_http_json(
                f"{base_url}/models/{quote(model, safe='')}:generateContent",
                {
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": effective_prompt}]}],
                    "generationConfig": {"maxOutputTokens": max_output_tokens, "responseMimeType": "application/json"},
                },
                {"x-goog-api-key": key},
            )
        else:
            response = ai_http_json(
                f"{base_url}/chat/completions",
                {
                    "model": model,
                    "max_tokens": max_output_tokens,
                    "stream": False,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": effective_prompt},
                    ],
                    "response_format": {"type": "json_object"},
                },
                {"Authorization": f"Bearer {key}"},
            )
        text = extract_ai_text(provider_type, response)
        try:
            if not text:
                raise ValueError("模型返回内容为空")
            return parse_ai_analysis(text)
        except ValueError as exc:
            last_content_error = exc
    raise last_content_error or ValueError("模型返回内容为空")


def ai_run_context(run: dict, events: list[dict]) -> dict:
    summary = report_summary(run, events)
    if len(events) > 32:
        step = max(1, len(events) // 30)
        sampled = events[::step][:31]
        if events[-1] not in sampled:
            sampled.append(events[-1])
    else:
        sampled = events
    train_args = ((run.get("parameters") or {}).get("train_args") or {})
    safe_arg_names = (
        "task", "model", "data", "epochs", "batch", "imgsz", "optimizer",
        "lr0", "lrf", "device", "workers", "seed", "deterministic", "amp",
    )
    return {
        "run": {
            "id": run["id"], "name": run["name"], "status": run["status"],
            "epochs": [run.get("current_epoch"), run.get("total_epochs")],
            "elapsed_seconds": run.get("elapsed_seconds"),
            "peak_gpu_memory_mb": run.get("peak_gpu_memory_mb"),
            "error": run.get("error_message") or "",
        },
        "train_args": {key: train_args.get(key) for key in safe_arg_names if key in train_args},
        "metrics": [
            {"epoch": event.get("epoch"), "metrics": event.get("metrics") or {}}
            for event in sampled
        ],
        "deterministic_summary": summary,
        "existing_metadata": {
            key: run.get(key) for key in (
                "group_name", "tags", "favorite", "hypothesis", "change_notes",
                "result_notes", "conclusion", "next_step", "baseline_run_id",
            )
        },
        "log_tail": str(run.get("log_tail") or "")[-5000:],
    }


def ai_analysis_prompt(context: dict, candidate_reports: list[dict] | None = None) -> str:
    schema = {
        "group_name": "简短实验分组",
        "tags": ["2-8个标签"],
        "favorite": False,
        "hypothesis": "可证伪的实验假设",
        "change_notes": "相对 baseline 的明确改动；未知时如实说明",
        "result_notes": "关键指标、最佳 epoch、资源与异常",
        "conclusion": "证据支持的结论及置信限制",
        "next_step": "按优先级给出下一步",
        "detailed_report": "中文 Markdown 详细报告，含摘要、对比、训练稳定性、局限和下一步",
    }
    if candidate_reports is None:
        task = "请独立审阅以下实验数据，并生成实验元数据与详细结论。"
        evidence = context
    else:
        task = (
            "请作为汇总审稿人，结合原始实验数据与多个模型的候选分析形成最终版本。"
            "候选意见冲突时以原始数值为准，并在报告中说明不确定性。"
        )
        evidence = {"experiment": context, "candidate_analyses": candidate_reports}
    return (
        task
        + "\nfavorite 只能在有明确 baseline 改善证据或该实验是关键里程碑时设为 true；没有 baseline 时默认 false。"
        + "\n只输出一个合法 JSON 对象，不要使用代码围栏。必须严格包含以下字段：\n"
        + json.dumps(schema, ensure_ascii=False, indent=2)
        + "\n证据：\n"
        + json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    )


def resolve_ai_model(config: dict, reference: str) -> tuple[dict, str]:
    provider_id, separator, model = reference.partition(":")
    if not separator or not model:
        raise ValueError(f"无效模型引用：{reference}")
    provider = next(
        (
            item for item in config["providers"]
            if item["id"] == provider_id and item["enabled"] and item["api_key"]
        ),
        None,
    )
    if not provider or model not in provider["models"]:
        raise ValueError(f"模型未启用或缺少 API Key：{reference}")
    return provider, model


def generate_ai_analysis(
    run_id: str,
    send_email_after: bool = False,
    already_started: bool = False,
) -> dict:
    config = AI_CONFIG.load()
    selected = config.get("selected_models") or []
    if not config.get("enabled") or not selected:
        raise ValueError("AI 功能未启用或尚未选择模型")
    if not already_started and not STORE.begin_ai_analysis(run_id, send_email_after):
        raise RuntimeError("该实验的 AI 分析正在运行")
    try:
        run = STORE.get_run(run_id)
        if not run:
            raise ValueError("实验不存在")
        events = STORE.metric_events(run_id)
        context = ai_run_context(run, events)
        prompt = ai_analysis_prompt(context)
        candidates = []
        errors = []
        worker_count = min(4, len(selected))
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ai-model") as executor:
            futures = {}
            for reference in selected:
                provider, model = resolve_ai_model(config, reference)
                future = executor.submit(
                    call_ai_model, provider, model, prompt, config["max_output_tokens"]
                )
                futures[future] = reference
            for future in as_completed(futures):
                reference = futures[future]
                try:
                    candidates.append({"model": reference, "analysis": future.result()})
                except Exception as exc:
                    errors.append(f"{reference}: {exc}")
        if not candidates:
            raise RuntimeError("所有模型均分析失败：" + "；".join(errors))
        used_models = [item["model"] for item in candidates]
        final_analysis = candidates[0]["analysis"]
        if len(candidates) > 1:
            synthesis_reference = config.get("synthesis_model") or candidates[0]["model"]
            provider, model = resolve_ai_model(config, synthesis_reference)
            compact_candidates = [
                {
                    "model": item["model"],
                    "analysis": {
                        **item["analysis"],
                        "detailed_report": item["analysis"]["detailed_report"][:7000],
                    },
                }
                for item in candidates
            ]
            final_analysis = call_ai_model(
                provider,
                model,
                ai_analysis_prompt(context, compact_candidates),
                config["max_output_tokens"],
            )
            used_models.append(f"汇总:{synthesis_reference}")
        if errors:
            final_analysis["detailed_report"] += "\n\n### 模型调用备注\n" + "\n".join(
                f"- {message}" for message in errors
            )
        updated = STORE.apply_ai_analysis(
            run_id, final_analysis, used_models,
            bool(config.get("apply_metadata")), bool(config.get("overwrite_metadata")),
        )
        if not updated:
            raise ValueError("实验不存在")
        if send_email_after:
            try:
                sent = send_status_email(updated, include_ai=True)
                STORE.complete_ai_email(
                    run_id,
                    sent,
                    "" if sent else "SMTP 未配置完整，报告已生成但未发送邮件。",
                )
            except Exception as exc:
                STORE.fail_ai_email(run_id, str(exc))
                traceback.print_exc()
        return STORE.get_run(run_id) or updated
    except Exception as exc:
        STORE.fail_ai_analysis(run_id, str(exc))
        raise


def report_svg(run: dict, events: list[dict]) -> str:
    numeric_keys = []
    priorities = ("map50-95", "map50", "precision", "recall", "box-loss", "cls-loss")
    all_keys = []
    for event in events:
        for key, value in (event.get("metrics") or {}).items():
            if isinstance(value, (int, float)) and key not in all_keys:
                all_keys.append(key)
    for priority in priorities:
        for key in all_keys:
            if priority in key.lower().replace("_", "-") and key not in numeric_keys:
                numeric_keys.append(key)
                break
    numeric_keys = numeric_keys[:4]
    width, height = 960, 560
    panels = []
    colors = ("#5ca8ff", "#54d18b", "#ffc857", "#b88cff")
    for index, key in enumerate(numeric_keys):
        values = [(int(event.get("epoch") or 0), float(event["metrics"][key])) for event in events if isinstance((event.get("metrics") or {}).get(key), (int, float))]
        if not values:
            continue
        col, row = index % 2, index // 2
        x0, y0, panel_w, panel_h = 42 + col * 458, 110 + row * 210, 420, 160
        minimum, maximum = min(value for _, value in values), max(value for _, value in values)
        span = maximum - minimum or 1.0
        max_epoch = max(epoch for epoch, _ in values) or 1
        points = " ".join(
            f"{x0 + epoch / max_epoch * panel_w:.1f},{y0 + panel_h - (value - minimum) / span * panel_h:.1f}"
            for epoch, value in values
        )
        panels.append(f'<rect x="{x0}" y="{y0}" width="{panel_w}" height="{panel_h}" rx="10" fill="#0e1629" stroke="#263452"/><text x="{x0}" y="{y0-12}" fill="#c8d5ec" font-size="15">{html.escape(key)}  ·  latest {values[-1][1]:.6g}</text><polyline points="{points}" fill="none" stroke="{colors[index]}" stroke-width="3" stroke-linejoin="round"/>')
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
    <rect width="100%" height="100%" rx="22" fill="#0b1020"/>
    <text x="42" y="48" fill="#e8edf7" font-family="Segoe UI,Arial" font-size="26" font-weight="700">YOLO 实验战报</text>
    <text x="42" y="80" fill="#97a6c4" font-family="Segoe UI,Arial" font-size="16">{html.escape(run['name'])} · {html.escape(run['status'])} · {html.escape(human_duration(run.get('elapsed_seconds')))}</text>
    <g font-family="Segoe UI,Arial">{''.join(panels)}</g></svg>'''


def send_status_email(run: dict, include_ai: bool | None = None):
    if not all((CFG.smtp_user, CFG.smtp_auth_code, CFG.mail_to)):
        print("Email disabled: QQ SMTP settings are incomplete", flush=True)
        return False
    status_cn = {
        "completed": "已完成",
        "failed": "失败",
        "stalled": "疑似卡住/掉线",
        "paused": "已安全暂停",
    }.get(run["status"], run["status"])
    subject = f"[YOLO实验{status_cn}] {run['name']}"
    events = STORE.metric_events(run["id"])
    summary = report_summary(run, events)
    lines = [
        f"实验：{run['name']}",
        f"状态：{status_cn}",
        f"进度：{run['current_epoch']}/{run['total_epochs']} epoch",
        f"耗时：{human_duration(run.get('elapsed_seconds'))}",
        f"最后上报：{run['updated_at']}",
        f"查看详情：{CFG.public_url}/runs/{quote(run['id'])}",
    ]
    if run.get("final_metrics"):
        lines.append("\n最终指标：")
        lines.extend(f"- {key}: {value}" for key, value in run["final_metrics"].items())
    if run.get("error_message"):
        lines.extend(("\n错误：", run["error_message"]))
    if run.get("log_tail"):
        lines.extend(("\n最后日志：", run["log_tail"][-4000:]))
    if include_ai is None:
        include_ai = bool(AI_CONFIG.load().get("email_report"))
    ai_report = str(run.get("ai_report") or "") if include_ai else ""
    if ai_report:
        lines.extend((
            "\nAI 实验结论：",
            f"模型：{', '.join(run.get('ai_models') or []) or '—'}",
            ai_report,
        ))
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = CFG.smtp_user
    message["To"] = CFG.mail_to
    message.set_content("\n".join(lines))
    best = summary.get("best") or {}
    cards = []
    for label, key in (("mAP50-95", "map50_95"), ("mAP50", "map50"), ("Precision", "precision"), ("Recall", "recall")):
        item = summary.get(key) or {}
        cards.append(f'<td style="padding:12px;background:#101a30;border:1px solid #263452"><div style="color:#97a6c4">{label}</div><div style="font-size:22px;font-weight:700">{html.escape(display_number(item.get("value", "—")))}</div></td>')
    delta = summary.get("baseline_delta")
    baseline_html = ""
    if summary.get("baseline"):
        baseline_html = f'<p>相对基线：{html.escape(summary["baseline"]["name"])}' + (f'，最佳指标变化 <b>{delta:+.6g}</b>' if delta is not None else '（没有同名指标可直接比较）') + '</p>'
    ai_html = ""
    if ai_report:
        ai_html = (
            '<div style="margin-top:20px;padding:18px;background:#101a30;border:1px solid #263452;border-radius:12px">'
            '<h3 style="margin-top:0">AI 实验结论</h3>'
            f'<p style="color:#97a6c4">模型：{html.escape(", ".join(run.get("ai_models") or []) or "—")}</p>'
            f'<pre style="white-space:pre-wrap;font-family:Segoe UI,Arial;color:#e8edf7">{html.escape(ai_report)}</pre></div>'
        )
    html_body = f'''<div style="font-family:Segoe UI,Arial;background:#0b1020;color:#e8edf7;padding:24px;border-radius:16px">
    <h2 style="margin-top:0">YOLO 实验战报 · {html.escape(status_cn)}</h2><h3>{html.escape(run['name'])}</h3>
    <p>Best Epoch：<b>{best.get('epoch', '—')}</b>　耗时：<b>{html.escape(human_duration(run.get('elapsed_seconds')))}</b>　显存峰值：<b>{summary['peak_gpu_memory_mb']:.0f} MiB</b></p>
    <table style="border-collapse:separate;border-spacing:8px;width:100%"><tr>{''.join(cards)}</tr></table>{baseline_html}
    {ai_html}
    <p><a style="color:#5ca8ff" href="{CFG.public_url}/runs/{quote(run['id'])}">查看完整曲线、日志和实验笔记</a></p></div>'''
    message.add_alternative(html_body, subtype="html")
    if events:
        message.add_attachment(report_svg(run, events).encode("utf-8"), maintype="image", subtype="svg+xml", filename=f"YOLO-report-{run['id'][:8]}.svg")
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(CFG.smtp_host, CFG.smtp_port, context=context, timeout=15) as smtp:
        smtp.login(CFG.smtp_user, CFG.smtp_auth_code)
        smtp.send_message(message)
    print(f"Status email sent for run {run['id']}: {run['status']}", flush=True)
    return True


def send_idle_email(agent: dict):
    if not all((CFG.smtp_user, CFG.smtp_auth_code, CFG.mail_to)):
        return
    message = EmailMessage()
    message["Subject"] = f"[YOLO GPU空闲提醒] {agent['hostname']}"
    message["From"] = CFG.smtp_user
    message["To"] = CFG.mail_to
    idle = sorted((gpu for gpu in agent.get("gpus", []) if gpu.get("idle_for_seconds")), key=lambda gpu: gpu.get("index", 0))
    lines = [f"训练机：{agent['hostname']}", f"已达到提醒条件：至少 {agent['idle_reminder_gpu_count']} 张 GPU 同时空闲 {human_duration(agent['idle_reminder_seconds'])}", "", "GPU 状态："]
    lines.extend(f"- GPU {gpu.get('index')}: 空闲 {human_duration(gpu.get('idle_for_seconds'))}，可用显存 {gpu.get('memory_free_mb', '—')} MiB" for gpu in idle)
    lines.extend(("", f"查看实验队列：{CFG.public_url}/queue"))
    message.set_content("\n".join(lines))
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(CFG.smtp_host, CFG.smtp_port, context=context, timeout=15) as smtp:
        smtp.login(CFG.smtp_user, CFG.smtp_auth_code)
        smtp.send_message(message)


def fault_suggestion(kind: str, job: dict, data: dict | None = None) -> str:
    """Return deterministic operator advice; this never changes training code."""
    data = data or {}
    batch = job.get("batch_size")
    if kind == "cuda_oom":
        if batch:
            smaller = max(int(job.get("min_batch_size") or 1), int(batch) // 2)
            return f"建议把 batch 从 {batch} 降到 {smaller}，确认 last.pt 可用后再重试；系统不会未经确认修改脚本。"
        return "建议先确认当前脚本的 batch，减半后从 last.pt 续训；也可收紧候选 GPU 或等待显存释放。"
    if kind in {"nonfinite_loss", "loss_explosion"}:
        return "建议从最后一个有限 loss 的 checkpoint 复查学习率、AMP/FP16 数值稳定性和输入数据；不要直接覆盖原实验。"
    if kind in {"disk_low", "disk_full"}:
        return "建议清理旧权重、缓存和日志，确认工作盘满足 Agent 的最低可用空间后再重排；不要删除仍在使用的 checkpoint。"
    if kind in {"process_exit", "recovery_process_missing"}:
        return "建议先查看任务日志尾部和最新 last.pt；有 checkpoint 时使用“恢复任务”，没有时修复退出原因后复制任务重跑。"
    if kind in {"unsafe_job", "preflight"}:
        return "请根据预检明细修正 Python、脚本、工作目录、数据 YAML 或 checkpoint 路径，再重新预检。"
    return "请先查看日志和预检明细，再决定停止、恢复或复制任务；系统不会未经确认修改训练代码。"


def send_anomaly_email(job: dict, payload: dict):
    if not all((CFG.smtp_user, CFG.smtp_auth_code, CFG.mail_to)):
        return
    message = EmailMessage()
    message["Subject"] = f"[YOLO异常:{payload.get('kind','unknown')}] {job['name']}"
    message["From"] = CFG.smtp_user
    message["To"] = CFG.mail_to
    suggestion = job.get("fault_suggestion") or fault_suggestion(str(payload.get("kind") or "unknown"), job, payload.get("data") or {})
    message.set_content(f"任务：{job['name']}\n异常：{payload.get('kind')}\n策略：{job['anomaly_policy']}\n详情：{payload.get('message','')}\n建议：{suggestion}\n\n队列：{CFG.public_url}/queue")
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(CFG.smtp_host, CFG.smtp_port, context=context, timeout=15) as smtp:
        smtp.login(CFG.smtp_user, CFG.smtp_auth_code)
        smtp.send_message(message)


STYLE = """
:root{color-scheme:dark;--bg:#0b1020;--panel:#131b31;--line:#263452;--text:#e8edf7;--muted:#97a6c4;--blue:#5ca8ff;--green:#54d18b;--red:#ff6b76;--amber:#ffc857}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}a{color:var(--blue);text-decoration:none}.wrap{max-width:1180px;margin:auto;padding:24px}.top{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:24px}.top-actions,.controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.brand{font-size:22px;font-weight:750}.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:18px;box-shadow:0 12px 30px #0003}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.stat{background:#0e1629;border:1px solid var(--line);border-radius:10px;padding:14px;min-width:0}.label{color:var(--muted);font-size:13px}.value{font-size:20px;font-weight:700;margin-top:3px;overflow-wrap:anywhere}.bar{height:12px;background:#09101f;border-radius:8px;overflow:hidden}.fill{height:100%;background:#438eff}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:11px 9px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600}.badge{display:inline-block;padding:3px 9px;border-radius:999px;font-size:12px;font-weight:700}.running{background:#17355a;color:#8ac4ff}.completed{background:#153b2a;color:#7be4a9}.failed{background:#4b1f2a;color:#ff9ca4}.stalled,.paused{background:#4a3718;color:#ffd982}.metrics{display:flex;flex-wrap:wrap;gap:8px}.metric,.tag{padding:7px 10px;background:#0e1629;border:1px solid var(--line);border-radius:8px}.tag{display:inline-block;padding:2px 7px;font-size:12px;margin:3px 4px 0 0;color:#b9c8e5}.login{max-width:380px;margin:12vh auto}.login input{width:100%;padding:12px;border:1px solid var(--line);border-radius:8px;background:#0b1020;color:var(--text);margin:9px 0}.button{display:inline-block;border:0;border-radius:8px;padding:10px 14px;background:#287ce8;color:white;font-weight:650;cursor:pointer}.button.secondary{background:#253653}.button.danger{background:#b92f43}.danger-zone{border-color:#6b2936}.danger-zone input{width:min(420px,100%);padding:11px;border:1px solid var(--line);border-radius:8px;background:#0b1020;color:var(--text);margin:8px 8px 8px 0}.muted{color:var(--muted)}pre{white-space:pre-wrap;word-break:break-word;background:#090f1d;border-radius:8px;padding:12px;max-height:420px;overflow:auto}.log-tail{min-height:110px}.chart-shell{position:relative;margin-top:12px}.chart{display:block;width:100%;height:320px;border:1px solid var(--line);border-radius:10px;background:#0e1629}.chart-tooltip{display:none;position:absolute;pointer-events:none;background:#07101f;border:1px solid #405374;border-radius:8px;padding:8px 10px;font:12px/1.45 ui-monospace,monospace;box-shadow:0 8px 22px #0008;z-index:2}.chart-empty{display:none;position:absolute;inset:0;align-items:center;justify-content:center;color:var(--muted);text-align:center;padding:30px}.chart-select,.filter-input{max-width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:#0b1020;color:var(--text)}.filter-input{min-width:150px}.compare-box{width:18px;height:18px}.favorite-button{border:0;background:transparent;color:#6f7f9e;font-size:22px;cursor:pointer;padding:0 5px}.favorite-button.active{color:var(--amber)}.live-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);margin-right:6px}.live-dot.offline{background:var(--amber)}.metadata-form{display:grid;grid-template-columns:1fr 2fr auto auto;gap:10px;align-items:end}.metadata-form label{display:grid;gap:5px}.nowrap{white-space:nowrap}.empty{text-align:center;color:var(--muted);padding:35px}
.panel-heading,.agent-heading,.gpu-card-top,.gpu-memory-caption{display:flex;align-items:center;justify-content:space-between;gap:12px}.panel-heading{margin-bottom:16px;flex-wrap:wrap}.panel-heading h3,.agent-heading h4{margin:0}.queue-summary,.queue-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.summary-chip{padding:5px 10px;border:1px solid var(--line);border-radius:999px;background:#0e1629;color:var(--muted);font-size:12px}.summary-chip strong{color:var(--text);margin-left:4px}.summary-chip.cancelled{border-color:#633041;background:#291927;color:#ff9ca4}.agent-list{display:grid;gap:14px}.agent-card{padding:16px;background:#0e1629;border:1px solid var(--line);border-radius:12px}.agent-heading{align-items:flex-start;margin-bottom:14px}.agent-meta{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin-top:4px}.agent-state,.gpu-state,.queue-status{display:inline-flex;align-items:center;gap:6px;border-radius:999px;font-size:12px;font-weight:700;white-space:nowrap}.agent-state{padding:5px 10px;background:#153b2a;color:#7be4a9}.agent-state.running{background:#17355a;color:#8ac4ff}.agent-state::before,.gpu-state::before,.queue-status::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}.gpu-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}.gpu-card{min-width:0;padding:12px;background:#0a1222;border:1px solid #263a5f;border-radius:10px}.gpu-card.is-idle{border-color:#255b4a}.gpu-card.is-busy{border-color:#614052}.gpu-card-top{align-items:flex-start}.gpu-title{min-width:0}.gpu-index{display:block;font-size:16px;font-weight:800;color:#f4f7ff}.gpu-name{display:block;color:var(--muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.gpu-state{padding:3px 7px;background:#153b2a;color:#7be4a9}.gpu-state.busy{background:#4b1f2a;color:#ff9ca4}.gpu-state.pending{background:#4a3718;color:#ffd982}.gpu-metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:12px 0}.gpu-metric{padding:8px;background:#101a2e;border-radius:7px}.gpu-metric span{display:block;color:var(--muted);font-size:11px}.gpu-metric strong{font-size:14px}.gpu-memory-caption{font-size:12px;color:var(--muted)}.gpu-memory-track{height:7px;margin:6px 0;background:#18233a;border-radius:99px;overflow:hidden}.gpu-memory-fill{height:100%;border-radius:inherit;background:linear-gradient(90deg,#45d2bd,#5ca8ff)}.gpu-free{font-size:12px;color:var(--muted)}.gpu-free strong{color:var(--text)}.queue-table{min-width:1280px}.queue-row{transition:background .15s ease,opacity .15s ease}.queue-row td:first-child{border-left:3px solid transparent}.queue-task-title{display:inline-block;max-width:330px;overflow-wrap:anywhere}.queue-task-link{color:#f1f6ff;text-decoration:underline;text-decoration-color:#3d78ba;text-underline-offset:4px}.queue-task-link:hover{color:#8ac4ff;text-decoration-color:#8ac4ff}.queue-task-link::after{content:"  ↗";color:#5ca8ff;font-size:11px}.queue-status{padding:4px 9px;background:#253653;color:#c8d6ef}.queue-status.status-running,.queue-status.status-leased{background:#17355a;color:#8ac4ff}.queue-status.status-completed{background:#153b2a;color:#7be4a9}.queue-status.status-failed{background:#4b1f2a;color:#ff9ca4}.queue-status.status-waiting_memory,.queue-status.status-paused{background:#4a3718;color:#ffd982}.queue-status.status-cancelled{background:#46202c;color:#ff9ca4;border:1px solid #743247}.queue-row.queue-cancelled{background:linear-gradient(90deg,#331a274f,transparent);color:#8994aa}.queue-row.queue-cancelled td:first-child{border-left-color:#b92f43}.queue-row.queue-cancelled .queue-task-title{text-decoration:line-through;text-decoration-color:#c85e70}.cancelled-note{margin-top:3px;color:#d77c8c;font-size:12px}.queue-row.queue-cancelled .label{color:#707b91}.queue-actions{margin-top:8px}.queue-actions .button{padding:6px 9px;font-size:12px}.queue-actions .button.danger{box-shadow:inset 0 0 0 1px #e14b63}.table-scroll{overflow:auto;border:1px solid #202d49;border-radius:10px}.table-scroll table th:first-child,.table-scroll table td:first-child{padding-left:12px}.queue-edit-form label{display:grid;gap:5px}.queue-edit-form input,.queue-edit-form select,.queue-edit-form textarea{width:100%}.queue-edit-form textarea{min-height:100px;resize:vertical}.form-wide{grid-column:1/-1}.edit-notice{padding:12px 14px;border:1px solid #365d4c;background:#102a23;border-radius:10px;color:#83e1ae;margin-bottom:16px}.edit-notice strong{color:#b8f2d1}.command-preview{font:13px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace}.script-info,.sweep-box{padding:12px 14px;background:#0e1629;border:1px solid var(--line);border-radius:10px}.sweep-box{display:grid;gap:12px}.preflight-status{display:inline-block;padding:3px 8px;border-radius:99px;font-size:12px;font-weight:700}.preflight-passed{background:#153b2a;color:#7be4a9}.preflight-pending{background:#17355a;color:#8ac4ff}.preflight-failed{background:#4b1f2a;color:#ff9ca4}.queue-reason{max-width:360px;margin:0 0 5px;color:#c8d6ef;font-size:12px}.queue-reason::before{content:"• ";color:var(--amber)}.fault-suggestion{max-width:360px;margin-top:8px;padding:8px;background:#312817;border:1px solid #5a4824;border-radius:7px;color:#ffe29a;font-size:12px}.check-list{margin:7px 0;padding-left:18px;max-width:360px}.check-ok{color:#7be4a9}.check-bad{color:#ff9ca4}details summary{cursor:pointer;color:var(--muted);font-size:12px}
.queue-mode-row{display:flex;align-items:end;gap:14px;flex-wrap:wrap;padding-top:4px}.queue-mode-row label{width:min(320px,100%)}.sweep-box{border-color:#36517d;background:linear-gradient(145deg,#101a30,#0d1729)}.sweep-box[hidden]{display:none}
.agent-disclosure{padding:0;overflow:hidden}.agent-disclosure>.agent-summary{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:18px;list-style:none;color:var(--text);font-size:inherit;cursor:pointer;user-select:none}.agent-disclosure>.agent-summary::-webkit-details-marker{display:none}.agent-summary-copy{min-width:0}.agent-summary-title,.agent-summary-side{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.agent-summary-title h3{margin:0}.agent-toggle-text{color:var(--blue);font-size:12px;font-weight:700}.agent-toggle-text::before{content:"展开显卡详情"}.agent-disclosure[open] .agent-toggle-text::before{content:"收起显卡详情"}.agent-summary-side{justify-content:flex-end;flex-shrink:0}.agent-chevron{width:10px;height:10px;border-right:2px solid var(--blue);border-bottom:2px solid var(--blue);transform:rotate(45deg);transition:transform .18s ease;margin:-4px 4px 0 2px}.agent-disclosure[open] .agent-chevron{transform:rotate(225deg);margin:4px 4px 0 2px}.agent-disclosure-body{padding:14px 18px 18px;border-top:1px solid var(--line)}
@media(max-width:780px){.metadata-form{grid-template-columns:1fr}.wrap{padding:15px}.hide-small{display:none}th,td{padding:9px 5px}.chart{height:280px}.top{align-items:flex-start}.value{font-size:17px}.gpu-grid{grid-template-columns:1fr}.agent-heading{flex-direction:column}.queue-summary{width:100%}.agent-disclosure>.agent-summary{align-items:flex-start;padding:15px}.agent-summary-side{justify-content:flex-start}.agent-summary-copy .muted{font-size:12px}.agent-disclosure-body{padding:12px 15px 15px}}
"""

STYLE += """
:root{color-scheme:dark;--bg:#111214;--panel:#1b1c1f;--panel-soft:#232529;--line:#33363b;--text:#f4f4f2;--muted:#a0a4aa;--blue:#b8c9ff;--green:#70d6a2;--red:#f18b90;--amber:#f0c876;--input:#202226;--button:#f0f0ee;--button-text:#17181b;--shadow:0 10px 28px #0002}
:root[data-theme=light]{color-scheme:light;--bg:#f7f7f5;--panel:#fff;--panel-soft:#f2f3f1;--line:#e3e5e3;--text:#1c1e20;--muted:#6b7177;--blue:#315bc1;--green:#19764f;--red:#b83e48;--amber:#875f0f;--input:#fafaf9;--button:#1e2023;--button-text:#fff;--shadow:0 8px 24px #1c1e200a}
html{background:var(--bg)}body{background:var(--bg);color:var(--text);font:14px/1.55 Inter,"Segoe UI",system-ui,-apple-system,sans-serif;min-width:320px}a{color:var(--blue)}a:hover{text-decoration:underline}.wrap{max-width:1480px;padding:28px clamp(16px,3vw,44px)}.top{margin-bottom:25px;align-items:flex-start}.brand{font-size:24px;letter-spacing:-.04em;font-weight:760}.top-actions{justify-content:flex-end}.panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;box-shadow:none;padding:22px;margin-bottom:18px}.panel h3{letter-spacing:-.025em}.label,.muted,th{color:var(--muted)}.value{color:var(--text);letter-spacing:-.03em}.stat{background:transparent;border:0;border-radius:0;padding:4px 2px}.grid{gap:14px}.button{background:var(--button);color:var(--button-text);border:1px solid transparent;border-radius:10px;box-shadow:none;transition:transform .15s ease,opacity .15s ease}.button:hover{opacity:.85;text-decoration:none;transform:translateY(-1px)}.button.secondary{background:var(--panel-soft);border-color:var(--line);color:var(--text)}.button.danger{background:#b54750;color:#fff}.filter-input,.chart-select,.login input,.danger-zone input{background:var(--input);color:var(--text);border:1px solid var(--line);border-radius:10px}.filter-input:focus,.chart-select:focus,.login input:focus,.danger-zone input:focus{outline:2px solid var(--blue);outline-offset:1px}.bar{height:8px;background:var(--panel-soft)}.fill{background:var(--blue)}.metric,.tag,.summary-chip{background:var(--panel-soft);border:0;color:var(--text)}.tag{color:var(--muted)}.badge.running,.agent-state.running,.queue-status.status-running,.queue-status.status-leased{background:color-mix(in srgb,var(--blue) 17%,transparent);color:var(--blue)}.badge.completed,.agent-state,.queue-status.status-completed{background:color-mix(in srgb,var(--green) 17%,transparent);color:var(--green)}.badge.failed,.queue-status.status-failed{background:color-mix(in srgb,var(--red) 17%,transparent);color:var(--red)}.badge.stalled,.badge.paused,.queue-status.status-waiting_memory,.queue-status.status-paused{background:color-mix(in srgb,var(--amber) 19%,transparent);color:var(--amber)}.chart,.chart-tooltip,.chart-select{background:var(--panel-soft);border-color:var(--line);color:var(--text)}.chart-tooltip{box-shadow:var(--shadow)}pre{background:var(--panel-soft);color:var(--text)}.agent-card,.gpu-card,.gpu-metric,.script-info,.sweep-box{background:var(--panel-soft);border-color:var(--line);color:var(--text)}.gpu-state,.gpu-state.pending,.gpu-state.busy,.preflight-status,.queue-status.status-cancelled{border:0}.queue-task-link{color:var(--text);text-decoration-color:var(--muted)}.queue-row.queue-cancelled{background:color-mix(in srgb,var(--red) 7%,transparent)}.table-scroll{border-color:var(--line)}.danger-zone{border-color:color-mix(in srgb,var(--red) 30%,var(--line))}.agent-disclosure-body{border-color:var(--line)}.agent-disclosure>.agent-summary{color:var(--text)}.gpu-index{color:var(--text)}.gpu-memory-track{background:var(--line)}.gpu-memory-fill{background:var(--blue)}.fault-suggestion{background:var(--panel-soft);border-color:var(--line);color:var(--text)}
table{table-layout:auto}th,td{border-bottom:1px solid var(--line)}tr:last-child td{border-bottom:0}th{font-size:12px;letter-spacing:.035em;text-transform:none;font-weight:650}.theme-toggle{font-size:13px;white-space:nowrap}.page-eyebrow{font-size:12px;letter-spacing:.13em;text-transform:uppercase;color:var(--muted);font-weight:700}.page-subtitle{color:var(--muted);margin-top:4px}.summary-grid{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin:2px 0 22px}.summary-card{min-width:0;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:17px 19px}.summary-card .summary-label{font-size:12px;color:var(--muted)}.summary-card .summary-value{font-size:28px;font-weight:750;line-height:1.2;letter-spacing:-.045em;margin-top:8px;white-space:nowrap}.summary-card .summary-foot{font-size:11px;color:var(--muted);margin-top:5px}.summary-card[data-tone=active] .summary-value{color:var(--green)}.summary-card[data-tone=attention] .summary-value{color:var(--amber)}.summary-card[data-tone=error] .summary-value{color:var(--red)}.summary-card[data-tone=unknown] .summary-value{color:var(--muted)}
.run-table{min-width:840px}.run-title{display:block;font-size:15px;font-weight:720;color:var(--text);line-height:1.3}.run-subtitle,.run-tertiary{display:block;color:var(--muted);font-size:12px;line-height:1.5;margin-top:3px}.run-tertiary{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11px}.run-cell{min-width:220px;max-width:380px}.progress-cell{min-width:145px}.progress-text{white-space:nowrap;font-variant-numeric:tabular-nums;font-weight:650}.progress-cell .bar{margin-top:8px}.result-cell{white-space:nowrap;font-variant-numeric:tabular-nums}.result-main{font-weight:700;font-size:15px}.delta-up{color:var(--green)}.delta-down{color:var(--red)}.result-delta{display:block;font-size:12px;margin-top:3px}.time-cell{white-space:nowrap}.section-intro{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:15px}.section-intro h3{margin:0}.section-intro p{margin:3px 0 0;color:var(--muted)}
.detail-hero{padding:24px 26px}.detail-kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:4px}.detail-kpi{padding:9px 19px;border-right:1px solid var(--line);min-width:0}.detail-kpi:last-child{border-right:0}.detail-kpi .label{font-size:12px}.detail-kpi .value{font-size:clamp(20px,2.2vw,30px);font-weight:750;margin-top:8px;white-space:nowrap}.detail-meta{display:flex;gap:12px;flex-wrap:wrap;margin:17px 19px 0;color:var(--muted);font-size:12px}.detail-meta span+span::before{content:'·';margin-right:12px}.detail-hero>.bar{margin:20px 19px 0}.tab-list{display:flex;gap:3px;overflow-x:auto;border-bottom:1px solid var(--line);margin:0 0 20px;padding:0 2px}.tab-button{appearance:none;border:0;border-bottom:2px solid transparent;background:transparent;color:var(--muted);padding:12px 14px;font:inherit;white-space:nowrap;cursor:pointer}.tab-button[aria-selected=true]{border-color:var(--text);color:var(--text);font-weight:700}.tab-button:hover{color:var(--text)}.tab-panel[hidden]{display:none}.tab-panel>.panel{margin-bottom:16px}.tab-panel>.panel:last-child{margin-bottom:0}.metric-section{margin:0 0 27px}.metric-section h3{margin:0 0 12px}.metric-feature-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.metric-feature{background:var(--panel-soft);border-radius:13px;padding:16px;min-width:0}.metric-feature .label{font-size:12px}.metric-feature strong{display:block;font-size:clamp(19px,2vw,27px);letter-spacing:-.04em;margin:7px 0 3px;font-variant-numeric:tabular-nums}.metric-feature small{color:var(--muted)}.loss-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.loss-item{border-top:1px solid var(--line);padding-top:12px}.loss-item strong{display:block;font-size:18px;margin-top:3px}.notes-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.notes-grid label{display:flex;flex-direction:column;gap:7px}.notes-grid textarea{min-height:110px}.notes-grid .wide{grid-column:1/-1}.danger-zone summary{cursor:pointer;color:var(--red);font-weight:650}.danger-zone details>div{padding-top:14px}.danger-zone .confirm-name{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}.env-brief{font-size:12px;color:var(--muted);margin-top:5px}.lineage-forest{display:grid;gap:17px}.lineage-tree,.lineage-tree ul{list-style:none;padding-left:0;margin:0}.lineage-tree ul{border-left:1px solid var(--line);padding-left:24px;margin-left:16px}.lineage-tree li{position:relative;margin:12px 0}.lineage-tree ul>li::before{content:'';position:absolute;width:15px;left:-24px;top:22px;border-top:1px solid var(--line)}.lineage-node{display:inline-flex;align-items:center;gap:13px;flex-wrap:wrap;padding:11px 15px;border-radius:12px;background:var(--panel-soft);max-width:100%}.lineage-node a{font-weight:700;color:var(--text)}.lineage-node .muted{font-size:12px}.lineage-node .badge{padding:2px 7px}.lineage-standalone{display:flex;gap:8px;flex-wrap:wrap}
@media(max-width:1180px){.summary-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.detail-kpis,.metric-feature-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.detail-kpi:nth-child(2){border-right:0}.detail-kpi:nth-child(-n+2){border-bottom:1px solid var(--line)}}
@media(max-width:700px){.wrap{padding:16px}.top{flex-direction:column;align-items:stretch}.top-actions{justify-content:flex-start}.summary-grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.summary-card{padding:13px}.summary-card .summary-value{font-size:23px}.detail-hero{padding:16px}.detail-kpi{padding:12px}.detail-kpi .value{font-size:20px}.metric-feature-grid,.loss-grid,.notes-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.notes-grid .wide{grid-column:1/-1}.tab-button{padding:10px}.panel{padding:17px}.detail-meta{margin:14px 12px 0}.detail-hero>.bar{margin:16px 12px 0}}
@media(max-width:440px){.summary-grid,.metric-feature-grid,.loss-grid,.notes-grid{grid-template-columns:1fr}.detail-kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.summary-card .summary-value{font-size:25px}}
"""


def page(title: str, body: str, refresh: int | None = None) -> str:
    refresh_tag = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">{refresh_tag}
    <title>{html.escape(title)} · MCONG Lab</title>
    <script>try{{document.documentElement.dataset.theme=localStorage.getItem('mcong-lab-theme')||'dark'}}catch(e){{document.documentElement.dataset.theme='dark'}}</script>
    <style>{STYLE}</style></head><body>{body}<script>
    (()=>{{const button=document.createElement('button');button.type='button';button.className='button secondary theme-toggle';
    const update=()=>{{const light=document.documentElement.dataset.theme==='light';button.textContent=light?'☾ 深色':'☀ 浅色';button.setAttribute('aria-label',light?'切换深色主题':'切换浅色主题');button.setAttribute('aria-pressed',String(light))}};
    button.addEventListener('click',()=>{{const next=document.documentElement.dataset.theme==='light'?'dark':'light';document.documentElement.dataset.theme=next;try{{localStorage.setItem('mcong-lab-theme',next)}}catch(e){{}}update();window.dispatchEvent(new Event('resize'))}});
    const actions=document.querySelector('.top-actions');if(actions)actions.prepend(button);else{{button.style.position='fixed';button.style.top='18px';button.style.right='18px';document.body.appendChild(button)}}update()}})();
    </script></body></html>"""


def status_badge(status: str) -> str:
    label = {
        "running": "运行中",
        "completed": "已完成",
        "failed": "失败",
        "stalled": "疑似卡住/掉线",
        "paused": "已安全暂停",
    }.get(status, status)
    return f'<span class="badge {html.escape(status)}">{html.escape(label)}</span>'


def metric_chips(metrics: dict) -> str:
    if not metrics:
        return '<span class="muted">暂无</span>'
    def display(value):
        if isinstance(value, float):
            return f"{value:.7g}"
        return str(value)

    return '<div class="metrics">' + "".join(
        f'<span class="metric"><span class="label">{html.escape(str(k))}</span> {html.escape(display(v))}</span>'
        for k, v in metrics.items()
    ) + "</div>"


def latest_metrics_for(run: dict, events: list[dict]) -> dict:
    if run.get("final_metrics"):
        return run["final_metrics"]
    for event in reversed(events):
        if event["metrics"]:
            return event["metrics"]
    return {}


def best_epoch_info(events: list[dict]) -> dict:
    epoch_events = [
        event for event in events
        if event.get("phase") == "epoch" and isinstance(event.get("metrics"), dict)
    ]
    keys = []
    for event in epoch_events:
        for key, value in event["metrics"].items():
            if isinstance(value, (int, float)) and key not in keys:
                keys.append(key)
    priorities = ("map50-95", "map50", "fitness", "precision", "recall")
    metric_key = next(
        (
            key for priority in priorities for key in keys
            if priority in key.lower().replace("_", "-")
        ),
        None,
    )
    if not metric_key:
        return {}
    candidates = [
        event for event in epoch_events
        if isinstance(event["metrics"].get(metric_key), (int, float))
    ]
    if not candidates:
        return {}
    best = max(candidates, key=lambda event: float(event["metrics"][metric_key]))
    latest = candidates[-1]
    best_value = float(best["metrics"][metric_key])
    latest_value = float(latest["metrics"][metric_key])
    return {
        "epoch": best["epoch"],
        "metric_key": metric_key,
        "value": best_value,
        "latest_epoch": latest["epoch"],
        "latest_value": latest_value,
        "delta_from_best": latest_value - best_value,
        "metrics": best["metrics"],
    }


def display_number(value) -> str:
    if isinstance(value, float):
        return f"{value:.7g}"
    return str(value)


def best_epoch_html(best: dict) -> str:
    if not best:
        return '<p class="muted">至少完成一个包含 mAP、fitness、precision 或 recall 的 Epoch 后显示。</p>'
    delta = best.get("delta_from_best", 0.0)
    delta_text = f"{delta:+.7g}"
    return f"""<div class="grid">
    <div class="stat"><div class="label">Best Epoch</div><div class="value">{best['epoch']}</div></div>
    <div class="stat"><div class="label">最佳指标</div><div class="value">{html.escape(display_number(best['value']))}</div><div class="label">{html.escape(best['metric_key'])}</div></div>
    <div class="stat"><div class="label">最新值</div><div class="value">{html.escape(display_number(best['latest_value']))}</div><div class="label">Epoch {best['latest_epoch']}</div></div>
    <div class="stat"><div class="label">最新值 − 最佳值</div><div class="value">{html.escape(delta_text)}</div></div></div>"""


CORE_METRIC_KEYS = {
    "mAP50-95": ("metrics/mAP50-95(B)", "metrics/mAP50-95(M)", "metrics/mAP50-95"),
    "mAP50": ("metrics/mAP50(B)", "metrics/mAP50(M)", "metrics/mAP50"),
    "Precision": ("metrics/precision(B)", "metrics/precision(M)", "metrics/precision"),
    "Recall": ("metrics/recall(B)", "metrics/recall(M)", "metrics/recall"),
}


def core_metric(metrics: dict, label: str) -> float | None:
    for key in CORE_METRIC_KEYS[label]:
        value = metrics.get(key)
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return float(value)
    return None


def best_core_metric(events: list[dict], label: str) -> tuple[float | None, int | None]:
    candidates = [
        (value, int(event["epoch"])) for event in events
        if isinstance(event.get("metrics"), dict)
        if (value := core_metric(event["metrics"], label)) is not None
    ]
    return max(candidates, key=lambda item: item[0]) if candidates else (None, None)


def percentage(value: float | None) -> str:
    return f"{value * 100:.2f}%" if value is not None else "—"


def baseline_delta_html(run: dict, current_best: float | None) -> str:
    baseline_id = str(run.get("baseline_run_id") or "")
    if not baseline_id:
        return "未设置基线"
    baseline = STORE.best_map50_95_by_run({baseline_id}).get(baseline_id)
    if baseline is None or current_best is None:
        return "等待可比结果"
    delta = (current_best - baseline) * 100
    return f'<span class="{"delta-up" if delta >= 0 else "delta-down"}">{delta:+.2f} pp</span>'


def performance_html(events: list[dict], latest: dict) -> str:
    cards = []
    for label in CORE_METRIC_KEYS:
        current = core_metric(latest, label)
        best, epoch = best_core_metric(events, label)
        cards.append(
            f'<div class="metric-feature"><span class="label">{html.escape(label)}</span>'
            f'<strong>{percentage(current)}</strong><small>最佳 {percentage(best)}'
            f'{f" · Epoch {epoch}" if epoch is not None else ""}</small></div>'
        )
    return '<div class="metric-feature-grid">' + "".join(cards) + "</div>"


def losses_html(latest: dict) -> str:
    items = []
    for label, key in (("Box loss", "box_loss"), ("Cls loss", "cls_loss"), ("DFL loss", "dfl_loss")):
        train = latest.get("train/" + key)
        val = latest.get("val/" + key)
        if not isinstance(train, (int, float)):
            train = None
        if not isinstance(val, (int, float)):
            val = None
        items.append(
            f'<div class="loss-item"><span class="label">{label}</span>'
            f'<strong>{display_number(float(train)) if train is not None else "—"}</strong>'
            f'<span class="label">验证 {display_number(float(val)) if val is not None else "—"}</span></div>'
        )
    return '<div class="loss-grid">' + "".join(items) + "</div>"


def environment_brief(parameters: dict) -> str:
    repro = parameters.get("reproducibility") if isinstance(parameters, dict) else None
    if not isinstance(repro, dict):
        return "环境信息待上传"
    libraries = repro.get("libraries") or {}
    if not isinstance(libraries, dict):
        libraries = {}
    parts = []
    for key, label in (("torch", "PyTorch"), ("cuda_runtime", "CUDA"), ("ultralytics", "YOLO")):
        if libraries.get(key):
            parts.append(f"{label} {libraries[key]}")
    return " · ".join(parts) if parts else "环境信息待上传"


def reproducibility_html(parameters: dict) -> str:
    repro = parameters.get("reproducibility") if isinstance(parameters, dict) else None
    if not isinstance(repro, dict) or not repro:
        return '<p class="muted">当前记录没有可复现信息；更新训练端 yolo_monitor.py 后的新实验会自动采集。</p>'
    rows = []

    def walk(prefix, value):
        if isinstance(value, dict):
            for key, child in value.items():
                walk(f"{prefix}.{key}" if prefix else str(key), child)
        elif value not in (None, "", [], {}):
            rows.append(
                f"<tr><td class=\"label\">{html.escape(prefix)}</td><td>{html.escape(display_number(value))}</td></tr>"
            )

    walk("", repro)
    return '<div style="overflow:auto"><table><tbody>' + "".join(rows) + "</tbody></table></div>"


def experiment_display_name(name: str) -> tuple[str, str]:
    """Keep the stored run name unchanged; only humanize known series IDs."""
    match = re.match(r"^([A-Za-z]{2,8})[-_ ]?(\d{3})(?:[-_ ]+(.*))?$", name)
    if not match:
        return name, ""
    code = f"{match.group(1).upper()}-{match.group(2)}"
    details = " · ".join(part for part in re.split(r"[_-]+", match.group(3) or "") if part)
    return code, details


def dashboard_run(run: dict, best_values: dict[str, float] | None = None) -> dict:
    keys = (
        "id", "name", "status", "total_epochs", "current_epoch", "started_at",
        "updated_at", "elapsed_seconds", "group_name", "tags", "favorite", "baseline_run_id",
        "metadata_revision",
    )
    result = {key: run.get(key) for key in keys}
    result["display_code"], result["display_subtitle"] = experiment_display_name(str(run.get("name") or ""))
    reproducibility = (run.get("parameters") or {}).get("reproducibility") or {}
    result["git_short"] = str((reproducibility.get("git") or {}).get("commit") or "")[:12]
    best_values = best_values or {}
    best = best_values.get(str(run.get("id") or ""))
    baseline = best_values.get(str(run.get("baseline_run_id") or ""))
    result["best_map50_95"] = best
    result["baseline_delta_pp"] = round((best - baseline) * 100, 4) if best is not None and baseline is not None else None
    return result


def dashboard_summary() -> dict:
    """Counts use the whole database, while the table may show only recent runs."""
    now_cst = datetime.now(timezone(timedelta(hours=8)))
    day_start = now_cst.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    day_end = day_start + timedelta(days=1)
    with STORE.connect() as conn:
        statuses = {row["status"]: row["count"] for row in conn.execute(
            "SELECT status,COUNT(*) AS count FROM runs GROUP BY status"
        )}
        queued = conn.execute(
            "SELECT COUNT(*) FROM queue_jobs WHERE status IN ('queued','waiting_memory')"
        ).fetchone()[0]
        finished_today = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE status='completed' AND ended_at>=? AND ended_at<?",
            (day_start.isoformat(timespec="seconds"), day_end.isoformat(timespec="seconds")),
        ).fetchone()[0]
    gpus = {}
    now = datetime.now(timezone.utc)
    for agent in STORE.list_agents():
        try:
            heartbeat = datetime.fromisoformat(str(agent.get("updated_at") or ""))
            if heartbeat.tzinfo is None or (now - heartbeat).total_seconds() > 120:
                continue
        except ValueError:
            continue
        host = str(agent.get("hostname") or agent.get("id") or "unknown")
        caps = agent.get("capabilities") or {}
        for gpu in agent.get("gpus") or []:
            if not isinstance(gpu, dict):
                continue
            key = (host, str(gpu.get("index", "")))
            if key in gpus:
                continue
            try:
                used = float(gpu.get("memory_used_mb") or 0)
                utilization = float(gpu.get("utilization_percent") or 0)
                busy = (used > float(caps.get("idle_memory_used_mb", 3000)) or
                        utilization > float(caps.get("idle_utilization_percent", 5)))
            except (TypeError, ValueError):
                busy = None
            if gpu.get("cuda_usable") is False or gpu.get("telemetry_usable") is False:
                busy = None
            gpus[key] = busy
    usable = [busy for busy in gpus.values() if busy is not None]
    return {
        "running": statuses.get("running", 0),
        "queued": queued,
        "completed": statuses.get("completed", 0),
        "failed": statuses.get("failed", 0),
        "stalled": statuses.get("stalled", 0),
        "gpu_busy": sum(usable),
        "gpu_total": len(usable),
        "completed_today": finished_today,
    }


def dashboard_payload(runs: list[dict] | None = None) -> dict:
    runs = runs if runs is not None else STORE.list_runs()
    related_ids = {str(run["id"]) for run in runs}
    related_ids.update(str(run.get("baseline_run_id")) for run in runs if run.get("baseline_run_id"))
    best_values = STORE.best_map50_95_by_run(related_ids)
    return {"runs": [dashboard_run(run, best_values) for run in runs], "summary": dashboard_summary()}


def run_snapshot(run: dict, events: list[dict]) -> dict:
    best = best_epoch_info(events)
    latest = latest_metrics_for(run, events)
    best_map, _ = best_core_metric(events, "mAP50-95")
    return {
        "run": run,
        "events": events,
        "best": best,
        "fragments": {
            "status": status_badge(run["status"]),
            "metrics": metric_chips(latest),
            "best": best_epoch_html(best),
            "best_map": percentage(best_map),
            "baseline_delta": baseline_delta_html(run, best_map),
            "performance": performance_html(events, latest),
            "losses": losses_html(latest),
            "environment_brief": environment_brief(run.get("parameters") or {}),
            "host": host_status_html(run.get("host_status") or {}),
            "result": metric_chips(run.get("result") or {}),
            "reproducibility": reproducibility_html(run.get("parameters") or {}),
        },
    }


def host_status_html(host: dict) -> str:
    if not host:
        return '<p class="muted">训练端尚未上报主机状态。更新训练端监控文件后会自动显示。</p>'
    memory = host.get("memory") or {}
    load = host.get("load") or {}
    cards = f"""<div class="grid">
    <div class="stat"><div class="label">主机</div><div class="value">{html.escape(str(host.get('hostname') or '—'))}</div></div>
    <div class="stat"><div class="label">CPU（1 分钟负载 / 核）</div><div class="value">{html.escape(str(load.get('per_cpu_percent', '—')))}%</div></div>
    <div class="stat"><div class="label">内存</div><div class="value">{html.escape(str(memory.get('used_mb', '—')))} / {html.escape(str(memory.get('total_mb', '—')))} MiB</div><div class="label">{html.escape(str(memory.get('used_percent', '—')))}%</div></div>
    <div class="stat"><div class="label">采样时间</div><div class="value" style="font-size:14px">{html.escape(str(host.get('sampled_at') or '—'))}</div></div></div>"""
    gpus = host.get("gpus") or []
    if not gpus:
        return cards + '<p class="muted">未读取到 NVIDIA GPU 状态。</p>'
    rows = "".join(
        f"""<tr><td>{html.escape(str(gpu.get('index', '—')))}</td><td>{html.escape(str(gpu.get('name', '—')))}</td>
        <td>{html.escape(str(gpu.get('utilization_percent', '—')))}%</td>
        <td>{html.escape(str(gpu.get('memory_used_mb', '—')))} / {html.escape(str(gpu.get('memory_total_mb', '—')))} MiB</td>
        <td>{html.escape(str(gpu.get('temperature_c', '—')))}°C</td></tr>"""
        for gpu in gpus
    )
    return cards + f"""<div style="overflow:auto;margin-top:12px"><table><thead><tr><th>GPU</th><th>型号</th><th>利用率</th><th>显存</th><th>温度</th></tr></thead><tbody>{rows}</tbody></table></div>"""


def trend_panel(sources: list[dict], compare: bool = False) -> str:
    """Render one honest metric scale at a time; comparison uses the same metric."""
    source_json = json.dumps(sources, ensure_ascii=False).replace("</", "<\\/")
    title = "多实验指标对比" if compare else "Epoch 指标趋势"
    subtitle = (
        "选择一个指标，在所有实验间使用同一纵轴刻度；悬停数据点查看精确值。"
        if compare
        else "每次只显示一个指标，纵轴为真实数值；悬停数据点查看精确值。"
    )
    mode = "compare" if compare else "single"
    template = """<div class="panel"><div class="top" style="margin-bottom:8px"><div><h3 style="margin:0">__TITLE__</h3><div class="muted">__SUBTITLE__</div></div><label class="controls"><span class="label">指标</span><select id="metricSelect" class="chart-select"></select></label></div>
    <div class="chart-shell"><canvas id="chart" class="chart"></canvas><div id="chartEmpty" class="chart-empty"></div><div id="chartTooltip" class="chart-tooltip"></div></div>
    <div id="chartLegend" class="metrics" style="margin-top:10px"></div><div id="chartStats" class="grid" style="margin-top:12px"></div>
    <script>
    (()=>{
      let sources=__SOURCES__;const mode='__MODE__';
      const colors=['#5ca8ff','#ffc857','#54d18b','#c28cff','#ff7f6e'];
      const dashes=[[],[7,4],[2,3],[10,3,2,3],[12,5]];
      const select=document.getElementById('metricSelect'), canvas=document.getElementById('chart');
      const ctx=canvas.getContext('2d'), empty=document.getElementById('chartEmpty');
      const tooltip=document.getElementById('chartTooltip'), stats=document.getElementById('chartStats');
      const legend=document.getElementById('chartLegend'); let hitPoints=[];
      const epochEvents=s=>s.events.filter(e=>e.phase==='epoch'&&e.metrics&&Object.keys(e.metrics).length);let keys=[];
      const priority=['metrics/map50-95','metrics/map50','metrics/precision','metrics/recall','val/box_loss','train/box_loss'];
      const storageKey='yolo-monitor-metric:'+mode+':'+sources.map(s=>s.id).join(',');
      function refreshKeys(){const current=select.value||sessionStorage.getItem(storageKey);keys=[...new Set(sources.flatMap(s=>epochEvents(s).flatMap(e=>Object.keys(e.metrics))))].filter(k=>sources.some(s=>epochEvents(s).some(e=>Number.isFinite(Number(e.metrics[k])))));keys.sort((a,b)=>{const rank=k=>{const x=k.toLowerCase();const i=priority.findIndex(p=>x.includes(p));return i<0?999:i};return rank(a)-rank(b)||a.localeCompare(b)});select.replaceChildren();keys.forEach(k=>{const o=document.createElement('option');o.value=k;o.textContent=k;select.appendChild(o)});if(current&&keys.includes(current))select.value=current}
      const fmt=v=>{if(!Number.isFinite(v))return '—';const a=Math.abs(v);if(a!==0&&(a<0.0001||a>=100000))return v.toExponential(4);return Number(v.toPrecision(7)).toString()};
      const pointsFor=(source,key)=>{const byEpoch=new Map();epochEvents(source).forEach(e=>{if(e.metrics[key]===undefined||e.metrics[key]===null)return;const epoch=Number(e.epoch),value=Number(e.metrics[key]);if(Number.isFinite(epoch)&&Number.isFinite(value))byEpoch.set(epoch,{epoch,value})});return [...byEpoch.values()].sort((a,b)=>a.epoch-b.epoch)};
      const addStat=(label,value,accent)=>{const box=document.createElement('div');box.className='stat';if(accent)box.style.borderColor=accent;const l=document.createElement('div');l.className='label';l.textContent=label;const v=document.createElement('div');v.className='value';v.textContent=value;box.append(l,v);stats.appendChild(box)};
      function render(){
        const key=select.value;sessionStorage.setItem(storageKey,key);stats.replaceChildren();legend.replaceChildren();tooltip.style.display='none';
        const light=document.documentElement.dataset.theme==='light',palette=light?['#315bc1','#a6631a','#19764f','#7550aa','#b83e48']:colors;
        const chartStyle=getComputedStyle(document.documentElement),axisColor=chartStyle.getPropertyValue('--muted').trim(),gridColor=chartStyle.getPropertyValue('--line').trim();
        const series=sources.map((s,i)=>({...s,color:palette[i%palette.length],dash:dashes[i%dashes.length],points:pointsFor(s,key)}));
        const all=series.flatMap(s=>s.points); const drawable=series.some(s=>s.points.length>=2);
        if(mode==='single'&&all.length){const vals=all.map(p=>p.value);addStat('最新值',fmt(all[all.length-1].value));addStat('最小值',fmt(Math.min(...vals)));addStat('最大值',fmt(Math.max(...vals)));addStat('Epoch 数据点',String(all.length))}
        if(mode==='compare')series.forEach(s=>{const p=s.points[s.points.length-1];addStat(s.name,p?('Epoch '+p.epoch+' · '+fmt(p.value)):'无此指标',s.color)});
        series.forEach(s=>{if(mode==='compare'){const chip=document.createElement('span');chip.className='metric';chip.style.borderColor=s.color;chip.textContent=s.name+(s.dash.length?'（虚线）':'（实线）');legend.appendChild(chip)}});
        const dpr=window.devicePixelRatio||1,w=canvas.clientWidth,h=canvas.clientHeight;canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);hitPoints=[];
        empty.style.display=drawable?'none':'flex';empty.textContent=all.length?'数据点不足 2 个，先显示精确值，暂不绘制趋势。':'该指标尚无 Epoch 数据。';if(!drawable)return;
        const left=74,right=24,top=24,bottom=48,pw=w-left-right,ph=h-top-bottom;
        let ymin=Math.min(...all.map(p=>p.value)),ymax=Math.max(...all.map(p=>p.value));let pad=(ymax-ymin)*0.08;if(!pad)pad=Math.max(Math.abs(ymax)*0.1,0.01);ymin-=pad;ymax+=pad;
        let xmin=Math.min(...all.map(p=>p.epoch)),xmax=Math.max(...all.map(p=>p.epoch));if(xmin===xmax){xmin-=0.5;xmax+=0.5}
        const xp=e=>left+(e-xmin)/(xmax-xmin)*pw,yp=v=>top+(ymax-v)/(ymax-ymin)*ph;
        ctx.font='12px ui-monospace,monospace';ctx.textBaseline='middle';ctx.lineWidth=1;ctx.setLineDash([]);
        for(let i=0;i<=5;i++){const y=top+ph*i/5,val=ymax-(ymax-ymin)*i/5;ctx.strokeStyle=gridColor;ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(w-right,y);ctx.stroke();ctx.fillStyle=axisColor;ctx.textAlign='right';ctx.fillText(fmt(val),left-10,y)}
        const xTicks=Math.min(5,Math.max(1,Math.round(xmax-xmin)));ctx.textAlign='center';for(let i=0;i<=xTicks;i++){const e=xmin+(xmax-xmin)*i/xTicks,x=xp(e);ctx.fillStyle=axisColor;ctx.fillText(String(Math.round(e)),x,h-bottom+22)}
        ctx.fillStyle=axisColor;ctx.fillText('Epoch',left+pw/2,h-12);
        series.forEach(s=>{if(!s.points.length)return;ctx.strokeStyle=s.color;ctx.fillStyle=s.color;ctx.lineWidth=2;ctx.setLineDash(s.dash);if(s.points.length>=2){ctx.beginPath();s.points.forEach((p,i)=>{const x=xp(p.epoch),y=yp(p.value);i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke()}ctx.setLineDash([]);s.points.forEach(p=>{const x=xp(p.epoch),y=yp(p.value);ctx.beginPath();ctx.arc(x,y,3.5,0,Math.PI*2);ctx.fill();hitPoints.push({x,y,p,s})})});
      }
      select.addEventListener('change',render);window.addEventListener('resize',render);window.updateTrendSources=next=>{sources=next;refreshKeys();render()};
      canvas.addEventListener('mousemove',ev=>{const r=canvas.getBoundingClientRect(),mx=ev.clientX-r.left,my=ev.clientY-r.top;let best=null,dist=12;hitPoints.forEach(h=>{const d=Math.hypot(mx-h.x,my-h.y);if(d<dist){best=h;dist=d}});if(!best){tooltip.style.display='none';return}tooltip.textContent=(mode==='compare'?best.s.name+'\\n':'')+'Epoch '+best.p.epoch+'\\n'+select.value+': '+fmt(best.p.value);tooltip.style.whiteSpace='pre';tooltip.style.display='block';tooltip.style.left=Math.min(canvas.clientWidth-210,best.x+12)+'px';tooltip.style.top=Math.max(4,best.y-55)+'px'});canvas.addEventListener('mouseleave',()=>tooltip.style.display='none');refreshKeys();render();
    })();
    </script></div>"""
    return (
        template.replace("__TITLE__", title)
        .replace("__SUBTITLE__", subtitle)
        .replace("__SOURCES__", source_json)
        .replace("__MODE__", mode)
    )


def dashboard_html(runs: list[dict]) -> str:
    payload_json = json.dumps(dashboard_payload(runs), ensure_ascii=False).replace("</", "<\\/")
    template = """<div class="wrap"><div class="top"><div><div class="page-eyebrow">Research workspace</div><div class="brand">MCONG Lab</div><div class="page-subtitle"><span id="liveDot" class="live-dot"></span><span id="liveText">实时连接中</span> · 实验总览</div></div><div class="top-actions"><a class="button secondary" href="/lineage">实验谱系</a><a class="button secondary" href="/queue">队列 / Agent</a><a class="button secondary" href="/ai">AI 配置</a><a class="button secondary" href="/logout">退出登录</a></div></div>
    <section class="summary-grid" aria-label="平台状态总览">
      <div class="summary-card" data-tone="active"><div class="summary-label">运行中</div><div id="summaryRunning" class="summary-value">—</div><div class="summary-foot">实时实验</div></div>
      <div class="summary-card"><div class="summary-label">排队中</div><div id="summaryQueued" class="summary-value">—</div><div class="summary-foot">包含等待显存</div></div>
      <div class="summary-card"><div class="summary-label">已完成</div><div id="summaryCompleted" class="summary-value">—</div><div class="summary-foot">全部历史记录</div></div>
      <div class="summary-card" data-tone="error"><div class="summary-label">失败</div><div id="summaryFailed" class="summary-value">—</div><div id="summaryFailedFoot" class="summary-foot">需要处理的记录</div></div>
      <div class="summary-card"><div class="summary-label">GPU Busy</div><div id="summaryGpu" class="summary-value">—</div><div id="summaryGpuFoot" class="summary-foot">训练机遥测</div></div>
      <div class="summary-card"><div class="summary-label">今日完成</div><div id="summaryToday" class="summary-value">—</div><div class="summary-foot">北京时间 00:00 起</div></div>
    </section>
    <div class="panel"><div class="controls"><input id="nameFilter" class="filter-input" type="search" placeholder="搜索编号或实验名称" aria-label="搜索实验"><select id="statusFilter" class="filter-input"><option value="">全部状态</option><option value="running">运行中</option><option value="stalled">疑似卡住/掉线</option><option value="completed">已完成</option><option value="failed">失败</option></select><select id="groupFilter" class="filter-input"><option value="">全部分组</option></select><select id="tagFilter" class="filter-input"><option value="">全部标签</option></select><label class="controls"><input id="favoriteFilter" type="checkbox">只看收藏</label><span id="visibleCount" class="muted"></span></div></div>
    <form id="compareForm" method="get" action="/compare"><div class="panel"><div class="section-intro"><div><h3>实验记录</h3><p>编号、进度与最佳检测性能一目了然</p></div><button class="button secondary" type="submit">对比所选实验</button></div><div style="overflow:auto"><table class="run-table"><thead><tr><th>对比</th><th>收藏</th><th>实验</th><th>状态</th><th>进度</th><th>Best mAP50-95</th><th>最后更新</th><th class="hide-small">耗时</th></tr></thead><tbody id="runRows"></tbody></table></div></div></form>
    <script>
    (()=>{
      const initial=__PAYLOAD__;let runs=initial.runs||[];const rows=document.getElementById('runRows'),form=document.getElementById('compareForm');
      const nameFilter=document.getElementById('nameFilter'),statusFilter=document.getElementById('statusFilter'),groupFilter=document.getElementById('groupFilter'),tagFilter=document.getElementById('tagFilter'),favoriteFilter=document.getElementById('favoriteFilter');
      const selectionKey='yolo-monitor-compare-selection';let selected=new Set();try{selected=new Set(JSON.parse(sessionStorage.getItem(selectionKey)||'[]'))}catch(e){}
      const statusLabels={running:'运行中',stalled:'疑似卡住/掉线',completed:'已完成',failed:'失败'};
      const duration=value=>{if(value===null||value===undefined)return '—';let s=Math.max(0,Math.floor(Number(value)||0)),h=Math.floor(s/3600);s%=3600;const m=Math.floor(s/60);s%=60;return h?(h+'小时 '+m+'分'):(m?(m+'分 '+s+'秒'):(s+'秒'))};
      const dateFormat=new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
      const dateParts=date=>Object.fromEntries(dateFormat.formatToParts(date).filter(x=>x.type!=='literal').map(x=>[x.type,x.value]));
      const dateKey=date=>{const p=dateParts(date);return p.year+'-'+p.month+'-'+p.day};
      const timeLabel=value=>{if(!value)return {short:'—',full:''};const date=new Date(value);if(!Number.isFinite(date.getTime()))return {short:'—',full:String(value)};const p=dateParts(date),full=p.year+'-'+p.month+'-'+p.day+' '+p.hour+':'+p.minute+':'+p.second+' CST',mins=Math.max(0,Math.floor((Date.now()-date.getTime())/60000));let short;if(mins<1)short='刚刚';else if(mins<60)short=mins+' 分钟前';else if(mins<24*60)short=Math.floor(mins/60)+' 小时前';else if(dateKey(date)===dateKey(new Date(Date.now()-86400000)))short='昨天 '+p.hour+':'+p.minute;else short=p.month+'月'+p.day+'日 '+p.hour+':'+p.minute;return {short,full}};
      const add=(parent,tag,text,className)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;parent.appendChild(node);return node};
      function renderSummary(s){const data=s||{};for(const [id,key] of [['summaryRunning','running'],['summaryQueued','queued'],['summaryCompleted','completed'],['summaryFailed','failed'],['summaryToday','completed_today']])document.getElementById(id).textContent=data[key]??'—';document.getElementById('summaryGpu').textContent=data.gpu_total?data.gpu_busy+' / '+data.gpu_total:'—';document.getElementById('summaryGpuFoot').textContent=data.gpu_total?'按 Agent 空闲阈值判定':'暂无新鲜 GPU 遥测';document.getElementById('summaryFailedFoot').textContent=data.stalled?'另有 '+data.stalled+' 条疑似掉线':'需要处理的记录'}
      function updateOptions(select,values,label){const current=select.value;select.replaceChildren();const first=document.createElement('option');first.value='';first.textContent=label;select.appendChild(first);[...new Set(values.filter(Boolean))].sort((a,b)=>a.localeCompare(b,'zh-CN')).forEach(value=>{const o=document.createElement('option');o.value=value;o.textContent=value;select.appendChild(o)});if([...select.options].some(o=>o.value===current))select.value=current}
      function refreshFilterOptions(){updateOptions(groupFilter,runs.map(r=>r.group_name),'全部分组');updateOptions(tagFilter,runs.flatMap(r=>r.tags||[]),'全部标签')}
      function matches(run){const q=nameFilter.value.trim().toLowerCase();return(!q||[run.name,run.display_code,run.display_subtitle].some(v=>String(v||'').toLowerCase().includes(q)))&&(!statusFilter.value||run.status===statusFilter.value)&&(!groupFilter.value||run.group_name===groupFilter.value)&&(!tagFilter.value||(run.tags||[]).includes(tagFilter.value))&&(!favoriteFilter.checked||run.favorite)}
      async function setFavorite(run,value){const password=prompt('收藏操作需要管理员密码：');if(password===null)return;const response=await fetch('/api/v1/web/runs/'+encodeURIComponent(run.id)+'/metadata',{method:'POST',headers:{'Content-Type':'application/json','X-Monitor-Request':'dashboard'},body:JSON.stringify({favorite:value,password})});if(!response.ok)alert(response.status===403?'管理员密码错误':'收藏更新失败');else{run.favorite=value;render()}}
      function render(){rows.replaceChildren();const visible=runs.filter(matches);document.getElementById('visibleCount').textContent='显示 '+visible.length+' / '+runs.length+' 条近期记录';visible.forEach(run=>{const tr=document.createElement('tr');const choose=add(tr,'td');const cb=document.createElement('input');cb.type='checkbox';cb.name='run';cb.value=run.id;cb.className='compare-box';cb.checked=selected.has(run.id);cb.setAttribute('aria-label','选择 '+run.name);cb.addEventListener('change',()=>{cb.checked?selected.add(run.id):selected.delete(run.id);sessionStorage.setItem(selectionKey,JSON.stringify([...selected]))});choose.appendChild(cb);const favCell=add(tr,'td');const star=add(favCell,'button',run.favorite?'★':'☆','favorite-button'+(run.favorite?' active':''));star.type='button';star.title=run.favorite?'取消收藏':'收藏实验';star.addEventListener('click',()=>setFavorite(run,!run.favorite));const info=add(tr,'td',undefined,'run-cell');const link=add(info,'a',run.display_code||run.name,'run-title');link.href='/runs/'+encodeURIComponent(run.id);link.title=run.name;if(run.display_subtitle)add(info,'span',run.display_subtitle,'run-subtitle');if(run.group_name)add(info,'span',run.group_name,'run-subtitle');const tertiary=add(info,'span',run.git_short?'Git '+run.git_short:'Run '+run.id.slice(0,12),'run-tertiary');tertiary.title=run.name;(run.tags||[]).forEach(tag=>add(info,'span',tag,'tag'));const status=add(tr,'td');add(status,'span',statusLabels[run.status]||run.status,'badge '+run.status);const progress=add(tr,'td',undefined,'progress-cell');const pct=run.total_epochs?Math.min(100,Math.round(100*(run.current_epoch||0)/run.total_epochs)):0;add(progress,'span',(run.current_epoch||0)+' / '+(run.total_epochs||0)+' · '+pct+'%','progress-text');const bar=add(progress,'div',undefined,'bar');add(bar,'div',undefined,'fill').style.width=pct+'%';const result=add(tr,'td',undefined,'result-cell');if(run.best_map50_95!==null&&run.best_map50_95!==undefined){add(result,'span',Number(run.best_map50_95).toFixed(4),'result-main');if(run.baseline_delta_pp!==null&&run.baseline_delta_pp!==undefined){const d=Number(run.baseline_delta_pp);add(result,'span',(d>=0?'↑ +':'↓ ')+d.toFixed(2)+' pp','result-delta '+(d>=0?'delta-up':'delta-down'))}}else add(result,'span','—','muted');const updated=add(tr,'td',undefined,'time-cell');const time=timeLabel(run.updated_at);add(updated,'span',time.short);updated.title=time.full;add(tr,'td',duration(run.elapsed_seconds),'hide-small');rows.appendChild(tr)});if(!visible.length){const tr=document.createElement('tr'),td=add(tr,'td','没有符合筛选条件的实验','empty');td.colSpan=8;rows.appendChild(tr)}}
      [nameFilter,statusFilter,groupFilter,tagFilter,favoriteFilter].forEach(control=>control.addEventListener('input',render));form.addEventListener('submit',e=>{const ids=[...form.querySelectorAll('input[name=run]:checked')].map(x=>x.value);if(ids.length<2||ids.length>5){e.preventDefault();alert('请选择 2–5 条实验记录进行对比。')}else sessionStorage.removeItem(selectionKey)});
      const dot=document.getElementById('liveDot'),liveText=document.getElementById('liveText'),stream=new EventSource('/events/dashboard');stream.onopen=()=>{dot.classList.remove('offline');liveText.textContent='实时连接正常'};stream.onerror=()=>{dot.classList.add('offline');liveText.textContent='实时连接中断，正在重连'};stream.onmessage=event=>{const data=JSON.parse(event.data);runs=data.runs||[];renderSummary(data.summary);refreshFilterOptions();render()};renderSummary(initial.summary);refreshFilterOptions();render();setInterval(render,60000);
    })();
    </script></div>"""
    return page("实验总览", template.replace("__PAYLOAD__", payload_json))


def lineage_html(runs: list[dict]) -> str:
    """Only draw stored baseline links; do not infer ancestry from similar names."""
    by_id = {run["id"]: run for run in runs}
    children: dict[str, list[dict]] = {}
    connected = set()
    for run in runs:
        parent_id = str(run.get("baseline_run_id") or "")
        if not parent_id or parent_id == run["id"]:
            continue
        connected.add(run["id"])
        if parent_id in by_id:
            connected.add(parent_id)
            children.setdefault(parent_id, []).append(run)
    for descendants in children.values():
        descendants.sort(key=lambda run: (str(run.get("name") or ""), run["id"]))
    best_values = STORE.best_map50_95_by_run(set(by_id))

    def node(run: dict, ancestors: frozenset[str] = frozenset()) -> str:
        run_id = run["id"]
        code, detail = experiment_display_name(str(run.get("name") or ""))
        score = best_values.get(run_id)
        score_text = f"Best mAP50-95 {percentage(score)}" if score is not None else "暂无 mAP50-95"
        missing_parent = bool(run.get("baseline_run_id") and run["baseline_run_id"] not in by_id)
        body = (
            f'<div class="lineage-node"><a href="/runs/{quote(run_id)}" title="{html.escape(run["name"])}">'
            f'{html.escape(code)}</a><span class="muted">{html.escape(detail)}</span>'
            f'<span class="muted">{html.escape(score_text)}</span>{status_badge(run["status"])}'
            f'{"<span class=muted>父记录不在当前范围</span>" if missing_parent else ""}</div>'
        )
        if run_id in ancestors:
            return f"<li>{body}<span class=muted>谱系循环，已停止展开</span></li>"
        descendants = children.get(run_id, [])
        branch = "<ul>" + "".join(node(child, ancestors | {run_id}) for child in descendants) + "</ul>" if descendants else ""
        return f"<li>{body}{branch}</li>"

    roots = [run for run in runs if run["id"] in connected and str(run.get("baseline_run_id") or "") not in by_id]
    placed = set()
    def collect(run: dict):
        if run["id"] in placed:
            return
        placed.add(run["id"])
        for child in children.get(run["id"], []):
            collect(child)
    for root in roots:
        collect(root)
    for run in runs:
        if run["id"] in connected and run["id"] not in placed:
            roots.append(run)
            collect(run)
    forest = "".join(f'<ul class="lineage-tree">{node(run)}</ul>' for run in roots)
    if not forest:
        forest = '<p class="muted">目前没有明确绑定的父子实验。可在实验记录里填写基线 Run ID，或让 Agent 根据 YAML 父实验自动绑定。</p>'
    standalone = [run for run in runs if run["id"] not in connected]
    standalone_html = "".join(
        f'<a class="summary-chip" href="/runs/{quote(run["id"])}" title="{html.escape(run["name"])}">'
        f'{html.escape(experiment_display_name(run["name"])[0])}</a>' for run in standalone
    )
    return page("实验谱系", f'''<div class="wrap"><div class="top"><div><a href="/">← 实验总览</a><div class="page-eyebrow">Research lineage</div><div class="brand">实验谱系</div><div class="page-subtitle">仅展示明确的基线关系，不按名称猜测父实验。</div></div><div class="top-actions"><a class="button secondary" href="/queue">队列 / Agent</a></div></div>
    <div class="panel"><div class="section-intro"><h3>父子实验</h3><span class="muted">节点显示各自历史最佳 mAP50-95</span></div><div class="lineage-forest">{forest}</div></div>
    <details class="panel"><summary>未关联实验 · {len(standalone)} 条</summary><div class="lineage-standalone" style="margin-top:15px">{standalone_html or '<span class="muted">暂无</span>'}</div></details></div>''')


def queue_html(jobs: list[dict], agents: list[dict], clone_job: dict | None = None) -> str:
    status_labels = {"queued": "排队中", "leased": "已领取", "running": "运行中", "waiting_memory": "等待显存", "paused": "已暂停", "completed": "已完成", "failed": "失败", "cancelled": "已取消"}
    policy_labels = {
        "notify": "仅通知", "stop": "直接停止",
        "retry_lower_batch": "降低 batch 重试", "retry_when_memory": "等待显存重试",
    }
    cancellable = {"queued", "waiting_memory", "leased", "running"}
    job_rows = []
    for job in jobs:
        status = str(job.get("status") or "")
        status_text = status_labels.get(status, status)
        candidates = ", ".join(map(str, job.get("gpu_candidates") or [])) or "任意"
        actions = [f'<a class="button secondary" href="/queue?clone={quote(job["id"])}#new-job">复制任务</a>']
        if status in {"leased", "running"} and not job.get("pause_requested"):
            actions.append(f'<button class="button secondary queue-action" data-id="{html.escape(job["id"])}" data-action="pause">完成当前 Epoch 后暂停</button>')
        if status in cancellable:
            actions.append(f'<button class="button danger queue-action" data-id="{html.escape(job["id"])}" data-action="cancel">取消任务</button>')
        elif status == "paused":
            checkpoint = str(job.get("last_checkpoint") or job.get("resume_checkpoint") or "")
            if checkpoint:
                actions.append(f'<button class="button queue-action" data-id="{html.escape(job["id"])}" data-action="resume" title="{html.escape(checkpoint)}">从 last.pt 恢复</button>')
            else:
                actions.append('<span class="label">未找到可用于恢复的 last.pt</span>')
        elif status in {"failed", "cancelled"}:
            checkpoint = str(job.get("last_checkpoint") or job.get("resume_checkpoint") or "")
            if checkpoint:
                actions.append(f'<button class="button queue-action" data-id="{html.escape(job["id"])}" data-action="requeue_resume" title="{html.escape(checkpoint)}">从 last.pt 续训</button>')
            else:
                actions.append('<span class="label">未发现 last.pt，只能从头运行</span>')
            actions.append(f'<button class="button secondary queue-action" data-id="{html.escape(job["id"])}" data-action="requeue">从头重新排队</button>')
            run_current_epoch = int(job.get("run_current_epoch") or 0)
            if status == "cancelled" or run_current_epoch < 3:
                actions.append(f'<button class="button danger queue-action" data-id="{html.escape(job["id"])}" data-action="delete">删除队列记录</button>')
            else:
                actions.append('<span class="label">已训练 ≥3 Epoch，不能删除队列记录</span>')
        else:
            actions.append(f'<button class="button secondary queue-action" data-id="{html.escape(job["id"])}" data-action="requeue">重新运行（从头）</button>')
        cancelled_note = '<div class="cancelled-note">不会被 Agent 领取</div>' if status == "cancelled" else ""
        output_dir = str(job.get("output_dir") or "")
        output_binding = (
            f'<div class="label" title="{html.escape(output_dir)}">输出已绑定 · '
            f'{html.escape(Path(output_dir).name or output_dir)}'
            f'{" · last.pt" if job.get("output_last_checkpoint") else ""}</div>'
            if output_dir else '<div class="label">输出目录尚未绑定</div>'
        )
        task_name = html.escape(job["name"])
        if STORE.queue_job_editable(job):
            task_title = f'<a class="queue-task-title queue-task-link" href="/queue/jobs/{quote(job["id"])}" title="查看并修改任务配置">{task_name}</a>'
        else:
            task_title = f'<strong class="queue-task-title">{task_name}</strong>'
        preflight = str(job.get("preflight_status") or "passed")
        preflight_label = {"pending": "预检中", "passed": "预检通过", "failed": "预检失败"}.get(preflight, preflight)
        preflight_details = job.get("preflight") or {}
        checks = preflight_details.get("checks") or []
        check_html = "".join(f'<li class="{"check-ok" if check.get("ok") else "check-bad"}">{html.escape(str(check.get("name") or "检查"))}：{html.escape(str(check.get("detail") or ""))}</li>' for check in checks)
        reason_html = "".join(f'<div class="queue-reason">{html.escape(reason)}</div>' for reason in job.get("queue_reasons") or [])
        suggestion_html = (
            f'<div class="fault-suggestion"><strong>处置建议</strong> {html.escape(job["fault_suggestion"])}</div>'
            if job.get("fault_suggestion") and status in {"failed", "cancelled", "paused", "waiting_memory"}
            else ""
        )
        job_rows.append(
            f'''<tr class="queue-row queue-{html.escape(status)}">
            <td>{task_title}<br><span class="label">{html.escape(job['id'][:12])}</span>{cancelled_note}{output_binding}<div class="queue-actions">{''.join(actions)}</div></td>
            <td><span class="queue-status status-{html.escape(status)}">{html.escape(status_text)}</span></td>
            <td>{job['priority']}</td>
            <td><strong>{job['required_gpu_count']} 张</strong><br><span class="label">候选 {html.escape(candidates)}</span></td>
            <td><span class="preflight-status preflight-{html.escape(preflight)}">{html.escape(preflight_label)}</span>{f'<details><summary>明细</summary><ul class="check-list">{check_html}</ul></details>' if checks else ''}</td>
            <td>{reason_html}{suggestion_html}</td>
            <td>{job['attempt_count']} / {job['retry_limit'] + 1}</td>
            <td>{html.escape(policy_labels.get(job['anomaly_policy'], job['anomaly_policy']))}</td>
            </tr>'''
        )
    job_rows_html = "".join(job_rows) or '<tr><td colspan="8" class="empty">队列为空</td></tr>'

    def number(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    agent_state_labels = {"idle": "Agent 空闲", "running": "正在执行任务"}
    agent_cards = []
    for agent in agents:
        gpu_cards = []
        for gpu in agent.get("gpus", []):
            used = max(0.0, number(gpu.get("memory_used_mb")))
            free = max(0.0, number(gpu.get("memory_free_mb")))
            total = max(0.0, number(gpu.get("memory_total_mb"), used + free))
            utilization = max(0.0, min(100.0, number(gpu.get("utilization_percent"))))
            temperature = number(gpu.get("temperature_c"), -1)
            idle_for = max(0.0, number(gpu.get("idle_for_seconds")))
            memory_percent = max(0.0, min(100.0, 100 * used / total)) if total else 0.0
            if gpu.get("cuda_usable") is False:
                gpu_class, state_class, state_text = "is-busy", "busy", "CUDA 不可用"
            elif gpu.get("telemetry_usable") is False:
                gpu_class, state_class, state_text = "is-busy", "busy", "遥测不可用"
            elif idle_for > 0:
                gpu_class, state_class, state_text = "is-idle", "", f"空闲 {human_duration(idle_for)}"
            elif utilization <= float((agent.get("capabilities") or {}).get("idle_utilization_percent", 5)) and used <= float((agent.get("capabilities") or {}).get("idle_memory_used_mb", 3000)):
                gpu_class, state_class, state_text = "", "pending", "空闲计时中"
            else:
                gpu_class, state_class, state_text = "is-busy", "busy", "使用中"
            name = str(gpu.get("name") or "NVIDIA GPU")
            temperature_text = f"{temperature:.0f} °C" if temperature >= 0 else "—"
            gpu_cards.append(f'''<div class="gpu-card {gpu_class}">
                <div class="gpu-card-top"><div class="gpu-title"><span class="gpu-index">GPU {html.escape(str(gpu.get('index', '—')))}</span><span class="gpu-name" title="{html.escape(name)}">{html.escape(name)}</span></div><span class="gpu-state {state_class}">{state_text}</span></div>
                <div class="gpu-metrics"><div class="gpu-metric"><span>利用率</span><strong>{utilization:.0f}%</strong></div><div class="gpu-metric"><span>温度</span><strong>{temperature_text}</strong></div></div>
                <div class="gpu-memory-caption"><span>显存占用</span><span>{used:,.0f} / {total:,.0f} MiB</span></div>
                <div class="gpu-memory-track"><div class="gpu-memory-fill" style="width:{memory_percent:.1f}%"></div></div>
                <div class="gpu-free">当前可用 <strong>{free:,.0f} MiB</strong></div>
            </div>''')
        if not gpu_cards:
            gpu_cards.append('<div class="empty">未读取到 GPU；请检查 nvidia-smi 和 Agent 日志。</div>')
        state = str(agent.get("state") or "idle")
        current_job = str(agent.get("current_job_id") or "")
        current_job_html = f'<span class="label">当前任务 {html.escape(current_job[:12])}</span>' if current_job else ""
        agent_cards.append(f'''<section class="agent-card"><div class="agent-heading"><div><h4>{html.escape(str(agent.get('hostname') or agent.get('id') or '未知主机'))}</h4><div class="agent-meta"><span class="label">Worker Slot · {html.escape(str(agent.get('id') or ''))}</span>{current_job_html}</div></div><div><span class="agent-state {'running' if state == 'running' else ''}">{html.escape(agent_state_labels.get(state, state))}</span><div class="label" style="margin-top:5px">心跳 {html.escape(str(agent.get('updated_at') or '—'))}</div></div></div><div class="gpu-grid">{''.join(gpu_cards)}</div></section>''')

    queue_counts = {
        "active": sum(1 for job in jobs if job.get("status") in cancellable),
        "completed": sum(1 for job in jobs if job.get("status") == "completed"),
        "cancelled": sum(1 for job in jobs if job.get("status") == "cancelled"),
        "failed": sum(1 for job in jobs if job.get("status") == "failed"),
    }
    catalogs = []
    seen_catalogs = set()
    for agent in agents:
        for item in agent.get("catalog") or []:
            if isinstance(item, dict):
                hostname = str(agent.get("hostname") or agent.get("id") or "")
                script_key = str(item.get("id") or item.get("path") or "")
                dedupe_key = (hostname, script_key)
                if not script_key or dedupe_key in seen_catalogs:
                    continue
                seen_catalogs.add(dedupe_key)
                catalogs.append({**item, "agent_id": agent.get("id"), "hostname": hostname})
    catalog_options = "".join(f'<option value="{html.escape(str(item.get("id") or ""))}">{html.escape(str(item.get("hostname") or "Agent"))} · {html.escape(str(item.get("name") or item.get("path") or "脚本"))}</option>' for item in catalogs)
    initial = clone_job or {}
    initial["command"] = initial.get("command_text", "")
    initial["source_job_id"] = initial.get("id", "") if clone_job else ""
    host_count = len({
        str(agent.get("hostname") or agent.get("id") or "").strip().casefold()
        for agent in agents if agent.get("hostname") or agent.get("id")
    })
    template = '''<div class="wrap"><div class="top"><div><a href="/">← 全部实验</a><div class="brand">实验队列与训练 Agent</div><div class="muted">页面已启用登录保护；高风险操作仍需再次输入管理员密码。</div></div><div class="top-actions"><a class="button secondary" href="/downloads/yolo-queue-agent.zip">下载 Linux Agent</a><a class="button secondary" href="/logout">退出登录</a></div></div>
    <div id="new-job" class="panel"><div class="panel-heading"><div><h3>__FORM_TITLE__</h3><div class="muted">选择 Agent 扫描到的训练脚本可自动填表；提交后先做无 GPU 预检，通过后才会领取。</div></div><span class="summary-chip">发现脚本 <strong>__CATALOG_COUNT__</strong></span></div>
    <form id="queueForm" class="grid queue-edit-form"><label class="form-wide">训练脚本选择器<select id="scriptSelector" class="filter-input"><option value="">手动填写 / 选择脚本</option>__CATALOG_OPTIONS__</select></label>
    <div id="scriptInfo" class="script-info form-wide muted">Agent 上线并完成扫描后，这里会显示脚本内置 checkpoint、数据配置和默认 GPU。</div>
    <div id="lineageInfo" class="script-info form-wide" hidden></div>
    <input name="script_id" type="hidden"><input name="script_path" type="hidden"><input name="source_job_id" type="hidden">
    <label>任务名称<input name="name" class="filter-input" required></label><label>工作目录<input name="working_directory" class="filter-input" required placeholder="/path/to/project"></label>
    <label class="form-wide">启动命令<input name="command" class="filter-input command-preview" required placeholder="/path/to/python /path/to/train_script.py"></label>
    <label>优先级<input name="priority" type="number" class="filter-input" value="0"></label><label>同时空闲 GPU 数<input name="required_gpu_count" type="number" min="1" max="16" class="filter-input" value="1"></label>
    <label>候选 GPU<input name="gpu_candidates" class="filter-input" placeholder="4,5；留空表示任意"></label><label>GPU 空闲阈值（秒）<input name="idle_seconds" type="number" min="0" class="filter-input" value="60"></label>
    <label>单卡最低空闲显存 MiB<input name="min_free_memory_mb" type="number" min="0" class="filter-input" value="2048"></label><label>最高 GPU 利用率 %<input name="max_gpu_utilization" type="number" min="0" max="100" step="0.1" class="filter-input" value="10"></label>
    <label>失败重试次数<input name="retry_limit" type="number" min="0" max="20" class="filter-input" value="0"></label><label>重试等待（秒）<input name="retry_delay_seconds" type="number" min="5" class="filter-input" value="60"></label>
    <label>异常策略<select id="queuePolicy" name="anomaly_policy" class="filter-input"><option value="notify">仅通知</option><option value="stop">直接停止</option><option value="retry_lower_batch">降低 batch 后重试</option><option value="retry_when_memory">等待显存重试</option></select></label><label><span>初始 batch <span id="queueBatchHint" class="label">可留空</span></span><input id="queueBatch" name="batch_size" type="number" min="1" class="filter-input" placeholder="可留空"></label>
    <label>最小 batch<input name="min_batch_size" type="number" min="1" class="filter-input" value="1"></label><label>断点 checkpoint<input name="resume_checkpoint" class="filter-input" placeholder="仅续训时填写 last.pt；脚本预训练权重不用填"></label>
    <label>基线 Run ID<input name="baseline_run_id" class="filter-input" placeholder="选择脚本后自动匹配；仍可手动修改"></label><label>管理员密码<input name="password" type="password" class="filter-input" required></label>
    <label class="form-wide">任务备注<textarea name="notes" class="filter-input"></textarea></label>
    <div class="queue-mode-row form-wide"><label>任务创建方式<select id="queueMode" class="filter-input"><option value="single">单个队列任务</option><option value="sweep">批量消融 / 参数扫描</option></select></label><span class="muted">选择批量模式后，参数组合会在下方展开。</span></div>
    <div id="sweepFields" class="sweep-box form-wide" hidden><div class="panel-heading"><div><strong>批量消融 / 参数扫描</strong><div class="muted">选择多个脚本和 batch，自动生成实验组合。</div></div><span class="summary-chip">最多 50 条</span></div><div class="grid"><label>batch 组合<input id="sweepBatches" class="filter-input" placeholder="64,32,16"></label><label class="form-wide">脚本组合（Ctrl / Shift 多选）<select id="sweepScripts" class="filter-input" multiple size="5">__CATALOG_OPTIONS__</select></label><div class="muted form-wide">将按“脚本 × batch”生成任务；模型/配置组合请用不同训练脚本表示。</div></div></div>
    <div class="form-wide controls"><button class="button" type="submit">预检并加入队列</button><span id="queueMessage" class="muted"></span></div></form></div>
    <details class="panel agent-disclosure"><summary class="agent-summary"><div class="agent-summary-copy"><div class="agent-summary-title"><h3>训练 Agent</h3><span class="agent-toggle-text" aria-hidden="true"></span></div><div class="muted">一台物理主机可运行多个 Worker Slot；点击查看各显卡实时占用。</div></div><div class="agent-summary-side"><span class="summary-chip">在线主机 <strong>__HOST_COUNT__</strong> · Worker Slot <strong>__SLOT_COUNT__</strong></span><span class="agent-chevron" aria-hidden="true"></span></div></summary><div class="agent-disclosure-body"><div class="agent-list">__AGENT_CARDS__</div></div></details>
    <div class="panel"><div class="panel-heading"><div><h3>队列</h3><div class="muted">等待原因会精确列出缺卡、显存、利用率和剩余空闲秒数。</div></div><div class="queue-summary">__QUEUE_SUMMARY__</div></div><div class="table-scroll"><table class="queue-table"><thead><tr><th>任务 / 操作</th><th>状态</th><th>优先级</th><th>GPU</th><th>预检</th><th>未启动原因 / 建议</th><th>尝试</th><th>异常策略</th></tr></thead><tbody>__JOB_ROWS__</tbody></table></div></div>
    <script>(()=>{const catalogs=__CATALOG_JSON__,initial=__INITIAL_JSON__,form=document.getElementById('queueForm'),selector=document.getElementById('scriptSelector'),message=document.getElementById('queueMessage'),policy=document.getElementById('queuePolicy'),batch=document.getElementById('queueBatch'),batchHint=document.getElementById('queueBatchHint'),queueMode=document.getElementById('queueMode'),sweepFields=document.getElementById('sweepFields'),sweepScripts=document.getElementById('sweepScripts'),lineageInfo=document.getElementById('lineageInfo');const field=n=>form.elements.namedItem(n),isSweep=()=>queueMode.value==='sweep';function showLineage(item){if(!item||!item.father_yaml_name){lineageInfo.hidden=true;lineageInfo.replaceChildren();return}lineageInfo.hidden=false;lineageInfo.replaceChildren();const title=document.createElement('strong');title.textContent='YAML 继承关系';lineageInfo.append(title,document.createElement('br'),document.createTextNode((item.model_yaml_name||'当前 YAML')+' → '+item.father_yaml_name),document.createElement('br'));const status=document.createElement('span');status.className='label';status.textContent=item.lineage_status==='matched'?'已精确匹配父 Run：'+item.father_run_name+'（'+item.father_run_id+'）':item.lineage_status==='run_missing'?'已识别父脚本，但监控库中暂未找到同名父 Run':item.lineage_status==='parent_script_missing'?'已识别父 YAML，但 Agent 目录中暂未找到对应父脚本':item.lineage_status==='reference_only'?'YAML 注释为“对照”，仅展示且不自动绑定':'父实验关系待解析';lineageInfo.append(status);if(item.change_summary){lineageInfo.append(document.createElement('br'),document.createTextNode('修改内容：'+item.change_summary))}}function applyScript(item,preserveName=false){if(!item)return;field('script_id').value=item.id||'';field('script_path').value=item.path||'';field('working_directory').value=item.working_directory||'';field('command').value=item.command||'';if(!preserveName)field('name').value=item.task_name||item.name||'';if(!preserveName||!field('baseline_run_id').value)field('baseline_run_id').value=item.father_run_id||'';if(item.default_batch)batch.value=item.default_batch;if(item.default_gpu_count)field('required_gpu_count').value=item.default_gpu_count;document.getElementById('scriptInfo').textContent='Python：'+(item.python||'—')+'　内置 checkpoint：'+(item.checkpoint||'未识别')+'　数据：'+((item.data_files||[]).join(', ')||'未识别')+'　默认 GPU：'+(item.default_device||'未识别');showLineage(item)}selector.addEventListener('change',()=>applyScript(catalogs.find(x=>x.id===selector.value)));function setInitial(){for(const [key,value] of Object.entries(initial)){const target=field(key);if(target&&value!==null&&value!==undefined){target.value=Array.isArray(value)?value.join(','):value}}if(initial.script_id){selector.value=initial.script_id;applyScript(catalogs.find(x=>x.id===initial.script_id),true)}if(initial.anomaly_policy)policy.value=initial.anomaly_policy}function syncBatchRequirement(){const required=policy.value==='retry_lower_batch';batch.required=required&&!isSweep();batch.placeholder=required?'必填，例如 64':'可留空';batchHint.textContent=required?'必填':'可留空'}function syncQueueMode(){sweepFields.hidden=!isSweep();syncBatchRequirement()}policy.addEventListener('change',syncBatchRequirement);queueMode.addEventListener('change',syncQueueMode);setInitial();syncQueueMode();form.addEventListener('submit',async e=>{e.preventDefault();syncBatchRequirement();if(!form.reportValidity())return;const data=Object.fromEntries(new FormData(form).entries());if(isSweep()){const batches=document.getElementById('sweepBatches').value.replace(/，/g,',').split(',').map(x=>Number(x.trim())).filter(x=>Number.isInteger(x)&&x>0);const scripts=Array.from(sweepScripts.selectedOptions).map(option=>catalogs.find(x=>x.id===option.value)).filter(Boolean);if(!batches.length||!scripts.length){message.textContent='批量扫描必须选择至少一个脚本并填写 batch 组合';return}data.sweep_jobs=[];for(const script of scripts)for(const value of batches)data.sweep_jobs.push({name:(script.task_name||script.name)+'-b'+value,working_directory:script.working_directory,command:script.command,script_id:script.id,script_path:script.path,batch_size:value,required_gpu_count:script.default_gpu_count||data.required_gpu_count,baseline_run_id:script.father_run_id||''});if(data.sweep_jobs.length>50){message.textContent='组合超过 50 条，请缩小范围';return}}message.textContent='正在创建并等待 Agent 预检…';const response=await fetch('/api/v1/web/queue',{method:'POST',headers:{'Content-Type':'application/json','X-Monitor-Request':'dashboard'},body:JSON.stringify(data)});let result={};try{result=await response.json()}catch{}message.textContent=response.ok?'已创建，预检结果将在队列表显示':(response.status===403?'管理员密码错误':result.error||'提交失败');if(response.ok)setTimeout(()=>location.href='/queue',700)});document.querySelectorAll('.queue-action').forEach(button=>button.addEventListener('click',async()=>{const action=button.dataset.action,labels={pause:'完成当前 Epoch 后暂停',resume:'从 checkpoint 恢复',cancel:'取消任务',requeue:'重新排队',requeue_resume:'从 checkpoint 续训',delete:'删除队列记录'};if(action==='delete'&&!confirm('仅删除这条符合条件的队列记录。实验结果、checkpoint 和训练文件不会被删除。确定继续吗？'))return;const password=prompt('请输入管理员密码以'+(labels[action]||'执行操作')+'：');if(password===null)return;const suffix=action==='delete'?'/delete':'/action';const body=action==='delete'?{password}:{password,action};const response=await fetch('/api/v1/web/queue/'+encodeURIComponent(button.dataset.id)+suffix,{method:'POST',headers:{'Content-Type':'application/json','X-Monitor-Request':'dashboard'},body:JSON.stringify(body)});if(response.ok)location.reload();else{let result={};try{result=await response.json()}catch{}alert(response.status===403?'管理员密码错误':result.error||'操作失败')}}))})();</script></div>'''
    summary = f'<span class="summary-chip">待处理 <strong>{queue_counts["active"]}</strong></span><span class="summary-chip">已完成 <strong>{queue_counts["completed"]}</strong></span><span class="summary-chip">失败 <strong>{queue_counts["failed"]}</strong></span><span class="summary-chip cancelled">已取消 <strong>{queue_counts["cancelled"]}</strong></span>'
    return page("实验队列", template.replace("__FORM_TITLE__", "复制队列任务" if clone_job else "新增队列任务").replace("__CATALOG_COUNT__", str(len(catalogs))).replace("__CATALOG_OPTIONS__", catalog_options).replace("__HOST_COUNT__", str(host_count)).replace("__SLOT_COUNT__", str(len(agents))).replace("__AGENT_CARDS__", ''.join(agent_cards) or '<div class="empty">还没有 Agent 上线</div>').replace("__QUEUE_SUMMARY__", summary).replace("__JOB_ROWS__", job_rows_html).replace("__CATALOG_JSON__", json.dumps(catalogs, ensure_ascii=False).replace("</", "<\\/")).replace("__INITIAL_JSON__", json.dumps(initial, ensure_ascii=False).replace("</", "<\\/")))


def queue_job_html(job: dict) -> str:
    status_labels = {"queued": "排队中", "waiting_memory": "等待显存"}
    policy_options = [
        ("notify", "仅通知"),
        ("stop", "直接停止"),
        ("retry_lower_batch", "降低 batch 后重试"),
        ("retry_when_memory", "等待显存后重试"),
    ]
    policy_html = "".join(
        f'<option value="{value}"{" selected" if job.get("anomaly_policy") == value else ""}>{label}</option>'
        for value, label in policy_options
    )
    candidates = ",".join(map(str, job.get("gpu_candidates") or []))
    batch_size = "" if job.get("batch_size") is None else str(job["batch_size"])
    job_id = str(job["id"])
    output_dir = html.escape(str(job.get("output_dir") or "尚未绑定"))
    output_last = html.escape(str(job.get("output_last_checkpoint") or "尚未生成"))
    output_best = html.escape(str(job.get("output_best_checkpoint") or "尚未生成"))
    output_csv = html.escape(str(job.get("output_results_csv") or "尚未生成"))
    return page("修改队列任务", f'''<div class="wrap">
    <div class="top"><div><a href="/queue">← 返回实验队列</a><div class="brand">{html.escape(job['name'])}</div><div class="muted">任务 ID：{html.escape(job_id)}</div></div><span class="queue-status status-{html.escape(job['status'])}">{html.escape(status_labels.get(job['status'], job['status']))}</span></div>
    <div class="edit-notice"><strong>该任务尚未开始，可以安全修改。</strong> 保存时会再次检查任务状态；如果 Agent 已经领取，服务器将拒绝修改。</div>
    <div class="panel"><div class="grid"><div class="stat"><div class="label">创建时间</div><div class="value" style="font-size:15px">{html.escape(job.get('created_at') or '—')}</div></div><div class="stat"><div class="label">最后修改</div><div class="value" style="font-size:15px">{html.escape(job.get('updated_at') or '—')}</div></div><div class="stat"><div class="label">尝试次数</div><div class="value">{job.get('attempt_count') or 0}</div></div><div class="stat"><div class="label">Run ID</div><div class="value" style="font-size:15px">{html.escape(job.get('run_id') or '—')}</div></div></div></div>
    <div class="panel"><div class="panel-heading"><div><h3>实验输出绑定</h3><div class="muted">绑定后暂停、失败和续训只使用该目录，不再扫描其他实验。</div></div></div>
      <div class="grid"><div class="stat"><div class="label">save_dir</div><div class="value" style="font-size:13px">{output_dir}</div></div><div class="stat"><div class="label">last.pt</div><div class="value" style="font-size:13px">{output_last}</div></div><div class="stat"><div class="label">best.pt</div><div class="value" style="font-size:13px">{output_best}</div></div><div class="stat"><div class="label">results.csv</div><div class="value" style="font-size:13px">{output_csv}</div></div></div></div>
    <div class="panel"><div class="panel-heading"><div><h3>任务配置</h3><div class="muted">这里显示的是加入队列时保存的完整配置。</div></div></div>
    <form id="queueEditForm" class="grid queue-edit-form">
      <label>任务名称<input name="name" class="filter-input" maxlength="200" required value="{html.escape(job['name'])}"></label>
      <label>工作目录<input name="working_directory" class="filter-input" maxlength="1000" required value="{html.escape(job['working_directory'])}"></label>
      <label class="form-wide">启动命令<textarea name="command" class="filter-input command-preview" maxlength="8000" required>{html.escape(job['command_text'])}</textarea></label>
      <label>优先级<input name="priority" type="number" min="-1000" max="1000" class="filter-input" value="{job['priority']}"></label>
      <label>同时空闲 GPU 数<input name="required_gpu_count" type="number" min="1" max="16" class="filter-input" value="{job['required_gpu_count']}"></label>
      <label>候选 GPU<input name="gpu_candidates" class="filter-input" value="{html.escape(candidates)}" placeholder="4,5；留空表示任意"></label>
      <label>GPU 空闲阈值（秒）<input name="idle_seconds" type="number" min="0" class="filter-input" value="{job['idle_seconds']}"></label>
      <label>单卡最低空闲显存 MiB<input name="min_free_memory_mb" type="number" min="0" class="filter-input" value="{job['min_free_memory_mb']}"></label>
      <label>最高 GPU 利用率 %<input name="max_gpu_utilization" type="number" min="0" max="100" step="0.1" class="filter-input" value="{job['max_gpu_utilization']}"></label>
      <label>失败重试次数<input name="retry_limit" type="number" min="0" max="20" class="filter-input" value="{job['retry_limit']}"></label>
      <label>重试等待（秒）<input name="retry_delay_seconds" type="number" min="5" class="filter-input" value="{job['retry_delay_seconds']}"></label>
      <label>异常策略<select id="editQueuePolicy" name="anomaly_policy" class="filter-input">{policy_html}</select></label>
      <label><span>初始 batch <span id="editQueueBatchHint" class="label">可留空</span></span><input id="editQueueBatch" name="batch_size" type="number" min="1" class="filter-input" value="{html.escape(batch_size)}"></label>
      <label>最小 batch<input name="min_batch_size" type="number" min="1" class="filter-input" value="{job['min_batch_size']}"></label>
      <label class="form-wide">断点 checkpoint<input name="resume_checkpoint" class="filter-input" maxlength="1000" value="{html.escape(job.get('resume_checkpoint') or '')}" placeholder="可选 last.pt 绝对路径"></label>
      <label>基线 Run ID<input name="baseline_run_id" class="filter-input" maxlength="80" value="{html.escape(job.get('baseline_run_id') or '')}"></label>
      <label>管理员密码<input name="password" type="password" class="filter-input" autocomplete="current-password" required></label>
      <label class="form-wide">任务备注<textarea name="notes" class="filter-input" maxlength="8000">{html.escape(job.get('notes') or '')}</textarea></label>
      <div class="form-wide controls"><button class="button" type="submit">保存修改</button><a class="button secondary" href="/queue">放弃并返回</a><span id="editMessage" class="muted"></span></div>
    </form></div>
    <script>(()=>{{const form=document.getElementById('queueEditForm'),message=document.getElementById('editMessage'),jobId={json.dumps(job_id)},policy=document.getElementById('editQueuePolicy'),batch=document.getElementById('editQueueBatch'),batchHint=document.getElementById('editQueueBatchHint');function syncBatchRequirement(){{const required=policy.value==='retry_lower_batch';batch.required=required;batch.placeholder=required?'必填，例如 64':'可留空';batchHint.textContent=required?'必填':'可留空';batchHint.style.color=required?'var(--amber)':''}}policy.addEventListener('change',syncBatchRequirement);syncBatchRequirement();form.addEventListener('submit',async event=>{{event.preventDefault();syncBatchRequirement();if(!form.reportValidity())return;message.textContent='保存中…';const data=Object.fromEntries(new FormData(form).entries());const response=await fetch('/api/v1/web/queue/'+encodeURIComponent(jobId)+'/update',{{method:'POST',headers:{{'Content-Type':'application/json','X-Monitor-Request':'dashboard'}},body:JSON.stringify(data)}});if(response.ok){{message.textContent='已保存';form.querySelector('[name=password]').value='';setTimeout(()=>location.href='/queue',600);return}}message.textContent=response.status===403?'管理员密码错误':response.status===409?'任务已经开始，不能再修改':response.status===400&&policy.value==='retry_lower_batch'?'降低 batch 后重试必须填写初始 batch':'保存失败'}})}})();</script>
    </div>''')


def run_html(run: dict, events: list[dict]) -> str:
    total = run["total_epochs"] or 0
    pct = min(100, 100 * run["current_epoch"] / total) if total else 0
    latest_metrics = latest_metrics_for(run, events)
    log_text = html.escape(run.get("log_tail") or "训练端尚未上报日志。更新训练端监控文件后，日志尾部会约每 15 秒刷新。")
    chart = trend_panel([{"id": run["id"], "name": run["name"], "events": events}])
    best = best_epoch_info(events)
    summary = report_summary(run, events)
    source_job = STORE.get_queue_job_by_run(run["id"], False)
    clone_button = f'<a class="button secondary" href="/queue?clone={quote(source_job["id"])}#new-job">基于此实验新建</a>' if source_job else ""
    artifacts = run.get("result") or {}
    artifact_panel = ""
    if artifacts.get("save_dir"):
        artifact_panel = f'''<div class="panel"><h3>实验输出绑定</h3><p class="muted">该 Run 的暂停、失败恢复和续训只会使用这里的 last.pt。</p>
        <div class="grid"><div class="stat"><div class="label">save_dir</div><div class="value" style="font-size:13px">{html.escape(str(artifacts.get("save_dir") or "—"))}</div></div>
        <div class="stat"><div class="label">last.pt</div><div class="value" style="font-size:13px">{html.escape(str(artifacts.get("last_model") or "—"))}</div></div>
        <div class="stat"><div class="label">best.pt</div><div class="value" style="font-size:13px">{html.escape(str(artifacts.get("best_model") or "—"))}</div></div>
        <div class="stat"><div class="label">results.csv</div><div class="value" style="font-size:13px">{html.escape(str(artifacts.get("results_csv") or "—"))}</div></div></div></div>'''
    lineage = STORE.lineage_for_run(run)
    lineage_panel = ""
    if lineage:
        status = str(lineage.get("lineage_status") or "none")
        status_text = {
            "matched": f'已精确匹配父 Run：{lineage.get("father_run_name") or ""}（{lineage.get("father_run_id") or ""}）',
            "run_missing": "已识别父脚本，但监控库中暂未找到实验名完全一致的父 Run；未进行模糊绑定。",
            "parent_script_missing": "已识别父 YAML，但 Agent 目录中暂未找到对应父脚本。",
            "parent_name_missing": "父脚本存在，但未能读取其实验名。",
            "reference_only": "YAML 注释为“对照”而非“父实验”，仅展示且不自动绑定。",
        }.get(status, "尚未识别到可自动绑定的父 Run。")
        change_summary = str(lineage.get("change_summary") or "")
        lineage_panel = f'''<div class="panel"><h3>YAML 继承关系</h3>
        <div><code>{html.escape(str(lineage.get("model_yaml_name") or "当前 YAML"))}</code> → <code>{html.escape(str(lineage.get("father_yaml_name") or "未识别"))}</code></div>
        <p class="muted">{html.escape(status_text)}</p>
        {f'<div class="script-info"><strong>从 YAML 注释提取的修改内容</strong><br>{html.escape(change_summary).replace(chr(10), "<br>")}</div>' if change_summary else ''}</div>'''
    ai_status = str(run.get("ai_status") or "not_requested")
    ai_status_text = {
        "not_requested": "尚未生成", "running": "生成中", "completed": "已生成", "failed": "生成失败",
    }.get(ai_status, ai_status)
    ai_models = "、".join(run.get("ai_models") or []) or "—"
    ai_report = str(run.get("ai_report") or "")
    ai_email_status = str(run.get("ai_email_status") or "not_requested")
    ai_email_status_text = {
        "not_requested": "未要求发送",
        "pending": "等待报告生成",
        "sent": "已发送",
        "skipped": "未发送",
        "failed": "发送失败",
    }.get(ai_email_status, ai_email_status)
    ai_message = str(run.get("ai_error") or run.get("ai_email_error") or "")
    ai_panel = f'''<div class="panel"><div class="panel-heading"><div><h3>AI 实验分析</h3>
    <div class="muted">状态：<strong id="aiStatusValue">{html.escape(ai_status_text)}</strong>　模型：<span id="aiModelsValue">{html.escape(ai_models)}</span>
    <span id="aiRequestedAt">{f"　提交时间：{html.escape(str(run.get('ai_requested_at') or ''))}" if run.get("ai_requested_at") else ""}</span>
    <span id="aiGeneratedAt">{f"　生成时间：{html.escape(str(run.get('ai_generated_at') or ''))}" if run.get("ai_generated_at") else ""}</span></div>
    <div id="aiEmailStatus" class="muted">邮件：{html.escape(ai_email_status_text)}
    {f"（{html.escape(str(run.get('ai_email_sent_at') or ''))}）" if run.get("ai_email_sent_at") else ""}</div></div>
    <form id="aiGenerateForm" class="controls"><a class="button secondary" href="/ai">配置模型</a>
    <input id="aiPassword" class="filter-input" style="width:180px" type="password" autocomplete="current-password" placeholder="管理员密码" required>
    <button id="generateAi" class="button" type="submit" {"disabled" if ai_status == "running" else ""}>{"正在生成…" if ai_status == "running" else "生成结论并发送邮件"}</button></form></div>
    <div id="aiMessage" class="muted">{html.escape(ai_message)}</div>
    <pre id="aiReport" class="log-tail" style="max-height:none;display:{'block' if ai_report else 'none'}">{html.escape(ai_report)}</pre>
    <div id="aiEmpty" class="empty" style="display:{'none' if ai_report else 'block'}">配置模型后可自动或手动生成详细结论。</div></div>'''
    metadata_fields = ("hypothesis", "change_notes", "result_notes", "conclusion", "next_step", "baseline_run_id")
    metadata_complete = sum(1 for field in metadata_fields if str(run.get(field) or "").strip())
    display_code, display_subtitle = experiment_display_name(run["name"])
    best_map, _ = best_core_metric(events, "mAP50-95")
    environment = environment_brief(run.get("parameters") or {})
    return page(run["name"], f"""<div class="wrap"><div class="top"><div><a href="/">← 实验总览</a><div class="page-eyebrow">Experiment detail</div><div class="brand" title="{html.escape(run['name'])}">{html.escape(display_code)}</div><div class="page-subtitle">{html.escape(display_subtitle or run['name'])}</div><div class="muted"><span id="liveDot" class="live-dot"></span><span id="liveText">实时连接中</span></div></div><div class="top-actions">{clone_button}<a class="button secondary" href="/lineage">实验谱系</a><div id="statusBadge">{status_badge(run['status'])}</div><a class="button secondary" href="/logout">退出登录</a></div></div>
    <div class="panel detail-hero"><div class="detail-kpis"><div class="detail-kpi"><div class="label">Epoch</div><div id="epochValue" class="value">{run['current_epoch']} / {total}</div></div><div class="detail-kpi"><div class="label">Best mAP50-95</div><div id="bestMapValue" class="value">{percentage(best_map)}</div></div><div class="detail-kpi"><div class="label">vs Baseline</div><div id="baselineDeltaValue" class="value">{baseline_delta_html(run, best_map)}</div></div><div class="detail-kpi"><div class="label">ETA</div><div id="etaValue" class="value">{human_duration(run.get('eta_seconds'))}</div></div></div><div class="bar"><div id="progressFill" class="fill" style="width:{pct:.1f}%"></div></div><div class="detail-meta"><span>Batch <strong id="batchValue">{run.get('current_batch') or '—'} / {run.get('total_batches') or '—'}</strong></span><span>已运行 <strong id="elapsedValue">{human_duration(run.get('elapsed_seconds'))}</strong></span><span id="environmentBrief">{html.escape(environment)}</span></div></div>
    <div id="dynamicError" class="panel" style="display:{'block' if run.get('error_message') else 'none'}"><h3>错误信息</h3><pre id="dynamicErrorText">{html.escape(run.get('error_message') or '')}</pre></div>
    <nav class="tab-list" role="tablist" aria-label="实验详情"><button class="tab-button" type="button" role="tab" data-tab="overview" aria-selected="true">总览</button><button class="tab-button" type="button" role="tab" data-tab="metrics" aria-selected="false">指标曲线</button><button class="tab-button" type="button" role="tab" data-tab="resources" aria-selected="false">资源监控</button><button class="tab-button" type="button" role="tab" data-tab="logs" aria-selected="false">日志</button><button class="tab-button" type="button" role="tab" data-tab="environment" aria-selected="false">配置环境</button><button class="tab-button" type="button" role="tab" data-tab="artifacts" aria-selected="false">产物</button><button class="tab-button" type="button" role="tab" data-tab="notes" aria-selected="false">实验记录</button></nav>
    <section class="tab-panel" id="tab-overview" role="tabpanel"><div class="panel"><div class="metric-section"><h3>Performance</h3><div id="performanceMetrics">{performance_html(events, latest_metrics)}</div></div><div class="metric-section"><h3>Loss</h3><div id="lossMetrics">{losses_html(latest_metrics)}</div></div><div class="muted">最佳值来自各指标的历史 Epoch；上方大数字为最新/最终值。完整曲线和所有原始指标见「指标曲线」。</div></div></section>
    <section class="tab-panel" id="tab-metrics" role="tabpanel" hidden>{chart}<div class="panel"><h3>最佳 Epoch</h3><div id="bestEpoch">{best_epoch_html(best)}</div><details><summary>查看全部最新/最终原始指标</summary><div id="latestMetrics">{metric_chips(latest_metrics)}</div></details></div></section>
    <section class="tab-panel" id="tab-resources" role="tabpanel" hidden><div class="panel"><h3>GPU / 主机状态</h3><div id="hostStatus">{host_status_html(run.get('host_status') or {})}</div></div></section>
    <section class="tab-panel" id="tab-logs" role="tabpanel" hidden><div class="panel"><h3>实时日志尾部</h3><div class="muted">SSE 实时更新；保留最近约 12,000 个字符。</div><pre id="logTail" class="log-tail">{log_text}</pre></div></section>
    <section class="tab-panel" id="tab-environment" role="tabpanel" hidden><div class="panel"><details><summary><strong>可复现信息</strong> · {html.escape(environment)}</summary><div id="reproducibility">{reproducibility_html(run.get('parameters') or {})}</div></details></div>{lineage_panel}<div class="panel"><h3>相对基线的模型配置差异</h3><pre>{html.escape(summary.get('config_diff') or '设置基线 Run ID 且新旧实验都上传模型 YAML 后，将自动显示逐行差异。')}</pre></div></section>
    <section class="tab-panel" id="tab-artifacts" role="tabpanel" hidden>{artifact_panel or '<div class="panel"><p class="muted">实验尚未上报输出目录。</p></div>'}<div class="panel"><h3>结果信息</h3><div id="resultMetrics">{metric_chips(run['result'])}</div></div></section>
    <section class="tab-panel" id="tab-notes" role="tabpanel" hidden><div class="panel"><div class="panel-heading"><h3>实验记录与标签</h3><span class="summary-chip">核心记录完整度 <strong>{metadata_complete} / {len(metadata_fields)}</strong></span></div><form id="metadataForm" class="notes-grid"><label><span class="label">分组</span><input id="groupName" class="filter-input" maxlength="80" value="{html.escape(run.get('group_name') or '')}" placeholder="例如 DroneVehicle"></label><label><span class="label">标签（逗号分隔）</span><input id="tagsInput" class="filter-input" value="{html.escape(', '.join(run.get('tags') or []))}" placeholder="PaperLAF, FP32Safe, baseline"></label><label class="controls"><input id="favoriteInput" type="checkbox" {'checked' if run.get('favorite') else ''}>收藏</label><label><span class="label">Hypothesis · 实验假设</span><textarea id="hypothesis" class="filter-input" placeholder="为什么做这个实验？预期哪些指标会改善？">{html.escape(run.get('hypothesis') or '')}</textarea></label><label><span class="label">Modification · 修改内容</span><textarea id="changeNotes" class="filter-input" placeholder="与父实验相比只改了什么？">{html.escape(run.get('change_notes') or '')}</textarea></label><label><span class="label">Conclusion · 结论</span><textarea id="conclusion" class="filter-input" placeholder="训练结束后填写结论">{html.escape(run.get('conclusion') or '')}</textarea></label><label><span class="label">结果记录</span><textarea id="resultNotes" class="filter-input">{html.escape(run.get('result_notes') or '')}</textarea></label><label><span class="label">下一步</span><textarea id="nextStep" class="filter-input">{html.escape(run.get('next_step') or '')}</textarea></label><label><span class="label">基线 Run ID</span><input id="baselineRunId" class="filter-input" maxlength="80" value="{html.escape(run.get('baseline_run_id') or '')}" placeholder="仅对显式父实验自动绑定"></label><label><span class="label">管理员密码</span><input id="metadataPassword" class="filter-input" type="password" autocomplete="current-password" required></label><div class="controls wide"><button class="button" type="submit">保存实验记录</button><span id="metadataMessage" class="muted">系统只补空字段，不覆盖人工内容。</span></div></form></div>{ai_panel}
    <details class="panel danger-zone"><summary>危险区域 · 删除实验记录</summary><div><p class="muted">删除将永久移除实验及全部指标。需要输入下面显示的实验编号和管理员密码。</p><form id="deleteRunForm" method="post" action="/runs/{quote(run['id'])}/delete"><label>输入 <strong>{html.escape(display_code)}</strong> 确认<br><input class="confirm-name" name="confirm_name" autocomplete="off" required></label><label>管理员密码<br><input type="password" name="password" autocomplete="current-password" required></label><button class="button danger" type="submit">永久删除</button></form></div></details></section>
    <script>
    (()=>{{const runId={json.dumps(run['id'])},duration=value=>{{if(value===null||value===undefined)return '—';let s=Math.max(0,Math.floor(Number(value)||0)),h=Math.floor(s/3600);s%=3600;const m=Math.floor(s/60);s%=60;return h?(h+'小时 '+m+'分'):(m?(m+'分 '+s+'秒'):(s+'秒'))}};
    const aiLabels={{not_requested:'尚未生成',running:'生成中',completed:'已生成',failed:'生成失败'}},emailLabels={{not_requested:'未要求发送',pending:'等待报告生成',sent:'已发送',skipped:'未发送',failed:'发送失败'}};
    const tabs=[...document.querySelectorAll('.tab-button')];function activateTab(name){{if(!tabs.some(tab=>tab.dataset.tab===name))name='overview';for(const tab of tabs){{const selected=tab.dataset.tab===name;tab.setAttribute('aria-selected',String(selected));document.getElementById('tab-'+tab.dataset.tab).hidden=!selected}}if(name==='metrics')requestAnimationFrame(()=>window.dispatchEvent(new Event('resize')))}}
    tabs.forEach(tab=>{{tab.setAttribute('aria-controls','tab-'+tab.dataset.tab);tab.addEventListener('click',()=>{{history.replaceState(null,'','#'+tab.dataset.tab);activateTab(tab.dataset.tab)}})}});window.addEventListener('hashchange',()=>activateTab(location.hash.slice(1)));activateTab(location.hash.slice(1)||'overview');
    const deleteForm=document.getElementById('deleteRunForm'),expectedName={json.dumps(display_code, ensure_ascii=False)};deleteForm.addEventListener('submit',event=>{{if(deleteForm.elements.namedItem('confirm_name').value.trim()!==expectedName){{event.preventDefault();alert('请输入准确的实验编号：'+expectedName);return}}if(!confirm('确定永久删除 '+expectedName+' 吗？此操作无法恢复。'))event.preventDefault()}});
    const aiButton=document.getElementById('generateAi'),aiMessage=document.getElementById('aiMessage'),aiReport=document.getElementById('aiReport'),aiEmpty=document.getElementById('aiEmpty');let aiPollTimer=null;
    const updateAiUi=r=>{{const status=r.ai_status||'not_requested',emailStatus=r.ai_email_status||'not_requested';document.getElementById('aiStatusValue').textContent=aiLabels[status]||status;document.getElementById('aiModelsValue').textContent=(r.ai_models||[]).join('、')||'—';document.getElementById('aiRequestedAt').textContent=r.ai_requested_at?'　提交时间：'+r.ai_requested_at:'';document.getElementById('aiGeneratedAt').textContent=r.ai_generated_at?'　生成时间：'+r.ai_generated_at:'';document.getElementById('aiEmailStatus').textContent='邮件：'+(emailLabels[emailStatus]||emailStatus)+(r.ai_email_sent_at?'（'+r.ai_email_sent_at+'）':'');const error=r.ai_error||r.ai_email_error||'';aiMessage.textContent=error||(status==='running'?'服务器已受理，正在生成报告；刷新页面不会丢失此状态。':status==='completed'?'报告已生成。':'');const report=r.ai_report||'';aiReport.textContent=report;aiReport.style.display=report?'block':'none';aiEmpty.style.display=report?'none':'block';aiButton.disabled=status==='running';aiButton.textContent=status==='running'?'正在生成…':'生成结论并发送邮件';if(status==='running'&&!aiPollTimer)aiPollTimer=setInterval(pollAi,3000);if(status!=='running'&&aiPollTimer){{clearInterval(aiPollTimer);aiPollTimer=null}}}};
    const pollAi=async()=>{{try{{const response=await fetch('/api/v1/web/runs/'+encodeURIComponent(runId)+'/ai-status',{{headers:{{'X-Monitor-Request':'dashboard'}},cache:'no-store'}});if(response.ok)updateAiUi(await response.json())}}catch{{}}}};
    const dot=document.getElementById('liveDot'),liveText=document.getElementById('liveText');const stream=new EventSource('/events/runs/'+encodeURIComponent(runId));stream.onopen=()=>{{dot.classList.remove('offline');liveText.textContent='实时连接正常'}};stream.onerror=()=>{{dot.classList.add('offline');liveText.textContent='实时连接中断，正在重连'}};stream.onmessage=event=>{{const data=JSON.parse(event.data),r=data.run,f=data.fragments,total=r.total_epochs||0;document.getElementById('statusBadge').innerHTML=f.status;document.getElementById('epochValue').textContent=(r.current_epoch||0)+' / '+total;document.getElementById('batchValue').textContent=(r.current_batch??'—')+' / '+(r.total_batches??'—');document.getElementById('elapsedValue').textContent=duration(r.elapsed_seconds);document.getElementById('etaValue').textContent=duration(r.eta_seconds);document.getElementById('bestMapValue').textContent=f.best_map;document.getElementById('baselineDeltaValue').innerHTML=f.baseline_delta;document.getElementById('performanceMetrics').innerHTML=f.performance;document.getElementById('lossMetrics').innerHTML=f.losses;document.getElementById('environmentBrief').textContent=f.environment_brief;document.getElementById('progressFill').style.width=(total?Math.min(100,100*(r.current_epoch||0)/total):0)+'%';document.getElementById('latestMetrics').innerHTML=f.metrics;document.getElementById('bestEpoch').innerHTML=f.best;document.getElementById('hostStatus').innerHTML=f.host;document.getElementById('resultMetrics').innerHTML=f.result;document.getElementById('reproducibility').innerHTML=f.reproducibility;document.getElementById('logTail').textContent=r.log_tail||'训练端尚未上报日志。';if(window.updateTrendSources)window.updateTrendSources([{{id:r.id,name:r.name,events:data.events}}]);const errorBox=document.getElementById('dynamicError');if(r.error_message){{errorBox.style.display='block';document.getElementById('dynamicErrorText').textContent=r.error_message}}else errorBox.style.display='none';if(document.activeElement!==document.getElementById('groupName'))document.getElementById('groupName').value=r.group_name||'';if(document.activeElement!==document.getElementById('tagsInput'))document.getElementById('tagsInput').value=(r.tags||[]).join(', ');document.getElementById('favoriteInput').checked=!!r.favorite;for(const [id,key] of [['hypothesis','hypothesis'],['changeNotes','change_notes'],['resultNotes','result_notes'],['conclusion','conclusion'],['nextStep','next_step'],['baselineRunId','baseline_run_id']]){{const field=document.getElementById(id);if(document.activeElement!==field)field.value=r[key]||''}}updateAiUi(r)}};
    const form=document.getElementById('metadataForm');form.addEventListener('submit',async event=>{{event.preventDefault();const message=document.getElementById('metadataMessage'),password=document.getElementById('metadataPassword');message.textContent='保存中…';const response=await fetch('/api/v1/web/runs/'+encodeURIComponent(runId)+'/metadata',{{method:'POST',headers:{{'Content-Type':'application/json','X-Monitor-Request':'dashboard'}},body:JSON.stringify({{group_name:document.getElementById('groupName').value,tags:document.getElementById('tagsInput').value,favorite:document.getElementById('favoriteInput').checked,hypothesis:document.getElementById('hypothesis').value,change_notes:document.getElementById('changeNotes').value,result_notes:document.getElementById('resultNotes').value,conclusion:document.getElementById('conclusion').value,next_step:document.getElementById('nextStep').value,baseline_run_id:document.getElementById('baselineRunId').value,password:password.value}})}});password.value='';message.textContent=response.ok?'已保存':(response.status===403?'管理员密码错误':'保存失败')}})}})();
    const aiForm=document.getElementById('aiGenerateForm');aiForm.addEventListener('submit',async event=>{{event.preventDefault();const password=document.getElementById('aiPassword');if(!password.value)return;aiButton.disabled=true;aiButton.textContent='正在提交…';aiMessage.textContent='正在向服务器提交请求…';const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),20000);try{{const response=await fetch('/api/v1/web/runs/'+encodeURIComponent(runId)+'/ai-generate',{{method:'POST',headers:{{'Content-Type':'application/json','X-Monitor-Request':'dashboard'}},body:JSON.stringify({{password:password.value,send_email:true}}),signal:controller.signal}});let result={{}};try{{result=await response.json()}}catch{{}}password.value='';if(response.ok){{updateAiUi(result);aiMessage.textContent='服务器已确认提交，正在生成报告；刷新页面不会丢失此状态。';await pollAi()}}else{{aiButton.disabled=false;aiButton.textContent='生成结论并发送邮件';aiMessage.textContent=result.error||(response.status===403?'管理员密码错误':'提交失败')}}}}catch(error){{aiButton.disabled=false;aiButton.textContent='生成结论并发送邮件';aiMessage.textContent=error.name==='AbortError'?'提交超时，服务器尚未确认，请稍后重试。':'网络错误，服务器未确认提交。'}}finally{{clearTimeout(timeout)}}}});pollAi();
    </script></div>""")


def compare_html(items: list[tuple[dict, list[dict]]]) -> str:
    rows = []
    sources = []
    for run, events in items:
        sources.append({"id": run["id"], "name": run["name"], "events": events})
        total = run.get("total_epochs") or 0
        best = best_epoch_info(events)
        best_text = (
            f"Epoch {best['epoch']} · {html.escape(best['metric_key'])} = {html.escape(display_number(best['value']))}"
            if best else "—"
        )
        rows.append(f"""<tr><td><a href="/runs/{quote(run['id'])}">{html.escape(run['name'])}</a></td><td>{status_badge(run['status'])}</td><td>{run['current_epoch']} / {total}</td><td>{best_text}</td><td>{human_duration(run.get('elapsed_seconds'))}</td><td>{metric_chips(latest_metrics_for(run, events))}</td></tr>""")
    table = "".join(rows)
    chart = trend_panel(sources, compare=True)
    return page("多实验对比", f"""<div class="wrap"><div class="top"><div><a href="/">← 全部实验</a><div class="brand">多实验对比</div><div class="muted">已选择 {len(items)} 条实验记录</div></div></div>{chart}
    <div class="panel"><h3>实验摘要与精确值</h3><div style="overflow:auto"><table><thead><tr><th>实验</th><th>状态</th><th>Epoch</th><th>最佳 Epoch / 指标</th><th>耗时</th><th>最新 / 最终指标</th></tr></thead><tbody>{table}</tbody></table></div></div></div>""")


def ai_settings_html() -> str:
    config = AI_CONFIG.public()
    selected = set(config.get("selected_models") or [])
    provider_cards, model_options = [], []
    synthesis_options = ['<option value="">第一个成功模型</option>']
    type_labels = {
        "deepseek": "DeepSeek Chat Completions",
        "openai_compatible": "OpenAI-compatible Chat Completions",
        "openai_responses": "OpenAI Responses API",
        "anthropic": "Anthropic Messages API",
        "gemini": "Google Gemini API",
    }
    for provider in config["providers"]:
        provider_id = html.escape(provider["id"])
        type_options = "".join(
            f'<option value="{html.escape(value)}" {"selected" if provider["type"] == value else ""}>{html.escape(label)}</option>'
            for value, label in type_labels.items()
        )
        key_hint = "已安全保存，留空保持不变" if provider["has_api_key"] else "粘贴 API Key"
        key_state = "已保存 Key" if provider["has_api_key"] else "尚未保存 Key"
        provider_cards.append(f'''<section class="agent-card ai-provider" data-provider="{provider_id}">
        <div class="agent-heading"><div><h4>{html.escape(provider["name"])}</h4><div class="label">{provider_id}</div></div>
        <label class="controls"><input class="provider-enabled" type="checkbox" {"checked" if provider["enabled"] else ""}>启用</label></div>
        <div class="grid"><label>API 类型<select class="provider-type filter-input">{type_options}</select></label>
        <label class="form-wide">Base URL<input class="provider-url filter-input" value="{html.escape(provider["base_url"])}"></label>
        <label class="form-wide">模型（逗号或换行分隔）<textarea class="provider-models filter-input">{html.escape(", ".join(provider["models"]))}</textarea></label>
        <label class="form-wide">API Key<input class="provider-key filter-input" type="password" autocomplete="new-password" placeholder="{html.escape(key_hint)}"></label>
        <label class="controls form-wide"><input class="provider-clear-key" type="checkbox">清除已保存的 Key</label>
        <div class="form-wide controls"><button class="button secondary test-provider" type="button">测试此供应商</button><span class="label">{key_state}</span></div></div></section>''')
        for model in provider["models"]:
            reference = f"{provider['id']}:{model}"
            model_options.append(
                f'<label class="controls"><input class="selected-model" type="checkbox" value="{html.escape(reference)}" {"checked" if reference in selected else ""}>'
                f'{html.escape(provider["name"])} · <code>{html.escape(model)}</code></label>'
            )
            synthesis_options.append(
                f'<option value="{html.escape(reference)}" {"selected" if config.get("synthesis_model") == reference else ""}>'
                f'{html.escape(provider["name"])} · {html.escape(model)}</option>'
            )
    template = '''<div class="wrap"><div class="top"><div><a href="/">← 全部实验</a><div class="brand">AI 实验分析配置</div>
    <div class="muted">API Key 仅保存在服务器 0600 配置文件中，页面只显示是否已保存。分析时会向所选厂商发送实验名、训练参数、指标采样、基线摘要和最近日志，不发送 API Key 或完整源码。</div></div><a class="button secondary" href="/logout">退出登录</a></div>
    <form id="aiConfigForm"><div class="panel"><div class="panel-heading"><div><h3>自动化策略</h3><div class="muted">多模型会并行独立审阅，再由汇总模型形成最终版本。</div></div></div>
    <div class="grid"><label class="controls"><input id="aiEnabled" type="checkbox" __ENABLED__>启用 AI</label>
    <label class="controls"><input id="autoCompleted" type="checkbox" __AUTO__>实验完成后自动分析</label>
    <label class="controls"><input id="emailReport" type="checkbox" __EMAIL__>把 AI 结论加入完成邮件</label>
    <label class="controls"><input id="applyMetadata" type="checkbox" __APPLY__>自动回填实验元数据</label>
    <label class="controls"><input id="overwriteMetadata" type="checkbox" __OVERWRITE__>允许覆盖已有人工内容</label>
    <label>最大输出 Token<input id="maxTokens" class="filter-input" type="number" min="500" max="16000" value="__MAX_TOKENS__"></label></div></div>
    <div class="panel"><h3>供应商</h3><div class="agent-list">__PROVIDERS__</div></div>
    <div class="panel"><h3>参与分析的模型（可多选）</h3><div class="grid">__MODELS__</div>
    <label style="display:block;margin-top:16px">最终汇总模型<select id="synthesisModel" class="filter-input">__SYNTHESIS__</select></label>
    <p class="muted">修改供应商的模型列表后先保存并刷新页面，即可在这里选择新模型。</p></div>
    <div class="panel"><div class="grid"><label>管理员密码<input id="aiPassword" class="filter-input" type="password" required autocomplete="current-password"></label>
    <div class="controls"><button class="button" type="submit">保存 AI 配置</button><span id="aiMessage" class="muted"></span></div></div></div></form>
    <script>(()=>{const form=document.getElementById('aiConfigForm'),message=document.getElementById('aiMessage'),password=document.getElementById('aiPassword');
    const providers=()=>[...document.querySelectorAll('.ai-provider')].map(card=>({id:card.dataset.provider,name:card.querySelector('h4').textContent,type:card.querySelector('.provider-type').value,base_url:card.querySelector('.provider-url').value,models:card.querySelector('.provider-models').value,enabled:card.querySelector('.provider-enabled').checked,api_key:card.querySelector('.provider-key').value,clear_api_key:card.querySelector('.provider-clear-key').checked}));
    const payload=()=>({enabled:document.getElementById('aiEnabled').checked,auto_on_completed:document.getElementById('autoCompleted').checked,email_report:document.getElementById('emailReport').checked,apply_metadata:document.getElementById('applyMetadata').checked,overwrite_metadata:document.getElementById('overwriteMetadata').checked,max_output_tokens:Number(document.getElementById('maxTokens').value),selected_models:[...document.querySelectorAll('.selected-model:checked')].map(x=>x.value),synthesis_model:document.getElementById('synthesisModel').value,providers:providers(),password:password.value});
    form.addEventListener('submit',async event=>{event.preventDefault();message.textContent='保存中…';const response=await fetch('/api/v1/web/ai/config',{method:'POST',headers:{'Content-Type':'application/json','X-Monitor-Request':'dashboard'},body:JSON.stringify(payload())});let result={};try{result=await response.json()}catch{}password.value='';message.textContent=response.ok?'已保存，正在刷新…':(result.error||'保存失败');if(response.ok)setTimeout(()=>location.reload(),600)});
    document.querySelectorAll('.test-provider').forEach(button=>button.addEventListener('click',async()=>{const card=button.closest('.ai-provider'),id=card.dataset.provider,models=card.querySelector('.provider-models').value.replace(/，/g,',').replace(/\\n/g,',').split(',').map(x=>x.trim()).filter(Boolean);if(!models.length){alert('请先填写模型');return}const secret=prompt('请输入管理员密码测试 '+id+'：');if(secret===null)return;const provider=providers().find(item=>item.id===id);provider.enabled=true;button.disabled=true;button.textContent='测试中…';const response=await fetch('/api/v1/web/ai/test',{method:'POST',headers:{'Content-Type':'application/json','X-Monitor-Request':'dashboard'},body:JSON.stringify({password:secret,model_reference:id+':'+models[0],provider})});let result={};try{result=await response.json()}catch{}button.disabled=false;button.textContent='测试此供应商';alert(response.ok?'调用成功：'+(result.summary||'模型已返回有效 JSON'):result.error||'测试失败')}))})();</script></div>'''
    return page(
        "AI 实验分析配置",
        template
        .replace("__ENABLED__", "checked" if config.get("enabled") else "")
        .replace("__AUTO__", "checked" if config.get("auto_on_completed") else "")
        .replace("__EMAIL__", "checked" if config.get("email_report") else "")
        .replace("__APPLY__", "checked" if config.get("apply_metadata") else "")
        .replace("__OVERWRITE__", "checked" if config.get("overwrite_metadata") else "")
        .replace("__MAX_TOKENS__", str(config.get("max_output_tokens") or 5000))
        .replace("__PROVIDERS__", "".join(provider_cards))
        .replace("__MODELS__", "".join(model_options) or '<div class="empty">请先配置供应商模型</div>')
        .replace("__SYNTHESIS__", "".join(synthesis_options)),
    )


def login_html(error: str = "") -> str:
    err = f'<p style="color:var(--red)">{html.escape(error)}</p>' if error else ""
    return page("登录", f"""<div class="wrap login"><div class="panel"><div class="page-eyebrow">Research workspace</div><div class="brand">MCONG Lab</div><p class="muted">请输入管理密码</p>{err}<form method="post" action="/login"><input type="password" name="password" autofocus required><button class="button" type="submit">登录</button></form></div></div>""")


def make_session() -> str:
    expires = int(time.time()) + 86400 * 7
    nonce = secrets.token_hex(8)
    body = f"{expires}.{nonce}"
    signature = hmac.new(CFG.session_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{body}.{signature}".encode()).decode()


def valid_session(value: str) -> bool:
    try:
        decoded = base64.urlsafe_b64decode(value.encode()).decode()
        expires, nonce, signature = decoded.split(".", 2)
        body = f"{expires}.{nonce}"
        expected = hmac.new(CFG.session_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        return int(expires) >= int(time.time()) and hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "ExperimentMonitor/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} [{self.log_date_time_string()}] {fmt % args}", flush=True)

    def send_bytes(self, status: int, body: bytes, content_type: str, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        for key, value in headers or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, status: int, content: str, headers=None):
        self.send_bytes(status, content.encode(), "text/html; charset=utf-8", headers)

    def send_json(self, status: int, data: dict):
        self.send_bytes(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def redirect(self, target: str, cookie: str | None = None):
        headers = [("Location", target)]
        if cookie:
            headers.append(("Set-Cookie", cookie))
        self.send_bytes(HTTPStatus.SEE_OTHER, b"", "text/plain", headers)

    def read_body(self, max_size=1_000_000) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > max_size:
            raise ValueError("request body too large")
        return self.rfile.read(length)

    def read_json(self) -> dict:
        try:
            value = json.loads(self.read_body().decode() or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def api_authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return bool(CFG.api_token) and hmac.compare_digest(auth, f"Bearer {CFG.api_token}")

    def web_authorized(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        token = cookie.get("monitor_session")
        return bool(token and CFG.session_secret and valid_session(token.value))

    def require_web(self) -> bool:
        if self.web_authorized():
            return True
        self.redirect("/login")
        return False

    def require_web_json(self) -> bool:
        if self.web_authorized():
            return True
        self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "login required"})
        return False

    def send_sse(self, kind: str, run_id: str = ""):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last_payload = b""
        last_heartbeat = 0.0
        last_dashboard_scan = 0.0
        try:
            for _ in range(300):
                if kind == "dashboard":
                    if time.monotonic() - last_dashboard_scan < 10:
                        time.sleep(2)
                        continue
                    data = dashboard_payload()
                    last_dashboard_scan = time.monotonic()
                else:
                    run = STORE.get_run(run_id)
                    if not run:
                        self.wfile.write(b"event: deleted\ndata: {}\n\n")
                        self.wfile.flush()
                        return
                    events = STORE.metric_events(run_id)
                    data = run_snapshot(run, events)
                payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
                now = time.monotonic()
                if payload != last_payload:
                    self.wfile.write(b"data: " + payload + b"\n\n")
                    self.wfile.flush()
                    last_payload = payload
                    last_heartbeat = now
                elif now - last_heartbeat >= 15:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_heartbeat = now
                time.sleep(2)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            return
        finally:
            self.close_connection = True

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health":
            self.send_json(200, {"ok": True, "time": utc_now()})
            return
        if path == "/login":
            if self.web_authorized():
                self.redirect("/")
            else:
                self.send_html(200, login_html())
            return
        if path == "/logout":
            flags = "Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
            if CFG.cookie_secure:
                flags += "; Secure"
            self.redirect("/login", f"monitor_session=; {flags}")
            return
        public_download = path in {"/api/v1/public/desktop/latest", "/downloads/YoloMonitorPet.exe"}
        if not public_download:
            if path.startswith("/api/v1/public/") or path.startswith("/events/"):
                if not self.require_web_json():
                    return
            elif not self.require_web():
                return
        if path == "/api/v1/public/runs":
            self.send_json(200, {"runs": STORE.list_runs(), "server_time": utc_now()})
            return
        if path == "/api/v1/public/queue":
            self.send_json(200, {"jobs": STORE.list_queue_jobs_with_reasons(False), "agents": STORE.list_agents(), "server_time": utc_now()})
            return
        if path.startswith("/api/v1/web/runs/") and path.endswith("/ai-status"):
            run_id = unquote(path[len("/api/v1/web/runs/") : -len("/ai-status")].strip("/"))
            run = STORE.get_run(run_id)
            if not run:
                self.send_json(404, {"error": "run not found"})
                return
            self.send_json(200, {
                "ai_status": run.get("ai_status") or "not_requested",
                "ai_requested_at": run.get("ai_requested_at"),
                "ai_generated_at": run.get("ai_generated_at"),
                "ai_models": run.get("ai_models") or [],
                "ai_report": run.get("ai_report") or "",
                "ai_error": run.get("ai_error") or "",
                "ai_email_status": run.get("ai_email_status") or "not_requested",
                "ai_email_sent_at": run.get("ai_email_sent_at"),
                "ai_email_error": run.get("ai_email_error") or "",
            })
            return
        if path == "/api/v1/public/desktop/latest":
            manifest_path = Path(CFG.download_dir) / "latest.json"
            try:
                if manifest_path.stat().st_size > 64_000:
                    raise ValueError("update manifest is too large")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                required = {"version", "download_url", "sha256", "size"}
                if not isinstance(manifest, dict) or not required.issubset(manifest):
                    raise ValueError("invalid update manifest")
            except (OSError, ValueError, json.JSONDecodeError):
                self.send_json(404, {"error": "desktop update is not published"})
                return
            self.send_json(200, manifest)
            return
        if path == "/downloads/YoloMonitorPet.exe":
            executable_path = Path(CFG.download_dir) / "YoloMonitorPet.exe"
            try:
                size = executable_path.stat().st_size
                if size <= 0 or size > 50_000_000:
                    raise ValueError("invalid executable size")
                body = executable_path.read_bytes()
            except (OSError, ValueError):
                self.send_json(404, {"error": "desktop executable is not published"})
                return
            self.send_bytes(
                200,
                body,
                "application/vnd.microsoft.portable-executable",
                [("Content-Disposition", 'attachment; filename="YoloMonitorPet.exe"')],
            )
            return
        if path == "/downloads/yolo-queue-agent.zip":
            archive_path = Path(CFG.download_dir) / "yolo-queue-agent.zip"
            try:
                size = archive_path.stat().st_size
                if size <= 0 or size > 10_000_000:
                    raise ValueError("invalid agent archive size")
                body = archive_path.read_bytes()
            except (OSError, ValueError):
                self.send_json(404, {"error": "queue agent is not published"})
                return
            self.send_bytes(200, body, "application/zip", [("Content-Disposition", 'attachment; filename="yolo-queue-agent.zip"')])
            return
        if path.startswith("/api/v1/public/runs/") and path.endswith("/report.svg"):
            run_id = unquote(path[len("/api/v1/public/runs/") : -len("/report.svg")].strip("/"))
            run = STORE.get_run(run_id)
            if not run:
                self.send_json(404, {"error": "run not found"})
                return
            self.send_bytes(200, report_svg(run, STORE.metric_events(run_id)).encode("utf-8"), "image/svg+xml; charset=utf-8")
            return
        if path.startswith("/api/v1/public/runs/"):
            run_id = unquote(path[len("/api/v1/public/runs/") :].strip("/"))
            run = STORE.get_run(run_id)
            if not run:
                self.send_json(404, {"error": "run not found"})
                return
            events = STORE.metric_events(run_id)
            self.send_json(
                200,
                {
                    "run": run,
                    "events": events,
                    "best": best_epoch_info(events),
                    "report": report_summary(run, events),
                    "server_time": utc_now(),
                },
            )
            return
        if path == "/events/dashboard":
            self.send_sse("dashboard")
            return
        if path.startswith("/events/runs/"):
            self.send_sse("run", unquote(path[len("/events/runs/"):]))
            return
        if path == "/":
            self.send_html(200, dashboard_html(STORE.list_runs()))
            return
        if path == "/ai":
            self.send_html(200, ai_settings_html())
            return
        if path == "/queue":
            query = parse_qs(parsed.query)
            clone_values = query.get("clone", [])
            clone_runs = query.get("clone_run", [])
            clone_job = STORE.get_queue_job(clone_values[0], True) if clone_values else (STORE.get_queue_job_by_run(clone_runs[0], True) if clone_runs else None)
            self.send_html(200, queue_html(STORE.list_queue_jobs_with_reasons(False), STORE.list_agents(), clone_job))
            return
        if path == "/lineage":
            self.send_html(200, lineage_html(STORE.list_runs(limit=1000)))
            return
        if path.startswith("/queue/jobs/"):
            job_id = unquote(path[len("/queue/jobs/") :].strip("/"))
            job = STORE.get_queue_job(job_id, True)
            if not job:
                self.send_html(404, page("未找到", '<div class="wrap"><div class="panel">队列任务不存在。<br><a href="/queue">返回实验队列</a></div></div>'))
                return
            if not STORE.queue_job_editable(job):
                self.send_html(
                    HTTPStatus.CONFLICT,
                    page("任务已锁定", '<div class="wrap"><div class="panel"><h2>任务已经开始或不再处于排队状态</h2><p class="muted">为保证实际执行配置与队列记录一致，此任务不能再查看或修改启动参数。</p><a class="button secondary" href="/queue">返回实验队列</a></div></div>'),
                )
                return
            self.send_html(200, queue_job_html(job))
            return
        if path == "/compare":
            selected = []
            for run_id in parse_qs(parsed.query).get("run", []):
                if run_id not in selected:
                    selected.append(run_id)
            selected = selected[:5]
            if len(selected) < 2:
                self.send_html(
                    400,
                    page(
                        "无法对比",
                        '<div class="wrap"><div class="panel"><h2>请选择 2–5 条实验记录</h2><a href="/">返回实验列表</a></div></div>',
                    ),
                )
                return
            items = []
            for run_id in selected:
                run = STORE.get_run(run_id)
                if run:
                    items.append((run, STORE.metric_events(run_id)))
            if len(items) < 2:
                self.send_html(404, page("无法对比", '<div class="wrap"><div class="panel">可用实验不足 2 条。</div></div>'))
                return
            self.send_html(200, compare_html(items))
            return
        if path.startswith("/runs/"):
            run_id = path[len("/runs/"):]
            run = STORE.get_run(run_id)
            if not run:
                self.send_html(404, page("未找到", '<div class="wrap"><div class="panel">实验不存在</div></div>'))
                return
            self.send_html(200, run_html(run, STORE.metric_events(run_id)))
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/login":
            try:
                form = parse_qs(self.read_body(10000).decode())
                password = form.get("password", [""])[0]
            except (UnicodeDecodeError, ValueError):
                password = ""
            if CFG.admin_password and hmac.compare_digest(password, CFG.admin_password):
                flags = "Path=/; Max-Age=604800; HttpOnly; SameSite=Lax"
                if CFG.cookie_secure:
                    flags += "; Secure"
                self.redirect("/", f"monitor_session={make_session()}; {flags}")
            else:
                self.send_html(HTTPStatus.UNAUTHORIZED, login_html("密码错误"))
            return
        web_api_request = path.startswith("/api/v1/web/")
        web_form_request = path.startswith("/runs/") and path.endswith("/delete")
        if web_api_request and not self.require_web_json():
            return
        if web_form_request and not self.require_web():
            return
        ai_generate_request = path.startswith("/api/v1/web/runs/") and path.endswith("/ai-generate")
        if path in {"/api/v1/web/ai/config", "/api/v1/web/ai/test"} or ai_generate_request:
            try:
                payload = self.read_json()
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            password = str(payload.pop("password", ""))
            if (
                self.headers.get("X-Monitor-Request") != "dashboard"
                or not CFG.admin_password
                or not hmac.compare_digest(password, CFG.admin_password)
            ):
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            try:
                if path == "/api/v1/web/ai/config":
                    AI_CONFIG.save(payload)
                    self.send_json(200, {"config": AI_CONFIG.public()})
                elif path == "/api/v1/web/ai/test":
                    config = AI_CONFIG.load()
                    reference = str(payload.get("model_reference") or "")
                    candidate = payload.get("provider")
                    if isinstance(candidate, dict):
                        candidate = dict(candidate)
                        saved = next(
                            (item for item in config["providers"] if item["id"] == str(candidate.get("id") or "")),
                            {},
                        )
                        if not str(candidate.get("api_key") or "").strip():
                            candidate["api_key"] = saved.get("api_key", "")
                        candidate["enabled"] = True
                        config = AiConfigStore.normalize({
                            "enabled": True,
                            "providers": [candidate],
                            "selected_models": [reference],
                            "synthesis_model": reference,
                            "max_output_tokens": config.get("max_output_tokens", 5000),
                        })
                        reference = (config.get("selected_models") or [reference])[0]
                    provider, model = resolve_ai_model(config, reference)
                    test_context = {
                        "run": {"name": "连接测试", "status": "completed"},
                        "metrics": [{"epoch": 1, "metrics": {"map50-95": 0.5}}],
                        "existing_metadata": {},
                    }
                    result = call_ai_model(
                        provider, model, ai_analysis_prompt(test_context),
                        min(2000, config["max_output_tokens"]),
                    )
                    self.send_json(200, {"ok": True, "summary": result["conclusion"][:300]})
                else:
                    run_id = unquote(path[len("/api/v1/web/runs/") : -len("/ai-generate")].strip("/"))
                    run = STORE.get_run(run_id)
                    if not run:
                        self.send_json(404, {"error": "run not found"})
                        return
                    if run["status"] not in {"completed", "failed", "paused"}:
                        self.send_json(HTTPStatus.CONFLICT, {"error": "实验尚未结束，不能生成最终结论"})
                        return
                    config = AI_CONFIG.load()
                    selected = config.get("selected_models") or []
                    if not config.get("enabled") or not selected:
                        raise ValueError("AI 功能未启用或尚未选择模型")
                    for reference in selected:
                        resolve_ai_model(config, reference)
                    send_email_after = bool(payload.get("send_email", True))
                    if not STORE.begin_ai_analysis(run_id, send_email_after):
                        self.send_json(HTTPStatus.CONFLICT, {"error": "AI 分析正在运行"})
                        return
                    threading.Thread(
                        target=self.safe_manual_ai,
                        args=(run_id, send_email_after),
                        daemon=True,
                    ).start()
                    current = STORE.get_run(run_id) or {}
                    self.send_json(HTTPStatus.ACCEPTED, {
                        "ok": True,
                        "ai_status": "running",
                        "ai_requested_at": current.get("ai_requested_at"),
                        "ai_email_status": current.get("ai_email_status"),
                    })
            except (ValueError, RuntimeError) as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception:
                traceback.print_exc()
                self.send_json(500, {"error": "AI configuration or request failed"})
            return
        if path.startswith("/api/v1/web/runs/") and path.endswith("/metadata"):
            try:
                payload = self.read_json()
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            password = str(payload.pop("password", ""))
            if (
                self.headers.get("X-Monitor-Request") != "dashboard"
                or not CFG.admin_password
                or not hmac.compare_digest(password, CFG.admin_password)
            ):
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            run_id = unquote(path[len("/api/v1/web/runs/") : -len("/metadata")].strip("/"))
            try:
                run = STORE.update_metadata(run_id, payload)
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            if not run:
                self.send_json(404, {"error": "run not found"})
            else:
                self.send_json(200, {"run": run})
            return
        queue_action_request = path.startswith("/api/v1/web/queue/") and path.endswith("/action")
        queue_update_request = path.startswith("/api/v1/web/queue/") and path.endswith("/update")
        queue_delete_request = path.startswith("/api/v1/web/queue/") and path.endswith("/delete")
        if path == "/api/v1/web/queue" or queue_action_request or queue_update_request or queue_delete_request:
            try:
                payload = self.read_json()
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            password = str(payload.pop("password", ""))
            if (
                self.headers.get("X-Monitor-Request") != "dashboard"
                or not CFG.admin_password
                or not hmac.compare_digest(password, CFG.admin_password)
            ):
                self.send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            try:
                if path == "/api/v1/web/queue":
                    sweep_jobs = payload.pop("sweep_jobs", None)
                    if sweep_jobs is not None:
                        if not isinstance(sweep_jobs, list) or not sweep_jobs or len(sweep_jobs) > 50:
                            raise ValueError("sweep_jobs must contain 1-50 jobs")
                        sweep_group_id = secrets.token_hex(8)
                        created = []
                        for override in sweep_jobs:
                            if not isinstance(override, dict):
                                raise ValueError("invalid sweep job")
                            item = {**payload, **override, "sweep_group_id": sweep_group_id}
                            created.append(STORE.create_queue_job(item))
                        self.send_json(201, {"jobs": [STORE.get_queue_job(job["id"], False) for job in created], "sweep_group_id": sweep_group_id})
                    else:
                        job = STORE.create_queue_job(payload)
                        self.send_json(201, {"job": STORE.get_queue_job(job["id"], False)})
                elif queue_action_request:
                    job_id = unquote(path[len("/api/v1/web/queue/") : -len("/action")].strip("/"))
                    job = STORE.queue_action(job_id, str(payload.get("action") or ""))
                    self.send_json(200, {"job": job}) if job else self.send_json(404, {"error": "job not found"})
                elif queue_delete_request:
                    job_id = unquote(path[len("/api/v1/web/queue/") : -len("/delete")].strip("/"))
                    if STORE.delete_queue_job(job_id):
                        self.send_json(200, {"deleted": True, "job_id": job_id})
                    else:
                        self.send_json(404, {"error": "job not found"})
                else:
                    job_id = unquote(path[len("/api/v1/web/queue/") : -len("/update")].strip("/"))
                    job = STORE.update_queue_job(job_id, payload)
                    self.send_json(200, {"job": STORE.get_queue_job(job_id, False)}) if job else self.send_json(404, {"error": "job not found"})
            except ValueError as exc:
                self.send_json(400, {"error": str(exc)})
            except RuntimeError as exc:
                self.send_json(HTTPStatus.CONFLICT, {"error": str(exc)})
            return
        if path.startswith("/runs/") and path.endswith("/delete"):
            run_id = path[len("/runs/") : -len("/delete")].strip("/")
            try:
                form = parse_qs(self.read_body(10000).decode())
                password = form.get("password", [""])[0]
                confirmation = form.get("confirm_name", [""])[0].strip()
            except (UnicodeDecodeError, ValueError):
                password = ""
                confirmation = ""
            if not CFG.admin_password or not hmac.compare_digest(password, CFG.admin_password):
                self.send_html(
                    HTTPStatus.FORBIDDEN,
                    page(
                        "删除失败",
                        f'<div class="wrap"><div class="panel"><h2>删除失败</h2><p>管理员密码错误，实验记录未删除。</p><a href="/runs/{quote(run_id)}">返回实验详情</a></div></div>',
                    ),
                )
                return
            target = STORE.get_run(run_id)
            if target and confirmation != experiment_display_name(target["name"])[0]:
                self.send_html(
                    HTTPStatus.BAD_REQUEST,
                    page("删除失败", f'<div class="wrap"><div class="panel"><h2>删除失败</h2><p>实验编号确认不匹配，记录未删除。</p><a href="/runs/{quote(run_id)}">返回实验详情</a></div></div>'),
                )
                return
            if STORE.delete_run(run_id):
                self.redirect("/")
            else:
                self.send_html(
                    HTTPStatus.NOT_FOUND,
                    page("未找到", '<div class="wrap"><div class="panel">实验不存在或已经删除。</div></div>'),
                )
            return
        if not path.startswith("/api/v1/"):
            self.send_json(404, {"error": "not found"})
            return
        if not self.api_authorized():
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        try:
            payload = self.read_json()
            if path == "/api/v1/agents/heartbeat":
                agent, notify = STORE.heartbeat_agent(payload)
                current_job = STORE.get_queue_job(agent.get("current_job_id") or "", False) if agent.get("current_job_id") else None
                if notify:
                    threading.Thread(target=self.safe_idle_email, args=(agent,), daemon=True).start()
                self.send_json(200, {"agent": agent, "idle_reminder": notify, "current_job": current_job,
                                     "preflight_jobs": STORE.pending_preflights(3),
                                     "reconcile_jobs": STORE.reconciliation_jobs(agent["id"]) if
                                     agent.get("capabilities", {}).get("reconcile_v1") else []})
                return
            if path == "/api/v1/agents/catalog":
                agent = STORE.publish_agent_catalog(payload)
                self.send_json(200, {"agent": agent})
                return
            if path == "/api/v1/agents/claim":
                agent_id = str(payload.get("agent_id") or "")[:120]
                gpus = payload.get("gpus") if isinstance(payload.get("gpus"), list) else []
                try:
                    job = STORE.claim_queue_job(agent_id, gpus)
                except PermissionError:
                    self.send_json(HTTPStatus.CONFLICT, {"error": "agent heartbeat required"})
                    return
                self.send_json(200, {"job": job})
                return
            if path.startswith("/api/v1/agents/jobs/"):
                suffix = path[len("/api/v1/agents/jobs/"):].strip("/").split("/")
                if len(suffix) != 2 or suffix[1] not in {"status", "anomaly", "preflight", "binding", "reconcile"}:
                    self.send_json(404, {"error": "not found"})
                    return
                job_id, action = suffix
                if action == "reconcile":
                    try:
                        job = STORE.reconcile_job(job_id, payload)
                    except PermissionError:
                        self.send_json(HTTPStatus.CONFLICT, {"error": "agent heartbeat required"})
                        return
                    self.send_json(200, {"job": job}) if job else self.send_json(404, {"error": "job not found"})
                    return
                if action == "status":
                    try:
                        job = STORE.agent_job_update(job_id, payload)
                    except PermissionError:
                        self.send_json(HTTPStatus.CONFLICT, {"error": "lease conflict"})
                        return
                    self.send_json(200, {"job": job}) if job else self.send_json(404, {"error": "job not found"})
                elif action == "anomaly":
                    try:
                        job = STORE.record_job_anomaly(job_id, payload)
                    except PermissionError:
                        self.send_json(HTTPStatus.CONFLICT, {"error": "lease conflict"})
                        return
                    if job:
                        threading.Thread(target=self.safe_anomaly_email, args=(job, payload), daemon=True).start()
                        self.send_json(200, {"job": STORE.get_queue_job(job_id, False)})
                    else:
                        self.send_json(404, {"error": "job not found"})
                elif action == "preflight":
                    try:
                        job = STORE.complete_preflight(job_id, payload)
                    except PermissionError:
                        self.send_json(HTTPStatus.CONFLICT, {"error": "agent heartbeat required"})
                        return
                    self.send_json(200, {"job": job}) if job else self.send_json(404, {"error": "job not found"})
                else:
                    try:
                        job = STORE.bind_agent_job_output(job_id, payload)
                    except PermissionError:
                        self.send_json(HTTPStatus.CONFLICT, {"error": "lease conflict"})
                        return
                    self.send_json(200, {"job": job}) if job else self.send_json(404, {"error": "job not found"})
                return
            if path == "/api/v1/runs/start":
                run = STORE.start_run(payload)
                self.send_json(201, {"run": run})
                return
            parts = path.strip("/").split("/")
            if len(parts) == 5 and parts[:3] == ["api", "v1", "runs"]:
                run_id, action = parts[3], parts[4]
            else:
                self.send_json(404, {"error": "not found"})
                return
            if action == "progress":
                run = STORE.progress(run_id, payload)
                if not run:
                    self.send_json(404, {"error": "run not found"})
                else:
                    self.send_json(200, {"run": run})
                return
            if action == "finish":
                run, first_finish = STORE.finish(run_id, payload)
                if not run:
                    self.send_json(404, {"error": "run not found"})
                else:
                    if first_finish:
                        threading.Thread(target=self.safe_finish_pipeline, args=(run,), daemon=True).start()
                    self.send_json(200, {"run": run})
                return
            self.send_json(404, {"error": "not found"})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception:
            traceback.print_exc()
            self.send_json(500, {"error": "internal server error"})

    @staticmethod
    def safe_email(run):
        try:
            send_status_email(run)
        except Exception:
            traceback.print_exc()

    @staticmethod
    def safe_finish_pipeline(run):
        current = run
        try:
            config = AI_CONFIG.load()
            if (
                run.get("status") == "completed"
                and config.get("enabled")
                and config.get("auto_on_completed")
                and config.get("selected_models")
            ):
                current = generate_ai_analysis(run["id"], send_email_after=False)
        except Exception:
            traceback.print_exc()
            current = STORE.get_run(run["id"]) or run
        try:
            send_status_email(current)
        except Exception:
            traceback.print_exc()

    @staticmethod
    def safe_manual_ai(run_id: str, send_email_after: bool):
        try:
            generate_ai_analysis(
                run_id,
                send_email_after=send_email_after,
                already_started=True,
            )
        except Exception:
            traceback.print_exc()

    @staticmethod
    def safe_idle_email(agent):
        try:
            send_idle_email(agent)
        except Exception:
            traceback.print_exc()

    @staticmethod
    def safe_anomaly_email(job, payload):
        try:
            send_anomaly_email(job, payload)
        except Exception:
            traceback.print_exc()


def watchdog_loop():
    while True:
        time.sleep(CFG.watchdog_seconds)
        try:
            for run in STORE.mark_stalled(CFG.stale_seconds):
                print(
                    f"Run {run['id']} marked stalled after {CFG.stale_seconds}s without updates",
                    flush=True,
                )
                threading.Thread(target=Handler.safe_email, args=(run,), daemon=True).start()
        except Exception:
            traceback.print_exc()


def validate_config():
    missing = [name for name, value in (
        ("MONITOR_API_TOKEN", CFG.api_token),
        ("MONITOR_ADMIN_PASSWORD", CFG.admin_password),
        ("MONITOR_SESSION_SECRET", CFG.session_secret),
    ) if not value]
    if missing:
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))
    if len(CFG.api_token) < 24 or len(CFG.session_secret) < 32:
        raise SystemExit("API token/session secret are too short; use the provided generator")


def main():
    validate_config()
    STORE.reconcile_catalog_lineage()
    STORE.enrich_run_metadata()
    httpd = ThreadingHTTPServer((CFG.bind, CFG.port), Handler)
    threading.Thread(target=watchdog_loop, name="run-watchdog", daemon=True).start()
    print(f"Experiment monitor listening on http://{CFG.bind}:{CFG.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
