// 배포 화면(src/web/app/api.mjs)의 응답 처리 시험. 실행: node --test tests/test_web_app_api.mjs
import assert from "node:assert/strict";
import test from "node:test";
import { askQuestion, citationItems, classify, inquiryUrl, jobToView, markNumbers, numberTokens, requestDraft, requestSources, safeUrl, sendPhoto, splitCitation } from "../src/web/app/api.mjs";

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

test("처음 화면으로 돌아가 취소하면 첫 요청을 멈추고 cancelled를 돌려준다", async () => {
  const controller = new AbortController();
  let seenSignal = null;
  const fetch = (url, options) => new Promise((resolve, reject) => {
    seenSignal = options.signal;
    options.signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })));
  });
  const pending = askQuestion("질문", { signal: controller.signal }, { fetch, askTimeoutMs: 60000 });
  controller.abort();
  const outcome = await pending;
  assert.equal(outcome.cancelled, true);
  assert.equal(seenSignal.aborted, true);
});

test("조회 중에 취소하면 더 조회하지 않는다", async () => {
  const controller = new AbortController();
  const steps = [reply(200, { answer: "확인 불가", citation: [], status: "deadline_exceeded", job_id: JOB, poll_url: "/jobs/" + JOB })];
  for (let i = 0; i < 10; i += 1) steps.push(reply(202, { job_id: JOB, status: "running", poll_url: "/jobs/" + JOB }));
  const { fetch, calls } = fakeFetch(steps);
  let sleeps = 0;
  const sleep = async () => { sleeps += 1; if (sleeps === 2) controller.abort(); };
  const outcome = await askQuestion("질문", { signal: controller.signal }, { fetch, sleep, askTimeoutMs: 1000 });
  assert.equal(outcome.cancelled, true);
  assert.deepEqual(calls.map((c) => c.method), ["POST", "GET"]);
});

test("이미 취소된 신호면 요청을 보내지 않는다", async () => {
  const controller = new AbortController();
  controller.abort();
  const { fetch, calls } = fakeFetch([]);
  const outcome = await askQuestion("질문", { signal: controller.signal }, { fetch });
  assert.equal(outcome.cancelled, true);
  assert.equal(calls.length, 0);
});

test("민원 초안 요청은 job_id와 상황만 보내고 https 신청 사이트만 남긴다", async () => {
  const draft = { title: "t", body: "본문", ai: true, notice: null,
    sites: [{ name: "응답소", url: "https://eungdapso.seoul.go.kr/" }, { name: "나쁜", url: "javascript:alert(1)" }] };
  const sent = [];
  const fetch = async (url, options) => { sent.push({ url, body: JSON.parse(options.body) }); return reply(200, { status: "ok", draft }); };
  const outcome = await requestDraft(JOB, "공원에서 흡연", { fetch });
  assert.equal(outcome.ok, true);
  assert.deepEqual(sent, [{ url: "/ui/draft", body: { job_id: JOB, details: "공원에서 흡연" } }]);
  assert.deepEqual(outcome.draft.sites.map((s) => s.name), ["응답소"]);
});

test("민원 초안: 잘못된 job_id는 보내지 않고, 429·오류는 안내 문구로", async () => {
  const none = await requestDraft("x", "", { fetch: async () => { throw new Error("no call"); } });
  assert.equal(none.ok, false);
  const busy = await requestDraft(JOB, "", { fetch: async () => reply(429, { error: "draft_busy" }) });
  assert.match(busy.text, /많아요/);
  const down = await requestDraft(JOB, "", { fetch: async () => { throw new TypeError("offline"); } });
  assert.equal(down.ok, false);
});

test("사진 보내기: 성공·글자 없음·과부하를 안내한다", async () => {
  const ok = await sendPhoto("AAAA", "image/jpeg", { fetch: async (url, options) => {
    assert.equal(url, "/ui/photo");
    assert.deepEqual(JSON.parse(options.body), { image: "AAAA", mime: "image/jpeg", lang: "ko" });
    return reply(200, { status: "ok", text: "금연구역", question: "서울시 금연구역 과태료는?", ai: true });
  } });
  assert.deepEqual(ok, { ok: true, text: "금연구역", question: "서울시 금연구역 과태료는?", display: null, translation: null, ai: true });
  assert.match((await sendPhoto("A", "image/jpeg", { fetch: async () => reply(422, { error: "no_text" }) })).text, /글자를 찾지/);
  assert.match((await sendPhoto("A", "image/jpeg", { fetch: async () => reply(429, { error: "photo_busy" }) })).text, /많아요/);
});

test("조문 원문 요청은 job_id로 보내고 실패하면 법제처 원문 보기를 안내한다", async () => {
  const ok = await requestSources(JOB, { fetch: async () => reply(200, { sources: [{ citation: "c", found: true, text: "t" }, { bad: 1 }] }) });
  assert.deepEqual(ok, { ok: true, sources: [{ citation: "c", found: true, text: "t" }] });
  assert.equal((await requestSources("x", { fetch: async () => { throw new Error("no"); } })).ok, false);
  assert.match((await requestSources(JOB, { fetch: async () => reply(500, {}) })).text, /원문 보기/);
});

test("원문 형광펜: 단위가 같은 숫자와 세 자리 이상 숫자만, 조문 번호는 표시하지 않는다", () => {
  const tokens = numberTokens("흡연하면 10만원의 과태료. 청년은 19세 이상 39세 이하. 5분당 500원. 제10조");
  const marked = (text) => markNumbers(text, tokens).filter((p) => p.mark).map((p) => p.text);
  assert.deepEqual(marked("제5조제4항을 위반한 사람은 10만원의 과태료"), ["10만원"]);
  assert.deepEqual(marked("19세 이상 39세 이하의 사람, 제19조"), ["19세", "39세"]);
  assert.deepEqual(marked("1회주차시5분당: 500 / 1500"), ["5분", "500"]);
  const all = markNumbers("원문 그대로 10만원 끝", tokens).map((p) => p.text).join("");
  assert.equal(all, "원문 그대로 10만원 끝");
});
