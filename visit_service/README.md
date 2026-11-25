# 방문 기록 서비스

`https://upstage.makertion.com/admin/visits`에서 관리자용 방문 기록을 조회한다.
아이디 `admin`과 기존 질문 로그의 비밀번호를 사용한다. 비밀번호는 저장소에 넣지 않고
기존 Kubernetes Secret `upstage-request-log-admin`에서 읽는다.

## 수집 범위

- GET `/`, `/index.html`, `/legacy.html`, `/test`, `/test/`, `/test.html`의 페이지 요청
- 한국 시간으로 표시하는 방문 시각, 경로, 응답 코드, 처리 시간, 보고된 IP,
  사용자 에이전트, 유입 사이트의 origin, Cloudflare 요청 식별자
- 오늘·최근 7일 필터, 페이지 이동, 자동 갱신, 자동 접속 추정 표시
- 30일 또는 최신 10,000건까지 보관하며 먼저 도달한 제한을 적용한다.
  보관 기간이 지난 기록은 쓰기 시점과 주기적 정리로 삭제한다.

질문 내용, 요청 본문, 쿠키, 인증 헤더, URL query/fragment는 수집하지 않는다.
유입 주소는 경로도 제거한다. IP와 자동 접속 추정은 사람의 신원을 확인하는 수단이 아니다.
방문 수는 고유 방문자 수가 아니며, 봇·새로고침도 포함된다. 배포 이전 방문은 복원하지 않는다.
브라우저가 네트워크 없이 복원한 페이지와 Cloudflare가 원본 서버에 전달하지 않은 요청은
기록되지 않는다. 질문 API 로그는 기존 `/admin/requests`에서 확인한다.

## 기존 앱과의 분리

전용 `upstage-visits` Deployment/Service/PVC를 사용한다. 원본 RAG 앱의 파일과
질문 로그 DB에는 접근하지 않는다. 새 IngressRoute는 위 GET 문서와 방문 관리자 경로만
우선 처리한다. 문서는 고정된 기존 Service에 프록시하고 본문·보안·캐시 헤더를 보존한다.
`POST /ask`, 웹 질문/OCR/작업 조회, 카카오, 기존 관리자 로그 및 정적 자산은
기존 라우팅을 그대로 사용한다.

SQLite WAL과 제한된 비동기 저장 큐를 사용한다. 저장 오류가 발생해도 문서 응답은 계속
제공하고 내부 `/health`의 logging 상태에 오류를 표시한다. 큐 포화·DB 오류·프로세스 강제 종료
상황에서는 일부 방문 기록이 유실될 수 있다. 웹페이지 조회를 위한 운영 로그이며 결제 감사용
기록으로 사용하지 않는다. 관리자 목록은 최대 50건씩 조회한다.

## 배포

이 디렉터리만 Docker build context로 사용한다. `base.json`과 `route.json`은 운영자가
검토 후 `/opt/upstage-bootcamp/visits/config`에 설치하는 파일이다. 배포기 `deploy.sh`와
`verify.py`도 `/opt/upstage-bootcamp/visits/bin`에 명시적으로 설치하며 Git 갱신으로
자동 교체하지 않는다. Secret 값과 기존 앱 리소스를 변경하지 않는다.

기존 앱의 신뢰 배포기는 이 기능을 main에 병합하기 **전에** 현재 `deploy/deploy.sh`로
갱신해야 한다. 방문 서비스와 배포기 문서만 변경되면 RAG 배포를 건너뛰며 실제 앱 SHA를
유지한다. RAG 본체는 여전히 1개 프로세스의 Recreate 배포이므로, 본체 코드 변경까지
무중단 배포가 보장되는 것은 아니다.

방문 배포기는 별도 저장소·잠금·상태 파일을 사용한다. main의 이 디렉터리 변경에만
이미지를 만들고 오프라인 테스트 후 배포한다. 처음에는 새 Pod의 정상 응답·인증·DB·문서
프록시를 확인한 뒤 새 라우트를 추가한다. 후속 방문 서비스 배포는
`maxUnavailable: 0, maxSurge: 1`로 기존 Pod가 응답하는 동안 새 Pod를 준비한다.
현재 단일 노드의 local-path PVC를 공유하며 SQLite WAL을 사용한다.
이는 호스트 장애까지 대비하는 다중 서버 고가용성 구성은 아니다.

`upstage-visits-deploy.timer`는 main을 약 1분마다 확인한다.
`/var/lib/upstage-bootcamp-visits/deployed-state`는 실제 배포 SHA와 이미지를 보관한다.
실패한 SHA는 `--retry` 또는 새 커밋 전까지 재시도하지 않는다.
`--check`는 상태를 읽기만 한다. 기존 배포 갱신 전 SQLite online backup을 생성하고
전용 PVC의 `/data/deploy-backups`에 최신 10개를 보관한다. 검증 실패 시 이전 방문 이미지로
되돌리며 DB를 덮어쓰지 않는다.

30일/1만 건 제한은 운영 DB와 관리자 조회에 적용된다. 배포 전 스냅샷은 복구용 별도
보관본으로, 날짜 기준 자동 삭제 없이 새 백업을 만들 때 최신 10개만 유지한다.

즉시 기존 문서 라우팅으로 돌아가려면 **새 방문 라우트만** 제거한다:

```sh
kubectl -n default delete ingressroute upstage-visits-https
```

원래 IngressRoute가 그대로 남아 있어 문서와 API를 계속 제공한다. 이후 방문 서비스의
원인을 확인하고 정상 상태에서 검토된 `route.json`을 재적용한다. PVC를 삭제하지 않는다.

## 로컬 검사

```sh
python -m unittest discover -s visit_service/tests -p 'test_app.py'
python -m unittest discover -s visit_service/tests -p 'test_audit.py'
python visit_service/tests/test_deploy_runner.py
node --test visit_service/tests/test_visit_log.mjs
python deploy/test_deploy_runner.py
```

방문 기록 데이터와 운영 비밀번호를 Git에 추가하지 않는다.
