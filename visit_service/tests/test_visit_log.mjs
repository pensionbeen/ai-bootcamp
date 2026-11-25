import test from "node:test";
import assert from "node:assert/strict";
import { createRecord, createVisitLoader, displayTime, visitUrl } from "../web/visit-log.mjs";

function page(overrides = {}) {
  return {
    items: [{ seq: 1, request_id: "visit-1", received_at: "2026-11-25T00:00:00Z", path: "/", http_status: 200 }],
    total: 1, next_before: null, summary: { today: 1, last_7_days: 1 },
    retention: { days: 30, max_records: 10_000 }, capturing_since: "2026-11-25T00:00:00Z",
    ...overrides
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const response = data => ({ ok: true, status: 200, json: async () => data });

test("uses server-side period filtering and positive sequence cursors", async () => {
  let request;
  const loader = createVisitLoader(async (url, options) => {
    request = { url, options };
    return response(page());
  });
  assert.equal((await loader.load({ period: "7d", before: 51 })).kind, "ok");
  assert.equal(request.url, "/admin/visits/data?period=7d&before=51");
  assert.equal(request.options.credentials, "same-origin");
  assert.equal(request.options.cache, "no-store");
  assert.equal(request.options.headers.Accept, "application/json");
  assert.equal(visitUrl(), "/admin/visits/data?period=all");
  assert.throws(() => visitUrl({ period: "invalid" }));
  for (const before of [0, -1, 1.5, "51", Number.MAX_SAFE_INTEGER + 1]) {
    assert.throws(() => visitUrl({ period: "today", before }));
  }
});

test("a delayed old filter response cannot replace the selected period even if abort is ignored", async () => {
  const oldResponse = deferred();
  let oldSignal;
  const loader = createVisitLoader((url, options) => {
    if (url.includes("period=all")) {
      oldSignal = options.signal;
      return oldResponse.promise;
    }
    return Promise.resolve(response(page({ total: 7 })));
  });
  const oldResult = loader.load({ period: "all" });
  const selectedResult = await loader.load({ period: "today" });
  assert.equal(oldSignal.aborted, true);
  assert.equal(selectedResult.kind, "ok");
  assert.equal(selectedResult.data.total, 7);
  oldResponse.resolve(response(page({ total: 99 })));
  assert.deepEqual(await oldResult, { kind: "superseded" });
});

test("cancels an older response while its JSON body is still parsing", async () => {
  const oldBody = deferred();
  let bodyStarted = false;
  const loader = createVisitLoader(async url => url.includes("period=all") ? {
    ok: true, status: 200, json: () => { bodyStarted = true; return oldBody.promise; }
  } : response(page()));
  const oldResult = loader.load({ period: "all" });
  await Promise.resolve();
  assert.equal(bodyStarted, true);
  assert.equal((await loader.load({ period: "7d" })).kind, "ok");
  oldBody.resolve(page({ total: 99 }));
  assert.deepEqual(await oldResult, { kind: "superseded" });
});

test("explicit cancellation suppresses an in-flight result", async () => {
  const pending = deferred();
  const loader = createVisitLoader(() => pending.promise);
  const result = loader.load({ period: "all" });
  loader.cancel();
  pending.resolve(response(page()));
  assert.deepEqual(await result, { kind: "superseded" });
});

test("authentication failure is distinguished without reading the response body", async () => {
  const loader = createVisitLoader(async () => ({
    status: 401, ok: false, json: () => { throw new Error("401 body must not be parsed"); }
  }));
  const result = await loader.load({ period: "all" });
  assert.equal(result.kind, "auth");
  assert.match(result.message, /다시 로그인/);
});

test("request timeout aborts the connection and provides a useful retry message", async () => {
  let onTimeout;
  let requestSignal;
  let cleared = false;
  const loader = createVisitLoader((_url, { signal }) => {
    requestSignal = signal;
    return new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(new Error("aborted"))));
  }, { setTimer(callback) { onTimeout = callback; return 123; }, clearTimer(timer) { assert.equal(timer, 123); cleared = true; } });
  const result = loader.load({ period: "all" });
  onTimeout();
  assert.equal(requestSignal.aborted, true);
  assert.deepEqual(await result, { kind: "error", message: "기록 조회 시간이 초과됐습니다. 다시 시도해 주세요." });
  assert.equal(cleared, true);
});

test("invalid payloads do not reach page rendering", async () => {
  for (const invalid of [null, page({ total: -1 }), page({ next_before: 0 }), page({ summary: {} }),
    page({ items: [{ seq: 1, received_at: "invalid", path: "/", http_status: 999 }] })]) {
    const result = await createVisitLoader(async () => response(invalid)).load({ period: "all" });
    assert.equal(result.kind, "error");
    assert.match(result.message, /응답 형식/);
  }
  const malformed = createVisitLoader(async () => ({ ok: true, status: 200, json() { throw new SyntaxError("bad JSON"); } }));
  assert.match((await malformed.load({ period: "all" })).message, /응답 형식/);
});

test("displayed dates use Korea time across UTC day boundaries", () => {
  const text = displayTime("2026-11-24T15:00:00Z");
  assert.match(text, /2026.*11.*25/);
  assert.match(text, /00:00:00/);
  assert.equal(displayTime("not-a-date"), "시각 확인 불가");
});

test("visitor-controlled strings are rendered as text and never inserted as markup or links", () => {
  // Fail immediately if rendering starts using HTML rather than text nodes.
  const element = () => ({
    textContent: "", children: [], hidden: false,
    set innerHTML(_value) { throw new Error("Unsafe innerHTML assignment"); },
    append(...nodes) { this.children.push(...nodes); }
  });
  const selectors = Object.fromEntries([".kind", "time", ".status", ".bot", ".path", ".ip", "dl"].map(key => [key, element()]));
  const card = { dataset: {}, classList: { toggle() {} }, querySelector: selector => selectors[selector] };
  const document = {
    getElementById: () => ({ content: { firstElementChild: { cloneNode: () => card } } }),
    createElement(tag) { assert.ok(tag === "dt" || tag === "dd"); return element(); }
  };
  const attack = '<img src=x onerror="alert(1)">';
  const record = { ...page().items[0], path: attack, reported_client_ip: attack,
    user_agent: attack, referrer: "javascript:alert(1)", reported_cf_ray: attack, request_id: attack, is_bot: true };
  createRecord(document, record);
  assert.equal(selectors[".path"].textContent, attack);
  assert.equal(selectors[".ip"].textContent, "IP " + attack);
  const details = selectors.dl.children.filter((_node, index) => index % 2 === 1).map(node => node.textContent);
  assert.ok(details.includes(attack));
  assert.ok(details.includes("javascript:alert(1)"));
  assert.equal(selectors[".bot"].hidden, false);
});
