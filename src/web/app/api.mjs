// 서버 응답 처리 (화면 요소를 건드리지 않는 순수 로직).
// docs/ui-guide.md 3장과 src/local_rag_service.py의 sync_view를 따릅니다.
// 기존 기본 화면(src/web/index.html)의 jobToView·classify와 같은 기준입니다.

export const UNKNOWN = "확인 불가";
export const POLL_MS = 3000;
export const POLL_LIMIT_MS = 10 * 60 * 1000;
// 서버는 /ui/ask에 최대 80초 안에 답하므로 브라우저 쪽 제한은 그보다 길게 둡니다.
export const ASK_TIMEOUT_MS = 100 * 1000;
const JOB_PATH = /^\/jobs\/[0-9a-f]{64}$/;

// GET /jobs/<id> 응답을 /ui/ask 응답과 같은 모양으로 바꿉니다.
export function jobToView(job) {
  const result = job && job.result && typeof job.result === "object" ? job.result : {};
  const response = result.response;
  let status = job.status;
  const view = { answer: UNKNOWN, citation: [], job_id: job.job_id };
  if (status === "completed" && result.status === "processing_error") status = "processing_error";
  if (status === "completed" && response && typeof response === "object" && typeof response.answer === "string") {
    view.answer = response.answer;
    view.citation = Array.isArray(response.citation) ? response.citation : [];
    view.source_url = response.source_url;
    view.effective_date = response.effective_date;
  } else if (status === "queued" || status === "running") {
    status = "deadline_exceeded";
  }
  view.status = status;
  if ("sources" in result) view.sources = result.sources;
  if ("as_of_date" in result) view.as_of_date = result.as_of_date;
  if (job.poll_url) view.poll_url = job.poll_url;
  if (job.message) view.message = job.message;
  if (job.result) view.result = job.result;
  return view;
}

function pick(value, index, count) {
  if (Array.isArray(value)) return value[index];
  // 문자열 하나는 첫 조항의 값입니다. 조항이 하나뿐일 때만 다른 자리에도 확실합니다.
  if (typeof value === "string" && (index === 0 || count === 1)) return value;
  return undefined;
}

// 근거 조항 목록: sources[]가 있으면 그것을, 없으면 맨 위 citation·source_url·effective_date를 씁니다.
export function citationItems(view) {
  const citations = Array.isArray(view.citation) ? view.citation.filter((c) => typeof c === "string") : [];
  if (!citations.length) return [];
  if (Array.isArray(view.sources) && view.sources.length) {
    const items = view.sources
      .filter((s) => s && typeof s.citation === "string")
      .map((s) => ({ label: s.citation, url: s.source_url, date: s.effective_date }));
    if (items.length) return items;
  }
  return citations.map((c, i) => ({
    label: c,
    url: pick(view.source_url, i, citations.length),
    date: pick(view.effective_date, i, citations.length),
  }));
}

// https:// 로 시작하는 주소만 링크로 씁니다.
export function safeUrl(url) {
  return typeof url === "string" && url.startsWith("https://") ? url : null;
}

// 국민신문고 문의 주소: 근거 부족·범위 밖으로 끝난 확인 불가에만 서버가 result.inquiry_url로 보냅니다.
// 없거나 https가 아니면 null(링크를 숨김). docs/ui-guide.md 3-1 (나).
export function inquiryUrl(view) {
  const result = view && view.result && typeof view.result === "object" ? view.result : null;
  return result ? safeUrl(result.inquiry_url) : null;
}

// 조항 라벨을 "조례 이름"과 "조·항·별표" 부분으로 나눕니다. 예:
// "서울특별시 청년 기본 조례 제3조 제1호" → { law: "서울특별시 청년 기본 조례", part: "제3조 제1호" }
export function splitCitation(label) {
  const match = /^(.*?(?:조례|조례안))\s+((?:제\d|별표|부칙).*)$/.exec(label);
  if (!match) return { law: label, part: "" };
  return { law: match[1], part: match[2] };
}

const ERROR_TEXT = {
  invalid_question: "질문을 입력해 주세요. 너무 긴 질문은 줄여 주세요.",
  request_too_large: "질문이 너무 깁니다. 줄여서 다시 보내 주세요.",
  local_origin_required: "이 주소에서는 질문을 보낼 수 없습니다. 서비스 주소로 직접 접속해 주세요.",
  job_not_found: "진행 중이던 질문을 찾을 수 없습니다. 다시 보내 주세요.",
};

// 응답 하나를 화면에서 어떻게 보여 줄지 정합니다.
// kind: answer(근거 있는 답) | unknown(확인 불가 결과) | poll(아직 처리 중) | retry(다시 시도) | error(요청 오류)
// unknown은 결과이고, retry·error는 근거가 없다는 뜻이 아닙니다.
export function classify(view, httpStatus) {
  const status = view && typeof view.status === "string" ? view.status : "";
  if (httpStatus >= 400 && !(view && view.job_id)) {
    return { kind: "error", text: ERROR_TEXT[status] || `요청을 처리하지 못했습니다(HTTP ${httpStatus}). 잠시 후 다시 시도해 주세요.` };
  }
  if (status === "completed") {
    if (citationItems(view).length) return { kind: "answer", text: "" };
    const resultStatus = view.result && typeof view.result.status === "string" ? view.result.status : "";
    if (resultStatus === "out_of_scope" || resultStatus === "insufficient_evidence") {
      return { kind: "unknown", reason: "no_basis", text: "서울시 조례 6종에서 이 질문의 근거 조항을 찾지 못했습니다." };
    }
    return { kind: "unknown", reason: "not_confirmed", text: "근거 후보는 찾았지만 답을 확정하지 못했습니다." };
  }
  if (status === "deadline_exceeded") {
    if (typeof view.poll_url === "string" && JOB_PATH.test(view.poll_url)) {
      return { kind: "poll", text: "답을 만드는 데 시간이 더 걸리고 있어요. 3초마다 결과를 확인합니다." };
    }
    return { kind: "retry", text: "답이 늦어지고 있습니다. 잠시 후 같은 질문을 다시 보내 주세요." };
  }
  if (status === "queue_full") return { kind: "retry", text: "지금 질문이 많아 대기열이 가득 찼습니다. 잠시 후 다시 보내 주세요." };
  if (status === "interrupted") return { kind: "retry", text: "이전 처리가 중단되었습니다. 잠시 후 같은 질문을 다시 보내 주세요." };
  if (status === "failed" || status === "processing_error") {
    return { kind: "retry", text: "처리 중 오류가 발생했습니다. 근거가 없다는 뜻은 아닙니다. 잠시 후 다시 보내 주세요." };
  }
  return { kind: "retry", text: "답을 받지 못했습니다. 잠시 후 다시 보내 주세요." };
}

async function readJson(response) {
  const text = await response.text();
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

const defaultSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// 질문을 보내고, 늦어지면 /jobs를 3초 간격으로 조회해 최종 결과 하나를 돌려줍니다.
// 반환: { view, info, httpStatus } 또는 네트워크 실패 시 { view: null, info: { kind: "retry" | "error", text } }
// hooks.onPoll(): 조회를 시작할 때마다 부릅니다. deps는 테스트에서 fetch·시계·sleep을 바꿔 넣을 때만 씁니다.
export async function askQuestion(question, hooks = {}, deps = {}) {
  const fetchImpl = deps.fetch || globalThis.fetch.bind(globalThis);
  const now = deps.now || Date.now;
  const sleep = deps.sleep || defaultSleep;
  const started = now();

  const controller = typeof AbortController === "function" ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), deps.askTimeoutMs ?? ASK_TIMEOUT_MS) : null;
  let response;
  let body;
  try {
    response = await fetchImpl("/ui/ask", {
      method: "POST",
      cache: "no-store",
      credentials: "omit",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
      signal: controller ? controller.signal : undefined,
    });
    body = await readJson(response);
  } catch (error) {
    if (error && error.name === "AbortError") {
      return { view: null, info: { kind: "retry", text: "응답이 너무 오래 걸립니다. 잠시 후 같은 질문을 다시 보내 주세요." } };
    }
    return { view: null, info: { kind: "error", text: "서버에 연결하지 못했습니다. 네트워크를 확인하고 잠시 후 다시 보내 주세요." } };
  } finally {
    if (timer) clearTimeout(timer);
  }

  let view = body || {};
  let httpStatus = body === null ? (response.status >= 400 ? response.status : 500) : response.status;
  let info = classify(view, httpStatus);

  while (info.kind === "poll") {
    if (now() - started > POLL_LIMIT_MS) {
      return { view, httpStatus, info: { kind: "retry", text: "답이 너무 늦어지고 있습니다. 잠시 후 같은 질문을 다시 보내 주세요." } };
    }
    await sleep(POLL_MS);
    if (hooks.onPoll) hooks.onPoll();
    let pollResponse;
    let pollBody;
    try {
      pollResponse = await fetchImpl(view.poll_url, { method: "GET", cache: "no-store", credentials: "omit" });
      pollBody = await readJson(pollResponse);
    } catch {
      // 기다리는 중 잠깐 끊긴 것은 제한 시간까지 계속 확인합니다.
      continue;
    }
    const pollUrl = view.poll_url;
    if (pollBody && pollBody.job_id) {
      view = jobToView(pollBody);
      // 아직 처리 중이면 같은 주소로 계속 확인합니다.
      if (!view.poll_url && view.status === "deadline_exceeded") view.poll_url = pollUrl;
      httpStatus = pollResponse.status;
    } else {
      view = pollBody || {};
      httpStatus = pollResponse.status >= 400 ? pollResponse.status : 500;
    }
    info = classify(view, httpStatus);
  }
  return { view, info, httpStatus };
}

// GET /health: 새 질문을 받을 준비가 됐는지 확인합니다. 모델은 부르지 않습니다.
export async function checkHealth(deps = {}) {
  const fetchImpl = deps.fetch || globalThis.fetch.bind(globalThis);
  try {
    const response = await fetchImpl("/health", { method: "GET", cache: "no-store", credentials: "omit" });
    const body = await readJson(response);
    return { ok: response.status === 200 && body && body.status === "ok", body };
  } catch {
    return { ok: false, body: null };
  }
}
