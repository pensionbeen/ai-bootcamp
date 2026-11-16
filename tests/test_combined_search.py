"""Combined-93 invariants, cache safety, no-call inspection and runner routing."""
import copy,importlib.util,json,os,pathlib,sys,tempfile,unittest
from unittest.mock import patch
PROJECT=pathlib.Path(os.environ.get("RAG_TEST_PROJECT",pathlib.Path(__file__).resolve().parents[1]))
sys.path.append(str(PROJECT/"src"))
import search_combined_attachments as s

@unittest.skipUnless((PROJECT / "data/processed/attachments/2026-10-01/search_validation_93_final/snapshot.json").exists(),
                     "저장소에 포함되지 않은 원본 자료가 필요합니다: data/processed/attachments/2026-10-01/search_validation_93_final/snapshot.json")
class CombinedSearchTests(unittest.TestCase):
    def test_malformed_gate_gets_one_grounded_repair(self):
        from unittest.mock import Mock
        evidence=s.view(self.corpus["catalog"],[self.cid]);request=s.payload("안내표지판 관련 질문",evidence)
        invalid=self.decision(decision="needs_clarification")
        invalid.update(selected_chunk_ids=[],missing_information=["구체적인 질문 대상"])
        fixed=copy.deepcopy(invalid);fixed["selected_chunk_ids"]=[self.cid]
        client=Mock();client.request.return_value=(self.response(fixed),"repair-key")
        result,meta=s.resolve_gate(client,request,self.response(invalid),"original-key",evidence)
        self.assertEqual(result["decision"],"needs_clarification")
        self.assertEqual(meta["raw_model_decision"],invalid)
        self.assertEqual(meta["initial_validation_error"],"missing_primary_support")
        client.request.assert_called_once()
        self.assertNotIn("expected_decision",json.dumps(client.request.call_args.args[1]))
    def test_failed_repair_does_not_loop_or_become_absence(self):
        from unittest.mock import Mock
        evidence=s.view(self.corpus["catalog"],[self.cid]);request=s.payload("질문",evidence)
        invalid=self.decision();invalid["selected_chunk_ids"]=["fabricated"]
        client=Mock();client.request.return_value=(self.response(invalid),"repair-key")
        with self.assertRaisesRegex(ValueError,"unknown_citation"):
            s.resolve_gate(client,request,self.response(invalid),"original-key",evidence)
        client.request.assert_called_once()
    @classmethod
    def setUpClass(cls):
        cls.corpus=s.load_corpus(PROJECT)
        cls.cid="2171833:22740781:candidate:p1-e2-image"
        cls.floor="2171833:22740739:candidate:p1-e2-image"
    def temporary(self):
        return tempfile.TemporaryDirectory(dir=pathlib.Path(__file__).resolve().parent,prefix="combined-test-")
    def fake_corpus(self):
        return {**self.corpus,"input_files":{},"corpus_hash":"offline-fixture"}
    def decision(self,cid=None,decision="supported"):
        return {"decision":decision,"selected_chunk_ids":[cid or self.cid],"reason":"저장된 근거 확인",
                "missing_information":[],"required_external_materials":[]}
    def response(self,r):
        return {"model":"solar-pro4-260806","choices":[{"finish_reason":"stop","message":{"content":json.dumps(r,ensure_ascii=False)}}]}
    def test_reviewed_93_and_conditional_bundle(self):
        c=self.corpus["catalog"];v=s.view(c,["2171833:22740727:candidate:p1-e2-r5"])
        self.assertEqual(len(c["records"]),93)
        self.assertEqual(len(v["records"]),14)
        self.assertEqual(sum(r["external_material_required"] is True for r in c["records"].values()),28)
        self.assertEqual(sum(r["external_material_required"] is None for r in c["records"].values()),62)
    def test_missing_context_and_external_flags_rejected(self):
        records=copy.deepcopy(self.corpus["records"]);r=next(x for x in records if x["origin"]=="reviewed_additional_31")
        r["external_material_required"]=False
        with self.assertRaisesRegex(ValueError,"external_flag"):s.build_catalog(records,self.corpus["catalog"]["external_tasks"])
        r["external_material_required"]=True;r["grounding_bundle"]["required_contexts"]=None
        with self.assertRaisesRegex(ValueError,"missing_context"):s.build_catalog(records,self.corpus["catalog"]["external_tasks"])
    def test_only_owned_source_alias_is_accepted(self):
        e=s.view(self.corpus["catalog"],[self.cid]);r=self.decision()
        owned=e["records"][0]["required_context_ids"]
        alias=next(c["origin_id"] for c in e["contexts"] if c["unit_id"] in owned)
        r["selected_chunk_ids"].append(alias)
        self.assertEqual(s.validate_result(r,e)["selected_chunk_ids"],[self.cid])
        r["selected_chunk_ids"]=["fabricated"]
        with self.assertRaisesRegex(ValueError,"unknown_citation"):s.validate_result(r,e)
    def test_payload_has_real_primary_enum_and_no_gold(self):
        e=s.view(self.corpus["catalog"],[self.cid]);p=s.payload("표지판의 문구는?",e)
        allowed=p["response_format"]["json_schema"]["schema"]["properties"]["selected_chunk_ids"]["items"]["enum"]
        self.assertEqual(set(allowed),{r["chunk_id"] for r in e["records"]})
        for key in ["expected_decision","expected_basis","cosine_similarity","embedding"]:
            self.assertNotIn('"'+key+'"',json.dumps(p))
    def test_query_index_and_vector_integrity(self):
        v1=[1.0]*1024;v2=[2.0]*1024
        b={"model":s.QUERY_MODEL,"data":[{"index":1,"embedding":v2},{"index":0,"embedding":v1}]}
        self.assertEqual(s.query_vectors(b,2),[v1,v2])
        b["data"][1]["index"]=1
        with self.assertRaises(ValueError):s.query_vectors(b,2)
        for v in [[0.0]*1024,[float("nan")]*1024,[1.0]*1023,[True]*1024]:
            with self.assertRaises(ValueError):s.vector_norm(v)
    def test_future_target_is_not_future_visit(self):
        for q in ["2027-01-01 시행 조례", "2027년 1월 1일 시행 판본"]:
            self.assertTrue(s.explicit_future_version(q))
        for q in ["2026년 10월 1일 시행 조례","2027년 1월 1일 방문 예정","2027-99-99 시행 조례"]:
            self.assertFalse(s.explicit_future_version(q))
    def test_new_price_and_legacy_age_policies(self):
        r=self.decision("2147709:22415979:candidate:p1-e5-r7")
        result,_=s.final_policies("어른 2명의 실제 결제 금액을 하나로 확정해주세요.",r,self.corpus)
        self.assertEqual(result["decision"],"insufficient_evidence")
        result,_=s.final_policies("어른 1회당 표의 이용료 범위는?",r,self.corpus)
        self.assertEqual(result["decision"],"supported")
        result,_=s.final_policies("캠핑장 어른 1명과 친구 1명의 이용료 합계는?",r,self.corpus)
        self.assertEqual(result["decision"],"needs_clarification")
    def test_context_does_not_authorize_legality(self):
        r=self.decision("2171833:22740725:candidate:legacy-2")
        result,_=s.final_policies("주차장 설치 의무를 충족하는지 확정해주세요.",r,self.corpus)
        self.assertEqual(result["decision"],"insufficient_evidence")
    def test_check_only_does_not_call_or_create(self):
        with self.temporary() as folder,patch.object(s,"call_api") as api:
            client=s.Client(pathlib.Path(folder),self.fake_corpus(),read_only=True)
            with self.assertRaisesRegex(ValueError,"check-only"):client.request("query",{"model":s.QUERY_MODEL,"input":["x"]})
            self.assertEqual(list(pathlib.Path(folder).iterdir()),[])
            api.assert_not_called()
    def test_successful_cached_request_is_reused(self):
        with self.temporary() as folder,patch.object(s,"call_api",return_value={"ok":True}) as api:
            client=s.Client(pathlib.Path(folder),self.fake_corpus())
            q={"model":s.QUERY_MODEL,"input":["x"]}
            self.assertEqual(client.request("query",q),client.request("query",q))
            self.assertEqual(api.call_count,1)
    def test_only_recorded_429_gets_one_delayed_retry(self):
        with self.temporary() as folder,patch.object(s,"call_api",side_effect=[s.RateLimited(60),{"ok":True}]) as api,patch.object(s.time,"sleep") as pause:
            root=pathlib.Path(folder);client=s.Client(root,self.fake_corpus())
            result,_=client.request("query",{"input":["x"]})
            self.assertEqual(result,{"ok":True});self.assertEqual(api.call_count,2)
            self.assertEqual(len(list(root.rglob("*.rejection.json"))),1)
            self.assertGreaterEqual(pause.call_args.args[0],59)
            before=api.call_count
            client.request("query",{"input":["x"]})
            self.assertEqual(api.call_count,before)
    def test_failed_request_is_never_automatically_repeated(self):
        with self.temporary() as folder,patch.object(s,"call_api",side_effect=ValueError("provider_failure")) as api:
            client=s.Client(pathlib.Path(folder),self.fake_corpus());q={"model":s.QUERY_MODEL,"input":["x"]}
            with self.assertRaisesRegex(ValueError,"provider_failure"):client.request("query",q)
            with self.assertRaisesRegex(ValueError,"미완료"):client.request("query",q)
            self.assertEqual(api.call_count,1)
    def test_mid_call_input_change_preserves_receipt_but_blocks_result(self):
        with self.temporary() as folder:
            root=pathlib.Path(folder);(root/"input.txt").write_bytes(b"before")
            c=self.fake_corpus();c["input_files"]={"input.txt":s.digest(b"before")}
            def fake(*args):
                (root/"input.txt").write_bytes(b"after");return {"ok":True}
            with patch.object(s,"call_api",side_effect=fake):
                with self.assertRaisesRegex(ValueError,"input_changed"):s.Client(root,c).request("query",{"input":["x"]})
            self.assertEqual(len(list(root.rglob("*.response.json"))),1)
    def test_response_binding_tampering_rejected(self):
        with self.temporary() as folder,patch.object(s,"call_api",return_value={"ok":True}):
            root=pathlib.Path(folder);client=s.Client(root,self.fake_corpus());q={"input":["x"]}
            client.request("query",q)
            path=next(root.rglob("*.response.json"));r=s.read(path);r["attempt_sha256"]="wrong";path.write_bytes(s.raw(r))
            with self.assertRaisesRegex(ValueError,"캐시"):client.request("query",q)
    def test_question_path_and_citations_end_to_end(self):
        corpus=self.fake_corpus()
        vector=next(r["embedding"] for r in corpus["records"] if r["chunk_id"]==self.floor)
        def fake(kind,raw,env):
            if kind=="query":return {"model":s.QUERY_MODEL,"data":[{"index":0,"embedding":vector}]}
            return self.response(self.decision(self.floor))
        with self.temporary() as folder,patch.object(s,"load_corpus",return_value=corpus),patch.object(s,"call_api",side_effect=fake) as api,patch.object(s.time,"sleep"):
            root=pathlib.Path(folder);q="국가유공자 주차구획 바닥에 적힌 문구는?"
            first=s.search_combined(root,q)
            self.assertEqual(first["called"],3)
            self.assertEqual(first["result"]["evidence"][0]["primary_quote"],corpus["catalog"]["records"][self.floor]["primary_text"])
            before={str(p):s.digest(p.read_bytes()) for p in root.rglob("*") if p.is_file()}
            checked=s.search_combined(root,q,check_only=True)
            after={str(p):s.digest(p.read_bytes()) for p in root.rglob("*") if p.is_file()}
            self.assertEqual(before,after);self.assertEqual(checked["called"],0);self.assertEqual(api.call_count,3)
            self.assertFalse(checked["result"]["answer_allowed"])
    def test_runner_defaults_to_combined_and_keeps_legacy_option(self):
        runner_path=pathlib.Path(os.environ.get("RAG_TEST_RUNNER",PROJECT/"run.py"))
        spec=importlib.util.spec_from_file_location("combined_runner_test",runner_path)
        runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner);runner.PROJECT_ROOT=PROJECT
        import contextlib,io,search_attachment_chunks as old
        result={"decision":"insufficient_evidence","user_message":"근거 부족","missing_information":[],"required_external_materials":[],"evidence":[]}
        out={"result":result,"called":0,"output_file":"fixture.json"}
        with patch.object(s,"search_combined",return_value=out) as combined,patch.object(old,"search_reviewed",return_value=out) as legacy,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(["search-attachments","--question","test","--check-only"]),0)
            self.assertEqual(runner.main(["search-attachments","--question","test","--corpus","reviewed62","--check-only"]),0)
            combined.assert_called_once();legacy.assert_called_once()
if __name__=="__main__":unittest.main(verbosity=2)

