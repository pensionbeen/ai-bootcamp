// 화면 글자 번역. 한국어 문장 자체를 열쇠로 씁니다: t("질문하기") → 지금 언어의 글자.
// 표에 없는 문장은 한국어 그대로 나옵니다(빠진 번역이 화면을 깨뜨리지 않게).
// 조례 원문·조항 이름·법제처 링크는 번역하지 않습니다. 답 번역은 서버(i18n_answer.py)가 숫자를 확인한 뒤 줍니다.

export const LANGS = [
  { code: "ko", name: "한국어" },
  { code: "en", name: "English" },
  { code: "zh-CN", name: "简体中文" },
  { code: "zh-TW", name: "繁體中文" },
  { code: "ja", name: "日本語" },
];

const CODES = LANGS.map((item) => item.code);
const SPEECH = { ko: "ko-KR", en: "en-US", "zh-CN": "zh-CN", "zh-TW": "zh-TW", ja: "ja-JP" };

let current = "ko";

export function validLang(value) {
  return CODES.includes(value) ? value : null;
}

export function getLang() {
  return current;
}

export function setLang(value) {
  current = validLang(value) || "ko";
  return current;
}

export function isForeign() {
  return current !== "ko";
}

export function speechLang() {
  return SPEECH[current];
}

// 처음 언어: 주소의 ?lang= → 전에 고른 언어 → 브라우저 언어 → 한국어.
export function pickLang(search, stored, browserLangs) {
  const fromUrl = new URLSearchParams(search || "").get("lang");
  if (validLang(fromUrl)) return fromUrl;
  if (validLang(stored)) return stored;
  for (const raw of browserLangs || []) {
    const tag = String(raw || "").toLowerCase();
    if (tag.startsWith("ko")) return "ko";
    if (tag.startsWith("ja")) return "ja";
    if (tag.startsWith("en")) return "en";
    if (tag === "zh-tw" || tag === "zh-hk" || tag === "zh-mo" || tag.startsWith("zh-hant")) return "zh-TW";
    if (tag.startsWith("zh")) return "zh-CN";
  }
  return "ko";
}

// [한국어, English, 简体中文, 繁體中文, 日本語]
const ROWS = [
  // 머리·메뉴
  ["본문 바로가기", "Skip to content", "跳到正文", "跳到正文", "本文へ移動"],
  ["서울특별시 조례 6종 안내 서비스", "Guide to 6 Seoul city ordinances", "首尔市6项条例指南", "首爾市6項條例指南", "ソウル市条例6件の案内サービス"],
  ["시행 기준일", "In force as of", "施行基准日", "施行基準日", "施行基準日"],
  ["질문하기", "Ask", "提问", "提問", "質問する"],
  ["분야별 질문", "Topics", "分类问题", "分類問題", "分野別の質問"],
  ["이용 안내", "How it works", "使用说明", "使用說明", "ご利用案内"],
  ["언어", "Language", "语言", "語言", "言語"],
  // 첫 화면
  ["서울시 조례 및 민원 AI 도우미 · Lawddy", "Seoul ordinance & civil-complaint AI helper · Lawddy", "首尔市条例与民愿 AI 助手 · Lawddy", "首爾市條例與民願 AI 助手 · Lawddy", "ソウル市条例・民願 AI ヘルパー · Lawddy"],
  ["어려운 생활 속 조례,", "Seoul's rules, explained", "首尔的规定，", "首爾的規定，", "ソウルのルール、"],
  ["로디에게 물어보세요.", "in your language.", "用您的语言问 Lawddy。", "用您的語言問 Lawddy。", "あなたの言葉で Lawddy に。"],
  [
    "주차장, 금연구역, 청년 정책, 도시공원, 동물보호, 자전거 이용에 관한 서울특별시 조례 6종에서 근거 조항을 찾아 답변합니다.",
    "Ask about parking, no-smoking zones, youth policy, city parks, animals and bicycles. Lawddy answers from 6 Seoul ordinances and shows the exact articles.",
    "关于停车、禁烟区、青年政策、城市公园、动物保护和自行车，Lawddy 从首尔市6项条例中找出依据条款来回答。",
    "關於停車、禁菸區、青年政策、城市公園、動物保護和自行車，Lawddy 從首爾市6項條例中找出依據條款來回答。",
    "駐車場、禁煙区域、青年政策、都市公園、動物保護、自転車について、ソウル市条例6件から根拠条文を探して答えます。",
  ],
  ["질문 입력", "Your question", "输入问题", "輸入問題", "質問を入力"],
  ["예) 금연구역 과태료는 얼마인가요?", "e.g. What is the fine for smoking in a no-smoking zone?", "例）在禁烟区吸烟罚款多少？", "例）在禁菸區吸菸罰款多少？", "例）禁煙区域でたばこを吸うと過料はいくらですか？"],
  ["말로 묻기", "Ask by voice", "语音提问", "語音提問", "声で質問"],
  ["사진으로 묻기", "Ask with a photo", "拍照提问", "拍照提問", "写真で質問"],
  ["조례 원문 기반 답변 · 근거 조항 및 출처 제공 · 글·말·사진으로 질문 · 5개 언어", "Answers from the ordinance text · Articles and sources shown · Ask by text, voice or photo · 5 languages", "基于条例原文回答 · 提供依据条款和出处 · 文字/语音/拍照提问 · 5种语言", "基於條例原文回答 · 提供依據條款和出處 · 文字/語音/拍照提問 · 5種語言", "条例原文にもとづく回答 · 根拠条文と出典を表示 · 文字・音声・写真で質問 · 5言語"],
  ["분야별 질문 살펴보기", "Browse questions by topic", "按分类浏览问题", "按分類瀏覽問題", "分野別の質問を見る"],
  ["분야별 자주 묻는 질문", "Frequently asked questions", "常见问题", "常見問題", "よくある質問"],
  ["질문을 누르면 바로 답변을 확인할 수 있습니다.", "Tap a question to see the answer.", "点击问题即可查看回答。", "點擊問題即可查看回答。", "質問を押すとすぐに回答が見られます。"],
  ["처음이라면 이렇게 써 보세요.", "First time? Here's how to use Lawddy.", "第一次使用？请按以下步骤。", "第一次使用？請按以下步驟。", "はじめての方はこちら。"],
  ["언어 고르기", "Pick your language", "选择语言", "選擇語言", "言語を選ぶ"],
  ["오른쪽 위에서 한국어·English·中文·日本語 중 하나를 고르세요. 질문과 답이 그 언어로 나와요.", "Choose English, 中文, 日本語 or 한국어 at the top right. Questions and answers appear in that language.", "在右上角选择 English、中文、日本語 或 한국어，问题和回答都会以该语言显示。", "在右上角選擇 English、中文、日本語 或 한국어，問題和回答都會以該語言顯示。", "右上で English・中文・日本語・한국어 から選ぶと、質問も回答もその言語で表示されます。"],
  ["글·말·사진으로 묻기", "Ask by text, voice or photo", "用文字、语音或照片提问", "用文字、語音或照片提問", "文字・音声・写真で質問"],
  ["궁금한 걸 쓰거나, 마이크로 말하거나, 표지판을 찍으세요. 사진은 저장하지 않아요.", "Type it, say it, or snap a Korean sign. We translate the sign for you. Photos are never stored.", "输入、说出，或拍下韩文标识牌，我们会为您翻译。照片不会被保存。", "輸入、說出，或拍下韓文標示牌，我們會為您翻譯。照片不會被保存。", "入力するか、話すか、韓国語の看板を撮影してください。看板は翻訳します。写真は保存しません。"],
  ["근거와 함께 답 받기", "Get an answer with its legal basis", "获得附依据的回答", "獲得附依據的回答", "根拠つきの回答"],
  ["서울시 조례 6종 원문에서 찾은 조항과 원문 링크를 보여 주고, 금액·나이 같은 숫자는 형광펜으로 표시해요.", "You see the exact article from 6 Seoul ordinances, a link to the official text, and the amounts highlighted.", "显示首尔市6项条例中的依据条款和原文链接，金额、年龄等数字以荧光笔标出。", "顯示首爾市6項條例中的依據條款和原文連結，金額、年齡等數字以螢光筆標出。", "ソウル市条例6件の該当条文と原文リンクを表示し、金額や年齢などの数字をマーカーで示します。"],
  ["사람에게 묻기", "Talk to a person", "人工咨询", "人工諮詢", "人に相談"],
  ["더 필요한 건 120다산콜재단(02-120)에 물어보세요. 외국어 상담은 9번이에요.", "Need more help? Call 120 Dasan Call at 02-120 and press 9 for English, Chinese, Japanese, Vietnamese or Mongolian.", "需要更多帮助？请拨打120茶山热线02-120，按9选择外语咨询。", "需要更多協助？請撥打120茶山熱線02-120，按9選擇外語諮詢。", "さらに相談したいときは120タサンコール（02-120）へ。外国語相談は9番です。"],
  ["질문 입력", "Your question", "输入问题", "輸入問題", "質問を入力"],
  ["생활 속 궁금한 점을 문장으로 입력합니다.", "Type, say, or photograph what you want to know, in your language.", "用您的语言输入、说出或拍下您想知道的事。", "用您的語言輸入、說出或拍下您想知道的事。", "知りたいことを、あなたの言葉で入力・音声・写真で。"],
  ["조례 검색", "Ordinance search", "检索条例", "檢索條例", "条例を検索"],
  ["2026-10-01 시행 서울특별시 조례 6종에서 관련 조문을 찾습니다.", "We search 6 Seoul ordinances in force on 2026-10-01 for the matching articles.", "在2026-10-01施行的首尔市6项条例中查找相关条文。", "在2026-10-01施行的首爾市6項條例中查找相關條文。", "2026-10-01施行のソウル市条例6件から関連条文を探します。"],
  ["근거 확인 후 답변", "Checked answer", "确认依据后回答", "確認依據後回答", "根拠を確認して回答"],
  ["인용한 조항이 원문에 있는지 확인한 뒤 답변과 원문 링크를 제공합니다.", "We check that the cited articles exist and that amounts in the translation match the Korean original.", "确认引用条款存在于原文，并核对译文中的金额与韩文原文一致。", "確認引用條款存在於原文，並核對譯文中的金額與韓文原文一致。", "引用した条文が原文にあるか、翻訳の金額が韓国語原文と同じかを確認します。"],
  ["민원 초안 작성", "Next step", "下一步", "下一步", "次の一歩"],
  ["필요하면 AI가 확인된 근거로 민원 초안을 써 드리고, 서울시 응답소·국민신문고 신청으로 이어 줍니다.", "Need a person? Call 120 Dasan Call (02-120, then press 9) for help in English, Chinese, Japanese, Vietnamese or Mongolian.", "需要人工帮助？请拨打120茶山热线（02-120后按9），提供英语、中文、日语、越南语、蒙古语咨询。", "需要人工協助？請撥打120茶山熱線（02-120後按9），提供英語、中文、日語、越南語、蒙古語諮詢。", "人に相談したいときは120タサンコール（02-120のあと9）。英語・中国語・日本語・ベトナム語・モンゴル語に対応。"],
  ["대상 조례", "Ordinances", "对象条例", "對象條例", "対象条例"],
  [
    "주차장 설치 및 관리 조례, 금연환경 조성 및 간접흡연 피해방지조례, 청년 기본 조례, 도시공원 조례, 동물보호 조례, 자전거이용 활성화에 관한 조례",
    "Parking Lots · No-Smoking Environment and Second-hand Smoke · Youth Framework · City Parks · Animal Protection · Bicycle Use (Seoul Metropolitan Government ordinances)",
    "停车场设置及管理 · 禁烟环境及二手烟防治 · 青年基本 · 城市公园 · 动物保护 · 自行车利用促进（首尔特别市条例）",
    "停車場設置及管理 · 禁菸環境及二手菸防治 · 青年基本 · 城市公園 · 動物保護 · 自行車利用促進（首爾特別市條例）",
    "駐車場設置・管理 · 禁煙環境・受動喫煙防止 · 青年基本 · 都市公園 · 動物保護 · 自転車利用活性化（ソウル特別市条例）",
  ],
  ["기준일", "As of", "基准日", "基準日", "基準日"],
  ["2026-10-01 시행본", "Version in force on 2026-10-01", "2026-10-01施行版", "2026-10-01施行版", "2026-10-01施行版"],
  ["자료 출처", "Source", "资料来源", "資料來源", "出典"],
  ["법제처 국가법령정보센터", "Korea Ministry of Government Legislation (law.go.kr)", "韩国法制处国家法令信息中心", "韓國法制處國家法令資訊中心", "韓国法制処 国家法令情報センター"],
  ["유의 사항", "Note", "注意事项", "注意事項", "ご注意"],
  [
    "본 서비스는 조례 안내를 돕는 참고 자료이며 법률 자문이 아닙니다. 정확한 내용은 근거 조항의 원문을 확인하세요.",
    "This is a reference guide, not legal advice. The Korean ordinance text is the official version; translations are for help only.",
    "本服务仅供参考，并非法律咨询。以韩文条例原文为准，译文仅供理解。",
    "本服務僅供參考，並非法律諮詢。以韓文條例原文為準，譯文僅供理解。",
    "本サービスは参考情報であり、法律相談ではありません。正式な内容は韓国語の条例原文です。翻訳は理解の手助けです。",
  ],
  // 결과 화면
  ["처음 화면으로", "Back to home", "返回首页", "返回首頁", "最初の画面へ"],
  ["홈", "Home", "首页", "首頁", "ホーム"],
  ["질문 결과", "Answer", "回答", "回答", "回答"],
  ["서울시 조례 및 민원 도우미", "Seoul ordinance & civil-complaint helper", "首尔市条例与民愿助手", "首爾市條例與民願助手", "ソウル市条例・民願ヘルパー"],
  ["제작", "Made by", "制作", "製作", "制作"],
  ["팀 로디", "Team Lawddy", "Lawddy 团队", "Lawddy 團隊", "チーム Lawddy"],
  ["AI 부트캠프 해커톤 출품작", "AI Bootcamp Hackathon entry", "AI 训练营黑客松作品", "AI 訓練營黑客松作品", "AIブートキャンプ・ハッカソン出品作"],
  ["자료 법제처 국가법령정보센터 (2026-10-01 시행본)", "Data: law.go.kr (in force 2026-10-01)", "资料：韩国法制处（2026-10-01施行版）", "資料：韓國法制處（2026-10-01施行版）", "資料：韓国法制処（2026-10-01施行版）"],
  [
    "본 서비스의 답변은 서울특별시 조례 6종에 근거한 안내이며 법적 효력이 없습니다. 근거를 찾지 못한 내용은 120다산콜재단(02-120) 또는 관할 구청에 문의하시기 바랍니다.",
    "Answers are guidance based on 6 Seoul ordinances and have no legal effect. If no basis is found, call 120 Dasan Call (02-120, then 9) or the district office.",
    "回答依据首尔市6项条例，仅供参考，不具法律效力。找不到依据时，请拨打120茶山热线（02-120后按9）或咨询所属区厅。",
    "回答依據首爾市6項條例，僅供參考，不具法律效力。找不到依據時，請撥打120茶山熱線（02-120後按9）或洽詢所屬區廳。",
    "回答はソウル市条例6件にもとづく案内で、法的効力はありません。根拠が見つからないときは120タサンコール（02-120のあと9）または区役所へ。",
  ],
  ["추가 질문", "Follow-up question", "追加提问", "追加提問", "追加の質問"],
  ["이어서 궁금한 점을 입력하세요", "Ask another question", "继续输入您的问题", "繼續輸入您的問題", "続けて質問を入力してください"],
  ["질문", "Question", "问题", "問題", "質問"],
  ["조례에서 근거 조항을 찾고 있습니다", "Searching the ordinances for the basis", "正在条例中查找依据条款", "正在條例中查找依據條款", "条例から根拠条文を探しています"],
  ["질문을 한국어로 옮기고 있어요", "Translating your question for the search", "正在把问题译成韩文以便检索", "正在把問題譯成韓文以便檢索", "検索のため質問を韓国語にしています"],
  ["답을 번역하고 금액을 확인하고 있어요", "Translating the answer and checking the amounts", "正在翻译回答并核对金额", "正在翻譯回答並核對金額", "回答を翻訳し、金額を確認しています"],
  ["근거 조항", "Legal basis", "依据条款", "依據條款", "根拠条文"],
  ["조문 원문 펼쳐 보기", "Show the article text", "展开条文原文", "展開條文原文", "条文の原文を見る"],
  ["조문 원문 접기", "Hide the article text", "收起条文原文", "收起條文原文", "条文の原文を閉じる"],
  ["원문을 불러오는 중", "Loading the text", "正在加载原文", "正在載入原文", "原文を読み込み中"],
  ["조항 이름과 시행일은 법제처 원문 데이터에서 그대로 가져왔습니다.", "Article names and dates come straight from the official law.go.kr data.", "条款名称和施行日直接取自韩国法制处原始数据。", "條款名稱和施行日直接取自韓國法制處原始資料。", "条文名と施行日は法制処の原データからそのまま表示しています。"],
  ["민원 초안 쓰기", "Write a complaint draft", "撰写民愿草稿", "撰寫民願草稿", "民願の下書きを書く"],
  ["답변 복사", "Copy answer", "复制回答", "複製回答", "回答をコピー"],
  ["복사했어요", "Copied", "已复制", "已複製", "コピーしました"],
  ["복사하지 못했어요", "Could not copy", "无法复制", "無法複製", "コピーできませんでした"],
  ["원문 보기", "Official text", "查看原文", "查看原文", "原文を見る"],
  ["다시 보내기", "Send again", "重新发送", "重新傳送", "もう一度送る"],
  ["{n}초", "{n}s", "{n}秒", "{n}秒", "{n}秒"],
  ["1초 미만", "under 1s", "不到1秒", "不到1秒", "1秒未満"],
  ["기준일 {d}", "as of {d}", "基准日 {d}", "基準日 {d}", "基準日 {d}"],
  ["시행 {d}", "in force {d}", "施行 {d}", "施行 {d}", "施行 {d}"],
  ["{label}, 법제처 원문 새 탭에서 열기", "{label}, open the official text in a new tab", "{label}，在新标签页打开原文", "{label}，在新分頁開啟原文", "{label}、原文を新しいタブで開く"],
  ["근거 확인", "Basis checked", "已确认依据", "已確認依據", "根拠確認済み"],
  ["확인 불가", "Not confirmed", "无法确认", "無法確認", "確認できません"],
  ["시간이 더 걸리고 있어요. 계속 확인하는 중", "This is taking longer. Still checking", "需要更长时间，仍在确认", "需要更長時間，仍在確認", "時間がかかっています。確認を続けています"],
  ["한국어 원문 보기", "Show the Korean original", "查看韩文原文", "查看韓文原文", "韓国語の原文を見る"],
  ["한국어 원문 접기", "Hide the Korean original", "收起韩文原文", "收起韓文原文", "韓国語の原文を閉じる"],
  ["AI 번역입니다. 금액·숫자는 한국어 원문과 같은지 확인했어요. 공식 내용은 한국어 원문입니다.", "AI translation. Amounts and numbers were checked against the Korean original, which is the official version.", "AI 翻译。金额和数字已与韩文原文核对，以韩文原文为准。", "AI 翻譯。金額和數字已與韓文原文核對，以韓文原文為準。", "AI翻訳です。金額・数字は韓国語原文と一致することを確認しました。正式な内容は韓国語原文です。"],
  ["번역에서 숫자가 달라질 수 있어 한국어 원문을 그대로 보여 드려요. 120다산콜(02-120 → 9)에서 외국어로 물어볼 수 있어요.", "We could not safely translate this answer, so it is shown in Korean. For help in your language, call 120 Dasan Call (02-120, then 9).", "为避免译错数字，回答以韩文显示。如需母语帮助，请拨打120茶山热线（02-120后按9）。", "為避免譯錯數字，回答以韓文顯示。如需母語協助，請撥打120茶山熱線（02-120後按9）。", "数字の誤訳を避けるため、回答は韓国語で表示しています。母語での相談は120タサンコール（02-120のあと9）へ。"],
  ["검색에 쓴 한국어 질문: {q}", "Searched in Korean as: {q}", "检索用的韩文问题：{q}", "檢索用的韓文問題：{q}", "検索に使った韓国語の質問：{q}"],
  // 120 안내 카드
  ["사람에게 물어보고 싶다면", "Want to talk to a person?", "想咨询工作人员？", "想諮詢工作人員？", "人に相談したいときは"],
  ["120다산콜재단에 전화하면 서울시 담당 부서를 안내받을 수 있어요. 휴대폰은 02-120, 외국어 상담(영어·중국어·일본어·베트남어·몽골어)은 9번, 평일 오전 9시~오후 6시예요.", "120 Dasan Call foreign-language line: call 02-120 and press 9. English, Chinese, Japanese, Vietnamese, Mongolian. Weekdays 9:00–18:00. You can also text 02-120 in English.", "120茶山热线外语咨询：拨打02-120后按9。提供英语、中文、日语、越南语、蒙古语，工作日9:00–18:00。", "120茶山熱線外語諮詢：撥打02-120後按9。提供英語、中文、日語、越南語、蒙古語，平日9:00–18:00。", "120タサンコール外国語相談：02-120に電話して9を押してください。英語・中国語・日本語・ベトナム語・モンゴル語、平日9時〜18時。"],
  ["02-120 전화하기", "Call 02-120", "拨打 02-120", "撥打 02-120", "02-120に電話"],
  // 확인 불가 안내
  ["조례 밖의 내용(국가 법률·시행규칙·개별 사업 등)일 수 있어요. 처벌 여부는 여기서 판단하지 않아요. 120다산콜재단(02-120, 외국어 9번)에 물어보면 담당 기관을 안내받을 수 있어요.", "This may be covered by a national law or a specific program, not these ordinances, so Lawddy does not say whether it is punishable. 120 Dasan Call (02-120, then 9) can point you to the right office.", "这可能属于国家法律或具体项目，而非这些条例，因此 Lawddy 不判断是否会受处罚。120茶山热线（02-120后按9）可为您指引负责机关。", "這可能屬於國家法律或具體項目，而非這些條例，因此 Lawddy 不判斷是否會受處罰。120茶山熱線（02-120後按9）可為您指引負責機關。", "この条例ではなく国の法律や個別の事業の内容かもしれないため、処罰の有無は判断しません。120タサンコール（02-120のあと9）が担当窓口を案内します。"],
  ["조례 밖의 내용(법률·시행규칙·개별 사업 등)일 수 있습니다. 관할 구청이나 담당 부서에 문의해 주세요.", "This may be covered by a national law or a specific program rather than these ordinances. Please ask 120 Dasan Call (02-120, then 9) or the district office.", "这可能属于法律或具体项目，而非这些条例。请咨询120茶山热线（02-120后按9）或所属区厅。", "這可能屬於法律或具體項目，而非這些條例。請洽詢120茶山熱線（02-120後按9）或所屬區廳。", "法律や個別の事業の内容かもしれません。120タサンコール（02-120のあと9）か区役所にお問い合わせください。"],
  ["질문을 조금 더 구체적으로 바꿔 보시거나, 관할 구청이나 담당 부서에 문의해 주세요.", "Try a more specific question, or ask 120 Dasan Call (02-120, then 9).", "请把问题问得更具体，或咨询120茶山热线（02-120后按9）。", "請把問題問得更具體，或洽詢120茶山熱線（02-120後按9）。", "質問をもう少し具体的にするか、120タサンコール（02-120のあと9）にお問い合わせください。"],
  ["국민신문고에 문의하기", "Ask on e-People (Korean ID needed)", "在国民申闻鼓咨询（需韩国实名认证）", "在國民申聞鼓諮詢（需韓國實名認證）", "国民申聞鼓で問い合わせ（韓国の実名認証が必要）"],
  ["요청을 보내지 못했어요", "Could not send", "发送失败", "傳送失敗", "送信できませんでした"],
  ["다시 시도해 주세요", "Please try again", "请重试", "請重試", "もう一度お試しください"],
  ["화면 처리 중 오류가 발생했습니다. 다시 보내 주세요.", "Something went wrong on this page. Please send again.", "页面出错，请重新发送。", "頁面出錯，請重新傳送。", "画面でエラーが起きました。もう一度送ってください。"],
  ["질문을 한국어로 옮기지 못했어요. 잠시 후 다시 보내 주세요.", "We could not translate your question. Please try again shortly.", "无法翻译您的问题，请稍后重试。", "無法翻譯您的問題，請稍後重試。", "質問を翻訳できませんでした。しばらくしてからもう一度送ってください。"],
  ["현재 서버 점검 중으로 질문에 답변할 수 없습니다. 잠시 후 다시 이용해 주세요.", "The service is under maintenance. Please try again later.", "服务维护中，请稍后再试。", "服務維護中，請稍後再試。", "メンテナンス中です。しばらくしてからご利用ください。"],
  ["서버가 아직 준비되지 않았습니다. 잠시 후 새로고침해 주세요.", "The service is not ready yet. Please reload in a moment.", "服务尚未就绪，请稍后刷新。", "服務尚未就緒，請稍後重新整理。", "まだ準備ができていません。少ししてから再読み込みしてください。"],
  ["듣는 중 (누르면 멈춤)", "Listening (tap to stop)", "正在听（点击停止）", "正在聽（點擊停止）", "聞いています（押すと停止）"],
  // 원문
  ["검색 DB에 저장된 조례 원문입니다. 답에 나온 숫자·금액을 형광펜으로 표시했어요.", "This is the Korean ordinance text. Numbers and amounts from the answer are highlighted.", "这是韩文条例原文，回答中的数字和金额已用荧光笔标出。", "這是韓文條例原文，回答中的數字和金額已用螢光筆標出。", "韓国語の条例原文です。回答に出た数字・金額をマーカーで示しています。"],
  ["검색 DB에 저장된 조례 원문입니다.", "This is the Korean ordinance text.", "这是韩文条例原文。", "這是韓文條例原文。", "韓国語の条例原文です。"],
  ["이 조항의 원문은 '원문 보기'로 법제처에서 확인해 주세요.", "Open 'Official text' to read this article on law.go.kr.", "请点击“查看原文”在法制处网站阅读此条。", "請點擊「查看原文」在法制處網站閱讀此條。", "「原文を見る」から法制処サイトでご確認ください。"],
  ["번역 (참고용)", "Translation (for reference)", "译文（仅供参考）", "譯文（僅供參考）", "翻訳（参考）"],
  ["번역하지 못했어요. 숫자가 달라질 수 있어 원문만 보여 드려요.", "Not translated: we show only the original so no number is changed.", "未翻译：为避免数字出错，只显示原文。", "未翻譯：為避免數字出錯，只顯示原文。", "数字の誤りを避けるため、原文のみ表示しています。"],
  // 사진
  ["사진에서 글자를 읽고 있어요. 잠시만 기다려 주세요.", "Reading the text in your photo…", "正在识别照片中的文字…", "正在辨識照片中的文字…", "写真の文字を読んでいます…"],
  ["사진을 열 수 없어요. 다른 사진으로 시도해 주세요.", "Could not open this photo. Try another one.", "无法打开照片，请换一张。", "無法開啟照片，請換一張。", "写真を開けません。別の写真でお試しください。"],
  ["사진을 읽고 질문을 만들었어요. 확인한 뒤 질문해 주세요.", "We read the photo and wrote a question. Check it, then ask.", "已读取照片并生成问题，确认后提问。", "已讀取照片並產生問題，確認後提問。", "写真を読んで質問を作りました。確認してから質問してください。"],
  ["사진에서 읽은 글로 질문을 만들었어요. 다듬은 뒤 질문해 주세요.", "We made a question from the text in the photo. Edit it, then ask.", "已根据照片文字生成问题，修改后提问。", "已根據照片文字產生問題，修改後提問。", "写真の文字から質問を作りました。直してから質問してください。"],
  ["사진에서 읽은 글 보기 (개인정보는 가림)", "Text found in the photo (personal info hidden)", "照片中识别的文字（已遮盖个人信息）", "照片中辨識的文字（已遮蓋個人資訊）", "写真から読んだ文字（個人情報は隠しています）"],
  ["로디가 사진을 읽고 있어요…", "Lawddy is reading your photo…", "Lawddy 正在读取照片…", "Lawddy 正在讀取照片…", "Lawddy が写真を読んでいます…"],
  ["표지판에 쓰여 있는 내용", "What the sign says", "标识牌上写的内容", "標示牌上寫的內容", "看板に書いてあること"],
  ["AI 번역입니다. 숫자는 원문과 같은지 확인했어요.", "AI translation. Numbers were checked against the original.", "AI 翻译，数字已与原文核对。", "AI 翻譯，數字已與原文核對。", "AI翻訳です。数字は原文と照合しました。"],
  ["이 표지판에 대해 이렇게 물어볼 수 있어요", "Ask Lawddy what this sign means for you", "可以这样询问这个标识牌", "可以這樣詢問這個標示牌", "この看板についてこう質問できます"],
  ["사진에서 읽은 한국어 원문", "Korean text read from the photo", "照片中的韩文原文", "照片中的韓文原文", "写真から読んだ韓国語の原文"],
  ["사진 속 글자를 찾지 못했어요. 표지판·안내문처럼 글자가 보이는 사진을 찍어 주세요. 상황을 묻고 싶다면 글이나 말로 물어봐 주세요.", "No text found in the photo. Please photograph a sign or notice with visible text. To ask about a situation, type or say your question.", "照片中没有找到文字。请拍摄有文字的标识牌或告示。想询问某种情况，请用文字或语音提问。", "照片中沒有找到文字。請拍攝有文字的標示牌或告示。想詢問某種情況，請用文字或語音提問。", "写真に文字が見つかりません。文字が見える看板や掲示を撮影してください。状況について聞きたいときは文字か音声で質問してください。"],
  ["이 질문으로 묻기", "Ask this", "用这个问题提问", "用這個問題提問", "この質問で聞く"],
  ["닫기", "Close", "关闭", "關閉", "閉じる"],
  ["근거: 서울특별시 {law}", "Basis: Seoul {law}", "依据：首尔特别市 {law}", "依據：首爾特別市 {law}", "根拠：ソウル特別市 {law}"],
  // api.mjs
  ["질문을 입력해 주세요. 너무 긴 질문은 줄여 주세요.", "Please enter a question (not too long).", "请输入问题，过长请缩短。", "請輸入問題，過長請縮短。", "質問を入力してください。長すぎる場合は短くしてください。"],
  ["질문이 너무 깁니다. 줄여서 다시 보내 주세요.", "Your question is too long. Please shorten it.", "问题太长，请缩短后重新发送。", "問題太長，請縮短後重新傳送。", "質問が長すぎます。短くして送ってください。"],
  ["이 주소에서는 질문을 보낼 수 없습니다. 서비스 주소로 직접 접속해 주세요.", "Questions can't be sent from this address. Please open the service directly.", "无法从此地址发送问题，请直接访问服务网址。", "無法從此網址傳送問題，請直接造訪服務網址。", "このアドレスからは送れません。サービスのアドレスに直接アクセスしてください。"],
  ["진행 중이던 질문을 찾을 수 없습니다. 다시 보내 주세요.", "We lost track of that question. Please send it again.", "找不到进行中的问题，请重新发送。", "找不到進行中的問題，請重新傳送。", "処理中の質問が見つかりません。もう一度送ってください。"],
  ["요청을 처리하지 못했습니다(HTTP {n}). 잠시 후 다시 시도해 주세요.", "The request failed (HTTP {n}). Please try again shortly.", "请求失败（HTTP {n}），请稍后重试。", "請求失敗（HTTP {n}），請稍後重試。", "リクエストを処理できませんでした（HTTP {n}）。しばらくしてからお試しください。"],
  ["서울시 조례 6종에서 이 질문의 근거 조항을 찾지 못했습니다.", "We found no article in the 6 Seoul ordinances that answers this.", "在首尔市6项条例中没有找到此问题的依据条款。", "在首爾市6項條例中沒有找到此問題的依據條款。", "ソウル市条例6件から、この質問の根拠条文が見つかりませんでした。"],
  ["근거 후보는 찾았지만 답을 확정하지 못했습니다.", "We found related articles but could not confirm an answer.", "找到了相关条款，但无法确定答案。", "找到了相關條款，但無法確定答案。", "関連する条文はありましたが、答えを確定できませんでした。"],
  ["답을 만드는 데 시간이 더 걸리고 있어요. 3초마다 결과를 확인합니다.", "This is taking longer. Checking every 3 seconds.", "需要更长时间，每3秒确认一次结果。", "需要更長時間，每3秒確認一次結果。", "時間がかかっています。3秒ごとに確認します。"],
  ["답이 늦어지고 있습니다. 잠시 후 같은 질문을 다시 보내 주세요.", "The answer is delayed. Please send the same question again shortly.", "回答延迟，请稍后重新发送同一问题。", "回答延遲，請稍後重新傳送同一問題。", "回答が遅れています。しばらくして同じ質問を送ってください。"],
  ["지금 질문이 많아 대기열이 가득 찼습니다. 잠시 후 다시 보내 주세요.", "We're busy right now. Please send again shortly.", "当前提问较多，请稍后重新发送。", "目前提問較多，請稍後重新傳送。", "ただいま混み合っています。しばらくしてから送ってください。"],
  ["이전 처리가 중단되었습니다. 잠시 후 같은 질문을 다시 보내 주세요.", "Processing was interrupted. Please send the same question again.", "处理中断，请重新发送同一问题。", "處理中斷，請重新傳送同一問題。", "処理が中断されました。同じ質問をもう一度送ってください。"],
  ["처리 중 오류가 발생했습니다. 근거가 없다는 뜻은 아닙니다. 잠시 후 다시 보내 주세요.", "An error occurred. This does not mean there is no basis. Please send again shortly.", "处理时出错，并不代表没有依据。请稍后重新发送。", "處理時出錯，並不代表沒有依據。請稍後重新傳送。", "エラーが起きました。根拠がないという意味ではありません。しばらくして送ってください。"],
  ["답을 받지 못했습니다. 잠시 후 다시 보내 주세요.", "No answer received. Please send again shortly.", "未收到回答，请稍后重新发送。", "未收到回答，請稍後重新傳送。", "回答を受け取れませんでした。しばらくして送ってください。"],
  ["응답이 너무 오래 걸립니다. 잠시 후 같은 질문을 다시 보내 주세요.", "The response is taking too long. Please send the same question again shortly.", "响应时间过长，请稍后重新发送同一问题。", "回應時間過長，請稍後重新傳送同一問題。", "応答に時間がかかりすぎています。しばらくして同じ質問を送ってください。"],
  ["서버에 연결하지 못했습니다. 네트워크를 확인하고 잠시 후 다시 보내 주세요.", "Could not reach the server. Check your connection and try again.", "无法连接服务器，请检查网络后重试。", "無法連線伺服器，請檢查網路後重試。", "サーバーに接続できません。ネットワークを確認してもう一度お試しください。"],
  ["답이 너무 늦어지고 있습니다. 잠시 후 같은 질문을 다시 보내 주세요.", "The answer is very late. Please send the same question again shortly.", "回答太慢，请稍后重新发送同一问题。", "回答太慢，請稍後重新傳送同一問題。", "回答がとても遅れています。しばらくして同じ質問を送ってください。"],
  ["서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.", "Could not reach the server. Please try again shortly.", "无法连接服务器，请稍后重试。", "無法連線伺服器，請稍後重試。", "サーバーに接続できません。しばらくしてお試しください。"],
  ["사진에서 글자를 찾지 못했어요. 글자가 잘 보이게 다시 찍어 주세요.", "No text found in the photo. Take it again so the words are clear.", "照片中没有识别到文字，请拍清楚后再试。", "照片中沒有辨識到文字，請拍清楚後再試。", "写真に文字が見つかりません。文字がはっきり写るように撮り直してください。"],
  ["사진을 읽을 수 없어요. JPG·PNG 사진으로 다시 시도해 주세요.", "Could not read this image. Try a JPG or PNG photo.", "无法读取图片，请使用 JPG 或 PNG 照片。", "無法讀取圖片，請使用 JPG 或 PNG 照片。", "画像を読めません。JPG・PNGの写真でお試しください。"],
  ["사진이 너무 커요. 더 작은 사진으로 시도해 주세요.", "The photo is too large. Try a smaller one.", "照片太大，请换一张小一点的。", "照片太大，請換一張小一點的。", "写真が大きすぎます。小さい写真でお試しください。"],
  ["지금 사진 요청이 많아요. 잠시 후 다시 시도해 주세요.", "Many photo requests right now. Please try again shortly.", "当前照片请求较多，请稍后重试。", "目前照片請求較多，請稍後重試。", "写真のリクエストが混み合っています。しばらくしてお試しください。"],
  ["사진을 읽지 못했어요. 잠시 후 다시 시도해 주세요.", "Could not read the photo. Please try again shortly.", "无法读取照片，请稍后重试。", "無法讀取照片，請稍後重試。", "写真を読めませんでした。しばらくしてお試しください。"],
  ["원문을 불러올 수 없어요.", "Could not load the text.", "无法加载原文。", "無法載入原文。", "原文を読み込めません。"],
  ["원문을 불러오지 못했어요. 각 조항의 '원문 보기'로 법제처에서 확인해 주세요.", "Could not load the text. Use 'Official text' on each article to read it on law.go.kr.", "无法加载原文，请点击各条款的“查看原文”在法制处网站确认。", "無法載入原文，請點擊各條款的「查看原文」在法制處網站確認。", "原文を読み込めません。各条文の「原文を見る」から法制処でご確認ください。"],
];

const DICT = new Map(ROWS.map(([ko, en, zhCN, zhTW, ja]) => [ko, { en, "zh-CN": zhCN, "zh-TW": zhTW, ja }]));

export function t(ko, vars) {
  return tIn(current, ko, vars);
}

// 지금 고른 언어와 상관없이 특정 언어로 (첫 화면 제목 돌리기에 씀).
export function tIn(lang, ko, vars) {
  const row = lang === "ko" ? null : DICT.get(ko);
  let text = row && row[lang] ? row[lang] : ko;
  if (vars) for (const [key, value] of Object.entries(vars)) text = text.split("{" + key + "}").join(String(value));
  return text;
}

// data-i18n(글자), data-i18n-ph(placeholder), data-i18n-aria(aria-label·title)가 붙은 요소를 지금 언어로 바꿉니다.
// 처음 넣은 한국어는 data-ko*에 남겨 두므로 언어를 여러 번 바꿔도 됩니다.
export function applyStatic(root) {
  root.querySelectorAll("[data-i18n]").forEach((el) => {
    if (el.dataset.ko === undefined) el.dataset.ko = el.textContent.trim();
    el.textContent = t(el.dataset.ko);
  });
  root.querySelectorAll("[data-i18n-ph]").forEach((el) => {
    if (el.dataset.koPh === undefined) el.dataset.koPh = el.getAttribute("placeholder") || "";
    el.setAttribute("placeholder", t(el.dataset.koPh));
  });
  root.querySelectorAll("[data-i18n-aria]").forEach((el) => {
    if (el.dataset.koAria === undefined) el.dataset.koAria = el.getAttribute("aria-label") || "";
    el.setAttribute("aria-label", t(el.dataset.koAria));
    if (el.hasAttribute("title")) el.setAttribute("title", t(el.dataset.koAria));
  });
}
