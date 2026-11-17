import assert from "node:assert/strict";
import test from "node:test";
import {TIMEOUT_MS, validateAskResponse} from "../src/web/ask-validation.mjs";

const answer = {
  answer: "답변", citation: ["조례 제1조"],
  source_url: "https://www.law.go.kr/example", effective_date: "2026-10-01",
};
const unknown = {answer: "확인 불가", citation: [], source_url: "", effective_date: ""};

function check(body, options = {}) {
  return validateAskResponse({
    status: 200, contentType: "application/json; charset=utf-8", ragStatus: "completed",
    elapsedMs: 200, rawBody: JSON.stringify(body), ...options,
  });
}

function state(result, id) {
  return result.checks.find(row => row.id === id).state;
}

test("accepts both published and completed unknown responses", () => {
  const result = check(answer);
  assert.ok(result.passed);
  assert.deepEqual(result.parsed, answer);
  assert.ok(check(unknown).passed);
  assert.ok(check({...unknown, answer: "확인 불가 (자료 없음)"}).passed);
  const noHeader = check(unknown, {ragStatus: null});
  assert.equal(state(noHeader, "outcome"), "info");
  assert.ok(noHeader.passed);
});

test("recognizes operational failures even in a valid 200 fallback", () => {
  for (const ragStatus of ["deadline_exceeded", "queue_full", "interrupted", "processing_error", "failed"]) {
    const result = check(unknown, {ragStatus});
    assert.equal(state(result, "fields"), "pass");
    assert.equal(state(result, "outcome"), "fail");
    assert.equal(result.passed, false);
  }
});

test("validates status, media type and deadline independently of JSON shape", () => {
  for (const [options, id] of [
    [{status: 202}, "http"], [{elapsedMs: TIMEOUT_MS + 1}, "time"],
    [{contentType: "text/html"}, "mime"], [{contentType: null}, "mime"],
  ]) {
    const result = check(answer, options);
    assert.equal(state(result, id), "fail");
    assert.equal(result.passed, false);
  }
  assert.ok(check(answer, {elapsedMs: TIMEOUT_MS, contentType: "Application/JSON;charset=UTF-8"}).passed);
});

test("distinguishes malformed JSON from non-object JSON and skips field checks", () => {
  for (const [rawBody, isJson] of [["<html>oops", false], ["", false], ["null", true], ["[1]", true], ['"OK"', true]]) {
    const result = check(answer, {rawBody});
    assert.equal(result.isJson, isJson);
    assert.equal(state(result, "json"), "fail");
    assert.equal(state(result, "fields"), "skip");
    assert.equal(result.passed, false);
  }
});

test("rejects missing or extra keys, invalid types, and missing metadata on published answers", () => {
  for (const [body, id] of [
    [{answer: "답변"}, "fields"], [{...answer, status: "completed"}, "fields"],
    [{...answer, answer: "  "}, "types"], [{...answer, citation: [1]}, "types"],
    [{...answer, citation: "제1조"}, "types"], [{...answer, source_url: []}, "types"],
    [{...answer, effective_date: []}, "types"], [{...answer, effective_date: "20261001"}, "source"],
    [{...answer, source_url: ""}, "source"], [{...unknown, citation: ["제1조"]}, "source"],
  ]) {
    const result = check(body);
    assert.equal(state(result, id), "fail");
    assert.equal(result.passed, false);
  }
});
