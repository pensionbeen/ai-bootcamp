// 서버 응답 처리 (화면 요소를 건드리지 않는 순수 로직).
// docs/ui-guide.md 3장과 src/local_rag_service.py의 sync_view를 따릅니다.
// 기존 기본 화면(src/web/index.html)의 jobToView·classify와 같은 기준입니다.

export const UNKNOWN = "확인 불가";
export const POLL_MS = 3000;
export const POLL_LIMIT_MS = 10 * 60 * 1000;
// 서버는 /ui/ask에 최대 80초 안에 답하므로 브라우저 쪽 제한은 그보다 길게 둡니다.
export const ASK_TIMEOUT_MS = 100 * 1000;
import { getLang, t } from "./i18n.mjs";

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
    return { kind: "error", text: ERROR_TEXT[status] ? t(ERROR_TEXT[status]) : t("요청을 처리하지 못했습니다(HTTP {n}). 잠시 후 다시 시도해 주세요.", { n: httpStatus }) };
  }
  if (status === "completed") {
    if (citationItems(view).length) return { kind: "answer", text: "" };
    const resultStatus = view.result && typeof view.result.status === "string" ? view.result.status : "";
    if (resultStatus === "out_of_scope" || resultStatus === "insufficient_evidence") {
      return { kind: "unknown", reason: "no_basis", text: t("서울시 조례 6종에서 이 질문의 근거 조항을 찾지 못했습니다.") };
    }
    return { kind: "unknown", reason: "not_confirmed", text: t("근거 후보는 찾았지만 답을 확정하지 못했습니다.") };
  }
  if (status === "deadline_exceeded") {
    if (typeof view.poll_url === "string" && JOB_PATH.test(view.poll_url)) {
      return { kind: "poll", text: t("답을 만드는 데 시간이 더 걸리고 있어요. 3초마다 결과를 확인합니다.") };
    }
    return { kind: "retry", text: t("답이 늦어지고 있습니다. 잠시 후 같은 질문을 다시 보내 주세요.") };
  }
  if (status === "queue_full") return { kind: "retry", text: t("지금 질문이 많아 대기열이 가득 찼습니다. 잠시 후 다시 보내 주세요.") };
  if (status === "interrupted") return { kind: "retry", text: t("이전 처리가 중단되었습니다. 잠시 후 같은 질문을 다시 보내 주세요.") };
  if (status === "failed" || status === "processing_error") {
    return { kind: "retry", text: t("처리 중 오류가 발생했습니다. 근거가 없다는 뜻은 아닙니다. 잠시 후 다시 보내 주세요.") };
  }
  return { kind: "retry", text: t("답을 받지 못했습니다. 잠시 후 다시 보내 주세요.") };
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
// hooks.signal: 화면이 이 질문을 그만 기다릴 때(처음 화면으로 돌아갈 때) abort하는 AbortSignal.
//   취소되면 요청·조회를 멈추고 { cancelled: true, view: null, info: null }을 돌려줍니다.
export const CANCELLED = Object.freeze({ cancelled: true, view: null, info: null });

export async function askQuestion(question, hooks = {}, deps = {}) {
  const fetchImpl = deps.fetch || globalThis.fetch.bind(globalThis);
  const now = deps.now || Date.now;
  const sleep = deps.sleep || defaultSleep;
  const started = now();
  const cancel = hooks.signal || null;
  const cancelled = () => Boolean(cancel && cancel.aborted);
  if (cancelled()) return CANCELLED;

  const controller = typeof AbortController === "function" ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), deps.askTimeoutMs ?? ASK_TIMEOUT_MS) : null;
  const onCancel = () => { if (controller) controller.abort(); };
  if (cancel) cancel.addEventListener("abort", onCancel, { once: true });
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
    if (cancelled()) return CANCELLED;
    if (error && error.name === "AbortError") {
      return { view: null, info: { kind: "retry", text: t("응답이 너무 오래 걸립니다. 잠시 후 같은 질문을 다시 보내 주세요.") } };
    }
    return { view: null, info: { kind: "error", text: t("서버에 연결하지 못했습니다. 네트워크를 확인하고 잠시 후 다시 보내 주세요.") } };
  } finally {
    if (timer) clearTimeout(timer);
    if (cancel) cancel.removeEventListener("abort", onCancel);
  }
  if (cancelled()) return CANCELLED;

  let view = body || {};
  let httpStatus = body === null ? (response.status >= 400 ? response.status : 500) : response.status;
  let info = classify(view, httpStatus);

  while (info.kind === "poll") {
    if (now() - started > POLL_LIMIT_MS) {
      return { view, httpStatus, info: { kind: "retry", text: t("답이 너무 늦어지고 있습니다. 잠시 후 같은 질문을 다시 보내 주세요.") } };
    }
    await sleep(POLL_MS);
    if (cancelled()) return CANCELLED;
    if (hooks.onPoll) hooks.onPoll();
    let pollResponse;
    let pollBody;
    try {
      pollResponse = await fetchImpl(view.poll_url, { method: "GET", cache: "no-store", credentials: "omit", signal: cancel || undefined });
      pollBody = await readJson(pollResponse);
    } catch {
      if (cancelled()) return CANCELLED;
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


// POST /ui/draft: 끝난 질문(job_id)과 시민이 적은 상황으로 민원 초안을 받습니다.
// 반환: { ok: true, draft: { title, body, ai, notice, sites:[{name,url,description}] } } 또는 { ok: false, text }
export async function requestDraft(jobId, details, deps = {}) {
  const fetchImpl = deps.fetch || globalThis.fetch.bind(globalThis);
  if (typeof jobId !== "string" || !/^[0-9a-f]{64}$/.test(jobId)) {
    return { ok: false, text: t("이 답변으로는 초안을 만들 수 없어요. 질문을 다시 보내 주세요.") };
  }
  let response;
  let body;
  try {
    response = await fetchImpl("/ui/draft", {
      method: "POST",
      cache: "no-store",
      credentials: "omit",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId, details: String(details || "").slice(0, 1000) }),
      signal: deps.signal,
    });
    body = await readJson(response);
  } catch {
    return { ok: false, text: t("서버에 연결하지 못했어요. 잠시 후 다시 눌러 주세요.") };
  }
  const draft = body && body.draft;
  if (response.status === 200 && draft && typeof draft.body === "string") {
    const sites = Array.isArray(draft.sites)
      ? draft.sites.filter((site) => site && typeof site.name === "string" && safeUrl(site.url))
      : [];
    return { ok: true, draft: { ...draft, sites } };
  }
  if (response.status === 429) return { ok: false, text: t("지금 초안 요청이 많아요. 잠시 후 다시 눌러 주세요.") };
  return { ok: false, text: t("초안을 만들지 못했어요. 잠시 후 다시 눌러 주세요.") };
}


// POST /ui/photo: 줄인 사진(JPEG/PNG base64)을 보내 읽은 글(개인정보 가림)과 제안 질문을 받습니다.
// 반환: { ok: true, text, question, ai } 또는 { ok: false, text }
export async function sendPhoto(base64, mime, deps = {}) {
  const fetchImpl = deps.fetch || globalThis.fetch.bind(globalThis);
  let response;
  let body;
  try {
    response = await fetchImpl("/ui/photo", {
      method: "POST", cache: "no-store", credentials: "omit",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image: base64, mime, lang: getLang() }),
    });
    body = await readJson(response);
  } catch {
    return { ok: false, text: t("서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.") };
  }
  if (response.status === 200 && body && typeof body.question === "string" && typeof body.text === "string") {
    const display = typeof body.question_display === "string" && body.question_display ? body.question_display : null;
    const translation = typeof body.text_translation === "string" && body.text_translation ? body.text_translation : null;
    return { ok: true, text: body.text, question: body.question, display, translation, ai: body.ai === true };
  }
  const error = body && typeof body.error === "string" ? body.error : "";
  if (error === "no_text") return { ok: false, text: t("사진 속 글자를 찾지 못했어요. 표지판·안내문처럼 글자가 보이는 사진을 찍어 주세요. 상황을 묻고 싶다면 글이나 말로 물어봐 주세요.") };
  if (error === "invalid_image") return { ok: false, text: t("사진을 읽을 수 없어요. JPG·PNG 사진으로 다시 시도해 주세요.") };
  if (response.status === 413) return { ok: false, text: t("사진이 너무 커요. 더 작은 사진으로 시도해 주세요.") };
  if (response.status === 429) return { ok: false, text: t("지금 사진 요청이 많아요. 잠시 후 다시 시도해 주세요.") };
  return { ok: false, text: t("사진을 읽지 못했어요. 잠시 후 다시 시도해 주세요.") };
}

// POST /ui/sources: 그 답에 인용된 조항의 원문. 반환: { ok: true, sources: [{citation, found, text}] } 또는 { ok: false, text }
export async function requestSources(jobId, deps = {}) {
  const fetchImpl = deps.fetch || globalThis.fetch.bind(globalThis);
  if (typeof jobId !== "string" || !/^[0-9a-f]{64}$/.test(jobId)) return { ok: false, text: t("원문을 불러올 수 없어요.") };
  try {
    const response = await fetchImpl("/ui/sources", {
      method: "POST", cache: "no-store", credentials: "omit",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId, lang: getLang() }),
    });
    const body = await readJson(response);
    if (response.status === 200 && body && Array.isArray(body.sources)) {
      return { ok: true, sources: body.sources.filter((s) => s && typeof s.citation === "string") };
    }
  } catch {
    // 아래 안내로
  }
  return { ok: false, text: t("원문을 불러오지 못했어요. 각 조항의 '원문 보기'로 법제처에서 확인해 주세요.") };
}

// 답에 나온 숫자·금액(예: "10만원", "19세", "5분", "3,000원")을 원문에서 찾아 표시할 조각으로 나눕니다.
// 단위가 있는 숫자는 단위까지 같아야 표시하고, 세 자리 이상 숫자는 표(별표)처럼 단위 없이 적힌 곳도 표시합니다.
// 조문 번호(제5조)는 표시하지 않습니다.
const UNITS = "만\\s*원|천\\s*원|원|세|살|분|시간|일|개월|년|퍼센트|%|급지|m²|미터";

export function numberTokens(answer) {
  const found = String(answer || "").matchAll(new RegExp("(?<![\\d,.제])(\\d[\\d,]*(?:\\.\\d+)?)\\s*(" + UNITS + ")?", "g"));
  const tokens = new Map();
  for (const match of found) {
    const digits = match[1].replace(/,$/, "");
    const unit = match[2] ? match[2].replace(/\s+/g, "") : "";
    if (!unit && digits.replace(/\D/g, "").length < 3) continue;
    tokens.set(digits + "|" + unit, { digits, unit });
  }
  return [...tokens.values()].sort((a, b) => b.digits.length - a.digits.length);
}

export function markNumbers(text, tokens) {
  const source = String(text || "");
  const esc = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const alternatives = [];
  for (const { digits, unit } of tokens) {
    const number = esc(digits).replace(/,/g, ",?");
    if (unit) alternatives.push(number + "\\s*" + unit.split("").map(esc).join("\\s*"));
    if (digits.replace(/\D/g, "").length >= 3) alternatives.push(number);
  }
  if (!alternatives.length) return [{ text: source, mark: false }];
  const pattern = new RegExp("(?<![\\d,.제])(?:" + alternatives.join("|") + ")(?![\\d])", "g");
  const parts = [];
  let last = 0;
  for (const match of source.matchAll(pattern)) {
    if (match.index > last) parts.push({ text: source.slice(last, match.index), mark: false });
    parts.push({ text: match[0], mark: true });
    last = match.index + match[0].length;
  }
  if (last < source.length) parts.push({ text: source.slice(last), mark: false });
  return parts;
}


// POST /ui/translate-question: 외국어 질문을 검색용 한국어 질문으로 바꿉니다.
// 반환: { ok: true, korean } 또는 { ok: false, text }
export async function translateQuestion(question, lang, deps = {}) {
  const fetchImpl = deps.fetch || globalThis.fetch.bind(globalThis);
  const failed = { ok: false, text: t("질문을 한국어로 옮기지 못했어요. 잠시 후 다시 보내 주세요.") };
  try {
    const response = await fetchImpl("/ui/translate-question", {
      method: "POST", cache: "no-store", credentials: "omit",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, lang }),
      signal: deps.signal,
    });
    const body = await readJson(response);
    if (response.status === 200 && body && typeof body.question_ko === "string" && body.question_ko.trim()) {
      return { ok: true, korean: body.question_ko.trim() };
    }
    if (response.status === 429) return { ok: false, text: t("지금 질문이 많아 대기열이 가득 찼습니다. 잠시 후 다시 보내 주세요.") };
  } catch (error) {
    if (error && error.name === "AbortError") return { ok: false, cancelled: true, text: "" };
  }
  return failed;
}

// POST /ui/translate-answer: 끝난 답을 번역합니다. 서버가 숫자를 대조해 verified를 줍니다.
// 반환: { text, verified } 또는 null(번역 없음 → 한국어 원문을 보여 줌)
export async function translateAnswer(jobId, lang, deps = {}) {
  const fetchImpl = deps.fetch || globalThis.fetch.bind(globalThis);
  if (typeof jobId !== "string" || !/^[0-9a-f]{64}$/.test(jobId)) return null;
  try {
    const response = await fetchImpl("/ui/translate-answer", {
      method: "POST", cache: "no-store", credentials: "omit",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId, lang }),
      signal: deps.signal,
    });
    const body = await readJson(response);
    const answer = response.status === 200 && body ? body.answer : null;
    if (answer && typeof answer.text === "string" && answer.text.trim() && answer.verified === true) {
      return { text: answer.text, verified: true };
    }
  } catch {
    // 한국어 원문으로
  }
  return null;
}

// 번역문 형광펜: 한국어 답의 숫자를 "값"으로 바꿔(10만원 → 100000) 번역문에서 같은 값을 찾아 표시합니다.
// 번역문에는 "KRW 100,000", "10万韩元", "10萬", "19 years" 처럼 다르게 적히므로 글자가 아니라 값으로 맞춥니다.
const SCALE = { 만: 10000, 万: 10000, 萬: 10000, 천: 1000, 千: 1000 };

export function numberValues(answer) {
  const values = new Set();
  for (const { digits, unit } of numberTokens(answer)) {
    const base = Number(digits.replace(/,/g, ""));
    if (!Number.isFinite(base)) continue;
    const scale = SCALE[unit.charAt(0)] && unit.length > 1 ? SCALE[unit.charAt(0)] : 1;
    values.add(base * scale);
  }
  return values;
}

export function markValues(text, values) {
  const source = String(text || "");
  if (!values || !values.size) return [{ text: source, mark: false }];
  const pattern = /(?<![\d,.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(\s*[만万萬천千])?(?:\s*(?:원|元|円))?/g;
  const parts = [];
  let last = 0;
  for (const match of source.matchAll(pattern)) {
    const before = source.slice(Math.max(0, match.index - 8), match.index);
    // 조문 번호(Article 10, 第10条, 제10조)는 금액이 아닙니다.
    if (/(?:Article|Art\.|第|제)\s*$/i.test(before)) continue;
    const base = Number(match[1].replace(/,/g, ""));
    const scale = match[2] ? SCALE[match[2].trim()] : 1;
    if (!values.has(base * scale)) continue;
    if (match.index > last) parts.push({ text: source.slice(last, match.index), mark: false });
    parts.push({ text: match[0], mark: true });
    last = match.index + match[0].length;
  }
  if (last < source.length) parts.push({ text: source.slice(last), mark: false });
  return parts.length ? parts : [{ text: source, mark: false }];
}
