# KubeGuardian AI

> AI 기반 Kubernetes 서비스 진단 Agent

템플릿 기반 AI 서비스가 올라간 Kubernetes 환경을 **규칙으로 먼저 점검**하고, 규칙이 문제를 찾았거나 **규칙은 통과했는데 실제로는 문제가 있을 때** AI 에이전트가 조사에 들어가 **무엇이 중요한지 골라내고 원인을 근거와 함께 설명**한다.

## 문서

| 문서 | 내용 |
|---|---|
| [기획서](docs/01-project-proposal.md) | 배경, 아키텍처, 감지기, 장애 시나리오, 평가 |
| [실행 계획서](docs/02-execution-plan.md) | 주차별 작업과 Exit 조건 (10/12~11/20) |
| [외부 서비스 분석](docs/03-market-analysis.md) | K8sGPT, HolmesGPT 등 비교 |
| [UI 화면 정의서](docs/04-ui-spec.md) | 화면 구성 |
| [규칙 점검 명세](docs/05-rules.md) | 감지기별 판정 조건·지속 시간·1차 심각도·출처 |
| [과제 산출물](docs/산출물/) | 역량·기술 스택, 문제 정의·서비스 기획, 시나리오 |
| [ADR](docs/adr/) · [학습 노트](docs/learning/) | 설계 결정 기록, 주간 학습 노트 |

## 개발 환경

필요 도구: [uv](https://docs.astral.sh/uv/) (Python 3.12는 uv가 설치), git

```bash
make setup   # 의존성 설치 + git 훅(pre-commit, 커밋 메시지 검사) 설치
make check   # lint + 테스트 (PR 올리기 전)
make format  # 자동 수정
make run     # 서버 로컬 실행 (http://localhost:8000/api/v1/health)
make doctor  # minikube·kubectl·docker·메모리 확인
```

비밀값은 `.env.example`을 복사한 `.env`에 넣는다. `.env`는 커밋되지 않는다.

## 레포 구조

```text
app/        # 진단 에이전트 (FastAPI, Python 루트: from core.config import ...) — api / service / core
tests/      # 테스트 (app 구조와 같게)
deploy/     # 쿠버네티스 매니페스트
scenarios/  # 장애 시나리오 (_template/ 복사해서 추가)
eval/       # 평가 러너·채점·리포트
scripts/    # 일회성 스파이크·스모크
docs/       # 기획·계획·규칙 명세·산출물·ADR·학습 노트
```

자세한 구조·배포 단위·기반 계약은 [실행 계획서 §1.2·§1.3](docs/02-execution-plan.md#12-kubeguardian-레포).

## 기여

브랜치·커밋·PR 규칙은 [CONTRIBUTING.md](.github/CONTRIBUTING.md). 기본 브랜치는 `develop`이다.

## 보고서 화면

`make run`으로 로컬 서버를 실행한 뒤 `http://localhost:8000/`에서 연다.
클러스터 배포는 `make kg-up` 후 `make kg-web`으로 `http://localhost:8080/`에 연결한다.
화면은 같은 SQLite 점검 DB와 `kubeguardian.yaml`의 대상 네임스페이스를 사용한다.

- **점검 이력**: 기간·심각도·이슈 관계·실행 방식·상태·검색 필터, 점검 상세·과정·근거 원문
- **이슈**: 최신·이전 보고서, 근거 링크, 점검·조사·Slack 알림 타임라인
- **점검 구성**: 대상 서비스·점검 규칙·기능 점검 정의, 점검 규칙 선택 저장
- **자동 점검**: 매시 정각(1시간 주기), Kubernetes CronJob에서 실행
- **지금 점검**: 백그라운드 실행, 동시 1건·직접 실행 1분 간격
- **질문**: 저장된 이력만 읽는 질의 패널, 선택한 이슈·점검·필터 맥락과 검증된 근거 인용

Slack 링크에 사용할 외부 접근 주소는 `web.base_url`로 설정한다(기본 `http://localhost:8080`).
인증 없는 PoC이므로 기존 배포처럼 ClusterIP와 port-forward로 접근한다.
화면 목업 사본은 [docs/ui-mockups](docs/ui-mockups/README.md)에 보존한다.

보고서 화면의 프로젝트 ID는 `kubeguardian.yaml`의 `project.id`로 정한다. 예시 설정은
`llm-gateway`를 사용하므로 `http://localhost:8080/llm-gateway/checks`로 연다.
Kubernetes 진단 대상은 별도의 `target.namespace: dev` 설정이며, 프로젝트 ID를 바꿔도
수집 범위는 바뀌지 않는다. `project`를 생략하면 ID는 `default`다.
