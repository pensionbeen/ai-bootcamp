# 화면(UI) 만들기 안내

서울시 조례 묻고 답하기 서비스의 화면을 새로 만들거나 고칠 때 보는 안내서입니다. 서버가 실제로 주고받는 값만 적었습니다.

## 1. 한눈에 보기

- 서버 하나가 **화면 파일**(HTML·CSS·JS·이미지)과 **API**를 같은 주소에서 함께 내보냅니다.
- 화면 파일은 `src/web/` 폴더에 둡니다. `http://127.0.0.1:8000/`을 열면 `src/web/index.html`이 나오고, `src/web/css/site.css`는 `http://127.0.0.1:8000/css/site.css`로 열립니다.
- 파일을 고친 뒤에는 브라우저를 새로 고치기만 하면 됩니다. 서버를 다시 켤 필요가 없습니다(서버가 파일을 매번 새로 읽고, 브라우저에 저장하지 말라는 헤더 `Cache-Control: no-store`를 붙입니다).
- 지금 들어 있는 `src/web/index.html`이 시민용 Lawddy 화면이고, 동작·스타일·이미지는 `src/web/app/`(`app.mjs` 화면, `api.mjs` 서버 응답 처리, `examples.mjs` 예시 질문, `app.css`)에 있습니다. 이전 기본 화면은 `src/web/legacy.html`(`/legacy.html`)로 남겨 두었습니다. `api.mjs`의 시험은 `tests/test_web_app_api.mjs`입니다.
- 화면은 `POST /ui/ask`, `GET /jobs/<job_id>`, `GET /health` 세 가지만 씁니다.
- `POST /ask`는 **채점용**입니다. 화면에서 부르지 말고, 서버 쪽 동작도 바꾸지 마세요.

## 2. 실행

프로젝트 폴더에서 다음 순서로 실행합니다.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS·Linux: source .venv/bin/activate
pip install -r requirements.txt
```

`.env.example`을 복사해 `.env`를 만들고 `UPSTAGE_API_KEY=` 뒤에 키를 넣습니다. 그다음 서버를 켭니다.

```bash
python run.py serve
```

브라우저에서 `http://127.0.0.1:8000/`을 엽니다. 서버는 `Ctrl+C`로 끕니다.

## 3. 화면이 쓰는 API

모든 응답 본문은 JSON(`application/json; charset=utf-8`)입니다.

### 3-1. `POST /ui/ask` — 질문 보내기

요청:

```http
POST /ui/ask
Content-Type: application/json

{"question": "서울시 금연구역에서 담배를 피우면 과태료가 얼마인가요?"}
```

- `Content-Type`은 반드시 `application/json`이어야 합니다(아니면 415).
- `question`은 비어 있지 않은 문자열입니다. 본문 전체는 32KB 이하여야 합니다.
- 서버는 답이 나올 때까지 **최대 80초** 기다린 뒤 응답합니다. 서버 쪽 처리 결과(정상 답, 확인 불가, 시간 초과, 대기열 초과, 처리 오류)는 모두 **HTTP 200**으로 오고, 본문의 `status`로 구분합니다. 요청이 잘못된 경우에만 4xx가 옵니다.
- 같은 질문이 처리 중이면 진행 중인 `job_id`를 공유합니다. 답변이 끝난 뒤 사용자가 같은 질문을 다시 보내면 `POST /ui/ask`로 제출하세요. 서버가 새 `job_id`를 발급하고 답을 새로 생성합니다. 이전 답변을 다시 열 때는 보관한 `job_id`로 `GET /jobs/<job_id>`를 호출합니다.

#### (가) 정상 답

```json
{
  "answer": "서울특별시 금연환경 조성 및 간접흡연 피해방지조례 제10조제1항에 따르면, 시장은 제5조제4항을 위반하여 금연구역에서 흡연을 한 사람에게 10만원의 과태료를 부과·징수한다.",
  "citation": ["서울특별시 금연환경 조성 및 간접흡연 피해방지조례 제10조 제1항"],
  "source_url": "https://www.law.go.kr/ordinInfoP.do?ordinSeq=2171803",
  "effective_date": "2026-10-01",
  "status": "completed",
  "job_id": "3f9a…(64자리 16진수)",
  "sources": [
    {
      "citation": "서울특별시 금연환경 조성 및 간접흡연 피해방지조례 제10조 제1항",
      "source_url": "https://www.law.go.kr/ordinInfoP.do?ordinSeq=2171803",
      "effective_date": "2026-10-01",
      "date_meaning": "source_ordinance_effective_date"
    }
  ],
  "as_of_date": "2026-10-01",
  "result": {
    "status": "code_checked_draft",
    "message": "출처·형식·수치 검사 완료 초안 · 의미 검증 미실행 · 최종 답변 승인 전",
    "response": {"answer": "…", "citation": ["…"], "source_url": "…", "effective_date": "…"},
    "sources": ["… 위 sources와 같음 …"],
    "as_of_date": "2026-10-01",
    "…": "그 밖의 내부 값"
  },
  "answer_allowed": false,
  "final_answer_generated": false
}
```

- 근거 조항이 여러 개이면 `citation`과 `sources`에 여러 개가 들어옵니다. 맨 위의 `source_url`·`effective_date`는 **첫 번째 조항의 값 하나**뿐이므로, 조항별 주소·시행일은 `sources[]`에서 읽으세요.
- 답 문장(`answer`)에는 "조례에서 확인되는 범위의 설명입니다…" 같은 안내 문장이 앞에 붙어 올 수 있습니다. 그대로 보여 주면 됩니다.

#### (나) 확인 불가 — 조례에 근거 없음

```json
{
  "answer": "확인 불가",
  "citation": [],
  "status": "completed",
  "job_id": "…",
  "as_of_date": "2026-10-01",
  "result": {
    "status": "out_of_scope",
    "message": "2026-10-01 기준 서울시 본청 6종 조례의 범위 밖입니다.",
    "inquiry_url": "https://www.epeople.go.kr/",
    "response": {"answer": "확인 불가", "citation": []},
    "as_of_date": "2026-10-01",
    "answer_allowed": false,
    "final_answer_generated": false
  },
  "answer_allowed": false,
  "final_answer_generated": false
}
```

시민 화면에서 **국민신문고에 문의하기** 링크를 연결할 때는 `result.inquiry_url` 값을 `href`로 사용하세요. 지금 화면(`src/web/app/app.mjs`)은 이 값이 있을 때 문의 버튼을 보여 줍니다. 서버는 근거 부족(`insufficient_evidence`)이나 범위 밖(`out_of_scope`)으로 판정해 `확인 불가`와 빈 인용을 반환할 때만 문의 주소를 보냅니다. 필드가 없으면 링크를 숨기세요. 답변이 다른 조례나 시행규칙을 가리키면서 조항을 인용했다면 해당 답과 근거 링크를 그대로 표시합니다.

#### (다) 확인 불가 — 답을 확정하지 못함

모양은 (나)와 같지만 `result.response`가 없고 `result.status`가 다릅니다.

```json
{
  "answer": "확인 불가",
  "citation": [],
  "status": "completed",
  "job_id": "…",
  "as_of_date": "2026-10-01",
  "result": {
    "status": "verification_failed",
    "message": "초안이 검증을 통과하지 못해 답변을 보류했습니다.",
    "…": "그 밖의 내부 값"
  },
  "answer_allowed": false,
  "final_answer_generated": false
}
```

`status`가 `"completed"`이고 `citation`이 비어 있으면 확인 불가입니다. 둘을 나누는 기준은 `result.status`입니다.

| `result.status` | 뜻 | 화면 안내 예 |
| --- | --- | --- |
| `out_of_scope`, `insufficient_evidence` | 조례 6종에서 근거를 찾지 못함 | 확인 불가 사유를 표시하고 `result.inquiry_url`이 있으면 문의 링크를 함께 표시 |
| 그 밖의 값(`verification_failed`, `code_check_failed`, `needs_clarification`, `external_material_required` 등) | 근거 후보는 있었지만 답을 확정하지 못함 | "이 질문은 지금 답을 확정하지 못했습니다(확인 불가)." |

`result.message`에는 서버가 만든 짧은 설명이 들어 있으니 안내 문장 뒤에 덧붙여 보여 줘도 됩니다.

#### (라) 80초 안에 답이 나오지 않음

```json
{
  "answer": "확인 불가",
  "citation": [],
  "status": "deadline_exceeded",
  "job_id": "…",
  "poll_url": "/jobs/…(64자리 16진수)",
  "answer_allowed": false,
  "final_answer_generated": false
}
```

처리는 서버에서 계속됩니다. 이때 `answer`의 "확인 불가"는 **아직 답이 없다는 뜻**이지 근거가 없다는 뜻이 아니므로 결과로 보여 주지 마세요. `poll_url`이 있으면 **3초 간격**으로 `GET poll_url`을 불러 결과를 확인합니다(3-2 참고). `poll_url`이 없으면 잠시 뒤 같은 질문을 다시 보내라고 안내합니다.

#### (마) 대기열 초과·처리 오류

동시에 4개 질문을 처리하고 12개까지 기다리게 합니다(기본값). 그보다 많으면 바로 아래처럼 답합니다.

```json
{"status": "queue_full", "answer_allowed": false, "final_answer_generated": false,
 "answer": "확인 불가", "citation": []}
```

처리 중 문제가 생기면 `status`가 `"processing_error"` 또는 `"failed"`입니다. `"failed"`일 때는 `job_id`와 `result`(`result.status`는 `"processing_error"`)가 함께 옵니다. 이전 처리가 중단된 질문은 `status`가 `"interrupted"`이고 `message`가 붙습니다.

```json
{"status": "processing_error", "answer_allowed": false, "final_answer_generated": false,
 "answer": "확인 불가", "citation": []}
```

모두 근거가 없다는 뜻이 아닙니다. "잠시 후 다시 보내 주세요"처럼 안내하세요.

#### (바) 잘못된 요청 (4xx)

| HTTP | `status` | 이유 |
| --- | --- | --- |
| 400 | `invalid_question` | `question`이 없거나 비었거나 너무 김 |
| 400 | `invalid_json_request`, `invalid_request_fields` | JSON이 아니거나 객체가 아님, 같은 이름의 값이 두 번 |
| 400 | `invalid_wait_seconds` | `wait_seconds`가 0~75 범위의 숫자가 아님 |
| 403 | `local_origin_required` | 다른 주소에서 연 화면이 보낸 요청(8장) |
| 404 | `not_found` | 주소가 틀림(`/ui/ask/`처럼 끝에 `/`가 붙어도 404) |
| 411 | `content_length_required` | 본문 길이 헤더가 없음 |
| 413 | `request_too_large` | 본문이 32KB를 넘음 |
| 415 | `application_json_required` | `Content-Type`이 JSON이 아님 |

본문은 `{"status": "…", "answer_allowed": false, "final_answer_generated": false, "answer": "확인 불가", "citation": []}` 모양입니다.

참고: 본문에 `wait_seconds`(0~75)를 넣으면 그 초만큼만 기다리고, 아직 처리 중이면 HTTP 202와 3-2의 `/jobs` 모양 본문을 받습니다. 기본 화면은 이 방식을 쓰지 않습니다.

### 3-2. `GET /jobs/<job_id>` — 늦어진 질문의 결과 확인

`poll_url` 그대로 부르면 됩니다. 조회는 모델을 다시 호출하지 않습니다. 완료된 답과 인용은 `result.response` 안에, 국민신문고 문의 주소가 있으면 `POST /ui/ask`와 같은 위치인 `result.inquiry_url`에 들어 있습니다.

```json
{
  "job_id": "…",
  "status": "completed",
  "answer_allowed": false,
  "final_answer_generated": false,
  "result": {
    "status": "code_checked_draft",
    "message": "…",
    "response": {"answer": "…", "citation": ["…"], "source_url": "…", "effective_date": "…"},
    "sources": [{"citation": "…", "source_url": "https://www.law.go.kr/...", "effective_date": "…"}],
    "as_of_date": "2026-10-01"
  }
}
```

| HTTP | 본문 `status` | 뜻 | 화면이 할 일 |
| --- | --- | --- | --- |
| 202 | `queued`, `running` | 아직 처리 중(`poll_url` 있음) | 3초 뒤 다시 조회 |
| 200 | `completed` | 끝남. `result.response`가 있으면 답, 없거나 `citation`이 비면 확인 불가 | 3-1과 같은 기준으로 표시 |
| 500 | `failed` 또는 `result.status`가 `processing_error` | 처리 오류 | 다시 보내라고 안내 |
| 409 | `interrupted` | 이전 처리가 중단됨(`message` 있음) | 같은 질문을 다시 보내라고 안내 |
| 404 | `job_not_found` | 없는 번호 | 질문을 다시 보내라고 안내 |

`result.response` 안의 `source_url`·`effective_date`는 조항이 하나면 문자열, 여러 개면 목록입니다. 조항별 값은 `result.sources[]`에서 읽는 것이 가장 쉽습니다. 지금 `src/web/app/api.mjs`의 `jobToView` 함수가 이 응답을 `/ui/ask` 모양으로 바꾸는 예시입니다. 조회를 무한히 반복하지 말고 상한(지금 화면은 10분)을 두세요.

### 3-3. `GET /health` — 서버 상태

준비됨(HTTP 200):

```json
{
  "status": "ok",
  "as_of_date": "2026-10-01",
  "database": {"identity": "…", "…": "…"},
  "answer_allowed": false,
  "final_answer_generated": false,
  "execution": "asynchronous_local",
  "http_wait_limit_seconds": 75,
  "sync_wait_seconds": 80,
  "workers": 4,
  "queue_size": 12
}
```

새 질문을 처리할 수 없음(HTTP 503):

```json
{"status": "not_ready", "reasons": ["missing_upstage_api_key"],
 "answer_allowed": false, "final_answer_generated": false}
```

`reasons`에는 `missing_upstage_api_key`(키 없음), `missing_dependency_requests`·`missing_dependency_dotenv`(패키지 미설치), `database_unavailable`(검색 자료 없음) 등이 들어갑니다. 이미 완료한 답은 해당 작업 번호로 조회할 수 있습니다. 같은 질문을 다시 제출해 새 답을 받으려면 서버가 새 요청을 처리할 수 있어야 합니다.

### 3-4. 필드 요약 (`POST /ui/ask` 응답)

| 필드 | 언제 있나 | 내용 |
| --- | --- | --- |
| `status` | 항상 | `completed`, `deadline_exceeded`, `queue_full`, `processing_error`, `failed`, `interrupted`, 또는 4xx 오류 코드 |
| `answer` | 항상 | 답 문장. 답이 없으면 `"확인 불가"` |
| `citation` | 항상 | 근거 조항 이름 목록. 확인 불가면 `[]` |
| `source_url` | 정상 답 | 첫 번째 조항의 법제처 원문 주소(문자열) |
| `effective_date` | 정상 답 | 첫 번째 조항의 시행일 `YYYY-MM-DD`(문자열) |
| `sources[]` | 정상 답 | 조항별 `citation`, `source_url`, `effective_date`(그리고 `date_meaning`) |
| `as_of_date` | 처리가 끝난 경우 | 기준일(`2026-10-01`) |
| `job_id` | 질문이 접수된 경우 | 64자리 16진수 |
| `poll_url` | `deadline_exceeded`일 때 | `/jobs/<job_id>` |
| `message` | `interrupted`일 때 | 짧은 설명 |
| `result.status` | 처리가 끝난 경우 | 확인 불가의 종류 구분(3-1의 표) |
| `result.message` | 처리가 끝난 경우 | 서버가 만든 짧은 설명 |
| `result.inquiry_url` | 근거 부족·범위 밖으로 정상 완료한 확인 불가 | 국민신문고 문의 링크의 `href`. 조례 원문 링크는 `sources[].source_url` |
| `answer_allowed`, `final_answer_generated` | 항상 | 늘 `false`. 화면에서는 쓰지 않아도 됩니다 |

## 4. 화면에서 꼭 지킬 것

- **답 문장은 그대로 보여 주세요.** 고치거나 줄이거나 요약하지 않습니다. HTML로 넣지 말고 텍스트로 넣습니다(`element.textContent = answer`; `innerHTML` 금지). 줄바꿈이 들어 있으니 CSS `white-space: pre-wrap`을 쓰면 보기 좋습니다.
- **근거 조항을 링크로 보여 주세요.** `sources[]`의 각 항목마다 `citation`을 글자로, `source_url`을 새 탭 링크(`target="_blank" rel="noopener noreferrer"`)로, `effective_date`를 "시행 2026-10-01"처럼 함께 보여 줍니다. 시연에서 확인하는 항목입니다. 주소는 `https://`로 시작할 때만 링크로 만드세요.
- **확인 불가와 오류를 구분해 안내하세요.** 확인 불가(`status: "completed"` + 빈 `citation`)는 결과이고, `deadline_exceeded`·`queue_full`·`processing_error`·`failed`·`interrupted`는 "다시 시도" 안내입니다. 오류를 "근거 없음"으로 보여 주면 안 됩니다.
- **요청 중에는 다시 보내지 못하게 하세요.** 보내기 버튼과 예시 질문 버튼을 응답이 올 때까지 막고, 경과 시간을 보여 주면 좋습니다. 응답은 최대 80초 걸리므로 브라우저 쪽 제한 시간은 그보다 길게(지금 화면은 100초) 잡습니다.

## 5. 파일 규칙

서버는 아래 규칙에 맞는 파일만 내보내고, 나머지는 모두 404입니다. 폴더 목록은 보여 주지 않습니다.

- **허용 확장자(소문자만):** `.html` `.css` `.js` `.mjs` `.json` `.svg` `.png` `.jpg` `.jpeg` `.gif` `.webp` `.ico` `.woff` `.woff2`. `.map`, `.py`, 확장자 없는 파일 등은 내보내지 않습니다.
- **경로 규칙:** 폴더·파일 이름은 영문자나 숫자로 시작하고, 영문자·숫자·`.`·`_`·`-`만 씁니다. 한글·공백이 든 이름, `.`으로 시작하는 이름(`.env` 등)은 쓸 수 없습니다. 대소문자도 실제 파일 이름과 똑같아야 합니다. 경로 전체는 200자 이하입니다.
- **주소 뒤 쿼리 문자열 불가:** `/app.css?v=2`처럼 `?`를 붙이면 404입니다. 캐시 걱정은 필요 없습니다(서버가 저장하지 말라고 알립니다). 이름을 바꾸고 싶으면 `app-v2.css`처럼 파일 이름 자체를 바꾸세요.
- **파일 하나는 10MB 이하.**
- **외부 주소는 막혀 있습니다.** 보안 헤더(CSP) 때문에 CDN의 스크립트·CSS, 구글 웹폰트, 외부 이미지는 불러와지지 않습니다. 필요한 라이브러리·글꼴·이미지는 내려받아 `src/web/` 안에 넣고 `/lib/…`, `/fonts/…`처럼 같은 주소로 불러오세요. `<script>`·`<style>`을 HTML 안에 직접 쓰는 것과 `data:` 이미지는 허용됩니다.
- `/`과 `/index.html`은 항상 `src/web/index.html`입니다. `/health`, `/jobs/…`, `/ui/ask`, `/ask`는 API가 먼저 쓰므로 같은 이름의 파일은 만들지 마세요.

## 6. 다른 주소의 개발 서버(React·Vite 등)를 쓸 때

서버는 **같은 주소에서 열린 화면의 요청만** 받습니다. 브라우저가 붙이는 `Origin`이 `http://127.0.0.1:8000`이나 `http://localhost:8000`이 아니면 403(`local_origin_required`)입니다. 예를 들어 `http://localhost:5173`에서 연 화면이 `http://127.0.0.1:8000/ui/ask`를 직접 부르면 막힙니다. 방법은 두 가지입니다.

**(가) 빌드 결과물을 `src/web/`에 복사해 확인하기.** `npm run build`로 만든 파일(`dist/` 등)을 `src/web/`에 복사하고 `http://127.0.0.1:8000/`에서 엽니다. 5장의 파일 규칙(확장자, 이름, 외부 주소 차단)에 맞는지 확인하세요. 빌드 도구가 `.map` 파일이나 외부 폰트 주소를 넣는 경우가 있으니 끄거나 내려받은 파일로 바꿉니다. 화면 안에서 API는 `/ui/ask`처럼 주소 없이 경로만 써서 부르세요.

**(나) 개발 서버의 프록시 쓰기.** 개발 서버가 `/ui/ask`·`/jobs`·`/health` 요청을 `http://127.0.0.1:8000`으로 넘기게 하고, 넘길 때 `Origin` 헤더를 지우고 `Host`를 `127.0.0.1:8000`으로 맞춥니다. Vite라면 다음과 같습니다.

```js
// vite.config.js  (이 예시는 실행해 확인하지 않았습니다)
import { defineConfig } from 'vite'

const api = {
  target: 'http://127.0.0.1:8000',
  changeOrigin: true, // Host를 127.0.0.1:8000으로 바꿈
  configure: (proxy) => {
    proxy.on('proxyReq', (proxyReq) => {
      proxyReq.removeHeader('origin') // 서버의 Origin 검사를 통과하도록 지움
    })
  },
}

export default defineConfig({
  server: {
    proxy: {
      '/ui/ask': api,
      '/jobs': api,
      '/health': api,
    },
  },
})
```

화면 코드에서는 `fetch('/ui/ask', …)`처럼 경로만 씁니다. 제출할 때는 (가)처럼 결과물을 `src/web/`에 넣어야 합니다.

## 7. 비용과 속도

- 완료 후 다시 제출하면 초안 생성·조문 검사에 모델 호출 비용이 다시 들 수 있습니다. 같은 질문의 검색용 임베딩은 공유할 수 있으며, 호출 횟수와 응답 시간은 질문과 처리 경로에 따라 달라집니다.
- 이미 받은 답을 다시 보여 주거나 진행 상황을 확인할 때는 해당 작업 번호로 조회하세요. 이 조회에는 모델 호출 비용이 들지 않습니다.
- 예시 질문(공개 예시에서 고름):
  - 서울시 공영주차장 1급지 노상주차장 요금은 5분에 얼마인가요?
  - 서울시 금연구역에서 담배를 피우면 과태료가 얼마인가요?
  - 서울시 청년 기본 조례에서 말하는 청년은 몇 살부터 몇 살까지인가요?
  - 경차를 서울시 공영주차장에 세우면 주차요금이 할인되나요?
  - 장애인 할인과 다둥이 행복카드 할인을 공영주차장에서 둘 다 받을 수 있나요?
  - 서울시에서 자전거 탈 때 헬멧을 안 쓰면 과태료가 얼마인가요? (조례에 근거가 없어 확인 불가가 나와야 하는 질문)

## 8. 자주 겪는 문제

| 증상 | 원인과 해결 |
| --- | --- |
| 403 `local_origin_required` | 다른 주소(예: `localhost:5173`, 파일을 직접 연 `file://`)에서 연 화면이 API를 불렀습니다. `http://127.0.0.1:8000/`에서 열거나 6장의 방법을 쓰세요. `127.0.0.1`과 `localhost`는 둘 다 되지만 포트는 서버를 띄운 포트(기본 8000)와 같아야 합니다. |
| 404 `not_found` | 경로나 파일 이름이 5장 규칙에 맞지 않습니다. 대소문자, 확장자(소문자, 허용 목록), `?` 쿼리 문자열, 한글·공백 이름, `.`으로 시작하는 이름을 확인하세요. 폴더 주소(`/assets`)는 열리지 않습니다. |
| `/health`가 503 | `reasons`를 보세요. `missing_upstage_api_key`면 `.env`에 `UPSTAGE_API_KEY`를 넣고 서버를 다시 켭니다. 패키지 이름이 보이면 `pip install -r requirements.txt`를 다시 실행합니다. |
| 스타일·스크립트·글꼴·이미지가 안 나옴 | 브라우저 개발자 도구 콘솔에 "Content Security Policy" 또는 "Refused to load"가 보이면 외부 주소를 쓴 것입니다. 파일을 `src/web/`에 넣고 같은 주소로 불러오세요. 404라면 위 404 항목을 보세요. |
| 고친 내용이 안 보임 | 다른 폴더의 파일을 고쳤는지 확인하세요. 서버가 읽는 곳은 `src/web/`입니다. |
