"""Host-only rollout checks with fake Docker/Kubernetes; no server is contacted."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
OLD_IMAGE = "upstage-visits:" + OLD_SHA
NEW_IMAGE = "upstage-visits:" + NEW_SHA

MOCK = r'''
import io, json, os, pathlib, shutil, subprocess, sys, tarfile
sys.stdout.reconfigure(newline="\n")
def path(value):
    if os.name == "nt" and len(value) > 2 and value[0] == "/" and value[2] == "/":
        value = value[1] + ":" + value[2:]
    return pathlib.Path(value)
root = pathlib.Path(os.environ["VISIT_DEPLOY_TEST_ROOT"])
command, args = sys.argv[1], sys.argv[2:]
with (root / "commands.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps([command, *args]) + "\n")
case = os.environ.get("VISIT_DEPLOY_TEST_CASE", "success")
sha = os.environ["VISIT_DEPLOY_TEST_SHA"]
live = root / "live-image"
if command == "flock":
    sys.exit(1 if case == "locked" else 0)
if command == "python3":
    converted = [str(path(x)) if x.startswith("/") else x for x in args]
    sys.exit(subprocess.run([sys.executable, *converted]).returncode)
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
        if os.environ.get("VISIT_DEPLOY_TEST_DIFF_REPO"):
            pathspec = args[args.index("--") + 1:]
            sys.exit(subprocess.run([os.environ["VISIT_DEPLOY_TEST_REAL_GIT"], "-C",
                os.environ["VISIT_DEPLOY_TEST_DIFF_REPO"], "diff", "--quiet",
                "HEAD~1", "HEAD", "--", *pathspec]).returncode)
        sys.exit(1)
    elif "archive" in args:
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
            for name in ["visit_service/Dockerfile", "visit_service/app.py"]:
                if case == "missing-dockerfile" and name.endswith("Dockerfile"):
                    continue
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
    if args[:3] == ["get", "deployment", "upstage-visits"]:
        if "jsonpath={.metadata.name}" in args:
            print("upstage-visits" if live.read_text() else "", end="")
        else:
            print(live.read_text(), end="")
    elif args[:3] == ["get", "ingressroute", "upstage-visits-https"]:
        print("upstage-visits-https" if (root / "route").exists() else "", end="")
    elif args[:2] == ["apply", "-f"]:
        manifest = json.loads(path(args[2]).read_text())
        if manifest.get("kind") == "List":
            expected = {("PersistentVolumeClaim", "upstage-visits-data"),
                        ("Service", "upstage-visits"), ("Deployment", "upstage-visits")}
            if {(x["kind"], x["metadata"]["name"]) for x in manifest["items"]} != expected:
                sys.exit("Unexpected resource mutation")
            deployment = next(x for x in manifest["items"] if x["kind"] == "Deployment")
            live.write_text(deployment["spec"]["template"]["spec"]["containers"][0]["image"])
            (root / "base-applied").write_text(json.dumps(manifest))
        elif manifest.get("kind") == "IngressRoute" and manifest["metadata"]["name"] == "upstage-visits-https":
            (root / "route").write_text("installed")
            if case == "route-failure":
                sys.exit(1)
        else:
            sys.exit("Unexpected apply")
    elif args[:3] == ["set", "image", "deployment/upstage-visits"]:
        if len(args) != 4 or not args[3].startswith("app=upstage-visits:"):
            sys.exit("Unexpected image mutation")
        live.write_text(args[3].split("=", 1)[1])
    elif args[:3] == ["rollout", "status", "deployment/upstage-visits"]:
        if case == "rollout-failure" and live.read_text() != os.environ["VISIT_DEPLOY_TEST_OLD_IMAGE"]:
            sys.exit(1)
    elif args[:3] == ["delete", "ingressroute", "upstage-visits-https"]:
        (root / "route").unlink(missing_ok=True)
    elif args and args[0] == "exec" and "deployment/upstage-visits" in args:
        sys.stdin.buffer.read()
        if "backup" in args:
            if case == "backup-failure":
                sys.exit(1)
            shutil.copyfile(root / "visit-log", root / "snapshot-log")
        elif "live" in args:
            if case == "live-failure" and live.read_text() != os.environ["VISIT_DEPLOY_TEST_OLD_IMAGE"]:
                sys.exit(1)
            if (root / "snapshot-log").exists() and (root / "visit-log").read_bytes() != (root / "snapshot-log").read_bytes():
                sys.exit("Visit records changed")
        else:
            sys.exit("Unexpected verifier mode")
    else:
        sys.exit("Unexpected kubectl operation: " + repr(args))
    sys.exit(0)
sys.exit("Unexpected mock command")
'''


def shell_path(value):
    text = Path(value).as_posix()
    return "/" + text[0].lower() + text[2:] if os.name == "nt" else text


class VisitDeploymentRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = shutil.which("bash")
        if not cls.bash and os.name == "nt":
            candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
            cls.bash = str(candidate) if candidate.exists() else None
        cls.git = shutil.which("git")
        if not cls.bash or not cls.git:
            raise unittest.SkipTest("host deployment tests require Bash and Git")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="upstage-visits-deploy-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.deploy, self.state, self.bin = (self.root / x for x in ("managed", "state", "bin"))
        for folder in [self.deploy / "bin", self.deploy / "config", self.state, self.bin]:
            folder.mkdir(parents=True)
        (self.deploy / "bin" / "verify.py").write_text("# trusted fixture\n")
        self.base = {"kind": "List", "items": [
            {"kind": "PersistentVolumeClaim", "metadata": {"name": "upstage-visits-data", "namespace": "default"}},
            {"kind": "Service", "metadata": {"name": "upstage-visits", "namespace": "default"}},
            {"kind": "Deployment", "metadata": {"name": "upstage-visits", "namespace": "default"}, "spec": {
                "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0}},
                "template": {"spec": {"containers": [{"name": "app", "image": "upstage-visits:bootstrap"}]}}}},
        ]}
        (self.deploy / "config" / "base.json").write_text(json.dumps(self.base))
        self.route = {"kind": "IngressRoute", "metadata": {"name": "upstage-visits-https", "namespace": "default"},
                      "spec": {"routes": [{"services": [{"name": "upstage-visits", "port": 80}]}]}}
        (self.deploy / "config" / "route.json").write_text(json.dumps(self.route))
        self.initial_state = OLD_SHA + " " + OLD_IMAGE + "\n"
        (self.state / "deployed-state").write_text(self.initial_state, newline="\n")
        (self.root / "live-image").write_text(OLD_IMAGE)
        (self.root / "route").write_text("installed")
        self.history = b"existing visit records\n"
        (self.root / "visit-log").write_bytes(self.history)
        self.core = b"unchanged RAG image, question logs and route\n"
        (self.root / "core-resources").write_bytes(self.core)
        helper = self.root / "mock.py"
        helper.write_text(MOCK, encoding="utf-8")
        for name in ("git", "docker", "kubectl", "flock", "python3"):
            wrapper = self.bin / name
            wrapper.write_text('#!/bin/sh\nexec "$VISIT_DEPLOY_TEST_PYTHON" "$VISIT_DEPLOY_TEST_HELPER" '
                               + name + ' "$@"\n', encoding="utf-8", newline="\n")
            wrapper.chmod(0o755)
        source = Path(__file__).parents[1].joinpath("deploy.sh").read_text(encoding="utf-8")
        for before, after in {
            "DEPLOY_ROOT=/opt/upstage-bootcamp/visits": "DEPLOY_ROOT=" + shell_path(self.deploy),
            "STATE_ROOT=/var/lib/upstage-bootcamp-visits": "STATE_ROOT=" + shell_path(self.state),
        }.items():
            self.assertEqual(source.count(before), 1)
            source = source.replace(before, after)
        script = self.root / "deploy.sh"
        script.write_text(source, encoding="utf-8", newline="\n")
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        VISIT_DEPLOY_TEST_ROOT=str(self.root), VISIT_DEPLOY_TEST_HELPER=helper.as_posix(),
                        VISIT_DEPLOY_TEST_PYTHON=Path(sys.executable).as_posix(), VISIT_DEPLOY_TEST_SHA=NEW_SHA,
                        VISIT_DEPLOY_TEST_OLD_IMAGE=OLD_IMAGE, MSYS_NO_PATHCONV="1",
                        VISIT_DEPLOY_TEST_BIN=shell_path(self.bin), VISIT_DEPLOY_TEST_SCRIPT=shell_path(script))

    def initial(self):
        (self.state / "deployed-state").unlink()
        (self.root / "live-image").write_text("")
        (self.root / "route").unlink()

    def run_deploy(self, case="success", *arguments):
        self.env["VISIT_DEPLOY_TEST_CASE"] = case
        (self.root / "commands.jsonl").write_text("")
        result = subprocess.run([self.bash, "-c", 'export PATH="$VISIT_DEPLOY_TEST_BIN:$PATH"; '
                                 'source "$VISIT_DEPLOY_TEST_SCRIPT" "$@"', "visit-runner", *arguments],
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.output = result.stdout + result.stderr
        self.commands = [json.loads(x) for x in (self.root / "commands.jsonl").read_text().splitlines()]
        self.assertEqual((self.root / "core-resources").read_bytes(), self.core)
        self.assertEqual((self.root / "visit-log").read_bytes(), self.history)
        return result

    def patches(self):
        return [x for x in self.commands if x[:5] == ["kubectl", "--namespace", "default", "set", "image"]]

    def mutations(self):
        return [x for x in self.commands if x[0] == "kubectl" and x[3] in ("set", "apply", "delete")]

    def preserved(self):
        self.assertEqual((self.root / "live-image").read_text(), OLD_IMAGE)
        self.assertEqual((self.state / "deployed-state").read_text(), self.initial_state)

    def changed_files(self, names):
        repo = self.root / "diff-repository"
        repo.mkdir()
        def git(*args):
            subprocess.run([self.git, "-C", str(repo), *args], check=True, capture_output=True, timeout=15)
        git("init")
        (repo / "baseline").write_text("unchanged\n")
        git("add", ".")
        git("-c", "user.name=Deployment Test", "-c", "user.email=deploy@example.invalid", "-c", "commit.gpgsign=false", "commit", "--no-verify", "-m", "baseline")
        for name in names:
            p = repo / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("changed\n")
        git("add", ".")
        git("-c", "user.name=Deployment Test", "-c", "user.email=deploy@example.invalid", "-c", "commit.gpgsign=false", "commit", "--no-verify", "-m", "change")
        self.env.update(VISIT_DEPLOY_TEST_REAL_GIT=self.git, VISIT_DEPLOY_TEST_DIFF_REPO=str(repo))

    def test_initial_deploy_uses_only_own_resources_and_routes_after_health(self):
        self.initial()
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual((self.root / "live-image").read_text(), NEW_IMAGE)
        self.assertTrue((self.root / "route").exists())
        self.assertEqual((self.state / "deployed-state").read_text(), NEW_SHA + " " + NEW_IMAGE + "\n")
        self.assertEqual(self.patches(), [])
        applies = [i for i, x in enumerate(self.commands) if x[:4] == ["kubectl", "--namespace", "default", "apply"]]
        health = [i for i, x in enumerate(self.commands) if x[0] == "kubectl" and "live" in x]
        self.assertEqual(len(applies), 2)
        self.assertLess(applies[0], health[0])
        self.assertLess(health[0], applies[1])
        self.assertFalse((self.root / "snapshot-log").exists())
        self.assertEqual(json.loads((self.deploy / "config" / "base.json").read_text()), self.base)

    def test_update_changes_only_visit_image_after_backup(self):
        self.changed_files(["visit_service/app.py"])
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual(self.patches(), [["kubectl", "--namespace", "default", "set", "image", "deployment/upstage-visits", "app=" + NEW_IMAGE]])
        self.assertEqual(self.mutations(), self.patches())
        self.assertEqual((self.root / "snapshot-log").read_bytes(), self.history)
        self.assertEqual((self.state / "deployed-state").read_text(), NEW_SHA + " " + NEW_IMAGE + "\n")

    def test_unrelated_main_change_skips_without_relabel(self):
        self.changed_files(["src/local_rag_service.py", "visit_service_extra/unknown.txt"])
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, self.output)
        self.assertIn("Visit service inputs unchanged", self.output)
        self.preserved()
        self.assertEqual(self.mutations(), [])
        self.assertFalse(any(x[:2] == ["docker", "build"] for x in self.commands))

    def test_preflight_failures_do_not_change_any_resource(self):
        for case in ["build-failure", "candidate-failure", "backup-failure", "missing-dockerfile", "rewritten-history", "diff-error"]:
            with self.subTest(case=case):
                (self.state / "failed-sha").unlink(missing_ok=True)
                result = self.run_deploy(case)
                self.assertNotEqual(result.returncode, 0, self.output)
                self.preserved()
                self.assertEqual(self.mutations(), [])
                self.assertEqual((self.state / "failed-sha").read_text().strip(), NEW_SHA)

    def test_failed_update_restores_only_previous_visit_image(self):
        for case in ["rollout-failure", "live-failure"]:
            with self.subTest(case=case):
                (self.state / "failed-sha").unlink(missing_ok=True)
                result = self.run_deploy(case)
                self.assertNotEqual(result.returncode, 0, self.output)
                self.preserved()
                self.assertEqual([x[-1] for x in self.patches()], ["app=" + NEW_IMAGE, "app=" + OLD_IMAGE])
                self.assertEqual(self.mutations(), self.patches())
                self.assertTrue((self.root / "route").exists())

    def test_failed_initial_route_is_removed_but_database_resources_are_retained(self):
        self.initial()
        result = self.run_deploy("route-failure")
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertFalse((self.root / "route").exists())
        self.assertTrue((self.root / "base-applied").exists())
        self.assertFalse((self.state / "deployed-state").exists())
        deletes = [x for x in self.mutations() if x[3] == "delete"]
        self.assertEqual(deletes, [["kubectl", "--namespace", "default", "delete", "ingressroute", "upstage-visits-https", "--ignore-not-found"]])
        result = self.run_deploy("success", "--retry")
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertIn("exists without deployed-state", self.output)
        self.assertEqual(self.mutations(), [])

    def test_failed_initial_health_never_exposes_route(self):
        self.initial()
        result = self.run_deploy("live-failure")
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertFalse((self.root / "route").exists())
        self.assertTrue((self.root / "base-applied").exists())
        self.assertFalse((self.state / "deployed-state").exists())
        self.assertEqual(len(self.mutations()), 1)

    def test_unrelated_resource_in_trusted_base_is_rejected(self):
        self.initial()
        self.base["items"][1]["metadata"]["name"] = "upstage-bootcamp"
        (self.deploy / "config" / "base.json").write_text(json.dumps(self.base))
        result = self.run_deploy()
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertIn("only the three visit resources", self.output)
        self.assertEqual(self.mutations(), [])

    def test_check_modes_are_read_only(self):
        result = self.run_deploy("success", "--check")
        self.assertEqual(result.returncode, 0, self.output)
        self.preserved()
        self.assertEqual(self.mutations(), [])
        self.assertFalse((self.deploy / "repository.git").exists())
        self.initial()
        result = self.run_deploy("success", "--check")
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual(self.mutations(), [])
        self.assertFalse((self.state / "deployed-state").exists())

    def test_same_commit_or_lock_is_noop(self):
        self.env["VISIT_DEPLOY_TEST_SHA"] = OLD_SHA
        for case in ["success", "locked"]:
            result = self.run_deploy(case)
            self.assertEqual(result.returncode, 0, self.output)
            self.preserved()
            self.assertEqual(self.mutations(), [])

    def test_failed_commit_requires_explicit_retry(self):
        (self.state / "failed-sha").write_text(NEW_SHA + "\n", newline="\n")
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, self.output)
        self.preserved()
        self.assertEqual(self.mutations(), [])
        result = self.run_deploy("success", "--retry")
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual(len(self.patches()), 1)
        self.assertFalse((self.state / "failed-sha").exists())

    def test_unmanaged_existing_image_is_not_overwritten(self):
        (self.root / "live-image").write_text("upstage-visits:" + "c" * 40)
        result = self.run_deploy()
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertIn("unmanaged change", self.output)
        self.assertEqual(self.mutations(), [])
        self.assertEqual((self.state / "deployed-state").read_text(), self.initial_state)


if __name__ == "__main__":
    unittest.main(verbosity=2)
