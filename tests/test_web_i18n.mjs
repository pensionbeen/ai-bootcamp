// 다국어 화면 글자(src/web/app/i18n.mjs)와 번역 요청(api.mjs) 시험. 실행: node --test tests/test_web_i18n.mjs
import assert from "node:assert/strict";
import test from "node:test";
import { classify, translateAnswer, translateQuestion } from "../src/web/app/api.mjs";
import { CATEGORIES } from "../src/web/app/examples.mjs";
import { LANGS, pickLang, setLang, speechLang, t } from "../src/web/app/i18n.mjs";

const JOB = "a".repeat(64);
const reply = (status, body) => ({ status, text: async () => JSON.stringify(body) });

test("처음 언어는 주소 → 저장값 → 브라우저 언어 → 한국어 순서로 정한다", () => {
  assert.equal(pickLang("?lang=ja", "en", ["ko-KR"]), "ja");
  assert.equal(pickLang("?lang=fr", "en", ["ko-KR"]), "en");
  assert.equal(pickLang("", null, ["zh-TW"]), "zh-TW");
  assert.equal(pickLang("", null, ["zh-Hant-HK"]), "zh-TW");
  assert.equal(pickLang("", null, ["zh-CN"]), "zh-CN");
  assert.equal(pickLang("", null, ["en-US", "ko"]), "en");
  assert.equal(pickLang("", null, ["fr-FR"]), "ko");
});

test("번역은 한국어 문장을 열쇠로 쓰고, 없는 문장은 한국어 그대로 둔다", () => {
  setLang("en");
  assert.equal(t("질문하기"), "Ask");
  assert.equal(t("기준일 {d}", { d: "2026-10-01" }), "as of 2026-10-01");
  assert.equal(t("표에 없는 문장"), "표에 없는 문장");
  assert.equal(speechLang(), "en-US");
  setLang("ja");
  assert.equal(speechLang(), "ja-JP");
  setLang("xx");
  assert.equal(t("질문하기"), "질문하기");
});

test("서버 안내 문장도 고른 언어로 나온다", () => {
  setLang("zh-CN");
  assert.match(classify({ status: "queue_full" }, 200).text, /稍后/);
  assert.match(classify({}, 502).text, /HTTP 502/);
  setLang("ko");
  assert.match(classify({ status: "queue_full" }, 200).text, /대기열/);
});

test("모든 외국어에 예시 질문 글자가 있고, 검색은 한국어 질문으로 한다", () => {
  for (const { code } of LANGS.filter((item) => item.code !== "ko")) {
    for (const category of CATEGORIES) {
      assert.ok(category.names[code] && category.laws[code], category.id + " " + code);
      for (const item of category.questions) {
        assert.ok(item.labels[code], item.label + " " + code);
        assert.match(item.question, /[가-힣]/);
      }
    }
  }
});

test("질문 번역: 성공하면 한국어 질문, 실패하면 다시 보내기 안내", async () => {
  setLang("en");
  let sent;
  const ok = await translateQuestion("smoking fine?", "en", { fetch: async (url, options) => { sent = JSON.parse(options.body); return reply(200, { question_ko: " 금연구역 과태료? " }); } });
  assert.deepEqual(ok, { ok: true, korean: "금연구역 과태료?" });
  assert.deepEqual(sent, { question: "smoking fine?", lang: "en" });
  const failed = await translateQuestion("x", "en", { fetch: async () => reply(502, { error: "translate_failed" }) });
  assert.equal(failed.ok, false);
  assert.match(failed.text, /translate/);
  const busy = await translateQuestion("x", "en", { fetch: async () => reply(429, {}) });
  assert.match(busy.text, /busy/);
  setLang("ko");
});

test("답 번역은 서버가 숫자를 확인한(verified) 것만 쓴다", async () => {
  const good = await translateAnswer(JOB, "en", { fetch: async () => reply(200, { answer: { text: "KRW 100,000 (10만원)", verified: true } }) });
  assert.deepEqual(good, { text: "KRW 100,000 (10만원)", verified: true });
  assert.equal(await translateAnswer(JOB, "en", { fetch: async () => reply(200, { answer: { text: "원문", verified: false } }) }), null);
  assert.equal(await translateAnswer(JOB, "en", { fetch: async () => reply(200, { answer: null }) }), null);
  assert.equal(await translateAnswer(JOB, "en", { fetch: async () => { throw new Error("down"); } }), null);
  assert.equal(await translateAnswer("bad", "en", { fetch: async () => assert.fail("요청하면 안 됨") }), null);
});

test("번역문 형광펜은 글자가 아니라 숫자 값으로 맞춘다", async () => {
  const { markValues, numberValues } = await import("../src/web/app/api.mjs");
  const values = numberValues("금연구역에서 흡연하면 10만원, 19세 이상 39세 이하");
  const marked = (text) => markValues(text, values).filter((part) => part.mark).map((part) => part.text);
  assert.deepEqual(marked("a fine of KRW 100,000 (10만원) under Article 10"), ["100,000", "10만원"]);
  assert.deepEqual(marked("罚款10万韩元，第10条"), ["10万"]);
  assert.deepEqual(marked("19歳以上39歳以下"), ["19", "39"]);
  assert.deepEqual(marked("KRW 50,000"), []);
  assert.equal(markValues("원문 그대로", values).map((part) => part.text).join(""), "원문 그대로");
});
