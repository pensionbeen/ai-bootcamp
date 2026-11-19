const byId = id => document.getElementById(id);
const records = byId("records");
const autoRefresh = byId("auto-refresh");
const message = byId("message");
const dateFormat = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"
});
const failureStatuses = new Set(["failed", "processing_error", "deadline_exceeded", "interrupted", "queue_full"]);
const writeLabels = {pending: "처리 중", written: "서버에서 응답 전송 완료", disconnected: "응답 중 연결 끊김", interrupted: "서버 재시작으로 처리 중단", legacy: "기존 저장 결과"};
let cursors = [null];
let pageIndex = 0;
let nextBefore = null;
let highestSequence = null;
let loading = false;
let authRequired = false;
let timer;

function displayTime(value) {
  if (!value) return "기록 없음";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "시각 확인 불가" : dateFormat.format(date);
}

function showMessage(text, kind = "") {
  message.textContent = text;
  message.className = kind;
}

function statusFor(record) {
  if (record.record_kind === "legacy") return {text: "기존 결과 · " + (record.rag_status || "저장됨"), kind: "neutral"};
  if (record.write_outcome === "pending") return {text: "처리 중", kind: "wait"};
  const status = record.http_status === null ? "HTTP 기록 없음" : "HTTP " + record.http_status;
  const text = status + " · " + (record.rag_status || writeLabels[record.write_outcome] || "상태 미확인");
  const failed = record.http_status >= 400 || failureStatuses.has(record.rag_status) || ["disconnected", "interrupted"].includes(record.write_outcome);
  return {text, kind: failed ? "bad" : "ok"};
}

function addDetail(list, label, value) {
  const term = document.createElement("dt");
  const detail = document.createElement("dd");
  term.textContent = label;
  detail.textContent = value ?? "기록 없음";
  list.append(term, detail);
}

function createRecord(record, openSequences, previousHighest) {
  const card = byId("record-template").content.firstElementChild.cloneNode(true);
  card.dataset.seq = String(record.seq);
  card.open = openSequences.has(String(record.seq));
  const legacy = record.record_kind === "legacy";
  const isNew = !legacy && previousHighest !== null && record.seq > previousHighest;
  card.classList.toggle("is-new", isNew);
  card.querySelector(".kind").textContent = legacy ? "기존 저장 질문" : isNew ? "새 요청" : "요청";
  const time = card.querySelector("time");
  time.textContent = legacy ? "요청 시각 기록 없음" : displayTime(record.received_at);
  if (record.received_at) time.dateTime = record.received_at;
  const status = statusFor(record);
  card.querySelector(".status").textContent = status.text;
  card.querySelector(".status").className = "status " + status.kind;
  const question = typeof record.question === "string" ? record.question : "";
  card.querySelector(".question").textContent = question.trim() ? question : "질문을 읽을 수 없거나 빈 질문입니다.";
  const details = card.querySelector("dl");
  if (legacy) {
    addDetail(details, "저장 파일 갱신", displayTime(record.last_saved_at));
    addDetail(details, "기록 범위", "요청별 기록 기능을 추가하기 전에 저장된 질문·결과입니다. 당시 요청 횟수와 요청 시각은 확인할 수 없습니다.");
  } else {
    const elapsed = Number.isFinite(record.elapsed_ms) ? (record.elapsed_ms / 1000).toFixed(2) + "초" :
      record.write_outcome === "pending" ? "처리 중" : "기록 없음";
    addDetail(details, "처리 시간", elapsed);
    addDetail(details, "응답 전송", writeLabels[record.write_outcome] || record.write_outcome);
    addDetail(details, "보고된 클라이언트 IP", record.reported_client_ip || "헤더 없음");
    addDetail(details, "직접 연결 주소", record.socket_peer);
    addDetail(details, "사용자 에이전트", record.user_agent || "헤더 없음");
    addDetail(details, "Cloudflare Ray ID", record.reported_cf_ray || "헤더 없음");
  }
  addDetail(details, "요청 기록 ID", record.request_id);
  if (record.job_id) addDetail(details, "질문 작업 ID", record.job_id);
  card.querySelector(".request-title").textContent = legacy ? "저장된 질문" : "요청 내용";
  const request = legacy ? {question: record.question} : record.request;
  card.querySelector(".request-json").textContent = request === null ? "읽을 수 있는 질문 필드가 없습니다." : JSON.stringify(request, null, 2);
  card.querySelector(".response-json").textContent = record.response === null ? "저장된 응답이 없습니다." : JSON.stringify(record.response, null, 2);
  return card;
}

function updateControls() {
  byId("refresh").disabled = loading;
  byId("refresh").textContent = authRequired ? "다시 로그인" : "새로고침";
  byId("latest").disabled = loading || pageIndex === 0;
  byId("newer").disabled = loading || pageIndex === 0;
  byId("older").disabled = loading || nextBefore === null;
  byId("page-number").textContent = (pageIndex + 1) + "페이지";
  byId("refresh-note").textContent = pageIndex > 0 ? "이전 기록을 보는 동안 자동 새로고침을 멈춥니다. 최신 기록으로 돌아가면 다시 시작합니다." : "";
  records.setAttribute("aria-busy", String(loading));
}

function scheduleRefresh() {
  clearTimeout(timer);
  if (autoRefresh.checked && pageIndex === 0 && !document.hidden && !authRequired) {
    timer = setTimeout(() => loadPage(0, false), 10_000);
  }
}

async function loadPage(targetPage = pageIndex, manual = true) {
  if (loading) return;
  clearTimeout(timer);
  loading = true;
  if (manual) showMessage("기록을 불러오는 중입니다.");
  updateControls();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15_000);
  try {
    const before = cursors[targetPage];
    const path = "/admin/requests/data" + (before === null ? "" : "?before=" + encodeURIComponent(before));
    const response = await fetch(path, {credentials: "same-origin", cache: "no-store", signal: controller.signal, headers: {Accept: "application/json"}});
    if (response.status === 401) {
      authRequired = true;
      throw new Error("관리자 인증이 필요합니다. 다시 로그인 버튼으로 페이지를 열어 주세요.");
    }
    if (!response.ok) throw new Error("기록을 불러오지 못했습니다. HTTP " + response.status);
    const data = await response.json();
    if (!Array.isArray(data.items) || !Number.isSafeInteger(data.total) ||
        !(data.next_before === null || Number.isSafeInteger(data.next_before))) {
      throw new Error("요청 기록 응답 형식을 확인할 수 없습니다.");
    }
    const openSequences = new Set([...records.querySelectorAll("details[open]")].map(card => card.dataset.seq));
    const previousHighest = highestSequence;
    const newCount = targetPage === 0 && previousHighest !== null ? data.items.filter(item => item.record_kind === "request" && item.seq > previousHighest).length : 0;
    const cards = data.items.map(item => createRecord(item, openSequences, previousHighest));
    records.replaceChildren(...cards);
    pageIndex = targetPage;
    if (targetPage === 0) cursors = [null];
    nextBefore = data.next_before;
    if (targetPage === 0 && data.items.length) highestSequence = Math.max(highestSequence ?? 0, ...data.items.map(item => item.seq));
    byId("empty").hidden = data.items.length !== 0;
    byId("summary").textContent = "보관된 기록 " + data.total.toLocaleString("ko-KR") + "개 · " + data.items.length + "개 표시";
    byId("updated").textContent = "갱신 " + displayTime(new Date().toISOString());
    showMessage(newCount ? "새 요청 " + newCount + "개가 도착했습니다." : "", newCount ? "success" : "");
  } catch (error) {
    const detail = controller.signal.aborted ? "기록 조회 시간이 초과됐습니다. 다시 시도해 주세요." :
      error instanceof TypeError ? "서버에 연결할 수 없습니다. 연결 상태를 확인해 주세요." : error.message;
    showMessage(detail, "error");
    if (!records.children.length) byId("summary").textContent = "기록 조회 실패";
  } finally {
    clearTimeout(timeout);
    loading = false;
    updateControls();
    scheduleRefresh();
  }
}

byId("refresh").addEventListener("click", () => authRequired ? location.reload() : loadPage());
byId("latest").addEventListener("click", () => loadPage(0));
byId("newer").addEventListener("click", () => loadPage(pageIndex - 1));
byId("older").addEventListener("click", () => {
  if (nextBefore === null) return;
  cursors = cursors.slice(0, pageIndex + 1);
  cursors.push(nextBefore);
  loadPage(pageIndex + 1);
});
autoRefresh.addEventListener("change", scheduleRefresh);
document.addEventListener("visibilitychange", scheduleRefresh);
loadPage();
