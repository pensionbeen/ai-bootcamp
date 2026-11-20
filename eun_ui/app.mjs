// 서울시 조례 도우미(팀 로디) — 화면 동작.
// 서버와 주고받는 규칙은 api.mjs에 있고, 이 파일은 화면만 그립니다.
// 답 문장·조항 이름은 모두 textContent로 넣습니다(innerHTML 쓰지 않음, docs/ui-guide.md 4장).

import { askQuestion, checkHealth, citationItems, inquiryUrl, safeUrl, splitCitation, UNKNOWN } from "./api.mjs";
import { CATEGORIES } from "./examples.mjs";

const $ = (id) => document.getElementById(id);
const form = $("ask-form");
const input = $("question");
const send = $("send");
const thread = $("thread");
const banner = $("health-banner");
const home = $("home");
const qa = $("qa");
const heroForm = $("hero-form");
const heroInput = $("hero-question");
const heroButton = heroForm.querySelector("button");
const back = $("back");
const groups = $("groups");

let busy = false;

function fromTemplate(id) {
  return $(id).content.firstElementChild.cloneNode(true);
}

/* ---- 화면 전환: 처음 화면(home) ↔ 질문 결과(qa) ---- */

function showView(name) {
  const isQa = name === "qa";
  home.hidden = isQa;
  qa.hidden = !isQa;
  document.body.classList.toggle("view-home", !isQa);
  document.body.classList.toggle("view-qa", isQa);
}

// 처음 화면으로 돌아가면 이전 질문·답변은 지웁니다. 진행 중이던 답은 도착해도 화면에 붙지 않습니다.
function goHome() {
  showView("home");
  thread.textContent = "";
  const target = location.hash && location.hash !== "#result" ? document.getElementById(location.hash.slice(1)) : null;
  if (target) target.scrollIntoView();
  else window.scrollTo(0, 0);
  if (!matchMedia("(pointer: coarse)").matches) heroInput.focus({ preventScroll: true });
}

back.addEventListener("click", () => {
  // 휴대폰·브라우저 뒤로가기와 같은 동작이 되도록 방문 기록을 함께 되돌립니다.
  if (history.state && history.state.view === "qa") history.back();
  else goHome();
});
window.addEventListener("popstate", (event) => {
  if (!(event.state && event.state.view === "qa") && !qa.hidden) goHome();
});
document.querySelectorAll("[data-home]").forEach((link) => {
  link.addEventListener("click", () => {
    if (!qa.hidden) {
      showView("home");
      thread.textContent = "";
    }
  });
});

/* ---- 분야별 예시 질문: 분야마다 이름 + 질문 목록 ---- */

function renderGroups() {
  CATEGORIES.forEach((category) => {
    const section = document.createElement("section");
    section.className = "group";
    section.setAttribute("aria-labelledby", "g-" + category.id);

    const plate = document.createElement("div");
    plate.className = "group-plate";
    const name = document.createElement("h3");
    name.className = "group-name";
    name.id = "g-" + category.id;
    name.textContent = category.name;
    plate.appendChild(name);

    const body = document.createElement("div");
    const list = document.createElement("ul");
    list.className = "group-list";
    category.questions.forEach((item) => {
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "example";
      const text = document.createElement("span");
      text.textContent = item.label;
      button.appendChild(text);
      if (item.note) {
        const note = document.createElement("span");
        note.className = "example-note";
        note.textContent = item.note;
        button.appendChild(note);
      }
      button.addEventListener("click", () => ask(item.question));
      li.appendChild(button);
      list.appendChild(li);
    });
    const law = document.createElement("p");
    law.className = "group-law";
    law.textContent = "근거: 서울특별시 " + category.law;
    body.append(list, law);

    section.append(plate, body);
    groups.appendChild(section);
  });
}

/* ---- 대화 ---- */

function scrollIntoViewSoft(element) {
  element.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
}

function addUserMessage(question) {
  const li = fromTemplate("tpl-user");
  li.querySelector(".bubble").textContent = question;
  thread.appendChild(li);
}

function addPending() {
  const li = fromTemplate("tpl-pending");
  thread.appendChild(li);
  const text = li.querySelector(".pending-text");
  const time = li.querySelector(".pending-time");
  const started = Date.now();
  const timer = setInterval(() => {
    time.textContent = Math.floor((Date.now() - started) / 1000) + "초";
  }, 250);
  return {
    element: li,
    setText(value) { text.textContent = value; },
    seconds() { return Math.floor((Date.now() - started) / 1000); },
    stop() { clearInterval(timer); },
  };
}

// "제10조 제1항" → 번호판 큰 글자 "제10조", 작은 글자 "제1항". 별표·부칙도 같은 방식으로 나눕니다.
export function plateParts(part) {
  const match = /^(제\d+조(?:의\d+)?|별표\s*\d+(?:의\d+)?|부칙)\s*(.*)$/.exec(part || "");
  if (!match) return { art: part || "", sub: "" };
  return { art: match[1].replace(/\s+/g, " "), sub: match[2] };
}

function sourceElement(item) {
  const li = fromTemplate("tpl-source");
  const link = li.querySelector(".source");
  const url = safeUrl(item.url);
  const { law, part } = splitCitation(item.label);
  const plate = plateParts(part || law);
  li.querySelector(".plate-art").textContent = plate.art;
  li.querySelector(".plate-sub").textContent = plate.sub;
  li.querySelector(".source-law").textContent = part ? law : "";
  li.querySelector(".source-date").textContent = typeof item.date === "string" && item.date ? "시행 " + item.date : "";
  if (url) {
    link.href = url;
    link.setAttribute("aria-label", item.label + ", 법제처 원문 새 탭에서 열기");
  } else {
    // https 주소가 아니면 링크로 만들지 않습니다.
    const span = document.createElement("span");
    span.className = "source";
    while (link.firstChild) span.appendChild(link.firstChild);
    span.querySelector(".source-open").remove();
    link.replaceWith(span);
  }
  return li;
}

function copyText(view, items) {
  const lines = [typeof view.answer === "string" ? view.answer : UNKNOWN];
  if (items.length) {
    lines.push("", "근거 조항");
    items.forEach((item) => {
      const date = item.date ? ` (시행 ${item.date})` : "";
      const url = safeUrl(item.url) ? " " + item.url : "";
      lines.push(`- ${item.label}${date}${url}`);
    });
  }
  const inquiry = items.length ? null : inquiryUrl(view);
  if (inquiry) lines.push("", "국민신문고 문의: " + inquiry);
  if (typeof view.as_of_date === "string") lines.push("", "기준일 " + view.as_of_date);
  lines.push("", "서울시 조례 도우미 (팀 로디)");
  return lines.join("\n");
}

function wireCopy(card, text) {
  const button = card.querySelector(".copy");
  const label = card.querySelector(".copy-text");
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      label.textContent = "복사했어요";
    } catch {
      label.textContent = "복사하지 못했어요";
    }
    setTimeout(() => { label.textContent = "답변 복사"; }, 1800);
  });
}

function answerCard(view, info, seconds) {
  const li = fromTemplate("tpl-answer");
  const reply = li.querySelector(".reply");
  const kind = li.querySelector(".reply-kind");
  const meta = li.querySelector(".reply-meta");
  const answer = li.querySelector(".answer");
  const reason = li.querySelector(".reason");
  const sources = li.querySelector(".sources");
  const list = li.querySelector(".source-list");
  const verified = li.querySelector(".verified");

  const items = info.kind === "answer" ? citationItems(view) : [];
  const took = seconds < 1 ? "1초 미만" : seconds + "초";
  meta.textContent = typeof view.as_of_date === "string" ? "기준일 " + view.as_of_date + " (" + took + ")" : took;

  if (info.kind === "answer") {
    kind.textContent = "근거 확인";
    answer.textContent = typeof view.answer === "string" ? view.answer : UNKNOWN;
    items.forEach((item) => list.appendChild(sourceElement(item)));
    sources.hidden = false;
    verified.hidden = false;
  } else {
    reply.classList.add("is-unknown");
    kind.textContent = "확인 불가";
    // 위 칸에 안내 문장을 크게 보여 주므로 "확인 불가" 한 단어짜리 답 문장은 숨깁니다.
    answer.hidden = true;
    const lead = document.createElement("p");
    lead.textContent = info.text;
    reason.appendChild(lead);
    const message = view.result && typeof view.result.message === "string" ? view.result.message : "";
    if (message) {
      const detail = document.createElement("p");
      detail.className = "reason-detail";
      detail.textContent = message;
      reason.appendChild(detail);
    }
    const inquiry = inquiryUrl(view);
    const hint = document.createElement("p");
    hint.className = "reason-detail";
    if (info.reason === "no_basis") {
      hint.textContent = inquiry
        ? "조례 밖의 내용(법률·시행규칙·개별 사업 등)일 수 있습니다. 국민신문고에 문의하시면 담당 기관이 답변합니다."
        : "조례 밖의 내용(법률·시행규칙·개별 사업 등)일 수 있습니다. 관할 구청이나 담당 부서에 문의해 주세요.";
    } else {
      hint.textContent = "질문을 조금 더 구체적으로 바꿔 보시거나, 관할 구청이나 담당 부서에 문의해 주세요.";
    }
    reason.appendChild(hint);
    if (inquiry) {
      // 서버가 준 주소만 씁니다(필드가 없으면 링크를 숨김, docs/ui-guide.md 3-1).
      const link = document.createElement("a");
      link.className = "inquiry";
      link.href = inquiry;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "국민신문고에 문의하기";
      reason.appendChild(link);
    }
    reason.hidden = false;
  }
  wireCopy(li, copyText(view, items));
  return li;
}

function noticeCard(info, question) {
  const li = fromTemplate("tpl-notice");
  li.querySelector(".notice-title").textContent = info.kind === "error" ? "요청을 보내지 못했어요" : "다시 시도해 주세요";
  li.querySelector(".notice-text").textContent = info.text;
  const retry = li.querySelector(".retry");
  retry.disabled = busy;
  retry.addEventListener("click", () => ask(question));
  return li;
}

/* ---- 보내기 ---- */

function setBusy(value) {
  busy = value;
  send.disabled = value;
  heroButton.disabled = value;
  document.querySelectorAll(".example, .retry").forEach((button) => { button.disabled = value; });
}

async function ask(raw) {
  const question = String(raw || "").trim();
  if (busy) return;
  if (!question) {
    input.focus();
    return;
  }
  setBusy(true);
  // 처음 화면에서 질문하면 결과 화면으로 넘어가고, 뒤로가기로 돌아올 수 있게 기록을 남깁니다.
  if (qa.hidden) {
    showView("qa");
    if (!(history.state && history.state.view === "qa")) history.pushState({ view: "qa" }, "", "#result");
    window.scrollTo(0, 0);
  }
  addUserMessage(question);
  input.value = "";
  heroInput.value = "";
  resizeInput();
  const pending = addPending();
  scrollIntoViewSoft(pending.element.previousElementSibling || pending.element);

  let outcome;
  try {
    outcome = await askQuestion(question, {
      onPoll: () => pending.setText("시간이 더 걸리고 있어요. 계속 확인하는 중"),
    });
  } catch {
    outcome = { view: null, info: { kind: "error", text: "화면 처리 중 오류가 발생했습니다. 다시 보내 주세요." } };
  }
  pending.stop();
  const seconds = pending.seconds();
  const { view, info } = outcome;
  setBusy(false);
  const card = info.kind === "answer" || info.kind === "unknown"
    ? answerCard(view, info, seconds)
    : noticeCard(info, question);
  if (!pending.element.isConnected) return; // 기다리는 동안 처음 화면으로 돌아간 경우
  pending.element.replaceWith(card);
  // 질문과 답이 함께 보이도록 질문 말풍선부터 보여 줍니다.
  scrollIntoViewSoft(card.previousElementSibling || card);
  if (!matchMedia("(pointer: coarse)").matches) input.focus();
}

function resizeInput() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 160) + "px";
}

heroForm.addEventListener("submit", (event) => {
  event.preventDefault();
  ask(heroInput.value);
});
form.addEventListener("submit", (event) => {
  event.preventDefault();
  ask(input.value);
});
input.addEventListener("input", resizeInput);
input.addEventListener("keydown", (event) => {
  // 한글 조합 중 Enter는 글자 확정이므로 보내지 않습니다.
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    ask(input.value);
  }
});

/* ---- 서버 상태 ---- */

async function showHealth() {
  const { ok, body } = await checkHealth();
  if (ok) {
    if (body && typeof body.as_of_date === "string") $("asof").textContent = body.as_of_date;
    return;
  }
  const reasons = body && Array.isArray(body.reasons) ? body.reasons : [];
  banner.textContent = reasons.includes("missing_upstage_api_key")
    ? "현재 서버 점검 중으로 질문에 답변할 수 없습니다. 잠시 후 다시 이용해 주세요."
    : "서버가 아직 준비되지 않았습니다. 잠시 후 새로고침해 주세요.";
  banner.hidden = false;
}

renderGroups();
// 새로고침했을 때 결과 화면 주소(#result)에 남아 있으면 처음 화면으로 돌려놓습니다.
if (location.hash === "#result") history.replaceState(null, "", location.pathname);
showView("home");
resizeInput();
showHealth();
