// 분야별 예시 질문. 화면의 "이런 걸 물어볼 수 있어요"에 나옵니다.
// 질문을 바꾸거나 더하려면 이 파일만 고치면 됩니다.
// 새 질문은 처음 한 번 Upstage API를 부릅니다(유료, 보통 20초). 같은 질문은 저장된 답이 바로 나옵니다.
// note: 버튼 옆에 작게 붙는 설명(선택).
// names·laws·labels·notes: 외국어 화면에 보이는 글자. 검색은 언제나 한국어 question으로 합니다(답은 그 뒤 번역).

export const CATEGORIES = [
  {
    id: "parking",
    name: "주차",
    names: { en: "Parking", "zh-CN": "停车", "zh-TW": "停車", ja: "駐車" },
    law: "주차장 설치 및 관리 조례",
    laws: { en: "Ordinance on Parking Lots", "zh-CN": "停车场设置及管理条例", "zh-TW": "停車場設置及管理條例", ja: "駐車場設置及び管理条例" },
    icon: "car",
    questions: [
      { label: "1급지 노상주차장 요금은 5분에 얼마예요?", labels: { en: "How much is street parking in a Zone 1 area per 5 minutes?", "zh-CN": "1级地区路边停车每5分钟多少钱？", "zh-TW": "1級地區路邊停車每5分鐘多少錢？", ja: "1級地の路上駐車場は5分いくらですか？" }, question: "서울시 공영주차장 1급지 노상주차장 요금은 5분에 얼마인가요?" },
      { label: "경차는 공영주차장 요금이 할인되나요?", labels: { en: "Do compact cars get a discount at public parking lots?", "zh-CN": "小型车在公营停车场有优惠吗？", "zh-TW": "小型車在公營停車場有優惠嗎？", ja: "軽自動車は公営駐車場の料金が割引されますか？" }, question: "경차를 서울시 공영주차장에 세우면 주차요금이 할인되나요?" },
      { label: "장애인 할인과 다둥이 할인, 둘 다 받을 수 있나요?", labels: { en: "Can I get both the disability and the large-family discount?", "zh-CN": "残障优惠和多子女优惠可以同时享受吗？", "zh-TW": "身障優惠和多子女優惠可以同時享受嗎？", ja: "障害者割引と多子世帯割引は両方受けられますか？" }, question: "장애인 할인과 다둥이 행복카드 할인을 공영주차장에서 둘 다 받을 수 있나요?" },
    ],
  },
  {
    id: "smoking",
    name: "금연",
    names: { en: "No smoking", "zh-CN": "禁烟", "zh-TW": "禁菸", ja: "禁煙" },
    law: "금연환경 조성 및 간접흡연 피해방지조례",
    laws: { en: "Ordinance on No-Smoking Environment", "zh-CN": "禁烟环境及二手烟防治条例", "zh-TW": "禁菸環境及二手菸防治條例", ja: "禁煙環境づくり及び受動喫煙防止条例" },
    icon: "smoke",
    questions: [
      { label: "금연구역에서 담배 피우면 과태료가 얼마예요?", labels: { en: "What is the fine for smoking in a no-smoking zone?", "zh-CN": "在禁烟区吸烟罚款多少？", "zh-TW": "在禁菸區吸菸罰款多少？", ja: "禁煙区域でたばこを吸うと過料はいくらですか？" }, question: "서울시 금연구역에서 담배를 피우면 과태료가 얼마인가요?" },
      { label: "공원에서 담배 피워도 되나요?", labels: { en: "Can I smoke in a city park?", "zh-CN": "可以在公园吸烟吗？", "zh-TW": "可以在公園吸菸嗎？", ja: "公園でたばこを吸ってもいいですか？" }, question: "서울시 도시공원에서 담배를 피워도 되나요?" },
    ],
  },
  {
    id: "youth",
    name: "청년",
    names: { en: "Youth", "zh-CN": "青年", "zh-TW": "青年", ja: "青年" },
    law: "청년 기본 조례",
    laws: { en: "Youth Framework Ordinance", "zh-CN": "青年基本条例", "zh-TW": "青年基本條例", ja: "青年基本条例" },
    icon: "person",
    questions: [
      { label: "청년은 몇 살부터 몇 살까지예요?", labels: { en: "What ages count as 'youth' in Seoul?", "zh-CN": "首尔的“青年”是指几岁到几岁？", "zh-TW": "首爾的「青年」是指幾歲到幾歲？", ja: "ソウルの「青年」は何歳から何歳までですか？" }, question: "서울시 청년 기본 조례에서 말하는 청년은 몇 살부터 몇 살까지인가요?" },
      { label: "군대 다녀오면 청년 나이 기준이 달라지나요?", labels: { en: "Does military service change the youth age limit?", "zh-CN": "服完兵役后青年年龄标准会变吗？", "zh-TW": "服完兵役後青年年齡標準會變嗎？", ja: "兵役を終えると青年の年齢基準は変わりますか？" }, question: "의무복무를 마친 제대군인은 서울시 청년정책 참여 나이 기준이 달라지나요?" },
    ],
  },
  {
    id: "park",
    name: "공원",
    names: { en: "Parks", "zh-CN": "公园", "zh-TW": "公園", ja: "公園" },
    law: "도시공원 조례",
    laws: { en: "City Parks Ordinance", "zh-CN": "城市公园条例", "zh-TW": "城市公園條例", ja: "都市公園条例" },
    icon: "tree",
    questions: [
      { label: "어린이공원에서 술 마셔도 되나요?", labels: { en: "Can I drink alcohol in a children's park?", "zh-CN": "可以在儿童公园喝酒吗？", "zh-TW": "可以在兒童公園喝酒嗎？", ja: "子ども公園でお酒を飲んでもいいですか？" }, question: "서울시 어린이공원에서 술을 마시면 과태료가 있나요?" },
    ],
  },
  {
    id: "animal",
    name: "반려동물",
    names: { en: "Pets", "zh-CN": "宠物", "zh-TW": "寵物", ja: "ペット" },
    law: "동물보호 조례",
    laws: { en: "Animal Protection Ordinance", "zh-CN": "动物保护条例", "zh-TW": "動物保護條例", ja: "動物保護条例" },
    icon: "paw",
    questions: [
      { label: "반려견 산책할 때 꼭 지켜야 할 게 뭐예요?", labels: { en: "What rules apply when walking my dog?", "zh-CN": "遛狗时必须遵守什么？", "zh-TW": "遛狗時必須遵守什麼？", ja: "犬の散歩で必ず守ることは何ですか？" }, question: "서울시에서 반려견을 데리고 외출할 때 소유자가 지켜야 할 사항은 무엇인가요?" },
      { label: "맹견은 어린이공원에 들어갈 수 있나요?", labels: { en: "Can dangerous dog breeds enter a children's park?", "zh-CN": "猛犬可以进入儿童公园吗？", "zh-TW": "猛犬可以進入兒童公園嗎？", ja: "猛犬は子ども公園に入れますか？" }, question: "서울시에서 맹견을 데리고 어린이공원에 들어갈 수 있나요?" },
    ],
  },
  {
    id: "bicycle",
    name: "자전거",
    names: { en: "Bicycles", "zh-CN": "自行车", "zh-TW": "自行車", ja: "自転車" },
    law: "자전거이용 활성화에 관한 조례",
    laws: { en: "Ordinance on Bicycle Use", "zh-CN": "自行车利用促进条例", "zh-TW": "自行車利用促進條例", ja: "自転車利用活性化条例" },
    icon: "bike",
    questions: [
      { label: "자전거주차장은 돈을 내야 하나요?", labels: { en: "Do I have to pay to use bicycle parking?", "zh-CN": "自行车停车场要收费吗？", "zh-TW": "自行車停車場要收費嗎？", ja: "自転車駐輪場はお金がかかりますか？" }, question: "서울시 자전거주차장은 돈을 내야 하나요? 낸다면 얼마인가요?" },
      { label: "헬멧 안 쓰면 과태료가 있나요?", labels: { en: "Is there a fine for riding without a helmet?", "zh-CN": "骑车不戴头盔会罚款吗？", "zh-TW": "騎車不戴安全帽會罰款嗎？", ja: "ヘルメットをかぶらないと過料がありますか？" }, question: "서울시에서 자전거 탈 때 헬멧을 안 쓰면 과태료가 얼마인가요?", note: "조례에 없는 내용", notes: { en: "Not in the ordinance", "zh-CN": "条例中没有", "zh-TW": "條例中沒有", ja: "条例にない内容" } },
    ],
  },
];
