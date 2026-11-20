"""The public notice contributes to prompts and must invalidate audit identity."""
import hashlib
import os
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch, Mock

import ask_local_rag

IDENTITY_MODULES = ("search_local_rag", "local_rag_answer", "local_rag_client", "public_rag_response",
                    "rag_numeric_amounts", "release_policy", *ask_local_rag.SOURCE_SELECTION_MODULES)
DB = {"database_sha256": "a" * 64, "identity": "test-db", "as_of_date": "2026-10-01"}


class PublicDependencyIdentityTests(unittest.TestCase):
    def test_question_aware_retrieval_and_single_generation_are_cached_without_regeneration(self):
        for forbidden_expansion in (False,True):
            with self.subTest(forbidden_expansion=forbidden_expansion), tempfile.TemporaryDirectory(
                    dir=Path(__file__).resolve().parents[1],prefix='single-generation-test-') as folder:
                project=Path(folder)
                modules={}
                for name in IDENTITY_MODULES:
                    path=project/(name+'.py');path.write_text('# test identity\n',encoding='utf-8')
                    modules[name]=types.SimpleNamespace(__file__=str(path))
                retrieval={'top_k':8,'evidence':[]}
                modules['search_local_rag'].retrieve=Mock(return_value=retrieval)
                client=types.SimpleNamespace(called=0,calls=[])
                def request(kind,payload):
                    client.called+=1;client.calls.append({'kind':kind,'at':time.monotonic()})
                    return {'model':'solar-embedding-2-query','data':[{'index':0,'embedding':[1.0]*1024}]},{'kind':kind}
                client.request=request
                def generate(question,evidence,injected,started=None):
                    injected.request('draft',{})
                    return {'request_more_evidence':forbidden_expansion,'draft_status':'code_checked_draft'}
                modules['local_rag_answer'].assess_and_answer=Mock(side_effect=generate)
                db={'database_sha256':'a'*64,'identity':'test-db','as_of_date':'2026-10-01'}
                with patch.object(ask_local_rag,'database_identity',return_value=db), \
                     patch.object(ask_local_rag.importlib,'import_module',side_effect=modules.__getitem__), \
                     patch.object(ask_local_rag,'Client',return_value=client):
                    result=ask_local_rag.run_question(project,'별표 1의 정의는?')
                    self.assertEqual(modules['local_rag_answer'].assess_and_answer.call_count,1)
                    # The provision-check deadline starts before embedding and retrieval.
                    started=modules['local_rag_answer'].assess_and_answer.call_args.kwargs['started']
                    self.assertIs(type(started),float)
                    self.assertLessEqual(started,client.calls[0]['at'])
                    self.assertEqual(modules['search_local_rag'].retrieve.call_count,1)
                    options=modules['search_local_rag'].retrieve.call_args.kwargs
                    self.assertEqual(options,{'top_k':8,'include_deleted':False,'question':'별표 1의 정의는?'})
                    self.assertEqual([entry['kind'] for entry in client.calls],['embedding','draft'])
                    if forbidden_expansion:
                        self.assertEqual(result['status'],'processing_error')
                        self.assertEqual(result['error'],'runtime_requested_forbidden_regeneration')
                    else:
                        self.assertEqual(result['status'],'completed',result)
                        self.assertEqual(result['schema_version'],'local-rag-single-question-v1')
                        cached=ask_local_rag.run_question(project,'별표 1의 정의는?')
                        self.assertTrue(cached['cached']);self.assertEqual(cached['called'],0)
                        self.assertEqual(modules['local_rag_answer'].assess_and_answer.call_count,1)

    def test_notice_source_change_invalidates_check_only_audit_without_api(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix="identity-test-") as folder:
            project = Path(folder)
            modules = {}
            for name in IDENTITY_MODULES:
                path = project / (name + ".py")
                path.write_text("NOTICE = 'original'\n", encoding="utf-8")
                modules[name] = types.SimpleNamespace(__file__=str(path))
            db = {"database_sha256": "a" * 64, "identity": "test-db", "as_of_date": "2026-10-01"}
            with patch.object(ask_local_rag, "database_identity", return_value=db), \
                    patch.object(ask_local_rag.importlib, "import_module", side_effect=modules.__getitem__), \
                    patch.object(ask_local_rag.Client, "request", side_effect=AssertionError("API forbidden")), \
                    patch.object(ask_local_rag.Client, "_api_key", side_effect=AssertionError("key forbidden")):
                first = ask_local_rag.run_question(project, "같은 질문", check_only=True)
                notice_file = Path(modules["public_rag_response"].__file__)
                notice_file.write_text("NOTICE = 'changed'\n", encoding="utf-8")
                second = ask_local_rag.run_question(project, "같은 질문", check_only=True)
                selector = Path(modules["source_first_answer"].__file__)
                selector.write_text("# updated source selection\n", encoding="utf-8")
                third = ask_local_rag.run_question(project, "같은 질문", check_only=True)
            self.assertEqual(first["status"], "check_only")
            self.assertEqual(second["status"], "check_only")
            self.assertEqual(first["called"], second["called"])
            self.assertEqual(second["called"], 0)
            self.assertIn("public_rag_response.py", first["code"]["files"])
            self.assertEqual(second["code"]["files"]["public_rag_response.py"],
                             hashlib.sha256(notice_file.read_bytes()).hexdigest())
            self.assertNotEqual(first["run_identity"], second["run_identity"])
            self.assertNotEqual(first["output_paths"], second["output_paths"])
            self.assertNotEqual(second["run_identity"], third["run_identity"])
            self.assertEqual(third["called"], 0)
            self.assertFalse((project / "data").exists())


class ClientOptionsIdentityTests(unittest.TestCase):
    """Service client options reach Client only; question identity, audit and module list are unchanged."""

    def run_with(self, project, **options):
        modules = {}
        for name in IDENTITY_MODULES:
            path = project / (name + ".py")
            path.write_text("# identity copy of " + name + "\n", encoding="utf-8")
            modules[name] = types.SimpleNamespace(__file__=str(path))
        modules["search_local_rag"].retrieve = Mock(return_value={"top_k": 8, "evidence": []})
        client = types.SimpleNamespace(called=0, calls=[])
        def request(kind, payload):
            client.called += 1
            client.calls.append({"kind": kind})
            return {"model": "solar-embedding-2-query", "data": [{"index": 0, "embedding": [1.0] * 1024}]}, {"kind": kind}
        client.request = request
        def generate(question, evidence, injected, started=None):
            injected.request("draft", {})
            return {"request_more_evidence": False, "draft_status": "code_checked_draft"}
        modules["local_rag_answer"].assess_and_answer = Mock(side_effect=generate)
        factory = Mock(return_value=client)
        with patch.object(ask_local_rag, "database_identity", return_value=DB), \
                patch.object(ask_local_rag.importlib, "import_module", side_effect=modules.__getitem__), \
                patch.object(ask_local_rag, "Client", factory):
            result = ask_local_rag.run_question(project, "같은 질문", **options)
        return result, factory

    def test_default_client_options_keep_client_call_audit_and_identity(self):
        from local_rag_client import SendGate
        options = {"parallel": True, "deadline_seconds": 75, "gate": SendGate()}
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix="identity-test-") as a, \
                tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix="identity-test-") as b:
            plain, plain_factory = self.run_with(Path(a))
            with_options, options_factory = self.run_with(Path(b), client_options=options)
            plain_input = (Path(plain["output_paths"]["output_folder"]) / "input.json").read_bytes()
            options_input = (Path(with_options["output_paths"]["output_folder"]) / "input.json").read_bytes()
        # Default: exactly the previous Client(project, env_file=...) call.
        plain_factory.assert_called_once_with(Path(a).resolve(), env_file=None)
        options_factory.assert_called_once_with(Path(b).resolve(), env_file=None, **options)
        self.assertEqual((plain["status"], with_options["status"]), ("completed", "completed"))
        self.assertEqual(plain["run_identity"], with_options["run_identity"])
        self.assertEqual(plain["code"], with_options["code"])
        self.assertEqual(set(plain["code"]["files"]), {name + ".py" for name in IDENTITY_MODULES} | {"ask_local_rag.py"})
        self.assertEqual(plain_input, options_input)
        # The audit differs only in the output paths (different temporary projects).
        def strip(audit):
            return {key: value for key, value in audit.items() if key != "output_paths"}
        self.assertEqual(strip(plain), strip(with_options))
        self.assertNotIn("deadline", plain_input.decode("utf-8"))
        self.assertNotIn("parallel", plain_input.decode("utf-8"))


class RateLimitedDraftIsNotSavedTests(unittest.TestCase):
    """A draft stopped only by a skipped HTTP 429 wait is not pinned as a saved processing_error.

    The real Client (fake transport, no API) runs inside the real run_question. The
    answer module is a stand-in mirroring assess_and_answer: a failed draft request
    gives decision processing_error, a failed provision check publishes the draft.
    """

    QUESTION = "속도 제한 질문"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix="identity-test-")
        self.project = Path(self.temp.name)
        self.now = 1000.0
        self.sleeps = []
        self.sent = []

    def tearDown(self):
        self.temp.cleanup()

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def modules(self):
        modules = {}
        for name in IDENTITY_MODULES:
            path = self.project / (name + ".py")
            path.write_text("# identity copy of " + name + "\n", encoding="utf-8")
            modules[name] = types.SimpleNamespace(__file__=str(path))
        modules["search_local_rag"].retrieve = Mock(return_value={"top_k": 8, "evidence": []})
        from local_rag_client import CHAT_MODEL
        def assess_and_answer(question, retrieval, client, started=None):
            try:
                client.request("draft", {"model": CHAT_MODEL, "messages": [{"role": "user", "content": question}]})
            except Exception:
                return {"decision": "processing_error", "error_code": "draft_request_failed"}
            try:
                client.request("provision_check", {"model": CHAT_MODEL, "messages": [
                    {"role": "user", "content": "확인 " + question}]}, timeout=40)
            except Exception:
                pass  # fail-open: the code-checked draft is published
            return {"decision": "supported", "draft_status": "code_checked_draft"}
        modules["local_rag_answer"].assess_and_answer = assess_and_answer
        return modules

    def transport(self, draft_statuses, check_statuses=()):
        import json
        from local_rag_client import CHAT_MODEL, QUERY_MODEL
        drafts, checks = list(draft_statuses), list(check_statuses)
        class Response:
            def __init__(self, status, body, retry_after=None):
                self.status_code, self.content = status, json.dumps(body).encode()
                self.headers = {"Retry-After": retry_after} if retry_after else {}
            def close(self):
                pass
        def send(url, headers=None, data=None, timeout=None, allow_redirects=None):
            body = json.loads(data)
            if body["model"] == QUERY_MODEL:
                self.sent.append("embedding")
                return Response(200, {"model": QUERY_MODEL, "data": [{"index": 0, "embedding": [1.0] * 1024}]})
            kind = "check" if body["messages"][0]["content"].startswith("확인") else "draft"
            self.sent.append(kind)
            status = (checks if kind == "check" else drafts).pop(0)
            return Response(status, {"model": CHAT_MODEL, "choices": []}, "120" if status == 429 else None)
        return send

    def run_once(self, attempt_id=None, **client_options):
        with patch.object(ask_local_rag, "database_identity", return_value=DB), \
                patch.object(ask_local_rag.importlib, "import_module", side_effect=self.modules().__getitem__), \
                patch.dict(os.environ, {"UPSTAGE_API_KEY": "fake-unit-test-secret"}):
            return ask_local_rag.run_question(self.project, self.QUESTION, client_options=client_options or None,
                                              attempt_id=attempt_id)

    def test_new_submission_reuses_only_embedding_even_after_a_legacy_answer(self):
        transport = self.transport([200] * 3, check_statuses=[200] * 3)
        legacy = self.run_once(transport=transport)
        first = self.run_once(attempt_id="first", transport=transport)
        repeated = self.run_once(attempt_id="second", transport=transport)
        self.assertEqual(len({a["run_identity"] for a in (legacy, first, repeated)}), 3)
        self.assertEqual(self.sent, ["embedding", "draft", "check", "draft", "check", "draft", "check"])
        same_job = self.run_once(attempt_id="first", transport=transport)
        self.assertTrue(same_job["cached"])
        self.assertEqual(same_job["called"], 0)
        self.assertEqual(same_job["output_paths"], first["output_paths"])

    def test_deadline_mode_draft_429_is_not_saved_and_the_rerun_sends_attempt_two(self):
        options = {"parallel": True, "deadline_seconds": 75, "sleeper": self.sleep, "clock": lambda: self.now}
        # Retry-After 120 -> bounded 60 s wait + 30 s minimum does not fit the 75 s deadline.
        first = self.run_once(transport=self.transport([429]), **options)
        self.assertEqual((first["status"], first["error"]), ("processing_error", "draft_rate_limited_retry_later"))
        self.assertEqual(first["called"], 2)
        folder = Path(first["output_folder"])
        self.assertFalse((folder / "audit.json").exists())
        self.assertFalse((folder / "audit.manifest.json").exists())
        self.assertEqual((self.sent, self.sleeps), (["embedding", "draft"], []))
        # Later (the 60 s wait has passed) the same question sends draft attempt 2 and is saved.
        self.now += 60
        second = self.run_once(transport=self.transport([200], check_statuses=[200]), **options)
        self.assertEqual(second["status"], "completed", second)
        self.assertEqual(second["result"]["decision"], "supported")
        self.assertTrue((folder / "audit.json").exists())
        self.assertEqual(self.sent, ["embedding", "draft", "draft", "check"])
        drafts = [path.parent for path in self.project.rglob("attempt-2.response.json")]
        self.assertEqual(len(drafts), 1)
        self.assertEqual(self.sleeps, [])

    def test_default_options_save_a_published_draft_whose_check_skipped_the_429_wait(self):
        with patch("requests.post", self.transport([200], check_statuses=[429])):
            result = self.run_once()
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["result"]["decision"], "supported")
        self.assertTrue(Path(result["output_paths"]["audit"]).exists())
        self.assertEqual(self.sent, ["embedding", "draft", "check"])


class PublicationModuleIdentityTests(unittest.TestCase):
    """Numeric checks and releasable issues decide what is published, so they are part of the code identity."""

    def check_only(self, project):
        with patch.object(ask_local_rag, "database_identity", return_value=DB), \
                patch.object(ask_local_rag.Client, "request", side_effect=AssertionError("API forbidden")), \
                patch.object(ask_local_rag.Client, "_api_key", side_effect=AssertionError("key forbidden")):
            return ask_local_rag.run_question(project, "같은 질문", check_only=True)

    def test_runtime_identity_lists_numeric_and_release_policy_files(self):
        import rag_numeric_amounts
        import release_policy
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix="identity-test-") as folder:
            result = self.check_only(Path(folder))  # real importlib.import_module
            self.assertFalse((Path(folder) / "data").exists())
        self.assertEqual(result["status"], "check_only")
        self.assertEqual(result["called"], 0)
        files = result["code"]["files"]
        for module in (rag_numeric_amounts, release_policy):
            name = Path(module.__file__).name
            self.assertIn(name, files)
            self.assertEqual(files[name], hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest())
        self.assertEqual(set(files), {name + ".py" for name in IDENTITY_MODULES} | {"ask_local_rag.py"})

    def test_release_policy_bytes_change_code_hash(self):
        import release_policy
        real = Path(release_policy.__file__)
        original = real.read_bytes()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix="identity-test-") as folder:
            project = Path(folder)
            modules = {}
            for name in IDENTITY_MODULES:
                path = project / (name + ".py")
                # Copies only; the real source files are never written.
                path.write_bytes(original if name == "release_policy" else ("# copy of " + name + "\n").encode())
                modules[name] = types.SimpleNamespace(__file__=str(path))
            with patch.object(ask_local_rag.importlib, "import_module", side_effect=modules.__getitem__):
                first = self.check_only(project)
                copy = Path(modules["release_policy"].__file__)
                copy.write_bytes(original + b"# changed\n")
                second = self.check_only(project)
        self.assertEqual(real.read_bytes(), original)
        self.assertEqual((first["status"], second["status"]), ("check_only", "check_only"))
        self.assertNotEqual(first["code"]["files"]["release_policy.py"], second["code"]["files"]["release_policy.py"])
        self.assertNotEqual(first["code"]["sha256"], second["code"]["sha256"])
        self.assertNotEqual(first["run_identity"], second["run_identity"])
        self.assertNotEqual(first["output_paths"], second["output_paths"])


if __name__ == "__main__":
    unittest.main()
