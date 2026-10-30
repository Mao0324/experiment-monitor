import http.cookiejar
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer


SERVER = Path(__file__).resolve().parents[1] / "server" / "monitor_server.py"


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.inside = True
            self.scripts.append("")

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.scripts[-1] += data


class DashboardUiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings = {
            "MONITOR_DB": str(Path(self.temp.name) / "monitor.db"),
            "MONITOR_AI_CONFIG": str(Path(self.temp.name) / "ai.json"),
            "MONITOR_ADMIN_PASSWORD": "ui-test-password",
            "MONITOR_SESSION_SECRET": "ui-test-secret",
            "MONITOR_COOKIE_SECURE": "false",
        }
        with patch.dict(os.environ, settings):
            spec = importlib.util.spec_from_file_location("monitor_dashboard_ui_test", SERVER)
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)
        self.store = self.module.STORE
        now = self.module.utc_now()
        parent = {"reproducibility": {"libraries": {"torch": "2.0", "cuda_runtime": "11.7", "ultralytics": "8.1"}}}
        with self.store.connect() as conn:
            for run_id, name, status, baseline, ended_at, params in (
                ("parent", "CPD003_Workers4_M2DLIFLabels_v1", "completed", "", now, parent),
                ("child", "CPD004_Workers8-DetTrue_M2DLIFLabels_v1", "running", "parent", None, parent),
            ):
                conn.execute(
                    """INSERT INTO runs(id,name,status,total_epochs,current_epoch,started_at,updated_at,
                    ended_at,parameters_json,baseline_run_id) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, name, status, 100, 4, now, now, ended_at, json.dumps(params), baseline),
                )
            for run_id, epoch, value in (("parent", 1, .67), ("child", 1, .65), ("child", 2, .6742)):
                conn.execute(
                    "INSERT INTO metric_events(run_id,epoch,phase,created_at,metrics_json) VALUES (?,?,'epoch',?,?)",
                    (run_id, epoch, now, json.dumps({"metrics/mAP50-95(B)": value, "metrics/mAP50(B)": .8})),
                )
            conn.execute(
                """INSERT INTO queue_jobs(id,name,status,command_text,working_directory,created_at,updated_at)
                VALUES ('queued','queued','queued','python train.py','/tmp',?,?)""",
                (now, now),
            )
            for agent_id in ("slot-1", "slot-2"):
                conn.execute(
                    "INSERT INTO agents(id,hostname,updated_at,gpus_json,capabilities_json) VALUES (?,?,?,?,?)",
                    (agent_id, "training-host", now,
                     json.dumps([{"index": 0, "memory_used_mb": 4000, "utilization_percent": 50},
                                 {"index": 1, "memory_used_mb": 500, "utilization_percent": 0}]),
                     json.dumps({"idle_memory_used_mb": 3000, "idle_utilization_percent": 5})),
                )

    def test_dashboard_counts_and_best_result_use_all_records_and_best_epoch(self):
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO metric_events(run_id,epoch,phase,created_at,metrics_json) VALUES ('child',3,'epoch',?,?)",
                (self.module.utc_now(), '{"metrics/mAP50-95(B)": NaN}'),
            )
        data = self.module.dashboard_payload(self.store.list_runs())
        runs = {run["id"]: run for run in data["runs"]}
        self.assertEqual(runs["child"]["display_code"], "CPD-004")
        self.assertEqual(runs["child"]["best_map50_95"], .6742)
        self.assertAlmostEqual(runs["child"]["baseline_delta_pp"], .42)
        self.assertEqual(data["summary"]["running"], 1)
        self.assertEqual(data["summary"]["queued"], 1)
        self.assertEqual(data["summary"]["completed_today"], 1)
        self.assertEqual((data["summary"]["gpu_busy"], data["summary"]["gpu_total"]), (1, 2))

    def test_duplicate_display_codes_show_distinct_titles_and_run_ids(self):
        now = self.module.utc_now()
        with self.store.connect() as conn:
            for run_id, name in (
                ("esc-b4", "ESC008_B4_FromB32Initializer_72E"),
                ("esc-b32", "ESC008_B32_FromB4Initializer_72E"),
                ("esc-repeat", "ESC008_B4_FromB32Initializer_72E"),
            ):
                conn.execute(
                    "INSERT INTO runs(id,name,status,started_at,updated_at) VALUES (?,?, 'completed',?,?)",
                    (run_id, name, now, now),
                )
        payload = self.module.dashboard_payload(self.store.list_runs())
        items = {item["id"]: item for item in payload["runs"]}
        self.assertEqual(items["esc-b32"]["display_title"], "ESC-008 · B32")
        self.assertEqual(items["esc-b4"]["display_title"], "ESC-008 · Run esc-b4")
        self.assertEqual(items["esc-repeat"]["display_title"], "ESC-008 · Run esc-repe")
        self.assertTrue(items["esc-b4"]["display_disambiguated"])
        self.assertIn("Run esc-b4", self.module.dashboard_html(self.store.list_runs()))
        self.assertIn('value="esc-b32"', self.module.baseline_run_options())

    def test_lineage_never_matches_parent_on_another_host(self):
        now = self.module.utc_now()
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO runs(id,name,status,started_at,updated_at) VALUES ('other-child','child-on-b','running',?,?)",
                (now, now),
            )
            conn.execute("UPDATE runs SET name='father-on-a',baseline_run_id='' WHERE id='parent'")
            conn.execute(
                "UPDATE agents SET hostname='host-a',catalog_json=? WHERE id='slot-1'",
                (json.dumps([{"id": "a-father", "task_name": "father-on-a", "working_directory": "/project",
                              "path": "/project/father.py", "model_yaml_name": "father.yaml",
                              "model_yaml": "/project/yaml/father.yaml"}]),),
            )
            conn.execute(
                "UPDATE agents SET hostname='host-b',catalog_json=? WHERE id='slot-2'",
                (json.dumps([{"id": "b-child", "task_name": "child-on-b", "working_directory": "/project",
                              "path": "/project/child.py", "model_yaml_name": "child.yaml",
                              "father_yaml_name": "father.yaml", "father_yaml": "/project/yaml/father.yaml",
                              "lineage_relation": "father"}]),),
            )
        self.store.reconcile_catalog_lineage()
        self.assertEqual(self.store.get_run("other-child")["baseline_run_id"], "")
        self.assertEqual(self.store.lineage_for_script("b-child")["lineage_status"], "parent_script_missing")

    def test_lineage_uses_exact_project_when_run_names_repeat(self):
        now = self.module.utc_now()
        with self.store.connect() as conn:
            for run_id, root in (("wrong-parent", "/old"), ("right-parent", "/new")):
                parameters = {"reproducibility": {"working_directory": root, "entrypoint": "father.py"}}
                conn.execute(
                    "INSERT INTO runs(id,name,status,started_at,updated_at,parameters_json) "
                    "VALUES (?,?, 'completed',?,?,?)",
                    (run_id, "reused-father-name", now, now, json.dumps(parameters)),
                )
            conn.execute(
                "INSERT INTO runs(id,name,status,started_at,updated_at) VALUES ('new-child','child-on-new','running',?,?)",
                (now, now),
            )
            catalog = [
                {"id": "new-father-script", "task_name": "reused-father-name", "working_directory": "/new",
                 "path": "/new/father.py", "model_yaml_name": "father.yaml", "model_yaml": "/new/yaml/father.yaml"},
                {"id": "new-child-script", "task_name": "child-on-new", "working_directory": "/new",
                 "path": "/new/child.py", "model_yaml_name": "child.yaml", "father_yaml_name": "father.yaml",
                 "father_yaml": "/new/yaml/father.yaml", "lineage_relation": "father"},
            ]
            conn.execute("UPDATE agents SET catalog_json=? WHERE id='slot-1'", (json.dumps(catalog),))
        self.store.reconcile_catalog_lineage()
        self.assertEqual(self.store.get_run("new-child")["baseline_run_id"], "right-parent")

    def test_detail_tabs_notes_theme_and_lineage_keep_existing_data(self):
        run = self.store.get_run("child")
        events = self.store.metric_events("child")
        detail = self.module.run_html(run, events)
        homepage = self.module.dashboard_html(self.store.list_runs())
        lineage = self.module.lineage_html(self.store.list_runs())
        self.assertIn("MCONG Lab", homepage)
        self.assertIn('data-tab="metrics"', detail)
        self.assertIn('data-tab="notes"', detail)
        self.assertIn('id="bestMapValue"', detail)
        self.assertIn('name="confirm_name"', detail)
        self.assertIn("Hypothesis", detail)
        self.assertIn("Modification", detail)
        self.assertIn("Conclusion", detail)
        self.assertIn('<select id="baselineRunId"', detail)
        self.assertIn('<option value="parent" selected>', detail)
        self.assertIn('Run child', detail)
        self.assertIn("PyTorch 2.0 · CUDA 11.7 · YOLO 8.1", detail)
        self.assertIn("mcong-lab-theme", detail)
        self.assertIn("CPD-003", lineage)
        self.assertIn("CPD-004", lineage)
        self.assertIn("67.42%", lineage)

    @unittest.skipUnless(shutil.which("node"), "Node.js is not available")
    def test_dashboard_and_detail_inline_scripts_parse(self):
        for page in (
            self.module.dashboard_html(self.store.list_runs()),
            self.module.run_html(self.store.get_run("child"), self.store.metric_events("child")),
        ):
            parser = Scripts()
            parser.feed(page)
            for script in parser.scripts:
                completed = subprocess.run(["node", "--check"], input=script, text=True, encoding="utf-8", capture_output=True)
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_delete_requires_password_and_typed_display_name(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.module.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        opener.open(urllib.request.Request(base + "/login", data=urllib.parse.urlencode({"password": "ui-test-password"}).encode()))
        def delete(confirm_name):
            data = urllib.parse.urlencode({"password": "ui-test-password", "confirm_name": confirm_name}).encode()
            return opener.open(urllib.request.Request(base + "/runs/child/delete", data=data))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            delete("CPD-999")
        self.assertEqual(caught.exception.code, 400)
        self.assertIsNotNone(self.store.get_run("child"))
        delete("CPD-004")
        self.assertIsNone(self.store.get_run("child"))


if __name__ == "__main__":
    unittest.main()
