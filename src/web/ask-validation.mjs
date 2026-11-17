export const TIMEOUT_MS = 90_000;

const fields = ["answer", "citation", "source_url", "effective_date"];
const failureStatuses = new Set(["deadline_exceeded", "failed", "interrupted", "processing_error", "queue_full"]);
const passOrFail = ok => ok ? "pass" : "fail";

// The field and outcome rules mirror scripts/check_submission.py.
export function validateAskResponse({status, contentType, ragStatus, elapsedMs, rawBody}) {
  const checks = [];
  function add(id, label, state, detail) {
    checks.push({id, label, state, detail});
  }

  add("http", "HTTP 상태", passOrFail(status === 200), `HTTP ${status} · 기대값 200`);
  add("time", "응답 시간", passOrFail(Number.isFinite(elapsedMs) && elapsedMs >= 0 && elapsedMs <= TIMEOUT_MS),
      `${(elapsedMs / 1000).toFixed(2)}초 / 제한 90초`);
  const mime = (contentType || "").split(";")[0].trim().toLowerCase();
  add("mime", "Content-Type", passOrFail(mime === "application/json"), contentType || "헤더 없음");

  let parsed = null;
  let isJson = false;
  try {
    parsed = JSON.parse(rawBody);
    isJson = true;
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
  }
  const isObject = parsed !== null && typeof parsed === "object" && !Array.isArray(parsed);
  const jsonDetail = isObject ? "JSON 객체로 읽었습니다." :
    (isJson ? "최상위 값이 JSON 객체여야 합니다." : "본문을 JSON으로 읽을 수 없습니다.");
  add("json", "JSON 본문", passOrFail(isObject), jsonDetail);

  if (isObject) {
    const missing = fields.filter(field => !Object.hasOwn(parsed, field));
    const extra = Object.keys(parsed).filter(field => !fields.includes(field));
    const differences = [];
    if (missing.length) differences.push(`누락 · ${missing.join(", ")}`);
    if (extra.length) differences.push(`추가 · ${extra.join(", ")}`);
    add("fields", "필수 필드 4개", passOrFail(!differences.length), differences.join(" / ") || fields.join(", "));

    const problems = [];
    if (typeof parsed.answer !== "string" || !parsed.answer.trim()) problems.push("answer는 내용이 있는 문자열이어야 합니다.");
    if (!Array.isArray(parsed.citation) || !parsed.citation.every(item => typeof item === "string")) {
      problems.push("citation은 문자열 배열이어야 합니다.");
    }
    if (typeof parsed.source_url !== "string") problems.push("source_url은 문자열이어야 합니다.");
    if (typeof parsed.effective_date !== "string") problems.push("effective_date는 문자열이어야 합니다.");
    add("types", "필드 자료형", passOrFail(!problems.length), problems.join(" ") || "문자열 3개와 인용 문자열 배열을 확인했습니다.");

    if (problems.length) {
      add("source", "출처와 시행일", "skip", "필드 자료형을 먼저 확인하세요.");
    } else if (parsed.answer.startsWith("확인 불가") && !parsed.citation.length) {
      add("source", "출처와 시행일", "pass", "‘확인 불가’와 빈 인용 배열에는 빈 출처와 시행일이 허용됩니다.");
    } else {
      const valid = parsed.source_url.startsWith("http") && /^\p{Nd}{4}-\p{Nd}{2}-\p{Nd}{2}$/u.test(parsed.effective_date);
      add("source", "출처와 시행일", passOrFail(valid),
          valid ? "출처 URL과 YYYY-MM-DD 형식을 확인했습니다." : "출처 URL과 YYYY-MM-DD 형식의 시행일이 필요합니다.");
    }
  } else {
    for (const [id, label] of [["fields", "필수 필드 4개"], ["types", "필드 자료형"], ["source", "출처와 시행일"]]) {
      add(id, label, "skip", "검사할 JSON 객체가 없습니다.");
    }
  }

  // This service can return a well-formed HTTP 200 fallback for a failed job.
  let outcomeState = "info";
  if (ragStatus === "completed") outcomeState = "pass";
  if (failureStatuses.has(ragStatus)) outcomeState = "fail";
  add("outcome", "서버 처리 상태", outcomeState, ragStatus || "X-Rag-Status 헤더 없음");
  return {passed: checks.every(check => check.state !== "fail"), checks, parsed, isJson};
}
