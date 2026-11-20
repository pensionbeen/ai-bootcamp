# 카카오톡 채널 연동 (스킬 서버)

카카오톡에서 "서울시 조례 도우미"에게 질문하면, 기존 백엔드(`POST /ui/ask`)가 만든 답을 카카오 말풍선으로 돌려줍니다. 기존 백엔드 코드는 고치지 않고, 이 폴더의 작은 서버가 중간에서 형식만 바꿉니다.

```
카카오톡 → 카카오 i 오픈빌더 → (POST /kakao/skill) skill_server.py → (POST /ui/ask) 기존 백엔드
```

| 파일 | 내용 |
| --- | --- |
| `skill_server.py` | 카카오 요청을 받는 서버. 외부 패키지 없이 Python 3.10+ 표준 라이브러리만 씀 |
| `kakao_format.py` | 백엔드 응답 → 카카오 응답 변환. 결과 분류 기준은 웹 화면(`eun_ui/api.mjs`)과 같음 |
| `test_skill_server.py` | 가짜 백엔드·가짜 카카오로 흐름 시험 (`python -m unittest eun_ui/kakao/test_skill_server.py`) |

## 카카오톡에서 보이는 모습

- **근거 있는 답**: 답 문장(말풍선) + "근거 조항" 카드(조항 이름, 시행일, 법제처 원문 버튼 최대 3개) + 예시 질문 빠른 답장
- **확인 불가**: 이유를 적은 카드. 서버가 `result.inquiry_url`을 주면 "국민신문고 문의" 버튼
- **다시 시도**(시간 초과·대기열 초과·오류): 안내 문장 + "다시 보내기" 빠른 답장. 근거 없음과 구분함
- "처음으로", "도움말", "시작"을 보내면 소개 문구와 예시 질문 버튼

## 5초 제한 처리

카카오는 스킬 서버의 응답을 **5초** 안에 받아야 합니다. 조례 답변은 보통 20초쯤 걸려서 두 방식을 씁니다.

1. **콜백(권장)**: 블록에서 Callback을 켜면 요청에 `callbackUrl`이 옵니다. 서버는 바로 "근거를 찾고 있어요" 대기 문구를 보내고(`useCallback: true`), 답이 나오면 `callbackUrl`로 최종 답을 보냅니다. 콜백 URL은 5분 동안 한 번만 쓸 수 있어서 4분 30초 안에 끝냅니다.
2. **콜백이 없을 때**: 3.5초 안에 답이 나오면 바로 보내고, 아니면 "결과 확인" 버튼을 보냅니다. 사용자가 누르면 그때까지 나온 답을 보여 줍니다(최대 10분 기다림).

콜백 설정이 안 돼 있어도 2번으로 동작하므로, 처음 연결할 때는 콜백 없이 시작해도 됩니다.

## 설정 순서

1. **카카오톡 채널 만들기**: [카카오 비즈니스](https://business.kakao.com)에서 채널 개설(채널 이름: 서울시 조례 도우미)
2. **봇 만들기**: [카카오 i 오픈빌더](https://i.kakao.com)에서 카카오톡 챗봇 생성 → 1번 채널 연결
3. **스킬 서버 켜기**: 기존 백엔드가 떠 있는 컴퓨터에서
   ```bash
   # 백엔드 (이미 켜져 있으면 생략)
   python -B run.py serve --port 8000
   # 스킬 서버 (다른 창에서)
   python eun_ui/kakao/skill_server.py
   ```
   기본으로 `0.0.0.0:8010`에서 받고, 백엔드는 `http://127.0.0.1:8000`으로 부릅니다. 바꾸려면 환경 변수 `KAKAO_SKILL_PORT`, `RAG_BASE_URL`을 씁니다.
4. **https 주소 만들기**: 카카오는 https 주소만 부릅니다. 해커톤 중에는 터널을 쓰면 빠릅니다.
   ```bash
   ngrok http 8010        # 또는 cloudflared tunnel --url http://localhost:8010
   ```
   나온 주소 뒤에 `/kakao/skill`을 붙입니다. 예: `https://xxxx.ngrok-free.app/kakao/skill`
5. **스킬 등록**: 오픈빌더 → 스킬 → 생성 → URL에 4번 주소 입력 → 저장
6. **블록 연결**: 시나리오의 **폴백 블록**(모든 발화가 여기로 옴)과 **웰컴 블록**에서
   - 파라미터 설정 → 스킬 선택: 5번 스킬
   - 봇 응답 → **스킬데이터 사용**
7. **(권장) 콜백 켜기**: 폴백 블록 더보기(…) → Callback 설정 → 활성화 → 기본 대기 문구 입력. 메뉴가 안 보이면 봇 설정에서 AI 챗봇 전환을 신청해야 할 수 있습니다(카카오 챗봇 관리자센터 문의).
8. **배포 후 확인**: 오픈빌더 **배포**를 누른 뒤 카카오톡 채널에서 직접 질문해 봅니다. 콜백은 봇 테스트 창에서는 확인되지 않고 운영 채널에서만 확인됩니다.

## 확인할 것

- 스킬 서버와 백엔드는 **같은 컴퓨터**에서 띄우는 것을 기본으로 했습니다. 백엔드는 루프백(127.0.0.1)으로 띄우면 같은 컴퓨터의 요청만 받습니다.
- 콜백을 보낼 주소는 `kakao.com`, `kakaoenterprise.com`으로 끝나는 https 주소만 허용합니다(다른 주소로 요청을 보내게 만드는 공격 방지). 운영에서 콜백이 안 오고 서버 기록에 `callbackUrl host not allowed`가 보이면, 기록에 나온 호스트를 `KAKAO_CALLBACK_HOSTS`에 더해 주세요. 그동안에는 "결과 확인" 방식으로 동작합니다.
- 질문마다 백엔드가 Upstage를 부르므로(유료) 공개 채널로 열 때는 사용량을 확인하세요.
- 시험: 가짜 백엔드·가짜 콜백으로 응답 형식, 글자 수·버튼 수 제한, 5초 안 응답, 결과 확인 흐름, 콜백 흐름, 늦은 답 조회를 확인했습니다. **실제 카카오 채널과는 아직 연결해 보지 않았습니다.**

## 참고

- [스킬 응답 JSON 포맷](https://kakaobusiness.gitbook.io/main/tool/chatbot/skill_guide/answer_json_format)
- [AI 챗봇 콜백 가이드](https://kakaobusiness.gitbook.io/main/tool/chatbot/skill_guide/ai_chatbot_callback_guide)

## 백엔드 내장판 (권장)

이제 `src/local_rag_service.py`가 `POST /kakao/skill`을 직접 처리합니다(`src/kakao_skill.py`).
main에 합쳐져 배포되면 오픈빌더 스킬 URL을 `https://upstage.makertion.com/kakao/skill` 로 두면 되고,
이 폴더의 `skill_server.py`나 터널은 따로 띄울 필요가 없습니다.
콜백 허용 호스트는 기본 `kakao.com`, `kakaoenterprise.com`이며 환경변수 `KAKAO_CALLBACK_HOSTS`(쉼표 구분)로 바꿀 수 있습니다.
