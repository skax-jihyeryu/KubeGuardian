# A6 — gateway의 서비스 주소 env 누락

A6는 F1이 설정한 gateway의 `ADMIN_SERVICE_URL` 항목 하나를 제거한다. 템플릿 앱은 이 항목이 없으면 `http://localhost:8001`을 사용한다. front-chat을 거친 로그인 요청이 5xx로 실패하지만 gateway와 admin Pod는 Ready 상태를 유지한다. A5는 선언된 주소를 localhost 값으로 바꾸고 S04로 감지한다. H1은 주소 설정 부재가 정상인 기준선이다.

## 실행 조건

- 환경 설치 가이드대로 준비한 로컬 `minikube` 컨텍스트의 `dev` 네임스페이스와 H0 정상 기준선을 사용한다. `kubectl`과 Python 3이 필요하다.
- 평가 설정에 실패 시 경로 B를 여는 로그인 API 시나리오를 두고, C05의 `required_config`에 gateway의 `ADMIN_SERVICE_URL`을 선언하지 않는다. 이 키를 운영자가 필수로 선언하면 C05의 규칙 감지로 경로 A가 된다. 시나리오 스크립트는 평가 설정을 바꾸지 않는다.
- 주입 전에 로컬 overlay의 gateway Deployment, F1의 직접 env 값, envFrom의 중복 키 여부, 실행 중인 앱의 localhost 기본값을 확인한다. 같은 키를 envFrom으로 다시 주입하거나 `valueFrom`으로 설정한 Deployment에서는 실행을 중단한다.

## 주입과 원복

```bash
scenarios/A6/inject.sh
scenarios/A6/verify_symptom.sh
scenarios/A6/reset.sh
```

검증은 로그인 5xx, gateway와 admin의 Ready 상태, 해당 요청의 localhost 업스트림 연결 실패 로그를 함께 확인한다. 경로 B의 원인 조사에서는 gateway Deployment의 env 키 목록과 gateway 로그를 대조한다.

원복에 필요한 항목과 위치는 Git이 무시하는 `.state/dev-gateway.json`에 보관한다. 원복은 UID와 resourceVersion을 확인한 뒤 원래 위치에 항목을 복구한다. 주입 뒤 다른 env 편집이나 Deployment 교체가 확인되면 원복을 중단하고 기록을 보존한다. 원복과 롤아웃 확인이 끝나면 기록을 삭제하며, 다시 원복을 실행해도 클러스터를 변경하지 않는다.

오프라인 테스트는 `uv run pytest tests/eval/test_scenario_a6.py`로 실행한다.
