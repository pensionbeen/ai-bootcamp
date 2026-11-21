// 서울시 조례 및 민원 도우미(팀 로디) — 화면 동작.
// 서버와 주고받는 규칙은 api.mjs에 있고, 이 파일은 화면만 그립니다.
// 답 문장·조항 이름은 모두 textContent로 넣습니다(innerHTML 쓰지 않음, docs/ui-guide.md 4장).

import { askQuestion, checkHealth, citationItems, inquiryUrl, markNumbers, markValues, numberTokens, numberValues, requestDraft, requestSources, safeUrl, sendPhoto, splitCitation, translateAnswer, translateQuestion, UNKNOWN } from "./api.mjs";
import { CATEGORIES } from "./examples.mjs";
import { applyStatic, getLang, isForeign, LANGS, pickLang, setLang, speechLang, t, tIn } from "./i18n.mjs";

// 민원 초안·국민신문고 연결은 보류(2026-11-21 결정). 코드는 남겨 두고 화면에서만 끕니다. 대신 120다산콜 안내를 씁니다.
const DRAFT_ENABLED = false;

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
// 지금 기다리는 질문 하나. 처음 화면으로 돌아가면 abort하고 비웁니다.
let current = null;

function fromTemplate(id) {
  const node = $(id).content.firstElementChild.cloneNode(true);
  applyStatic(node);
  return node;
}

/* ---- 언어: 한국어·영어·중국어(간체·번체)·일본어 ---- */

const LANG_KEY = "lawddy-lang";
const langSelect = $("lang");

function storedLang() {
  try { return localStorage.getItem(LANG_KEY); } catch { return null; }
}

function applyLang(code) {
  const lang = setLang(code);
  document.documentElement.lang = lang;
  document.body.classList.toggle("is-foreign", lang !== "ko");
  if (langSelect.value !== lang) langSelect.value = lang;
  applyStatic(document);
  renderGroups();
  setupHero();
}

/* ---- 첫 화면 제목: 한국어 → English → 中文 → 日本語를 2초씩 돌려 보여 줌 ---- */

const HERO_LANGS = ["ko", "en", "zh-CN", "zh-TW", "ja"];
const HERO_LINES = ["어려운 생활 속 조례,", "로디에게 물어보세요."];
let heroTimer = null;

function showHero(lang) {
  const title = $("hero-title");
  const lines = title.querySelectorAll(".hero-line");
  lines.forEach((line, index) => { line.textContent = tIn(lang, HERO_LINES[index]); });
  title.lang = lang;
}

function setupHero() {
  const title = $("hero-title");
  // 화면 읽기 프로그램에는 고른 언어 제목 하나만 읽히게 합니다(돌아가는 글자는 숨김).
  title.setAttribute("aria-label", HERO_LINES.map((line) => t(line)).join(" "));
  title.querySelectorAll(".hero-line").forEach((line) => line.setAttribute("aria-hidden", "true"));
  if (heroTimer) clearInterval(heroTimer);
  const order = [getLang(), ...HERO_LANGS.filter((code) => code !== getLang())];
  let index = 0;
  showHero(order[0]);
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  heroTimer = setInterval(() => {
    if (home.hidden || document.hidden) return;
    index = (index + 1) % order.length;
    title.classList.add("is-switching");
    setTimeout(() => {
      showHero(order[index]);
      title.classList.remove("is-switching");
    }, 180);
  }, 2000);
}

function setupLang() {
  LANGS.forEach(({ code, name }) => {
    const option = document.createElement("option");
    option.value = code;
    option.textContent = name;
    langSelect.appendChild(option);
  });
  langSelect.addEventListener("change", () => {
    try { localStorage.setItem(LANG_KEY, langSelect.value); } catch { /* 저장 못 해도 이번 화면은 바뀜 */ }
    applyLang(langSelect.value);
  });
  applyLang(pickLang(location.search, storedLang(), navigator.languages || [navigator.language]));
}

/* ---- 화면 전환: 처음 화면(home) ↔ 질문 결과(qa) ---- */

function showView(name) {
  const isQa = name === "qa";
  home.hidden = isQa;
  qa.hidden = !isQa;
  document.body.classList.toggle("view-home", !isQa);
  document.body.classList.toggle("view-qa", isQa);
}

// 기다리던 질문을 그만 기다립니다: 요청·조회를 멈추고 입력을 다시 풉니다.
function cancelCurrent() {
  if (!current) return;
  current.controller.abort();
  current.pending.stop();
  current = null;
  setBusy(false);
}

// 처음 화면으로 돌아가면 이전 질문·답변은 지우고, 기다리던 질문도 취소합니다.
function clearThread() {
  cancelCurrent();
  thread.textContent = "";
}

function goHome() {
  showView("home");
  clearThread();
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
      clearThread();
    }
  });
});

/* ---- 분야별 예시 질문: 분야마다 이름 + 질문 목록 ---- */

// 외국어 화면에서도 검색은 한국어 질문(item.question)으로 하고, 버튼에는 그 언어 글자를 보여 줍니다.
function localized(map, fallback) {
  const lang = getLang();
  return lang !== "ko" && map && typeof map[lang] === "string" ? map[lang] : fallback;
}

function renderGroups() {
  groups.textContent = "";
  CATEGORIES.forEach((category) => {
    const section = document.createElement("section");
    section.className = "group";
    section.setAttribute("aria-labelledby", "g-" + category.id);

    const plate = document.createElement("div");
    plate.className = "group-plate";
    const name = document.createElement("h3");
    name.className = "group-name";
    name.id = "g-" + category.id;
    name.textContent = localized(category.names, category.name);
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
      const label = localized(item.labels, item.label);
      text.textContent = label;
      button.appendChild(text);
      if (item.note) {
        const note = document.createElement("span");
        note.className = "example-note";
        note.textContent = localized(item.notes, item.note);
        button.appendChild(note);
      }
      button.disabled = busy;
      button.addEventListener("click", () => ask(label, { korean: item.question }));
      li.appendChild(button);
      list.appendChild(li);
    });
    const law = document.createElement("p");
    law.className = "group-law";
    law.textContent = t("근거: 서울특별시 {law}", { law: localized(category.laws, category.law) });
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
  return {
    // 외국어 질문이면 검색에 실제로 쓴 한국어 질문을 작게 보여 줍니다(무엇을 찾았는지 확인할 수 있게).
    showKorean(korean) {
      if (!korean || korean === question) return;
      const line = li.querySelector(".q-korean");
      line.textContent = t("검색에 쓴 한국어 질문: {q}", { q: korean });
      line.hidden = false;
    },
  };
}

function addPending() {
  const li = fromTemplate("tpl-pending");
  thread.appendChild(li);
  const text = li.querySelector(".pending-text");
  const time = li.querySelector(".pending-time");
  const started = Date.now();
  const timer = setInterval(() => {
    time.textContent = t("{n}초", { n: Math.floor((Date.now() - started) / 1000) });
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
  li.querySelector(".source-date").textContent = typeof item.date === "string" && item.date ? t("시행 {d}", { d: item.date }) : "";
  if (url) {
    link.href = url;
    link.setAttribute("aria-label", t("{label}, 법제처 원문 새 탭에서 열기", { label: item.label }));
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

function copyText(view, items, translated) {
  const korean = typeof view.answer === "string" ? view.answer : UNKNOWN;
  const lines = translated ? [translated.text, "", "[한국어 원문 / Korean original]", korean] : [korean];
  if (items.length) {
    lines.push("", t("근거 조항"));
    items.forEach((item) => {
      const date = item.date ? " (" + t("시행 {d}", { d: item.date }) + ")" : "";
      const url = safeUrl(item.url) ? " " + item.url : "";
      lines.push(`- ${item.label}${date}${url}`);
    });
  }
  lines.push("", t("사람에게 물어보고 싶다면") + " 120 (02-120)");
  if (typeof view.as_of_date === "string") lines.push("", t("기준일 {d}", { d: view.as_of_date }));
  lines.push("", "Lawddy — " + t("서울시 조례 및 민원 도우미"));
  return lines.join("\n");
}

function wireCopy(card, text) {
  const button = card.querySelector(".copy");
  const label = card.querySelector(".copy-text");
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      label.textContent = t("복사했어요");
    } catch {
      label.textContent = t("복사하지 못했어요");
    }
    setTimeout(() => { label.textContent = t("답변 복사"); }, 1800);
  });
}

// 형광펜 조각(markNumbers·markValues 결과)을 <mark>와 글자로 붙입니다(textContent만 씀).
function appendMarked(element, parts) {
  parts.forEach((part) => {
    if (part.mark) {
      const mark = document.createElement("mark");
      mark.textContent = part.text;
      element.appendChild(mark);
    } else {
      element.appendChild(document.createTextNode(part.text));
    }
  });
}

/* ---- 조문 원문 펼쳐 보기: 답에 나온 숫자·금액을 원문에서 형광펜으로 표시 ---- */

function wireSourceTexts(card, view, items) {
  const open = card.querySelector(".source-text-open");
  const box = card.querySelector(".source-texts");
  if (!items.length || typeof view.job_id !== "string") {
    open.hidden = true;
    return;
  }
  let loaded = false;
  open.addEventListener("click", async () => {
    if (loaded) {
      box.hidden = !box.hidden;
      open.textContent = box.hidden ? t("조문 원문 펼쳐 보기") : t("조문 원문 접기");
      return;
    }
    open.disabled = true;
    open.textContent = t("원문을 불러오는 중");
    const outcome = await requestSources(view.job_id);
    open.disabled = false;
    box.textContent = "";
    if (!outcome.ok) {
      open.textContent = t("조문 원문 펼쳐 보기");
      const p = document.createElement("p");
      p.className = "source-text-note";
      p.textContent = outcome.text;
      box.appendChild(p);
      box.hidden = false;
      return;
    }
    loaded = true;
    const tokens = numberTokens(typeof view.answer === "string" ? view.answer : "");
    const legend = document.createElement("p");
    legend.className = "source-text-note";
    legend.textContent = isForeign()
      ? t("번역을 먼저 보여 드리고, 공식인 한국어 원문은 아래에 있어요. 답에 나온 숫자·금액은 형광펜으로 표시했어요.")
      : tokens.length
        ? t("검색 DB에 저장된 조례 원문입니다. 답에 나온 숫자·금액을 형광펜으로 표시했어요.")
        : t("검색 DB에 저장된 조례 원문입니다.");
    box.appendChild(legend);
    outcome.sources.forEach((source) => {
      const block = document.createElement("section");
      block.className = "article-text";
      // 외국어 화면은 내 언어(번역)가 맨 위에 오도록 한국어 조항 제목을 원문 쪽으로 내립니다.
      const title = document.createElement("h4");
      title.textContent = source.citation;
      title.lang = "ko";
      if (!isForeign() || !source.found) block.appendChild(title);
      const pre = document.createElement("p");
      pre.className = "article-body";
      pre.lang = "ko";
      if (source.found && typeof source.text === "string") {
        markNumbers(source.text, tokens).forEach((part) => {
          if (part.mark) {
            const mark = document.createElement("mark");
            mark.textContent = part.text;
            pre.appendChild(mark);
          } else {
            pre.appendChild(document.createTextNode(part.text));
          }
        });
      } else {
        pre.textContent = t("이 조항의 원문은 '원문 보기'로 법제처에서 확인해 주세요.");
      }
      // 외국어 화면: 번역을 위에, 한국어 원문을 아래에 둡니다. 서버가 숫자를 대조해 다르면 번역을 주지 않습니다.
      if (isForeign() && source.found) {
        const translation = document.createElement("div");
        translation.className = "article-translation";
        const label = document.createElement("p");
        label.className = "article-translation-label";
        label.textContent = t("번역 (참고용)");
        const body = document.createElement("p");
        if (typeof source.translation === "string" && source.translation.trim()) {
          appendMarked(body, markValues(source.translation, numberValues(typeof view.answer === "string" ? view.answer : "")));
        } else {
          body.className = "source-text-note";
          body.textContent = t("번역하지 못했어요. 숫자가 달라질 수 있어 원문만 보여 드려요.");
        }
        translation.append(label, body);
        block.appendChild(translation);
        const originalLabel = document.createElement("p");
        originalLabel.className = "article-original-label";
        originalLabel.textContent = t("한국어 원문 (공식)");
        block.appendChild(originalLabel);
        // 원문 첫 줄에 조항 제목이 이미 들어 있으면 제목을 또 붙이지 않습니다.
        if (!String(source.text || "").startsWith(source.citation)) {
          title.classList.add("article-original-title");
          block.appendChild(title);
        }
      }
      block.appendChild(pre);
      box.appendChild(block);
    });
    box.hidden = false;
    open.textContent = t("조문 원문 접기");
  });
}

/* ---- 민원 초안 ---- */

// 끝난 답(근거 확인·확인 불가)마다 "민원 초안 쓰기"를 붙입니다. 초안은 서버(POST /ui/draft)가 만들고,
// 관련 조례 목록은 서버 코드가 근거에서 붙입니다. 신청은 시민이 응답소·국민신문고에서 직접 합니다.
function wireDraft(card, view) {
  const open = card.querySelector(".draft-open");
  // 민원 초안은 한국어 화면에서만: 국민신문고는 실명인증이 필요해 관광객은 쓰기 어렵고, 대신 120 안내를 붙입니다.
  if (!DRAFT_ENABLED || isForeign()) return;
  if (!view || view.status !== "completed" || typeof view.job_id !== "string") return;
  open.hidden = false;
  let panel = null;
  open.addEventListener("click", () => {
    if (panel) {
      panel.hidden = !panel.hidden;
      if (!panel.hidden) panel.querySelector(".draft-details").focus();
      return;
    }
    panel = fromTemplate("tpl-draft");
    card.querySelector(".reply").after(panel);
    setupDraft(panel, view.job_id);
    panel.querySelector(".draft-details").focus();
  });
}

function setupDraft(panel, jobId) {
  const make = panel.querySelector(".draft-make");
  const details = panel.querySelector(".draft-details");
  const status = panel.querySelector(".draft-status");
  const result = panel.querySelector(".draft-result");
  const notice = panel.querySelector(".draft-notice");
  const body = panel.querySelector(".draft-body");
  const copy = panel.querySelector(".draft-copy");
  const sites = panel.querySelector(".draft-sites");

  make.addEventListener("click", async () => {
    make.disabled = true;
    status.hidden = false;
    status.textContent = "초안을 쓰고 있어요. 보통 10~30초 걸려요.";
    const outcome = await requestDraft(jobId, details.value);
    make.disabled = false;
    if (!outcome.ok) {
      status.textContent = outcome.text;
      return;
    }
    status.hidden = true;
    make.textContent = "다시 만들기";
    const draft = outcome.draft;
    notice.hidden = !draft.notice;
    notice.textContent = draft.notice || "";
    body.value = draft.body;
    sites.textContent = "";
    draft.sites.forEach((site) => {
      const li = document.createElement("li");
      const link = document.createElement("a");
      link.className = "draft-site";
      link.href = safeUrl(site.url);
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      const name = document.createElement("span");
      name.className = "draft-site-name";
      name.textContent = site.name + "에 신청하기";
      const desc = document.createElement("span");
      desc.className = "draft-site-desc";
      desc.textContent = typeof site.description === "string" ? site.description : "";
      link.append(name, desc);
      li.appendChild(link);
      sites.appendChild(li);
    });
    result.hidden = false;
    body.focus({ preventScroll: true });
    scrollIntoViewSoft(result);
  });

  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(body.value);
      copy.textContent = "복사했어요";
    } catch {
      body.select();
      copy.textContent = "직접 복사해 주세요 (Ctrl+C)";
    }
    setTimeout(() => { copy.textContent = "초안 복사"; }, 1800);
  });
}

function answerCard(view, info, seconds, translated) {
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
  const took = seconds < 1 ? t("1초 미만") : t("{n}초", { n: seconds });
  meta.textContent = typeof view.as_of_date === "string" ? t("기준일 {d}", { d: view.as_of_date }) + " (" + took + ")" : took;

  if (info.kind === "answer") {
    kind.textContent = t("근거 확인");
    const korean = typeof view.answer === "string" ? view.answer : UNKNOWN;
    answer.textContent = translated ? translated.text : korean;
    if (isForeign()) showTranslation(li, korean, translated);
    items.forEach((item) => list.appendChild(sourceElement(item)));
    sources.hidden = false;
    verified.hidden = false;
  } else {
    reply.classList.add("is-unknown");
    kind.textContent = t("확인 불가");
    // 위 칸에 안내 문장을 크게 보여 주므로 "확인 불가" 한 단어짜리 답 문장은 숨깁니다.
    answer.hidden = true;
    const lead = document.createElement("p");
    lead.textContent = info.text;
    reason.appendChild(lead);
    const message = view.result && typeof view.result.message === "string" ? view.result.message : "";
    if (message && !isForeign()) {
      const detail = document.createElement("p");
      detail.className = "reason-detail";
      detail.textContent = message;
      reason.appendChild(detail);
    }
    const inquiry = inquiryUrl(view);
    const hint = document.createElement("p");
    hint.className = "reason-detail";
    if (info.reason === "no_basis") {
      hint.textContent = t("조례 밖의 내용(국가 법률·시행규칙·개별 사업 등)일 수 있어요. 처벌 여부는 여기서 판단하지 않아요. 120다산콜재단(02-120, 외국어 9번)에 물어보면 담당 기관을 안내받을 수 있어요.");
    } else {
      hint.textContent = t("질문을 조금 더 구체적으로 바꿔 보시거나, 관할 구청이나 담당 부서에 문의해 주세요.");
    }
    reason.appendChild(hint);
    if (inquiry && DRAFT_ENABLED) {
      // 서버가 준 주소만 씁니다(필드가 없으면 링크를 숨김, docs/ui-guide.md 3-1).
      const link = document.createElement("a");
      link.className = "inquiry";
      link.href = inquiry;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = t("국민신문고에 문의하기");
      reason.appendChild(link);
    }
    reason.hidden = false;
  }
  wireCopy(li, copyText(view, items, translated));
  wireDraft(li, view);
  wireSourceTexts(li, view, items);
  li.querySelector(".reply-foot").after(fromTemplate("tpl-help"));
  return li;
}

// 외국어 답: 번역이 숫자 검증을 통과했으면 번역 + "한국어 원문 보기", 아니면 한국어 원문 + 안내.
function showTranslation(card, korean, translated) {
  const note = card.querySelector(".answer-note");
  note.hidden = false;
  if (!translated) {
    card.querySelector(".answer").lang = "ko";
    note.textContent = t("번역에서 숫자가 달라질 수 있어 한국어 원문을 그대로 보여 드려요. 120다산콜(02-120 → 9)에서 외국어로 물어볼 수 있어요.");
    return;
  }
  note.textContent = t("AI 번역입니다. 금액·숫자는 한국어 원문과 같은지 확인했어요. 공식 내용은 한국어 원문입니다.");
  const toggle = card.querySelector(".original-toggle");
  const original = card.querySelector(".answer-original");
  original.textContent = korean;
  toggle.textContent = t("한국어 원문 보기");
  toggle.hidden = false;
  toggle.addEventListener("click", () => {
    original.hidden = !original.hidden;
    toggle.textContent = original.hidden ? t("한국어 원문 보기") : t("한국어 원문 접기");
  });
}

function noticeCard(info, question, options) {
  const li = fromTemplate("tpl-notice");
  li.querySelector(".notice-title").textContent = info.kind === "error" ? t("요청을 보내지 못했어요") : t("다시 시도해 주세요");
  li.querySelector(".notice-text").textContent = info.text;
  const retry = li.querySelector(".retry");
  retry.disabled = busy;
  retry.addEventListener("click", () => ask(question, options));
  return li;
}

/* ---- 보내기 ---- */

function setBusy(value) {
  busy = value;
  send.disabled = value;
  heroButton.disabled = value;
  document.querySelectorAll(".example, .retry").forEach((button) => { button.disabled = value; });
}

// options.korean: 이미 한국어로 된 검색 질문(예시 질문 버튼). 없고 외국어 화면이면 먼저 한국어로 옮깁니다.
async function ask(raw, options = {}) {
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
  const userMessage = addUserMessage(question);
  input.value = "";
  heroInput.value = "";
  resizeInput();
  const pending = addPending();
  scrollIntoViewSoft(pending.element.previousElementSibling || pending.element);
  const request = { controller: new AbortController(), pending };
  current = request;

  const lang = getLang();
  const signal = request.controller.signal;
  let korean = typeof options.korean === "string" && options.korean ? options.korean : null;
  let outcome = null;
  let translated = null;
  try {
    if (!korean && lang !== "ko") {
      pending.setText(t("질문을 한국어로 옮기고 있어요"));
      const moved = await translateQuestion(question, lang, { signal });
      if (moved.ok) korean = moved.korean;
      else outcome = moved.cancelled ? { cancelled: true } : { view: null, info: { kind: "retry", text: moved.text } };
    }
    if (!outcome) {
      if (lang !== "ko") {
        userMessage.showKorean(korean);
        pending.setText(t("조례에서 근거 조항을 찾고 있습니다"));
      }
      outcome = await askQuestion(korean || question, {
        onPoll: () => pending.setText(t("시간이 더 걸리고 있어요. 계속 확인하는 중")),
        signal,
      });
    }
    if (lang !== "ko" && !outcome.cancelled && outcome.info && outcome.info.kind === "answer" && current === request) {
      pending.setText(t("답을 번역하고 금액을 확인하고 있어요"));
      translated = await translateAnswer(outcome.view.job_id, lang, { signal });
    }
  } catch {
    outcome = signal.aborted ? { cancelled: true } : { view: null, info: { kind: "error", text: t("화면 처리 중 오류가 발생했습니다. 다시 보내 주세요.") } };
  }
  // 기다리는 동안 처음 화면으로 돌아갔으면(취소) 이 결과는 버립니다. 입력은 cancelCurrent가 이미 풀었습니다.
  if (current !== request || outcome.cancelled) return;
  current = null;
  pending.stop();
  const seconds = pending.seconds();
  const { view, info } = outcome;
  setBusy(false);
  const card = info.kind === "answer" || info.kind === "unknown"
    ? answerCard(view, info, seconds, translated)
    : noticeCard(info, question, options);
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
    ? t("현재 서버 점검 중으로 질문에 답변할 수 없습니다. 잠시 후 다시 이용해 주세요.")
    : t("서버가 아직 준비되지 않았습니다. 잠시 후 새로고침해 주세요.");
  banner.hidden = false;
}

/* ---- 말로 묻기: 브라우저 음성 인식(지원하는 브라우저에서만 버튼이 보임) ---- */

const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let listening = null;

function setupVoice() {
  if (!Recognition) return;
  document.querySelectorAll(".voice-btn").forEach((button) => {
    button.hidden = false;
    button.addEventListener("click", () => {
      if (listening) {
        listening.stop();
        return;
      }
      const target = $(button.dataset.target);
      const recognition = new Recognition();
      recognition.lang = speechLang();
      recognition.interimResults = true;
      recognition.maxAlternatives = 1;
      const before = target.value ? target.value.trim() + " " : "";
      recognition.onresult = (event) => {
        let text = "";
        for (const result of event.results) text += result[0].transcript;
        target.value = before + text;
        if (target === input) resizeInput();
      };
      recognition.onend = () => {
        listening = null;
        button.classList.remove("is-listening");
        button.setAttribute("aria-label", t("말로 묻기"));
        target.focus();
      };
      recognition.onerror = () => { recognition.stop(); };
      listening = recognition;
      button.classList.add("is-listening");
      button.setAttribute("aria-label", t("듣는 중 (누르면 멈춤)"));
      recognition.start();
    });
  });
}

/* ---- 사진으로 묻기: 사진을 줄여(위치 정보 제거) 보내고, 읽은 글과 제안 질문을 보여 줌 ---- */

const photoInput = $("photo-input");
let photoTarget = null;

function photoPanelFor(targetId) {
  return $(targetId === "question" ? "photo-panel-question" : "photo-panel-hero");
}

async function shrinkPhoto(file) {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, 1600 / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  const dataUrl = canvas.toDataURL("image/jpeg", 0.85);
  return dataUrl.slice(dataUrl.indexOf(",") + 1);
}

function showPhotoPanel(panel, parts) {
  panel.textContent = "";
  parts.forEach((part) => panel.appendChild(part));
  panel.hidden = false;
}

function setupPhoto() {
  document.querySelectorAll(".photo-btn").forEach((button) => {
    button.addEventListener("click", () => {
      photoTarget = button.dataset.target;
      photoInput.value = "";
      photoInput.click();
    });
  });
  photoInput.addEventListener("change", async () => {
    const file = photoInput.files && photoInput.files[0];
    if (!file || !photoTarget) return;
    const target = $(photoTarget);
    const panel = photoPanelFor(photoTarget);
    const status = document.createElement("p");
    status.className = "photo-status";
    status.textContent = t("로디가 사진을 읽고 있어요…");
    status.classList.add("is-reading");
    showPhotoPanel(panel, [status]);
    let outcome;
    try {
      outcome = await sendPhoto(await shrinkPhoto(file), "image/jpeg");
    } catch {
      outcome = { ok: false, text: t("사진을 열 수 없어요. 다른 사진으로 시도해 주세요.") };
    }
    if (!outcome.ok) {
      status.textContent = outcome.text;
      return;
    }
    // 외국어 화면: 표지판에 쓰인 내용을 먼저 그 언어로 보여 줍니다(숫자는 원문과 대조된 번역만).
    const signBlock = document.createElement("section");
    signBlock.className = "photo-sign";
    if (isForeign() && outcome.translation) {
      const signTitle = document.createElement("p");
      signTitle.className = "photo-sign-title";
      signTitle.textContent = t("표지판에 쓰여 있는 내용");
      const signText = document.createElement("p");
      signText.className = "photo-sign-text";
      appendMarked(signText, markValues(outcome.translation, numberValues(outcome.text)));
      const signNote = document.createElement("p");
      signNote.className = "photo-sign-note";
      signNote.textContent = t("AI 번역입니다. 숫자는 원문과 같은지 확인했어요.");
      signBlock.append(signTitle, signText, signNote);
    } else {
      signBlock.hidden = true;
    }
    const head = document.createElement("p");
    head.className = "photo-head";
    head.textContent = isForeign() && outcome.translation ? t("이 표지판에 대해 이렇게 물어볼 수 있어요") : outcome.ai ? t("사진을 읽고 질문을 만들었어요. 확인한 뒤 질문해 주세요.") : t("사진에서 읽은 글로 질문을 만들었어요. 다듬은 뒤 질문해 주세요.");
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = isForeign() ? t("사진에서 읽은 한국어 원문") : t("사진에서 읽은 글 보기 (개인정보는 가림)");
    const read = document.createElement("p");
    read.className = "photo-text";
    read.lang = "ko";
    read.textContent = outcome.text;
    details.append(summary, read);
    const actions = document.createElement("div");
    actions.className = "photo-actions";
    const askButton = document.createElement("button");
    askButton.type = "button";
    askButton.className = "photo-ask";
    askButton.textContent = t("이 질문으로 묻기");
    // 외국어 화면: 입력칸에는 그 언어로 바꾼 질문을 보여 주고, 손대지 않았으면 한국어 질문으로 바로 검색합니다.
    const shown = isForeign() && outcome.display ? outcome.display : outcome.question;
    askButton.addEventListener("click", () => {
      panel.hidden = true;
      const edited = target.value.trim() !== shown.trim();
      ask(target.value, edited || !isForeign() ? {} : { korean: outcome.question });
    });
    const close = document.createElement("button");
    close.type = "button";
    close.className = "photo-close";
    close.textContent = t("닫기");
    close.addEventListener("click", () => { panel.hidden = true; });
    actions.append(askButton, close);
    target.value = shown;
    if (target === input) resizeInput();
    showPhotoPanel(panel, [signBlock, head, details, actions]);
    target.focus();
  });
}

setupLang();
setupVoice();
setupPhoto();
// 새로고침했을 때 결과 화면 주소(#result)에 남아 있으면 처음 화면으로 돌려놓습니다.
if (location.hash === "#result") history.replaceState(null, "", location.pathname);
showView("home");
resizeInput();
showHealth();
