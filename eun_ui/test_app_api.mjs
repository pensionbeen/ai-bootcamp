// 새 화면(eun_ui/api.mjs)의 응답 처리 시험. 실행: node --test eun_ui/test_app_api.mjs
import assert from "node:assert/strict";
import test from "node:test";
import { askQuestion, citationItems, classify, inquiryUrl, jobToView, safeUrl, splitCitation } from "./api.mjs";

const JOB = "a".repeat(64);
const answerBody = {
  answer: "청년은 19세 이상 39세 이하입니다.",
  citation: ["서울특별시 청년 기본 조례 제3조 제1호", "서울특별시 청년 기본 조례 제24조"],
  source_url: "https://www.law.go.kr/ordinInfoP.do?ordinSeq=2054497",
  effective_date: "2026-10-01",
  status: "completed",
  job_id: JOB,
  sources: [
    { citation: "서울특별시 청년 기본 조례 제3조 제1호", source_url: "https://www.law.go.kr/a", effective_date: "2026-10-01" },
    { citation: "서울특별시 청년 기본 조례 제24조", source_url: "https://www.law.go.kr/b", effective_date: "2026-10-01" },
  ],
  as_of_date: "2026-10-01",
  result: { status: "code_checked_draft" },
};

function reply(status, body) {
  return { status, text: async () => (typeof body === "string" ? body : JSON.stringify(body)) };
}

function fakeFetch(steps) {
  const calls = [];
  const fetch = async (url, options) => {
    calls.push({ url, method: options.method });
    const step = steps.shift();
    if (!step) throw new Error("unexpected request " + url);
    if (step instanceof Error) throw step;
    return step;
  };
  return { fetch, calls };
}

test("근거가 있는 답은 answer, sources[]를 조항 목록으로 쓴다", () => {
  assert.equal(classify(answerBody, 200).kind, "answer");
  const items = citationItems(answerBody);
  assert.deepEqual(items.map((i) => i.url), ["https://www.law.go.kr/a", "https://www.law.go.kr/b"]);
});

test("sources[]가 없으면 첫 조항에만 맨 위 주소·시행일을 붙인다", () => {
  const view = { ...answerBody, sources: undefined };
  const items = citationItems(view);
  assert.equal(items[0].url, answerBody.source_url);
  assert.equal(items[1].url, undefined);
});

test("확인 불가는 근거 없음과 확정 못함을 나눈다", () => {
  const base = { answer: "확인 불가", citation: [], status: "completed", job_id: JOB };
  assert.deepEqual(classify({ ...base, result: { status: "out_of_scope" } }, 200).reason, "no_basis");
  assert.deepEqual(classify({ ...base, result: { status: "insufficient_evidence" } }, 200).reason, "no_basis");
  assert.deepEqual(classify({ ...base, result: { status: "verification_failed" } }, 200).reason, "not_confirmed");
});

test("시간 초과·대기열·오류는 확인 불가 결과가 아니라 다시 시도로 분류한다", () => {
  const unknownBody = { answer: "확인 불가", citation: [] };
  assert.equal(classify({ ...unknownBody, status: "deadline_exceeded", poll_url: "/jobs/" + JOB }, 200).kind, "poll");
  assert.equal(classify({ ...unknownBody, status: "deadline_exceeded" }, 200).kind, "retry");
  for (const status of ["queue_full", "processing_error", "failed", "interrupted"]) {
    assert.equal(classify({ ...unknownBody, status }, 200).kind, "retry", status);
  }
  assert.equal(classify({ status: "invalid_question" }, 400).kind, "error");
});

test("jobs 응답을 /ui/ask 모양으로 바꾼다", () => {
  const view = jobToView({
    job_id: JOB, status: "completed",
    result: { status: "code_checked_draft", response: { answer: "답", citation: ["조례 제1조"] }, sources: [], as_of_date: "2026-10-01" },
  });
  assert.equal(view.answer, "답");
  assert.equal(view.status, "completed");
  assert.equal(jobToView({ job_id: JOB, status: "running" }).status, "deadline_exceeded");
  assert.equal(jobToView({ job_id: JOB, status: "completed", result: { status: "processing_error" } }).status, "processing_error");
});

test("https 주소만 링크로 쓴다", () => {
  assert.equal(safeUrl("https://www.law.go.kr/x"), "https://www.law.go.kr/x");
  assert.equal(safeUrl("javascript:alert(1)"), null);
  assert.equal(safeUrl("http://www.law.go.kr/x"), null);
});

test("조항 라벨을 조례 이름과 조·항으로 나눈다", () => {
  assert.deepEqual(splitCitation("서울특별시 청년 기본 조례 제3조 제1호"), { law: "서울특별시 청년 기본 조례", part: "제3조 제1호" });
  assert.deepEqual(splitCitation("서울특별시 주차장 설치 및 관리 조례 별표 2"), { law: "서울특별시 주차장 설치 및 관리 조례", part: "별표 2" });
  assert.deepEqual(splitCitation("서울특별시 금연환경 조성 및 간접흡연 피해방지조례 제10조 제1항"),
    { law: "서울특별시 금연환경 조성 및 간접흡연 피해방지조례", part: "제10조 제1항" });
  assert.deepEqual(splitCitation("알 수 없는 라벨"), { law: "알 수 없는 라벨", part: "" });
});

test("늦어진 질문은 /jobs를 조회해 끝난 결과를 돌려준다", async () => {
  const { fetch, calls } = fakeFetch([
    reply(200, { answer: "확인 불가", citation: [], status: "deadline_exceeded", job_id: JOB, poll_url: "/jobs/" + JOB }),
    reply(202, { job_id: JOB, status: "running", poll_url: "/jobs/" + JOB }),
    new TypeError("network"),
    reply(200, { job_id: JOB, status: "completed", result: { status: "code_checked_draft", response: { answer: "답", citation: ["조례 제1조"] } } }),
  ]);
  let polls = 0;
  const outcome = await askQuestion("질문", { onPoll: () => { polls += 1; } }, { fetch, sleep: async () => {}, askTimeoutMs: 1000 });
  assert.equal(outcome.info.kind, "answer");
  assert.equal(outcome.view.answer, "답");
  assert.equal(polls, 3);
  assert.deepEqual(calls.map((c) => c.method), ["POST", "GET", "GET", "GET"]);
  assert.ok(calls.slice(1).every((c) => c.url === "/jobs/" + JOB));
});

test("조회는 10분 상한에서 멈춘다", async () => {
  let clock = 0;
  const steps = [reply(200, { answer: "확인 불가", citation: [], status: "deadline_exceeded", job_id: JOB, poll_url: "/jobs/" + JOB })];
  for (let i = 0; i < 400; i += 1) steps.push(reply(202, { job_id: JOB, status: "running", poll_url: "/jobs/" + JOB }));
  const { fetch } = fakeFetch(steps);
  const outcome = await askQuestion("질문", {}, { fetch, now: () => clock, sleep: async () => { clock += 3000; }, askTimeoutMs: 1000 });
  assert.equal(outcome.info.kind, "retry");
});

test("서버 연결 실패와 JSON이 아닌 응답을 오류로 안내한다", async () => {
  const offline = await askQuestion("질문", {}, { fetch: fakeFetch([new TypeError("offline")]).fetch, askTimeoutMs: 1000 });
  assert.equal(offline.info.kind, "error");
  const broken = await askQuestion("질문", {}, { fetch: fakeFetch([reply(502, "<html>bad gateway</html>")]).fetch, askTimeoutMs: 1000 });
  assert.equal(broken.info.kind, "error");
});

test("국민신문고 문의 주소는 서버가 준 https 주소만 쓴다", () => {
  const base = { answer: "확인 불가", citation: [], status: "completed" };
  assert.equal(inquiryUrl({ ...base, result: { status: "out_of_scope", inquiry_url: "https://www.epeople.go.kr/" } }), "https://www.epeople.go.kr/");
  assert.equal(inquiryUrl({ ...base, result: { status: "out_of_scope" } }), null);
  assert.equal(inquiryUrl({ ...base, result: { status: "out_of_scope", inquiry_url: "javascript:alert(1)" } }), null);
  assert.equal(inquiryUrl({ ...base }), null);
});
