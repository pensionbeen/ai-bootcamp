"""Exercise deployment decisions with fake external commands; never contact a server."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
OLD_IMAGE = "upstage-bootcamp:8208d87"
NEW_IMAGE = "upstage-bootcamp:" + NEW_SHA

MOCK = r'''
import io, json, os, pathlib, shutil, subprocess, sys, tarfile
sys.stdout.reconfigure(newline="\n")

def path(value):
    if os.name == "nt" and len(value) > 2 and value[0] == "/" and value[2] == "/":
        value = value[1] + ":" + value[2:]
    return pathlib.Path(value)

root = pathlib.Path(os.environ["DEPLOY_TEST_ROOT"])
command, args = sys.argv[1], sys.argv[2:]
with (root / "commands.jsonl").open("a", encoding="utf-8") as handle:
    handle.write(json.dumps([command, *args]) + "\n")
case = os.environ.get("DEPLOY_TEST_CASE", "success")
sha = os.environ["DEPLOY_TEST_SHA"]
live = root / "live-image"
if command == "flock":
    sys.exit(0)
if command == "git":
    if "ls-remote" in args:
        print(sha + "\trefs/heads/main")
    elif "init" in args:
        path(args[-1]).mkdir(parents=True)
    elif "get-url" in args:
        print("https://github.com/pensionbeen/ai-bootcamp.git")
    elif "rev-parse" in args:
        print(sha)
    elif "merge-base" in args:
        sys.exit(1 if case == "rewritten-history" else 0)
    elif "diff" in args:
        if case == "diff-error":
            sys.exit(128)
        if os.environ.get("DEPLOY_TEST_DIFF_REPO"):
            # Exercise the runner's actual Git pathspec against real commits.
            diff_args = args[args.index("diff"):]
            pathspec = diff_args[diff_args.index("--") + 1:]
            result = subprocess.run([os.environ["DEPLOY_TEST_REAL_GIT"], "-C",
                                     os.environ["DEPLOY_TEST_DIFF_REPO"], "diff", "--quiet",
                                     "HEAD~1", "HEAD", "--", *pathspec])
            sys.exit(result.returncode)
        sys.exit(1)
    elif "archive" in args:
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
            names = ["deploy/Dockerfile", "src/request_audit.py", "src/web/request-log.html",
                     "src/web/request-log.mjs", "src/web/test.html"]
            if case == "missing-logger":
                names.remove("src/request_audit.py")
            for name in names:
                data = b"fixture\n"
                item = tarfile.TarInfo(name)
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))
    sys.exit(0)
if command == "docker":
    if args[:2] == ["image", "inspect"]:
        print(sha)
    elif args and args[0] == "run":
        sys.stdin.buffer.read()
        sys.exit(1 if case == "candidate-failure" else 0)
    elif args and args[0] == "build" and case == "build-failure":
        sys.exit(1)
    sys.exit(0)
if command == "kubectl":
    if args[:2] != ["--namespace", "default"]:
        sys.exit("Unexpected namespace")
    args = args[2:]
    if args[:3] == ["get", "deployment", "upstage-bootcamp"]:
        print(live.read_text(), end="")
    elif args[:3] == ["set", "image", "deployment/upstage-bootcamp"]:
        if len(args) != 4 or not args[3].startswith("app=upstage-bootcamp:"):
            sys.exit("Unexpected image mutation")
        live.write_text(args[3].split("=", 1)[1])
    elif args[:3] == ["rollout", "status", "deployment/upstage-bootcamp"]:
        if case == "rollout-failure" and live.read_text() != os.environ["DEPLOY_TEST_OLD_IMAGE"]:
            sys.exit(1)
    elif args and args[0] == "exec":
        sys.stdin.buffer.read()
        if "backup" in args:
            if case == "backup-failure":
                sys.exit(1)
            shutil.copyfile(root / "persistent-log", root / "snapshot-log")
        elif "live" in args:
            if case == "live-failure" and live.read_text() != os.environ["DEPLOY_TEST_OLD_IMAGE"]:
                sys.exit(1)
            if (root / "persistent-log").read_bytes() != (root / "snapshot-log").read_bytes():
                sys.exit("Historical records changed")
        else:
            sys.exit("Unexpected exec mode")
    else:
        sys.exit("Unexpected kubectl operation: " + repr(args))
    sys.exit(0)
sys.exit("Unexpected mock command")
'''


def shell_path(value):
    text = Path(value).as_posix()
    if os.name == "nt":
        text = "/" + text[0].lower() + text[2:]
    return text


class DeploymentRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = shutil.which("bash")
        if not cls.bash and os.name == "nt":
            candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
            if candidate.exists():
                cls.bash = str(candidate)
        if not cls.bash:
            raise unittest.SkipTest("bash is required for deployment runner tests")
        cls.git = shutil.which("git")
        if not cls.git:
            raise unittest.SkipTest("git is required for deployment runner tests")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="upstage-deploy-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.deploy = self.root / "managed"
        self.state = self.root / "state"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.deploy / "bin").mkdir(parents=True)
        (self.deploy / "bin" / "verify.py").write_text("# trusted verifier fixture\n")
        self.state.mkdir()
        self.initial_state = OLD_SHA + " " + OLD_IMAGE + "\n"
        (self.state / "deployed-state").write_text(self.initial_state, newline="\n")
        (self.root / "live-image").write_text(OLD_IMAGE)
        self.history = b"existing request IDs, questions and response records\n"
        (self.root / "persistent-log").write_bytes(self.history)
        helper = self.root / "mock.py"
        helper.write_text(textwrap.dedent(MOCK), encoding="utf-8")
        for name in ("git", "docker", "kubectl", "flock"):
            wrapper = self.bin / name
            wrapper.write_text('#!/bin/sh\nexec "$DEPLOY_TEST_PYTHON" "$DEPLOY_TEST_HELPER" '
                               + name + ' "$@"\n', encoding="utf-8", newline="\n")
            wrapper.chmod(0o755)
        source = Path(__file__).with_name("deploy.sh").read_text(encoding="utf-8")
        assignments = {
            "DEPLOY_ROOT=/opt/upstage-bootcamp/autodeploy": "DEPLOY_ROOT=" + shell_path(self.deploy),
            "STATE_ROOT=/var/lib/upstage-bootcamp-deploy": "STATE_ROOT=" + shell_path(self.state),
        }
        for before, after in assignments.items():
            self.assertEqual(source.count(before), 1, "Test fixture must replace each managed root exactly once")
            source = source.replace(before, after)
        self.script = self.root / "deploy.sh"
        self.script.write_text(source, encoding="utf-8", newline="\n")
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        DEPLOY_TEST_ROOT=str(self.root), DEPLOY_TEST_HELPER=helper.as_posix(),
                        DEPLOY_TEST_PYTHON=Path(sys.executable).as_posix(), DEPLOY_TEST_SHA=NEW_SHA,
                        DEPLOY_TEST_OLD_IMAGE=OLD_IMAGE, MSYS_NO_PATHCONV="1",
                        DEPLOY_TEST_BIN=shell_path(self.bin), DEPLOY_TEST_SCRIPT=shell_path(self.script))

    def run_deploy(self, case="success", *arguments):
        self.env["DEPLOY_TEST_CASE"] = case
        result = subprocess.run([self.bash, "-c", 'export PATH="$DEPLOY_TEST_BIN:$PATH"; '
                                 'source "$DEPLOY_TEST_SCRIPT" "$@"', "test-runner", *arguments], env=self.env,
                                capture_output=True, text=True, timeout=30)
        self.output = result.stdout + result.stderr
        recorded = self.root / "commands.jsonl"
        self.commands = [json.loads(line) for line in recorded.read_text().splitlines()] if recorded.exists() else []
        return result

    def patches(self):
        return [call for call in self.commands if call[:5] == ["kubectl", "--namespace", "default", "set", "image"]]

    def changed_files(self, names):
        """Supply actual Git changes without allowing any production commands."""
        repository = self.root / "diff-repository"
        repository.mkdir()
        def git(*args):
            subprocess.run([self.git, "-C", str(repository), *args], check=True,
                           capture_output=True, text=True, timeout=15)
        git("init")
        (repository / "baseline").write_text("unchanged\n")
        git("add", ".")
        git("-c", "user.name=Deployment Test", "-c", "user.email=deploy@example.invalid",
            "commit", "--no-verify", "-m", "baseline")
        for name in names:
            target = repository / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("changed\n")
        git("add", ".")
        git("-c", "user.name=Deployment Test", "-c", "user.email=deploy@example.invalid",
            "commit", "--no-verify", "-m", "change")
        self.env.update(DEPLOY_TEST_REAL_GIT=self.git, DEPLOY_TEST_DIFF_REPO=str(repository))

    def assert_preserved(self):
        self.assertEqual((self.root / "persistent-log").read_bytes(), self.history)
        self.assertEqual((self.root / "live-image").read_text(), OLD_IMAGE)
        self.assertEqual((self.state / "deployed-state").read_text(), self.initial_state)

    def test_success_changes_only_app_image_and_preserves_history(self):
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual(self.patches(), [["kubectl", "--namespace", "default", "set", "image",
                                          "deployment/upstage-bootcamp", "app=" + NEW_IMAGE]])
        self.assertEqual((self.state / "deployed-state").read_text(), NEW_SHA + " " + NEW_IMAGE + "\n")
        self.assertEqual((self.root / "persistent-log").read_bytes(), self.history)
        self.assertEqual((self.root / "snapshot-log").read_bytes(), self.history)
        self.assertFalse((self.state / "failed-sha").exists())

    def test_failed_checks_do_not_replace_running_application(self):
        for case in ("build-failure", "candidate-failure", "backup-failure", "missing-logger", "rewritten-history"):
            with self.subTest(case=case):
                if (self.state / "failed-sha").exists():
                    (self.state / "failed-sha").unlink()
                (self.root / "commands.jsonl").write_text("")
                result = self.run_deploy(case)
                self.assertNotEqual(result.returncode, 0, self.output)
                self.assertEqual(self.patches(), [])
                self.assert_preserved()
                self.assertEqual((self.state / "failed-sha").read_text().strip(), NEW_SHA)

    def test_rollout_or_live_failure_restores_only_previous_image(self):
        for case in ("rollout-failure", "live-failure"):
            with self.subTest(case=case):
                if (self.state / "failed-sha").exists():
                    (self.state / "failed-sha").unlink()
                (self.root / "commands.jsonl").write_text("")
                result = self.run_deploy(case)
                self.assertNotEqual(result.returncode, 0, self.output)
                self.assertEqual([call[-1] for call in self.patches()], ["app=" + NEW_IMAGE, "app=" + OLD_IMAGE])
                self.assert_preserved()
                self.assertEqual((self.root / "snapshot-log").read_bytes(), self.history)

    def test_current_commit_is_noop(self):
        self.env["DEPLOY_TEST_SHA"] = OLD_SHA
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, self.output)
        self.assert_preserved()
        self.assertFalse(any(call[:2] in (["docker", "run"], ["docker", "build"]) for call in self.commands))
        self.assertEqual(self.patches(), [])

    def test_visit_only_change_does_not_relabel_or_restart_rag(self):
        self.changed_files(["visit_service/server.py", "visit_service/web/admin.html"])
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, self.output)
        self.assertIn("RAG application inputs unchanged", self.output)
        self.assert_preserved()
        self.assertEqual(self.patches(), [])
        self.assertFalse((self.root / "snapshot-log").exists())
        self.assertFalse((self.state / "failed-sha").exists())
        self.assertFalse(any(call[:2] in (["docker", "run"], ["docker", "build"]) for call in self.commands))
        diff = next(call for call in self.commands if call[0] == "git" and "diff" in call)
        self.assertEqual(diff[diff.index("--quiet") + 1:diff.index("--")], [OLD_SHA, NEW_SHA])

    def test_runner_only_change_does_not_restart_rag(self):
        self.changed_files(["deploy/deploy.sh", "deploy/test_deploy_runner.py", "deploy/README.md"])
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, self.output)
        self.assertIn("RAG application inputs unchanged", self.output)
        self.assert_preserved()
        self.assertEqual(self.patches(), [])
        self.assertFalse(any(call[:2] == ["docker", "build"] for call in self.commands))

    def test_mixed_visit_and_application_change_still_deploys(self):
        self.changed_files(["visit_service/server.py", "src/local_rag_service.py"])
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual([call[-1] for call in self.patches()], ["app=" + NEW_IMAGE])
        self.assertEqual((self.state / "deployed-state").read_text(), NEW_SHA + " " + NEW_IMAGE + "\n")

    def test_new_unknown_file_is_not_exempt_from_deployment(self):
        self.changed_files(["visit_service_extra/config.json"])
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual([call[-1] for call in self.patches()], ["app=" + NEW_IMAGE])

    def test_visit_only_change_still_requires_running_revision_ancestor(self):
        self.changed_files(["visit_service/server.py"])
        result = self.run_deploy("rewritten-history")
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertIn("history rewrite", self.output)
        self.assert_preserved()
        self.assertEqual(self.patches(), [])
        self.assertFalse(any(call[0] == "git" and "diff" in call for call in self.commands))

    def test_diff_failure_is_not_treated_as_unchanged_or_deployed(self):
        result = self.run_deploy("diff-error")
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertIn("git diff exit 128", self.output)
        self.assert_preserved()
        self.assertEqual(self.patches(), [])
        self.assertFalse(any(call[:2] == ["docker", "build"] for call in self.commands))
        self.assertEqual((self.state / "failed-sha").read_text().strip(), NEW_SHA)

    def test_check_mode_reads_access_without_deploying(self):
        result = self.run_deploy("success", "--check")
        self.assertEqual(result.returncode, 0, self.output)
        self.assert_preserved()
        self.assertEqual(self.patches(), [])
        self.assertFalse((self.root / "snapshot-log").exists())
        self.assertFalse((self.deploy / "repository.git").exists())
        self.assertFalse(any(call[:2] in (["docker", "run"], ["docker", "build"]) for call in self.commands))
        self.assertFalse(any(call[0] == "git" and "diff" in call for call in self.commands))

    def test_unmanaged_live_image_is_never_replaced(self):
        changed = "upstage-bootcamp:ccccccc"
        (self.root / "live-image").write_text(changed)
        result = self.run_deploy()
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertIn("unmanaged change", self.output)
        self.assertEqual(self.patches(), [])
        self.assertEqual((self.root / "live-image").read_text(), changed)
        self.assertEqual((self.root / "persistent-log").read_bytes(), self.history)
        self.assertEqual((self.state / "deployed-state").read_text(), self.initial_state)
        self.assertFalse(any(call[:2] in (["docker", "run"], ["docker", "build"]) for call in self.commands))

    def test_failed_commit_waits_for_explicit_retry(self):
        (self.state / "failed-sha").write_text(NEW_SHA + "\n", newline="\n")
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, self.output)
        self.assert_preserved()
        self.assertEqual(self.patches(), [])
        self.assertFalse(any(call[:2] == ["docker", "build"] for call in self.commands))
        result = self.run_deploy("success", "--retry")
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual(len(self.patches()), 1)
        self.assertFalse((self.state / "failed-sha").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
