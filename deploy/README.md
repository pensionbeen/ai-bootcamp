# 운영 서버 자동 배포

`main`의 변경을 서버가 약 1분마다 확인합니다. 새 커밋을 빌드하고 격리된 컨테이너에서 `/ask` 계약, `/test`, 관리자 인증과 로그 테스트를 통과한 경우에만 `default/upstage-bootcamp`의 `app` 이미지를 교체합니다. 빌드 시간과 점검 시간만큼 반영이 늦어질 수 있습니다.

## 보존되는 항목

- `upstage-bootcamp-data` PVC와 `/app/data/processed/local_rag` 경로를 그대로 사용합니다. 요청 기록 SQLite와 기존 답변 데이터가 여기에 있습니다.
- 관리자 비밀번호와 Upstage API 키는 기존 Kubernetes Secret 참조를 유지합니다.
- Service, Ingress, Cloudflare 규칙과 다른 서비스의 배포 설정은 변경하지 않습니다.
- 교체 직전에 SQLite의 온라인 백업과 기록 식별자 목록을 전용 `deploy-backups` 폴더에 보관합니다. 교체 후 기존 기록이 유지되는지 점검합니다.

운영 배포 방식은 기존 `Recreate`를 유지하므로 이미지 교체 시 짧은 접속 중단이 생길 수 있습니다. 앱의 정상 종료 대기 설정은 유지됩니다. 데이터 구조를 호환되지 않게 바꾸는 변경은 자동 배포에 맡기지 말고 별도의 마이그레이션 계획을 사용해야 합니다.

## 인증과 설치 위치

개발자 컴퓨터의 Git Credential Manager 인증은 GitHub push/병합에만 사용합니다. 공개 저장소를 읽는 서버에는 개인 GitHub 인증정보를 복사하지 않습니다.

| 위치 | 용도 |
| --- | --- |
| `/opt/upstage-bootcamp/autodeploy/bin/deploy.sh` | root가 소유하는 배포 실행 파일 |
| `/opt/upstage-bootcamp/autodeploy/bin/verify.py` | root가 소유하는 검증·백업 코드 |
| `/opt/upstage-bootcamp/autodeploy/repository.git` | 자동 배포 전용 Git 저장소 |
| `/opt/upstage-bootcamp/autodeploy/releases` | 빌드 중에만 사용하는 임시 소스 |
| `/var/lib/upstage-bootcamp-deploy/deployed-state` | 마지막으로 확인된 전체 커밋 SHA와 이미지명 한 쌍 |
| `/var/lib/upstage-bootcamp-deploy/deployments.log` | 성공한 배포 기록 |
| `/var/lib/upstage-bootcamp-deploy/last-failure` | 마지막 실패 요약 |

설치 시 실행 파일과 검증 파일을 위 `bin` 경로에 복사하고 root 소유로 설정합니다. 두 systemd 파일은 `/etc/systemd/system`에 설치합니다. `deployed-state`는 현재 운영 이미지와 실제 커밋으로 먼저 초기화해야 합니다. 예를 들어 `전체40자리SHA upstage-bootcamp:이미지태그`를 한 줄로 저장합니다. 검증된 상태에서 다음 순서로 활성화합니다.

```bash
sudo /opt/upstage-bootcamp/autodeploy/bin/deploy.sh --check
sudo systemctl daemon-reload
sudo systemctl enable --now upstage-bootcamp-deploy.timer
```

Git에서 실행 파일을 변경해도 설치된 root 실행 파일은 자동으로 교체되지 않습니다. 배포 실행 파일 자체를 수정할 때에는 리뷰와 검증 후 해당 파일만 명시적으로 다시 설치합니다.

## 상태 확인과 실패 대응

```bash
systemctl status upstage-bootcamp-deploy.timer
sudo journalctl -u upstage-bootcamp-deploy.service -n 100 --no-pager
sudo cat /var/lib/upstage-bootcamp-deploy/deployed-state
```

빌드나 사전 검증이 실패하면 운영 이미지는 교체하지 않습니다. 교체 후 검증이 실패하면 이전 이미지로 되돌리며, DB는 자동으로 복원하거나 지우지 않습니다. 실패한 동일 커밋은 매분 재시도하지 않고 `failed-sha`에 보류합니다. 원인을 고친 커밋을 `main`에 올리거나, 일시적인 장애를 해결한 뒤 명시적으로 다시 실행합니다.

```bash
sudo /opt/upstage-bootcamp/autodeploy/bin/deploy.sh --retry
```

`main` 강제 푸시로 마지막 정상 배포 커밋이 사라지면 배포를 중단합니다. 운영 이미지를 외부에서 바꾼 경우에도 덮어쓰지 않고 운영자가 상태를 확인하도록 중단합니다. 자동 배포 중지는 이 서비스 전용 timer만 중지합니다.

```bash
sudo systemctl disable --now upstage-bootcamp-deploy.timer
```

Docker 전체 이미지 정리, 다른 컨테이너 삭제, PVC 삭제와 DB 자동 복원은 실행하지 않습니다.
