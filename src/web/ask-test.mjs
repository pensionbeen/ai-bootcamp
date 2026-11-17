import { TIMEOUT_MS, validateAskResponse } from "./ask-validation.mjs";

const byId = id => document.getElementById(id);
const form = byId("test-form");
const input = byId("question");
const result = byId("result");
const submit = byId("run-check");
const progress = byId("progress");
const rawToggle = byId("show-raw");
const responseBody = byId("response-body");
const stateLabels = {pass: "통과", fail: "실패", skip: "생략", info: "참고"};
let rawText = "";
let formattedText = "";

function refreshRequest() {
  byId("request-body").textContent = JSON.stringify({question: input.value.trim()}, null, 2);
  input.setCustomValidity("");
  result.hidden = true;
  progress.textContent = "";
}

function showResult(passed, checks, raw, formatted = null) {
  const verdict = byId("verdict");
  verdict.textContent = passed ? "검사 통과" : "검사 실패";
  verdict.className = passed ? "pass" : "fail";
  const rows = checks.map(({label, state, detail}) => {
    const item = document.createElement("li");
    const badge = document.createElement("span");
    badge.className = state;
    badge.textContent = stateLabels[state];
    const name = document.createElement("strong");
    name.textContent = label;
    const description = document.createElement("small");
    description.textContent = detail;
    item.append(badge, name, description);
    return item;
  });
  byId("checks").replaceChildren(...rows);
  rawText = raw;
  formattedText = formatted;
  rawToggle.checked = false;
  byId("raw-option").hidden = formatted === null;
  responseBody.textContent = formatted ?? (raw || "응답 본문 없음");
  result.hidden = false;
}

rawToggle.addEventListener("change", () => {
  responseBody.textContent = rawToggle.checked ? rawText : formattedText;
});

form.addEventListener("submit", async event => {
  event.preventDefault();
  if (submit.disabled) return;
  const question = input.value.trim();
  if (!question) {
    input.setCustomValidity("질문을 입력해 주세요.");
    input.reportValidity();
    return;
  }

  result.hidden = true;
  submit.disabled = true;
  input.readOnly = true;
  progress.textContent = "응답을 기다리는 중입니다. 최대 90초가 걸립니다.";
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), TIMEOUT_MS);
  const started = performance.now();
  let response;
  try {
    response = await fetch("/ask", {
      method: "POST",
      headers: {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
      body: JSON.stringify({question}),
      signal: controller.signal,
      cache: "no-store"
    });
    const bytes = await response.arrayBuffer();
    const elapsedMs = performance.now() - started;
    let raw;
    try {
      raw = new TextDecoder("utf-8", {fatal: true, ignoreBOM: true}).decode(bytes);
    } catch {
      showResult(false, [{label: "응답 인코딩", state: "fail",
        detail: `HTTP ${response.status} · 응답 본문을 UTF-8로 읽을 수 없습니다.` }], new TextDecoder().decode(bytes));
      return;
    }
    const report = validateAskResponse({
      status: response.status, contentType: response.headers.get("Content-Type"),
      ragStatus: response.headers.get("X-Rag-Status"), elapsedMs, rawBody: raw
    });
    showResult(report.passed, report.checks, raw, report.isJson ? JSON.stringify(report.parsed, null, 2) : null);
  } catch {
    const elapsed = ((performance.now() - started) / 1000).toFixed(2);
    const reason = controller.signal.aborted ? "90초 안에 응답을 모두 받지 못했습니다." : "서버 응답을 끝까지 받을 수 없습니다.";
    const status = response ? `HTTP ${response.status} · ` : "";
    showResult(false, [{label: "요청 전송", state: "fail", detail: `${reason} ${status}${elapsed}초 경과` }], "");
  } finally {
    clearTimeout(timeout);
    submit.disabled = false;
    input.readOnly = false;
    progress.textContent = "";
    byId("result-title").focus();
  }
});

input.addEventListener("input", refreshRequest);
refreshRequest();
