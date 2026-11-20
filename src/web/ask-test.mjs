import { TIMEOUT_MS, validateAskResponse } from "./ask-validation.mjs";

const byId = id => document.getElementById(id);
const input = byId("question");
const result = byId("result");
const submit = byId("run-check");
const stateLabels = {pass: "통과", fail: "실패", skip: "생략", info: "참고"};
const diagnosticErrors = {
  submission_check_not_configured: "운영 서버에 검사할 공개 주소가 설정되지 않았습니다.",
  submission_check_busy: "제출 검사기가 사용 중입니다. 잠시 후 다시 시도해 주세요.",
  upstream_unreachable: "제출 검사기가 공개 /ask 서버에 연결하지 못했습니다.",
  upstream_timeout: "공개 /ask에서 제한 시간 안에 응답을 모두 받지 못했습니다.",
  upstream_body_too_large: "응답이 제출 검사기가 읽을 수 있는 크기를 초과했습니다.",
  upstream_invalid_utf8: "응답을 UTF-8로 읽을 수 없습니다."
};

function refreshRequest() {
  byId("request-body").textContent = JSON.stringify({question: input.value.trim()}, null, 2);
  input.setCustomValidity("");
  result.hidden = true;
}

function makeReport(client, title) {
  const card = byId("report-template").content.firstElementChild.cloneNode(true);
  card.dataset.client = client;
  card.querySelector(".client-name").textContent = title;
  byId("reports").append(card);
  return card;
}

function showReport(card, passed, checks, raw, formatted = null, note = "") {
  const outcome = card.querySelector(".outcome");
  outcome.textContent = passed ? "검사 통과" : "검사 실패";
  outcome.className = "outcome " + (passed ? "pass" : "fail");
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
  card.querySelector(".checks").replaceChildren(...rows);
  card.querySelector(".detail-note").textContent = note;
  const responseBody = card.querySelector(".response-body");
  responseBody.textContent = formatted ?? (raw || "응답 본문 없음");
  if (formatted !== null) {
    const rawOption = card.querySelector(".raw-option");
    rawOption.hidden = false;
    rawOption.querySelector("input").addEventListener("change", event => {
      responseBody.textContent = event.target.checked ? raw : formatted;
    });
  }
  card.querySelector(".result-grid").hidden = false;
}

// The diagnostic envelope reports the public /ask response seen by Python.
// The envelope's own HTTP status is only about running the check.
function readDiagnostic(response, text) {
  let payload;
  try { payload = JSON.parse(text); } catch { /* The diagnostic endpoint itself may be unavailable. */ }
  if (!response.ok || !Number.isInteger(payload?.http_status) ||
      !Number.isFinite(payload.elapsed_ms) || typeof payload.body !== "string") {
    const reason = diagnosticErrors[payload?.error] || "제출 검사기에서 결과를 받지 못했습니다.";
    throw new Error(`${reason} (검사 요청 HTTP ${response.status})`);
  }
  return {status: payload.http_status, contentType: payload.content_type, ragStatus: payload.rag_status,
    elapsedMs: payload.elapsed_ms, rawBody: payload.body};
}

async function runCheck(card, path, body) {
  const diagnostic = path === "/test/submission";
  const controller = new AbortController();
  // The measured public request still has 90 s. Allow the checker time to return its measurement.
  const timeout = setTimeout(() => controller.abort(), TIMEOUT_MS + (diagnostic ? 10_000 : 0));
  const started = performance.now();
  try {
    const response = await fetch(path, {
      method: "POST", headers: {"Content-Type": "application/json; charset=utf-8", Accept: "application/json"},
      body, signal: controller.signal, cache: "no-store"
    });
    const bytes = await response.arrayBuffer();
    const elapsedMs = performance.now() - started;
    let text;
    try {
      text = new TextDecoder("utf-8", {fatal: true, ignoreBOM: true}).decode(bytes);
    } catch {
      showReport(card, false, [{label: "응답 인코딩", state: "fail", detail: `HTTP ${response.status} · UTF-8 해석 실패` }], new TextDecoder().decode(bytes));
      return false;
    }
    const sample = diagnostic ? readDiagnostic(response, text) : {
      status: response.status, contentType: response.headers.get("Content-Type"),
      ragStatus: response.headers.get("X-Rag-Status"), elapsedMs, rawBody: text
    };
    const report = validateAskResponse(sample);
    const note = sample.status === 403 && /error code:\s*1010/i.test(sample.rawBody)
      ? "Cloudflare가 이 클라이언트의 요청을 차단했습니다(1010). 질문이 조례 API에 도달하기 전의 응답입니다." : "";
    const formatted = report.isJson ? JSON.stringify(report.parsed, null, 2) : null;
    showReport(card, report.passed, report.checks, sample.rawBody, formatted, note);
    return report.passed;
  } catch (error) {
    const seconds = ((performance.now() - started) / 1000).toFixed(2);
    const message = controller.signal.aborted ? "응답 대기 시간이 초과됐습니다." :
      error instanceof TypeError ? "서버에 연결하거나 응답을 읽지 못했습니다." : error.message;
    showReport(card, false, [{label: "요청 결과", state: "fail", detail: `${message} · ${seconds}초 경과` }], "");
    return false;
  } finally {
    clearTimeout(timeout);
  }
}

byId("test-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (submit.disabled) return;
  const question = input.value.trim();
  if (!question) {
    input.setCustomValidity("질문을 입력해 주세요.");
    input.reportValidity();
    return;
  }
  const body = JSON.stringify({question});
  const verdict = byId("verdict");
  submit.disabled = true;
  input.readOnly = true;
  verdict.className = "info";
  verdict.textContent = "검사 중";
  byId("reports").replaceChildren();
  const browser = makeReport("browser", "브라우저 직접 요청");
  const submission = makeReport("submission", "저장소 제출 검사기 (Python urllib)");
  result.hidden = false;
  try {
    const outcomes = await Promise.all([runCheck(browser, "/ask", body), runCheck(submission, "/test/submission", body)]);
    const count = outcomes.filter(Boolean).length;
    verdict.className = count === 2 ? "pass" : "fail";
    verdict.textContent = `두 방식 중 ${count}개 통과`;
  } finally {
    submit.disabled = false;
    input.readOnly = false;
    byId("result-title").focus();
  }
});

input.addEventListener("input", refreshRequest);
refreshRequest();
