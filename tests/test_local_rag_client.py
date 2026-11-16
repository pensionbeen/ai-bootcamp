"""Focused receipt/cost-boundary tests; transports are fake and never use API."""
import json
from pathlib import Path
import tempfile
import sqlite3
import sys
import threading
import types
import unittest
from unittest.mock import patch

from local_rag_client import Client, CacheMiss, RateLimitedNoResend, CHAT_MODEL, QUERY_MODEL, ENDPOINTS, digest, raw, read, save, request_wire
from local_rag_client import ProviderServerError, RESENDABLE_SERVER_STATUSES, SendGate
from ask_local_rag import query_vector, run_question
from request_lock import RequestInProgress


PAYLOAD = {"model": QUERY_MODEL, "input": ["테스트 질문"]}


def client_identity(kind, payload):
    wire, wire_format = request_wire(kind, payload)
    descriptor = {"endpoint": ENDPOINTS[kind], "kind": kind, "payload": payload}
    if wire_format:
        descriptor.update(wire_format=wire_format, wire_sha256=digest(wire))
    return digest(raw(descriptor))


class Response:
    def __init__(self, status=200, content=None, headers=None):
        self.status_code = status
        self.content = content or json.dumps({"model": QUERY_MODEL, "data": []}).encode()
        self.headers = headers or {}

    def close(self):
        pass


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1], prefix="client-test-")
        self.project = Path(self.temp.name)
        self.now = 1000.0
        self.sleeps = []

    def tearDown(self):
        self.temp.cleanup()

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def client(self, transport, **kwargs):
        client = Client(self.project, transport=transport, sleeper=self.sleep,
                        clock=lambda: self.now, **kwargs)
        client._key = "fake-unit-test-secret"
        return client

    def test_exact_cache_reuses_receipts_without_key(self):
        calls = []
        client = self.client(lambda *a, **k: calls.append(1) or Response())
        body, meta = client.request("embedding", PAYLOAD)
        before = {p: p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
        cached = Client(self.project, read_only=True)
        with patch.object(cached, "_api_key", side_effect=AssertionError("key read")):
            body2, meta2 = cached.request("embedding", PAYLOAD)
        self.assertEqual(body, body2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(meta["called"], 1)
        self.assertEqual(meta2["called"], 0)
        self.assertEqual(before, {p: p.read_bytes() for p in self.project.rglob("*") if p.is_file()})

    def test_read_only_miss_has_no_writes_or_key_read(self):
        client = Client(self.project, check_only=True)
        with patch.object(client, "_api_key", side_effect=AssertionError("key read")):
            with self.assertRaises(CacheMiss):
                client.request("embedding", PAYLOAD)
        self.assertEqual(list(self.project.rglob("*")), [])

    def ordered_payload(self):
        def object_schema(properties):
            return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}
        string={'type':'string'}
        citations={'type':'array','items':{'type':'string','enum':['C001']}}
        schema=object_schema({
            'decision':{'type':'string','enum':['supported','needs_clarification']},
            'missing_information':{'type':'array','items':string},
            'missing_materials':{'type':'array','items':string},
            'claims':{'type':'array','items':object_schema({'text':string,'citation_ids':citations})},
            'calculations':{'type':'array','items':object_schema({
                'expression':string,'result':string,'citation_ids':citations})}})
        return {'model':CHAT_MODEL,'messages':[{'role':'user','content':'JSON 테스트'}],
                'response_format':{'type':'json_schema','json_schema':{
                    'name':'local_rag_single_generation','strict':True,'schema':schema}}}

    def test_ordered_draft_preserves_schema_semantics_and_exact_wire_receipt(self):
        payload=self.ordered_payload();observed=[]
        client=self.client(lambda *a,**k:observed.append(k['data']) or Response(
            content=json.dumps({'model':CHAT_MODEL,'choices':[]}).encode()))
        _,meta=client.request('draft',payload)
        folder=client.cache_dir/meta['request_identity'];descriptor=read(folder/'request.json')
        wire,version=request_wire('draft',payload)
        self.assertEqual(version,'ordered_json_v1')
        self.assertEqual(observed,[wire])
        self.assertEqual((folder/'request.body.json').read_bytes(),wire)
        self.assertEqual(json.loads(wire),payload)
        schema=json.loads(wire)['response_format']['json_schema']['schema']
        self.assertEqual(list(schema['properties']),schema['required'])
        self.assertEqual(list(schema['properties'])[:3],['decision','missing_information','missing_materials'])
        self.assertEqual(descriptor['wire_sha256'],digest(wire))
        self.assertEqual(descriptor['wire_format'],'ordered_json_v1')
        self.assertEqual(read(folder/'attempt-1.json')['payload_sha256'],digest(wire))
        # Sorted request.json can be replayed without losing required order.
        self.assertEqual(request_wire('draft',descriptor['payload']),(wire,version))
        cached=Client(self.project,read_only=True)
        with patch.object(cached,'_api_key',side_effect=AssertionError('key read')):
            _,again=cached.request('draft',descriptor['payload'])
        self.assertTrue(again['cached']);self.assertEqual(again['request_identity'],meta['request_identity'])
        self.assertEqual(len(observed),1)

    def test_ordered_draft_keeps_legacy_descriptor_and_embedding_identity_unchanged(self):
        payload=self.ordered_payload()
        old_descriptor={'endpoint':ENDPOINTS['draft'],'kind':'draft','payload':payload}
        old_identity=digest(raw(old_descriptor));old_folder=self.project/'data/processed/local_rag/2026-10-01/request_cache'/old_identity
        save(old_folder/'request.json',old_descriptor)
        old_bytes=(old_folder/'request.json').read_bytes()
        client=self.client(lambda *a,**k:Response(content=json.dumps({'model':CHAT_MODEL,'choices':[]}).encode()))
        _,meta=client.request('draft',payload)
        self.assertNotEqual(meta['request_identity'],old_identity)
        self.assertEqual((old_folder/'request.json').read_bytes(),old_bytes)
        self.assertEqual(list(old_folder.iterdir()),[old_folder/'request.json'])
        self.assertEqual(request_wire('embedding',PAYLOAD),(raw(PAYLOAD),None))
        embedding=self.client(lambda *a,**k:Response());_,emb=embedding.request('embedding',PAYLOAD)
        expected=digest(raw({'endpoint':ENDPOINTS['embedding'],'kind':'embedding','payload':PAYLOAD}))
        self.assertEqual(emb['request_identity'],expected)
        self.assertFalse((embedding.cache_dir/expected/'request.body.json').exists())

    def test_ordered_wire_tampering_is_rejected_before_cache_or_transport(self):
        payload=self.ordered_payload();observed=[]
        client=self.client(lambda *a,**k:observed.append(1) or Response(content=json.dumps({'model':CHAT_MODEL,'choices':[]}).encode()))
        _,meta=client.request('draft',payload)
        wire_path=client.cache_dir/meta['request_identity']/'request.body.json'
        wire_path.write_bytes(raw(payload))
        with self.assertRaisesRegex(ValueError,'saved_request_wire_mismatch'):client.request('draft',payload)
        self.assertEqual(observed,[1])

    def test_ordered_draft_429_retries_same_wire_and_uncertain_attempt_does_not_repeat(self):
        payload=self.ordered_payload();observed=[]
        replies=[Response(429,headers={'Retry-After':'60'}),Response(content=json.dumps({'model':CHAT_MODEL,'choices':[]}).encode())]
        client=self.client(lambda *a,**k:observed.append(k['data']) or replies.pop(0))
        _,meta=client.request('draft',payload)
        self.assertEqual(len(observed),2);self.assertEqual(observed[0],observed[1])
        self.assertEqual(meta['called'],2);self.assertEqual(sum(self.sleeps),60)
        payload['messages'][0]['content']='JSON 다른 요청'
        attempted=[]
        def timeout(*args,**kwargs):
            attempted.append(kwargs['data']);raise TimeoutError('unknown outcome')
        failing=self.client(timeout)
        with self.assertRaisesRegex(RuntimeError,'unknown_no_automatic_retry'):failing.request('draft',payload)
        with self.assertRaisesRegex(ValueError,'uncertain_attempt'):self.client(timeout).request('draft',payload)
        self.assertEqual(len(attempted),1)

    def test_known_chat_snapshot_is_accepted_and_cached_without_call(self):
        payload = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": "테스트"}]}
        response = Response(content=json.dumps({"model": "solar-pro4-260806", "choices": []}).encode())
        calls = []
        client = self.client(lambda *a, **k: calls.append(1) or response)
        body, meta = client.request("gate", payload)
        self.assertEqual(meta["requested_model"], CHAT_MODEL)
        self.assertEqual(meta["returned_model"], "solar-pro4-260806")
        cached = Client(self.project, read_only=True)
        with patch.object(cached, "_api_key", side_effect=AssertionError("key read")):
            body2, meta2 = cached.request("gate", payload)
        self.assertEqual(body, body2)
        self.assertEqual(meta2["returned_model"], "solar-pro4-260806")
        self.assertEqual(meta2["called"], 0)
        self.assertEqual(len(calls), 1)

    def test_other_chat_snapshot_rejected_without_repeating(self):
        payload = {"model": CHAT_MODEL, "messages": []}
        calls = []
        client = self.client(lambda *a, **k: calls.append(1) or Response(
            content=json.dumps({"model": "solar-pro4-999999", "choices": []}).encode()))
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "unexpected_response_model"):
                client.request("gate", payload)
        self.assertEqual(len(calls), 1)

    def test_query_model_remains_exact(self):
        client = self.client(lambda *a, **k: Response(
            content=json.dumps({"model": QUERY_MODEL + "-260806", "data": []}).encode()))
        with self.assertRaisesRegex(ValueError, "unexpected_response_model"):
            client.request("embedding", PAYLOAD)

    def test_unknown_timeout_never_retries_even_new_client(self):
        calls = []
        def timeout(*args, **kwargs):
            calls.append(1)
            raise TimeoutError("fake-unit-test-secret must not be saved")
        first = self.client(timeout)
        with self.assertRaisesRegex(RuntimeError, "outcome_unknown"):
            first.request("embedding", PAYLOAD)
        second = self.client(timeout)
        with self.assertRaisesRegex(ValueError, "^uncertain_attempt_no_automatic_retry$"):
            second.request("embedding", PAYLOAD)
        self.assertEqual(len(calls), 1)
        saved = b"".join(p.read_bytes() for p in self.project.rglob("*") if p.is_file())
        self.assertNotIn(b"fake-unit-test-secret", saved)

    def test_transport_allows_long_reasoning_without_automatic_retry(self):
        observed = []
        def transport(*args, **kwargs):
            observed.append(kwargs)
            return Response()
        client = self.client(transport)
        client.request("embedding", PAYLOAD)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["timeout"], (10, 180))
        self.assertFalse(observed[0]["allow_redirects"])

    def test_per_request_timeout_only_shortens_wait_and_keeps_identity(self):
        observed = []
        def transport(*args, **kwargs):
            observed.append(kwargs)
            return Response()
        body, meta = self.client(transport).request("embedding", PAYLOAD, timeout=42.5)
        self.assertEqual(observed[0]["timeout"], (10, 42.5))
        cached = Client(self.project, read_only=True)
        body2, meta2 = cached.request("embedding", PAYLOAD)
        self.assertEqual((body2, meta2["request_identity"], meta2["cached"]), (body, meta["request_identity"], True))
        receipts = b"".join(p.read_bytes() for p in self.project.rglob("*") if p.is_file())
        self.assertNotIn(b"42.5", receipts)
        self.client(transport).request("embedding", {**PAYLOAD, "input": ["다른 질문"]}, timeout=3)
        self.assertEqual(observed[1]["timeout"], (3, 3))
        before = sorted(self.project.rglob("*"))
        for bad in (0, -1, 180.5, float("nan"), float("inf"), True, "5"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "invalid_request_timeout"):
                self.client(lambda *a, **k: self.fail("transport")).request(
                    "embedding", {**PAYLOAD, "input": ["새 질문"]}, timeout=bad)
        self.assertEqual(sorted(self.project.rglob("*")), before)
        self.assertEqual(len(observed), 2)

    def test_timeout_failure_names_transport_error_without_retry(self):
        class ReadTimeout(Exception):
            pass
        calls = []
        def timeout(*args, **kwargs):
            calls.append(kwargs["timeout"])
            raise ReadTimeout("fake-unit-test-secret must not be saved")
        with self.assertRaisesRegex(RuntimeError, "^request_outcome_unknown_no_automatic_retry$") as caught:
            self.client(timeout).request("embedding", PAYLOAD, timeout=20)
        self.assertEqual(caught.exception.transport_error_type, "ReadTimeout")
        failure = next(self.project.rglob("attempt-1.failure.json"))
        self.assertEqual(read(failure), {"error_type": "ReadTimeout", "outcome": "unknown_no_automatic_retry"})
        with self.assertRaisesRegex(ValueError, "^uncertain_attempt_no_automatic_retry$"):
            self.client(timeout).request("embedding", PAYLOAD, timeout=20)
        self.assertEqual(calls, [(10, 20)])

    def test_malformed_response_preserved_and_never_repeated(self):
        calls = []
        client = self.client(lambda *a, **k: calls.append(1) or Response(content=b"not-json fake-unit-test-secret"))
        with self.assertRaises(ValueError):
            client.request("embedding", PAYLOAD)
        self.assertEqual(next(self.project.rglob("*.response.raw")).read_bytes(), b"not-json [REDACTED]")
        with self.assertRaises(ValueError):
            client.request("embedding", PAYLOAD)
        self.assertEqual(len(calls), 1)

    def test_only_one_429_retry_and_bounded_wait(self):
        responses = [Response(429, headers={"Retry-After": "900"}), Response(429)]
        client = self.client(lambda *a, **k: responses.pop(0))
        with self.assertRaisesRegex(ValueError, "second_rate_limit"):
            client.request("embedding", PAYLOAD)
        self.assertEqual(client.called, 2)
        self.assertEqual(sum(self.sleeps), 300)
        self.assertTrue(all(s <= 30 for s in self.sleeps))

    def test_deadline_bound_429_fails_at_once_without_wait_or_resend(self):
        calls = []
        def transport(*args, **kwargs):
            calls.append(kwargs["data"])
            return Response(429, headers={"Retry-After": "120"}, content=b"rate limited fake-unit-test-secret")
        payload = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": "확인"}]}
        client = self.client(transport)
        with self.assertRaisesRegex(RateLimitedNoResend, "^rate_limited_no_resend$") as caught:
            client.request("provision_check", payload, timeout=40)
        self.assertIsInstance(caught.exception, RuntimeError)
        self.assertEqual((caught.exception.http_status, caught.exception.retry_wait_seconds), (429, 120.0))
        self.assertNotIn("outcome_unknown", str(caught.exception))
        self.assertFalse(hasattr(caught.exception, "transport_error_type"))
        self.assertEqual((self.sleeps, len(calls), client.called, client.calls), ([], 1, 1, []))
        folder = next(self.project.rglob("attempt-1.json")).parent
        # The 429 receipt is saved exactly as before; nothing marks the failure in the cache.
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["attempt-1.json", "attempt-1.response.json", "attempt-1.response.raw", "request.json"])
        receipt = read(folder / "attempt-1.response.json")
        self.assertEqual((receipt["http_status"], receipt["retry_after"]), (429, "120"))
        self.assertEqual((folder / "attempt-1.response.raw").read_bytes(), b"rate limited [REDACTED]")
        # A second deadline-bound call before the resend wait elapses also fails without sending.
        self.now += 50
        with self.assertRaises(RateLimitedNoResend) as again:
            self.client(transport).request("provision_check", payload, timeout=40)
        self.assertEqual(again.exception.retry_wait_seconds, 70.0)
        self.assertEqual((self.sleeps, len(calls)), ([], 1))
        # Read-only replay never sends either and reports the cache miss as before.
        with self.assertRaises(CacheMiss):
            Client(self.project, read_only=True).request("provision_check", payload, timeout=40)
        # Existing state rule: once the wait has elapsed, the same body may be sent once more.
        self.now += 70
        later = self.client(lambda *a, **k: calls.append(k["data"]) or
                            Response(content=json.dumps({"model": CHAT_MODEL, "choices": []}).encode()))
        _, meta = later.request("provision_check", payload, timeout=40)
        self.assertEqual((self.sleeps, len(calls), meta["called"]), ([], 2, 1))
        self.assertEqual(calls[1], calls[0])
        self.assertEqual(Path(meta["response_path"]).name, "attempt-2.response.json")

    def test_deadline_bound_second_429_keeps_the_existing_stop(self):
        responses = [Response(429), Response(429)]
        payload = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": "확인"}]}
        with self.assertRaises(RateLimitedNoResend):
            self.client(lambda *a, **k: responses.pop(0)).request("provision_check", payload, timeout=40)
        self.now += 60
        with self.assertRaisesRegex(ValueError, "second_rate_limit"):
            self.client(lambda *a, **k: responses.pop(0)).request("provision_check", payload, timeout=40)
        with self.assertRaisesRegex(ValueError, "second_rate_limit"):
            self.client(lambda *a, **k: self.fail("transport")).request("provision_check", payload, timeout=40)
        self.assertEqual((self.sleeps, responses), ([], []))

    def test_request_without_timeout_keeps_waiting_once_after_429(self):
        responses = [Response(429, headers={"Retry-After": "120"}),
                     Response(content=json.dumps({"model": CHAT_MODEL, "choices": []}).encode())]
        client = self.client(lambda *a, **k: responses.pop(0))
        _, meta = client.request("provision_check", {"model": CHAT_MODEL, "messages": []})
        self.assertEqual((sum(self.sleeps), meta["called"], responses), (120, 2, []))

    def test_rate_limited_error_survives_copy(self):
        import copy
        import pickle
        error = RateLimitedNoResend(12.5)
        for clone in (copy.deepcopy(error), pickle.loads(pickle.dumps(error))):
            self.assertEqual((str(clone), clone.http_status, clone.retry_wait_seconds),
                             ("rate_limited_no_resend", 429, 12.5))

    def chat_ok(self):
        return Response(content=json.dumps({"model": CHAT_MODEL, "choices": []}).encode())

    def test_first_server_error_is_not_resent_by_the_client_and_attempt_two_goes_at_once(self):
        for status in sorted(RESENDABLE_SERVER_STATUSES):
            with self.subTest(status=status):
                payload = self.ordered_payload()
                payload["messages"][0]["content"] = "JSON 서버 오류 " + str(status)
                sent = []
                def failing(*args, **kwargs):
                    sent.append(kwargs["data"])
                    return Response(status, content=b"server error fake-unit-test-secret")
                client = self.client(failing)
                with self.assertRaisesRegex(ProviderServerError, "^provider_server_error_resendable$") as caught:
                    client.request("draft", payload)
                self.assertIsInstance(caught.exception, RuntimeError)
                self.assertEqual(caught.exception.http_status, status)
                self.assertFalse(hasattr(caught.exception, "transport_error_type"))
                self.assertEqual((self.sleeps, len(sent), client.called, client.calls), ([], 1, 1, []))
                folder = client.cache_dir / digest(raw({"endpoint": ENDPOINTS["draft"], "kind": "draft",
                    "payload": payload, "wire_format": "ordered_json_v1", "wire_sha256": digest(sent[0])}))
                self.assertEqual(sorted(p.name for p in folder.iterdir()),
                                 ["attempt-1.json", "attempt-1.response.json", "attempt-1.response.raw",
                                  "request.body.json", "request.json"])
                self.assertEqual(read(folder / "attempt-1.response.json")["http_status"], status)
                self.assertEqual((folder / "attempt-1.response.raw").read_bytes(), b"server error [REDACTED]")
                # Read-only replay never sends the second attempt.
                with self.assertRaises(CacheMiss):
                    Client(self.project, read_only=True).request("draft", payload)
                # A later call (deadline-bound, as the draft retry is) sends attempt 2 without waiting.
                later = self.client(lambda *a, **k: sent.append(k["data"]) or self.chat_ok())
                _, meta = later.request("draft", payload, timeout=40)
                self.assertEqual((self.sleeps, len(sent), meta["called"], later.called), ([], 2, 1, 1))
                self.assertEqual(sent[1], sent[0])
                self.assertEqual(read(folder / "attempt-2.json")["payload_sha256"], digest(sent[0]))
                self.assertEqual(Path(meta["response_path"]).name, "attempt-2.response.json")
                # The success is then an ordinary cached receipt.
                cached = Client(self.project, read_only=True)
                with patch.object(cached, "_api_key", side_effect=AssertionError("key read")):
                    _, again = cached.request("draft", payload)
                self.assertEqual((again["cached"], again["called"], len(sent)), (True, 0, 2))

    def test_first_server_error_with_timeout_is_also_left_to_the_caller(self):
        sent = []
        client = self.client(lambda *a, **k: sent.append(k["timeout"]) or Response(503))
        payload = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": "확인"}]}
        with self.assertRaises(ProviderServerError) as caught:
            client.request("provision_check", payload, timeout=40)
        self.assertEqual((caught.exception.http_status, sent, self.sleeps), (503, [(10, 40)], []))

    def test_second_server_error_stops_permanently_without_a_third_send(self):
        responses = [Response(500), Response(503)]
        payload = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": "초안"}]}
        with self.assertRaises(ProviderServerError):
            self.client(lambda *a, **k: responses.pop(0)).request("draft", payload)
        with self.assertRaisesRegex(ValueError, "^provider_http_error:503$"):
            self.client(lambda *a, **k: responses.pop(0)).request("draft", payload, timeout=40)
        for timeout in (None, 40):
            with self.assertRaisesRegex(ValueError, "^provider_http_error:503$"):
                self.client(lambda *a, **k: self.fail("third send")).request("draft", payload, timeout=timeout)
        with self.assertRaisesRegex(ValueError, "^provider_http_error:503$"):
            Client(self.project, read_only=True).request("draft", payload)
        self.assertEqual((responses, self.sleeps), ([], []))
        self.assertEqual(sorted(p.name for p in self.project.rglob("attempt-*.json")),
                         ["attempt-1.json", "attempt-1.response.json", "attempt-2.json", "attempt-2.response.json"])

    def test_server_error_then_429_keeps_the_rate_limit_stop(self):
        responses = [Response(502), Response(429, headers={"Retry-After": "60"})]
        payload = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": "초안"}]}
        with self.assertRaises(ProviderServerError):
            self.client(lambda *a, **k: responses.pop(0)).request("draft", payload)
        with self.assertRaisesRegex(ValueError, "^second_rate_limit_response_stop$"):
            self.client(lambda *a, **k: responses.pop(0)).request("draft", payload, timeout=40)
        with self.assertRaisesRegex(ValueError, "^second_rate_limit_response_stop$"):
            self.client(lambda *a, **k: self.fail("third send")).request("draft", payload)
        self.assertEqual((responses, self.sleeps), ([], []))

    def test_429_then_server_error_keeps_the_existing_stop(self):
        responses = [Response(429, headers={"Retry-After": "60"}), Response(500)]
        payload = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": "초안"}]}
        with self.assertRaisesRegex(ValueError, "^provider_http_error:500$"):
            self.client(lambda *a, **k: responses.pop(0)).request("draft", payload)
        self.assertEqual((responses, sum(self.sleeps)), ([], 60))
        with self.assertRaisesRegex(ValueError, "^provider_http_error:500$"):
            self.client(lambda *a, **k: self.fail("third send")).request("draft", payload)

    def test_client_errors_and_other_server_statuses_are_never_resent(self):
        for status in (400, 401, 404, 408, 422, 501, 505):
            with self.subTest(status=status):
                sent = []
                payload = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": "상태 " + str(status)}]}
                for timeout in (None, 40, None):
                    with self.assertRaisesRegex(ValueError, "^provider_http_error:" + str(status) + "$"):
                        self.client(lambda *a, **k: sent.append(1) or Response(status)).request(
                            "draft", payload, timeout=timeout)
                self.assertEqual((sent, self.sleeps), ([1], []))

    def test_unknown_outcome_never_becomes_resendable(self):
        sent = []
        def timeout(*args, **kwargs):
            sent.append(1)
            raise TimeoutError("unknown outcome")
        payload = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": "초안"}]}
        with self.assertRaisesRegex(RuntimeError, "^request_outcome_unknown_no_automatic_retry$"):
            self.client(timeout).request("draft", payload)
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "^uncertain_attempt_no_automatic_retry$"):
                self.client(lambda *a, **k: self.fail("resend")).request("draft", payload, timeout=40)
        self.assertEqual(sent, [1])

    def test_provider_server_error_survives_copy(self):
        import copy
        import pickle
        error = ProviderServerError(503)
        for clone in (copy.deepcopy(error), pickle.loads(pickle.dumps(error))):
            self.assertEqual((type(clone), str(clone), clone.http_status),
                             (ProviderServerError, "provider_server_error_resendable", 503))

    def test_embedding_then_draft_has_no_forced_wait_even_with_legacy_timestamp(self):
        client = self.client(lambda *a, **k: Response())
        legacy = client.cache_dir / ".last-call.json"
        save(legacy, {"epoch": self.now})
        saved = legacy.read_bytes()
        client.request("embedding", PAYLOAD)
        other = self.client(lambda *a, **k: Response(content=json.dumps({"model":CHAT_MODEL,"choices":[]}).encode()))
        _, meta = other.request("draft", {"model":CHAT_MODEL,"messages":[{"role":"user","content":"테스트"}]})
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.now, 1000.0)
        self.assertEqual((client.called,other.called,meta["called"]),(1,1,1))
        self.assertEqual(legacy.read_bytes(),saved)

    def test_one_logical_chat_request_records_two_transport_attempts_after_429(self):
        responses=[Response(429,headers={"Retry-After":"90"}),
                   Response(content=json.dumps({"model":CHAT_MODEL,"choices":[]}).encode())]
        client=self.client(lambda *a,**k:responses.pop(0))
        _,meta=client.request("draft",{"model":CHAT_MODEL,"messages":[]})
        self.assertEqual(sum(self.sleeps),90)
        self.assertEqual(len(client.calls),1)
        self.assertEqual((client.called,meta["called"]),(2,2))
        folder=Path(meta["response_path"]).parent
        self.assertTrue((folder/'attempt-1.response.json').exists())
        self.assertTrue((folder/'attempt-2.response.json').exists())
        self.assertEqual(json.loads((folder/'attempt-1.response.json').read_bytes())['http_status'],429)
        self.assertEqual(json.loads((folder/'attempt-2.response.json').read_bytes())['http_status'],200)

    def test_vector_index_dimension_and_finiteness_are_checked(self):
        body = {"model": QUERY_MODEL, "data": [{"index": 0, "embedding": [1.0] * 1024}]}
        self.assertEqual(len(query_vector(body)), 1024)
        for bad in (True, 1):
            body["data"][0]["index"] = bad
            with self.assertRaises(ValueError):
                query_vector(body)
        body["data"][0]["index"] = 0
        body["data"][0]["embedding"][0] = float("nan")
        with self.assertRaises(ValueError):
            query_vector(body)

    def database(self):
        database = self.project / "rag.sqlite3"
        connection = sqlite3.connect(database)
        metadata = {"as_of_date": "2026-10-01", "dimension": 1024,
                    "model": "solar-embedding-2-passage", "query_model": QUERY_MODEL,
                    "identity": "fake-db-identity", "answer_allowed": False}
        connection.execute("CREATE TABLE metadata(key TEXT,value_json TEXT)")
        connection.executemany("INSERT INTO metadata VALUES (?,?)", [(k, json.dumps(v)) for k, v in metadata.items()])
        connection.commit()
        connection.close()
        save(database.with_suffix(".manifest.json"), {"as_of_date": "2026-10-01", "answer_allowed": False,
             "identity": "fake-db-identity", "database_sha256": digest(database.read_bytes())})
        return database

    def test_runtime_check_only_never_reads_key_or_writes(self):
        database = self.database()
        fake_answer = types.ModuleType("local_rag_answer")
        fake_answer.__file__ = __file__
        before = {p: p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
        with patch.dict(sys.modules, {"local_rag_answer": fake_answer}), \
                patch.object(Client, "_api_key", side_effect=AssertionError("key read")), \
                patch.object(Client, "request", side_effect=AssertionError("API call")):
            result = run_question(self.project, "테스트 질문", database, check_only=True)
        self.assertEqual(result["status"], "check_only")
        self.assertEqual(result["called"], 0)
        self.assertEqual(before, {p: p.read_bytes() for p in self.project.rglob("*") if p.is_file()})

    # ---- service options: parallel (per-body lock), deadline_seconds (short 429 wait), SendGate

    def chat_payload(self, text):
        return {"model": CHAT_MODEL, "messages": [{"role": "user", "content": text}]}

    def run_in_thread(self, function):
        outcome = {}
        def target():
            try:
                outcome["value"] = function()
            except BaseException as exc:  # reported to the test thread
                outcome["error"] = exc
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread, outcome

    def test_parallel_sends_two_different_bodies_at_the_same_time(self):
        barrier = threading.Barrier(2, timeout=5)
        sent = []
        def transport(*args, **kwargs):
            barrier.wait()  # both sends must be in flight together, or this breaks
            sent.append(kwargs["data"])
            return self.chat_ok()
        first, first_result = self.run_in_thread(
            lambda: self.client(transport, parallel=True).request("draft", self.chat_payload("하나")))
        second, second_result = self.run_in_thread(
            lambda: self.client(transport, parallel=True).request("draft", self.chat_payload("둘")))
        for thread in (first, second):
            thread.join(10)
        self.assertNotIn("error", first_result)
        self.assertNotIn("error", second_result)
        self.assertEqual(len(sent), 2)
        self.assertEqual(sorted(p.name for p in self.project.rglob(".request.lock")), [".request.lock"] * 2)
        self.assertFalse((self.project / "data/processed/local_rag/2026-10-01/request_cache/.api.lock").exists())

    def test_default_mode_keeps_one_global_lock(self):
        entered, release = threading.Event(), threading.Event()
        def blocking(*args, **kwargs):
            entered.set()
            release.wait(5)
            return self.chat_ok()
        first, result = self.run_in_thread(lambda: self.client(blocking).request("draft", self.chat_payload("하나")))
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(RequestInProgress, "request_already_running"):
                self.client(lambda *a, **k: self.fail("second send")).request("draft", self.chat_payload("둘"))
        finally:
            release.set()
            first.join(10)
        self.assertNotIn("error", result)
        # The refused body wrote nothing.
        self.assertEqual(len(list(self.project.rglob("request.json"))), 1)

    def test_parallel_same_body_runs_once_and_keeps_receipts_whole(self):
        locked, release_lock = threading.Event(), threading.Event()
        entered, release = threading.Event(), threading.Event()
        sent = []
        def blocking(*args, **kwargs):
            sent.append(kwargs["data"])
            entered.set()
            release.wait(5)
            return self.chat_ok()
        payload = self.chat_payload("같은 본문")
        first_client = self.client(blocking, parallel=True)
        def key_under_lock():
            locked.set()  # the body's lock is held; nothing is written yet
            release_lock.wait(5)
            return "fake-unit-test-secret"
        first_client._api_key = key_under_lock
        first, result = self.run_in_thread(lambda: first_client.request("draft", payload))
        folder = self.project / "data/processed/local_rag/2026-10-01/request_cache" / client_identity("draft", payload)
        try:
            self.assertTrue(locked.wait(5))
            # Same body while its lock is held: refused by the per-body lock, no write.
            with self.assertRaisesRegex(RequestInProgress, "request_already_running"):
                self.client(lambda *a, **k: self.fail("second send"), parallel=True).request("draft", payload)
            self.assertEqual(sorted(p.name for p in folder.iterdir()), [".request.lock"])
            release_lock.set()
            self.assertTrue(entered.wait(5))
            # Same body while its attempt is in flight: the existing state rule refuses it, no write.
            before = sorted(p.name for p in folder.iterdir())
            with self.assertRaisesRegex(ValueError, "^uncertain_attempt_no_automatic_retry$"):
                self.client(lambda *a, **k: self.fail("second send"), parallel=True).request("draft", payload)
            self.assertEqual(sorted(p.name for p in folder.iterdir()), before)
        finally:
            release_lock.set()
            release.set()
            first.join(10)
        self.assertNotIn("error", result)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         [".request.lock", "attempt-1.json", "attempt-1.response.json", "attempt-1.response.raw",
                          "request.json"])
        _, again = self.client(lambda *a, **k: self.fail("cached send"), parallel=True).request("draft", payload)
        self.assertEqual((again["cached"], again["called"], len(sent)), (True, 0, 1))
        # Options never enter the request identity: the default client finds the same receipt.
        cached = Client(self.project, read_only=True)
        _, plain = cached.request("draft", payload)
        self.assertEqual(plain["request_identity"], again["request_identity"])
        self.assertEqual(plain["request_identity"], digest(raw({"endpoint": ENDPOINTS["draft"], "kind": "draft",
                                                                 "payload": payload})))

    def test_deadline_mode_429_waits_retry_after_and_resends_within_remaining_time(self):
        observed = []
        responses = [Response(429, headers={"Retry-After": "3"}), Response()]
        def transport(*args, **kwargs):
            observed.append(kwargs["timeout"])
            return responses.pop(0)
        client = self.client(transport, deadline_seconds=75, parallel=True, gate=SendGate())
        _, meta = client.request("embedding", PAYLOAD)
        self.assertEqual(self.sleeps, [3.0])
        self.assertEqual((meta["called"], responses), (2, []))
        # First attempt keeps the normal read limit; the resend reads at most the time left (1075 - 1003).
        self.assertEqual(observed, [(10, 180), (10, 72.0)])
        self.assertEqual(Path(meta["response_path"]).name, "attempt-2.response.json")
        receipts = b"".join(p.read_bytes() for p in self.project.rglob("*") if p.is_file())
        self.assertNotIn(b"deadline", receipts)
        self.assertNotIn(b"parallel", receipts)

    def test_deadline_mode_retry_wait_bounds(self):
        for value, expected in (("3", 3.0), ("", 10.0), (None, 10.0), ("900", 60.0), ("0", 2.0), ("1.5", 2.0),
                                ("abc", 10.0), ("nan", 10.0), ("inf", 10.0), ("Thu, 01 Jan 1970 00:00:20 GMT", 5.0)):
            with self.subTest(value=value):
                self.assertEqual(Client._deadline_retry_seconds(value, 15.0), expected)

    def test_deadline_mode_skips_resend_when_time_is_short_without_writing(self):
        cases = (("embedding", PAYLOAD, 40, "15"),  # 15 s wait + 30 s minimum > 40 s
                 ("draft", self.chat_payload("초안"), 34, "5"),  # 5 s wait + 30 s minimum > 34 s
                 ("draft", self.chat_payload("기본 대기"), 39, ""))  # 10 s default + 30 s > 39 s
        for kind, payload, deadline, retry_after in cases:
            with self.subTest(kind=kind, deadline=deadline):
                self.sleeps.clear()
                sent = []
                client = self.client(lambda *a, **k: sent.append(1) or Response(
                    429, headers={"Retry-After": retry_after} if retry_after else {}), deadline_seconds=deadline)
                with self.assertRaisesRegex(RateLimitedNoResend, "^rate_limited_no_resend$") as caught:
                    client.request(kind, payload)
                self.assertEqual(caught.exception.retry_wait_seconds, float(retry_after or 10))
                self.assertEqual((self.sleeps, sent, client.called, client.calls), ([], [1], 1, []))
                self.assertEqual(client.rate_limit_skipped, {kind})
                folder = client.cache_dir / client_identity(kind, payload)
                names = sorted(p.name for p in folder.iterdir())
                self.assertNotIn("attempt-2.json", names)
                self.assertEqual([n for n in names if n.startswith("attempt-")],
                                 ["attempt-1.json", "attempt-1.response.json", "attempt-1.response.raw"])
        # Exactly at the boundary: 5 s wait + 30 s minimum <= 35 s, so the draft is resent once.
        self.sleeps.clear()
        responses = [Response(429, headers={"Retry-After": "5"}), self.chat_ok()]
        fits = self.client(lambda *a, **k: responses.pop(0), deadline_seconds=35)
        _, meta = fits.request("draft", self.chat_payload("맞는 마감"))
        self.assertEqual(fits.rate_limit_skipped, set())
        self.assertEqual((self.sleeps, meta["called"]), ([5.0], 2))

    def test_deadline_mode_later_call_resends_once_after_skipped_wait(self):
        payload = self.chat_payload("나중 호출")
        with self.assertRaises(RateLimitedNoResend):
            self.client(lambda *a, **k: Response(429, headers={"Retry-After": "30"}),
                        deadline_seconds=40).request("draft", payload)
        self.now += 30
        sent = []
        _, meta = self.client(lambda *a, **k: sent.append(1) or self.chat_ok(), deadline_seconds=75).request(
            "draft", payload)
        self.assertEqual((self.sleeps, sent, meta["called"]), ([], [1], 1))
        self.assertEqual(Path(meta["response_path"]).name, "attempt-2.response.json")

    def test_deadline_mode_second_429_keeps_the_existing_stop(self):
        responses = [Response(429, headers={"Retry-After": "2"}), Response(429, headers={"Retry-After": "2"})]
        client = self.client(lambda *a, **k: responses.pop(0), deadline_seconds=75)
        with self.assertRaisesRegex(ValueError, "^second_rate_limit_response_stop$"):
            client.request("embedding", PAYLOAD)
        self.assertEqual((self.sleeps, client.called, responses), ([2.0], 2, []))
        with self.assertRaisesRegex(ValueError, "^second_rate_limit_response_stop$"):
            self.client(lambda *a, **k: self.fail("third send"), deadline_seconds=75).request("embedding", PAYLOAD)

    def test_default_mode_still_waits_at_least_sixty_seconds(self):
        responses = [Response(429, headers={"Retry-After": "3"}), Response()]
        client = self.client(lambda *a, **k: responses.pop(0))
        client.request("embedding", PAYLOAD)
        self.assertEqual(sum(self.sleeps), 60)

    def test_deadline_mode_call_with_timeout_keeps_the_existing_rule(self):
        sent = []
        client = self.client(lambda *a, **k: sent.append(k["timeout"]) or Response(429, headers={"Retry-After": "3"}),
                             deadline_seconds=75)
        with self.assertRaises(RateLimitedNoResend) as caught:
            client.request("provision_check", self.chat_payload("확인"), timeout=40)
        # The 60-300 s bound of the timeout rule, not the short deadline wait.
        self.assertEqual((caught.exception.retry_wait_seconds, sent, self.sleeps), (60.0, [(10, 40)], []))
        self.assertEqual(client.rate_limit_skipped, {"provision_check"})

    def test_rate_limit_skipped_is_empty_after_normal_calls(self):
        client = self.client(lambda *a, **k: Response())
        self.assertEqual(client.rate_limit_skipped, set())
        client.request("embedding", PAYLOAD)
        client.request("embedding", PAYLOAD)  # cached
        self.assertEqual(client.rate_limit_skipped, set())
        # A waited-out 429 (default mode) is not a skip either.
        responses = [Response(429, headers={"Retry-After": "3"}), self.chat_ok()]
        waited = self.client(lambda *a, **k: responses.pop(0))
        waited.request("draft", self.chat_payload("기다림"))
        self.assertEqual(waited.rate_limit_skipped, set())
        receipts = b"".join(p.read_bytes() for p in self.project.rglob("*") if p.is_file())
        self.assertNotIn(b"rate_limit_skipped", receipts)

    def test_invalid_service_options_are_rejected(self):
        for options in ({"parallel": 1}, {"deadline_seconds": 0}, {"deadline_seconds": -5},
                        {"deadline_seconds": float("inf")}, {"deadline_seconds": True}, {"deadline_seconds": "75"},
                        {"gate": object()}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                Client(self.project, **options)

    def test_gate_defers_another_clients_first_send_until_the_hold(self):
        gate = SendGate()
        # A: a deadline-bound call that receives 429 (Retry-After 5) records the hold and stops.
        a = self.client(lambda *a, **k: Response(429, headers={"Retry-After": "5"}), gate=gate, deadline_seconds=75)
        with self.assertRaises(RateLimitedNoResend):
            a.request("provision_check", self.chat_payload("A"), timeout=40)
        self.assertEqual(gate.until(), 1005.0)
        sent_at = []
        b = self.client(lambda *a, **k: sent_at.append(self.now) or Response(), gate=gate, deadline_seconds=75)
        b.request("embedding", PAYLOAD)
        self.assertEqual((self.sleeps, sent_at), ([5.0], [1005.0]))

    def test_gate_refuses_without_writing_when_the_hold_does_not_fit(self):
        gate = SendGate()
        gate.hold(self.now + 50)
        # 50 s hold + 30 s embedding minimum > 75 s deadline.
        client = self.client(lambda *a, **k: self.fail("send"), gate=gate, deadline_seconds=75, parallel=True)
        with self.assertRaisesRegex(RateLimitedNoResend, "^rate_limited_no_resend$") as caught:
            client.request("embedding", PAYLOAD)
        self.assertEqual((caught.exception.retry_wait_seconds, self.sleeps), (50.0, []))
        self.assertEqual(client.rate_limit_skipped, {"embedding"})
        # A call with timeout never sleeps for a hold either.
        checker = self.client(lambda *a, **k: self.fail("send"), gate=gate)
        with self.assertRaises(RateLimitedNoResend):
            checker.request("provision_check", self.chat_payload("확인"), timeout=40)
        self.assertEqual(checker.rate_limit_skipped, {"provision_check"})
        self.assertEqual((list(self.project.rglob("*")), self.sleeps), ([], []))
        # A hold that fits is waited out; without a deadline any hold is waited out.
        self.now += 20
        _, meta = self.client(lambda *a, **k: Response(), gate=gate, deadline_seconds=75).request("embedding", PAYLOAD)
        self.assertEqual((self.sleeps, meta["called"]), ([30.0], 1))

    def test_gate_hold_extended_while_waiting_is_waited_out(self):
        gate = SendGate()
        gate.hold(self.now + 5)
        def extending_sleep(seconds):
            self.sleep(seconds)
            if len(self.sleeps) == 1:
                gate.hold(self.now + 4)  # another worker got a 429 meanwhile
        sent_at = []
        client = Client(self.project, transport=lambda *a, **k: sent_at.append(self.now) or Response(),
                        sleeper=extending_sleep, clock=lambda: self.now, gate=gate, deadline_seconds=75)
        client._key = "fake-unit-test-secret"
        client.request("embedding", PAYLOAD)
        self.assertEqual((self.sleeps, sent_at), ([5.0, 4.0], [1009.0]))

    def test_429_receipt_sets_gate_hold_and_none_gate_has_no_effect(self):
        gate = SendGate()
        responses = [Response(429, headers={"Retry-After": "7"}), Response()]
        client = self.client(lambda *a, **k: responses.pop(0), gate=gate)  # default mode: 60 s resend wait
        client.request("embedding", PAYLOAD)
        self.assertEqual(gate.until(), 1007.0)
        self.assertEqual(sum(self.sleeps), 60)  # the passed hold adds no wait
        self.assertEqual(SendGate().until(), 0.0)

    def test_runtime_rejects_changed_database_before_api(self):
        database = self.database()
        with database.open("ab") as handle:
            handle.write(b"changed")
        with patch.object(Client, "request", side_effect=AssertionError("API call")):
            result = run_question(self.project, "테스트 질문", database)
        self.assertEqual(result["status"], "processing_error")
        self.assertEqual(result["error"], "database_manifest_hash_mismatch")
        self.assertEqual(result["called"], 0)
        self.assertFalse(result["legal_absence_established"])


if __name__ == "__main__":
    unittest.main()
