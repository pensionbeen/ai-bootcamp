const PERIODS = new Set(["all", "today", "7d"]);
const dateFormat = new Intl.DateTimeFormat("ko-KR", {
  timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"
});
const count = value => value.toLocaleString("ko-KR");
const nonnegativeInteger = value => Number.isSafeInteger(value) && value >= 0;

export function displayTime(value) {
  if (!value) return "기록 없음";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "시각 확인 불가" : dateFormat.format(date);
}

export function visitUrl({ period = "all", before = null } = {}) {
  if (!PERIODS.has(period) || !(before === null || (Number.isSafeInteger(before) && before > 0))) {
    throw new Error("조회 조건을 확인해 주세요.");
  }
  const query = new URLSearchParams({ period });
  if (before !== null) query.set("before", String(before));
  return "/admin/visits/data?" + query.toString();
}

export function validPage(data) {
  return Boolean(data && Array.isArray(data.items) && nonnegativeInteger(data.total) &&
    (data.next_before === null || (Number.isSafeInteger(data.next_before) && data.next_before > 0)) &&
    data.summary && nonnegativeInteger(data.summary.today) && nonnegativeInteger(data.summary.last_7_days) &&
    data.retention && Number.isSafeInteger(data.retention.days) && data.retention.days > 0 &&
    Number.isSafeInteger(data.retention.max_records) && data.retention.max_records > 0 &&
    (data.capturing_since === null || typeof data.capturing_since === "string") &&
    data.items.every(item => item && Number.isSafeInteger(item.seq) && item.seq > 0 &&
      typeof item.request_id === "string" && typeof item.path === "string" &&
      typeof item.received_at === "string" &&
      (item.http_status === null || (Number.isInteger(item.http_status) && item.http_status >= 100 && item.http_status <= 599))));
}

// A cancelled fetch may still resolve. Only the latest selection may update the page.
export function createVisitLoader(fetchImpl = globalThis.fetch.bind(globalThis), options = {}) {
  const setTimer = options.setTimer || setTimeout;
  const clearTimer = options.clearTimer || clearTimeout;
  const timeoutMs = options.timeoutMs ?? 15_000;
  let pending = null;
  function cancel() {
    if (pending) pending.controller.abort();
    pending = null;
  }
  return {
    cancel,
    async load(selection) {
      cancel();
      const request = { controller: new AbortController(), timedOut: false };
      pending = request;
      const timer = setTimer(() => {
        request.timedOut = true;
        request.controller.abort();
      }, timeoutMs);
      try {
        const response = await fetchImpl(visitUrl(selection), {
          credentials: "same-origin", cache: "no-store", signal: request.controller.signal,
          headers: { Accept: "application/json" }
        });
        if (pending !== request) return { kind: "superseded" };
        if (response.status === 401) return { kind: "auth", message: "관리자 인증이 필요합니다. 다시 로그인 버튼으로 페이지를 열어 주세요." };
        if (!response.ok) throw new Error("기록을 불러오지 못했습니다. HTTP " + response.status);
        const data = await response.json();
        if (pending !== request) return { kind: "superseded" };
        if (request.timedOut) return { kind: "error", message: "기록 조회 시간이 초과됐습니다. 다시 시도해 주세요." };
        if (!validPage(data)) throw new Error("방문 기록 응답 형식을 확인할 수 없습니다.");
        return { kind: "ok", data };
      } catch (error) {
        if (pending !== request) return { kind: "superseded" };
        const message = request.timedOut ? "기록 조회 시간이 초과됐습니다. 다시 시도해 주세요." :
          error instanceof TypeError ? "서버에 연결할 수 없습니다. 연결 상태를 확인해 주세요." :
          error instanceof SyntaxError ? "방문 기록 응답 형식을 확인할 수 없습니다." : error.message;
        return { kind: "error", message };
      } finally {
        clearTimer(timer);
        if (pending === request) pending = null;
      }
    }
  };
}

function addDetail(document, list, label, value) {
  const term = document.createElement("dt");
  const detail = document.createElement("dd");
  term.textContent = label;
  detail.textContent = typeof value === "string" && value ? value : "기록 없음";
  list.append(term, detail);
}

export function createRecord(document, record, openSequences = new Set(), previousHighest = null) {
  const card = document.getElementById("record-template").content.firstElementChild.cloneNode(true);
  card.dataset.seq = String(record.seq);
  card.open = openSequences.has(String(record.seq));
  const isNew = previousHighest !== null && record.seq > previousHighest;
  card.classList.toggle("is-new", isNew);
  card.querySelector(".kind").textContent = isNew ? "새 방문" : "페이지 조회";
  const time = card.querySelector("time");
  time.textContent = displayTime(record.received_at);
  time.dateTime = record.received_at;
  const status = card.querySelector(".status");
  status.textContent = record.http_status === null ? "상태 기록 없음" : "HTTP " + record.http_status;
  status.className = "status " + (record.http_status === null ? "neutral" : record.http_status >= 400 ? "bad" : "ok");
  card.querySelector(".bot").hidden = record.is_bot !== true;
  card.querySelector(".path").textContent = record.path;
  card.querySelector(".ip").textContent = "IP " + (record.reported_client_ip || "헤더 없음");
  const list = card.querySelector("dl");
  addDetail(document, list, "보고된 클라이언트 IP", record.reported_client_ip);
  addDetail(document, list, "사용자 에이전트", record.user_agent);
  addDetail(document, list, "유입 주소", record.referrer || "없음 또는 브라우저에서 전달하지 않음");
  const elapsed = Number.isFinite(record.elapsed_ms) ? (record.elapsed_ms / 1000).toFixed(2) + "초" : null;
  addDetail(document, list, "응답 시간", elapsed);
  addDetail(document, list, "접속 분류", record.is_bot === true ? "자동 접속 추정 (사용자 에이전트 기준)" : "자동 접속으로 분류되지 않음");
  addDetail(document, list, "Cloudflare Ray ID", record.reported_cf_ray);
  addDetail(document, list, "기록 ID", record.request_id);
  return card;
}

export function mountVisitLog(document, location = globalThis.location, fetchImpl = globalThis.fetch.bind(globalThis)) {
  const byId = id => document.getElementById(id);
  const records = byId("records");
  const autoRefresh = byId("auto-refresh");
  const periodSelect = byId("period");
  const loader = createVisitLoader(fetchImpl);
  let period = "all";
  let cursors = [null];
  let pageIndex = 0;
  let nextBefore = null;
  let highestSequence = null;
  let loading = false;
  let authRequired = false;
  let refreshTimer;
  let revision = 0;

  function showMessage(text, kind = "") {
    byId("message").textContent = text;
    byId("message").className = kind;
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
    clearTimeout(refreshTimer);
    if (autoRefresh.checked && pageIndex === 0 && !document.hidden && !authRequired && !loading) {
      refreshTimer = setTimeout(() => loadPage(0, false), 10_000);
    }
  }

  async function loadPage(targetPage = pageIndex, manual = true) {
    if (!Number.isSafeInteger(targetPage) || targetPage < 0 || targetPage >= cursors.length) return;
    const requestRevision = ++revision;
    clearTimeout(refreshTimer);
    loading = true;
    if (manual) showMessage("기록을 불러오는 중입니다.");
    updateControls();
    const result = await loader.load({ period, before: cursors[targetPage] });
    if (requestRevision !== revision || result.kind === "superseded") return;
    if (result.kind === "ok") {
      const data = result.data;
      authRequired = false;
      const open = new Set([...records.querySelectorAll("details[open]")].map(card => card.dataset.seq));
      const previousHighest = highestSequence;
      const newCount = targetPage === 0 && previousHighest !== null ? data.items.filter(item => item.seq > previousHighest).length : 0;
      records.replaceChildren(...data.items.map(item => createRecord(document, item, open, previousHighest)));
      pageIndex = targetPage;
      if (targetPage === 0) cursors = [null];
      nextBefore = data.next_before;
      if (targetPage === 0 && data.items.length) highestSequence = Math.max(highestSequence ?? 0, ...data.items.map(item => item.seq));
      byId("empty").hidden = data.items.length !== 0;
      byId("summary").textContent = "선택 기간의 페이지 조회 " + count(data.total) + "건 · " + count(data.items.length) + "개 표시";
      byId("today-count").textContent = count(data.summary.today) + "건";
      byId("week-count").textContent = count(data.summary.last_7_days) + "건";
      byId("retention").textContent = "최근 " + count(data.retention.days) + "일의 기록을 최대 " + count(data.retention.max_records) + "건 보관합니다. 집계에는 현재 보관 중인 기록만 포함됩니다.";
      byId("capturing-since").textContent = data.capturing_since ? "기록 시작: " + displayTime(data.capturing_since) + ". 기능을 켜기 전의 방문은 확인할 수 없습니다." : "기능을 켠 이후의 방문부터 기록합니다. 이전 방문은 확인할 수 없습니다.";
      byId("updated").textContent = "갱신 " + displayTime(new Date().toISOString());
      showMessage(newCount ? "새 페이지 조회 " + count(newCount) + "건이 기록됐습니다." : "", newCount ? "success" : "");
    } else {
      authRequired = result.kind === "auth";
      showMessage(result.message, "error");
      if (!records.children.length) byId("summary").textContent = "기록 조회 실패";
    }
    loading = false;
    updateControls();
    scheduleRefresh();
  }

  byId("refresh").addEventListener("click", () => authRequired ? location.reload() : loadPage());
  byId("latest").addEventListener("click", () => loadPage(0));
  byId("newer").addEventListener("click", () => loadPage(pageIndex - 1));
  byId("older").addEventListener("click", () => {
    if (loading || nextBefore === null) return;
    cursors = cursors.slice(0, pageIndex + 1);
    cursors.push(nextBefore);
    loadPage(pageIndex + 1);
  });
  periodSelect.addEventListener("change", () => {
    period = PERIODS.has(periodSelect.value) ? periodSelect.value : "all";
    cursors = [null];
    pageIndex = 0;
    nextBefore = null;
    highestSequence = null;
    records.replaceChildren();
    byId("empty").hidden = true;
    byId("summary").textContent = "기록을 불러오는 중입니다.";
    loadPage(0);
  });
  autoRefresh.addEventListener("change", scheduleRefresh);
  document.addEventListener("visibilitychange", scheduleRefresh);
  loadPage();
  return {
    dispose() {
      revision++;
      clearTimeout(refreshTimer);
      loader.cancel();
    }
  };
}

if (typeof document !== "undefined") mountVisitLog(document);
