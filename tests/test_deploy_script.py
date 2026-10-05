import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


class TestDeployScript(unittest.TestCase):
    """Runs scripts/deploy.sh against real git repositories; only docker and the health check are stand-ins."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        self.dev = self.tmp / "dev"
        self.server = self.tmp / "server"
        self.calls = self.tmp / "compose_calls.log"
        self.healthy = self.tmp / "healthy"
        shim = self.tmp / "compose-shim"
        shim.write_text(f"#!/usr/bin/env bash\necho \"$*\" >> {self.calls}\n")
        shim.chmod(0o755)

        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=self.tmp)
        self.git("clone", "-q", str(self.origin), str(self.dev), cwd=self.tmp)
        (self.dev / "scripts").mkdir()
        shutil.copy(REPO_ROOT / "scripts" / "deploy.sh", self.dev / "scripts" / "deploy.sh")
        (self.dev / "app.txt").write_text("version 1\n")
        self.commit("v1")
        self.git("clone", "-q", str(self.origin), str(self.server), cwd=self.tmp)
        self.v1 = self.head(self.server)
        self.env = dict(os.environ, **GIT_ENV, COMPOSE=str(shim), HEALTH_CMD=f"test -f {self.healthy}",
                        HEALTH_WAIT_SECONDS="0", LOCK_FILE=str(self.tmp / "deploy.lock"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def git(self, *args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                              env=dict(os.environ, **GIT_ENV)).stdout.strip()

    def head(self, repo):
        return self.git("rev-parse", "HEAD", cwd=repo)

    def commit(self, message):
        self.git("add", "-A", cwd=self.dev)
        self.git("commit", "-q", "-m", message, cwd=self.dev)
        self.git("push", "-q", "origin", "main", cwd=self.dev)
        return self.head(self.dev)

    def deploy(self):
        return subprocess.run([str(self.server / "scripts" / "deploy.sh")], cwd=self.server, env=self.env,
                              capture_output=True, text=True)

    def compose_calls(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_healthy_update_moves_server_to_new_version(self):
        (self.dev / "app.txt").write_text("version 2\n")
        v2 = self.commit("v2")
        self.healthy.touch()
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.head(self.server), v2)
        self.assertEqual((self.server / "app.txt").read_text(), "version 2\n")
        self.assertEqual(self.compose_calls(), ["up -d --build"])

    def test_unhealthy_update_rolls_back_and_alerts(self):
        (self.dev / "app.txt").write_text("broken version\n")
        self.commit("broken")
        result = self.deploy()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.head(self.server), self.v1)
        self.assertEqual((self.server / "app.txt").read_text(), "version 1\n")
        calls = self.compose_calls()
        self.assertEqual(calls[:2], ["up -d --build", "up -d --build"])
        self.assertTrue(calls[2].startswith("exec -T gateway python -m src.utils.alerts [Assistant] Automatic update FAILED"))

    def test_already_up_to_date_does_nothing(self):
        result = self.deploy()
        self.assertEqual(result.returncode, 0)
        self.assertIn("already up to date", result.stdout)
        self.assertEqual(self.compose_calls(), [])

    def test_local_server_edits_are_never_overwritten(self):
        (self.dev / "app.txt").write_text("version 2\n")
        self.commit("v2")
        (self.server / "app.txt").write_text("edited on the server\n")
        self.git("commit", "-q", "-am", "local edit", cwd=self.server)
        local = self.head(self.server)
        result = self.deploy()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.head(self.server), local)
        self.assertEqual((self.server / "app.txt").read_text(), "edited on the server\n")
        self.assertTrue(self.compose_calls()[0].startswith("exec -T gateway python -m src.utils.alerts"))


class TestDeployWorkflow(unittest.TestCase):
    def test_deploy_runs_only_after_tests_on_main(self):
        workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text())
        deploy = workflow["jobs"]["deploy"]
        self.assertEqual(deploy["needs"], "test")
        self.assertEqual(deploy["if"], "github.event_name == 'push' && github.ref == 'refs/heads/main'")
        script = deploy["steps"][0]["run"]
        self.assertIn("StrictHostKeyChecking=yes", script)
        self.assertIn("python -m unittest discover -s tests -t .", workflow["jobs"]["test"]["steps"][-1]["run"])


if __name__ == "__main__":
    unittest.main()
