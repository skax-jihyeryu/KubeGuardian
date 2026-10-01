# KubeGuardian AI

> AI 기반 Kubernetes 서비스 진단 Agent

템플릿 기반 AI 서비스가 올라간 Kubernetes 환경을 **규칙으로 먼저 점검**하고, 규칙이 문제를 찾았거나 **규칙은 통과했는데 실제로는 문제가 있을 때** AI 에이전트가 조사에 들어가 **무엇이 중요한지 골라내고 원인을 근거와 함께 설명**한다.

## 문서

| 문서 | 내용 |
|---|---|
| [기획서](docs/01-project-proposal.md) | 배경, 아키텍처, 감지기, 장애 시나리오, 평가 |
| [실행 계획서](docs/02-execution-plan.md) | 8주 주차별 작업과 Exit 조건 |
| [외부 서비스 분석](docs/03-market-analysis.md) | K8sGPT, HolmesGPT 등 비교 |
| [UI 화면 정의서](docs/04-ui-spec.md) | 화면 구성 |
| [과제 산출물](docs/산출물/) | 역량·기술 스택, 문제 정의·서비스 기획, 시나리오 |
| [ADR](docs/adr/) · [학습 노트](docs/learning/) | 설계 결정 기록, 주간 학습 노트 |

## 개발 환경

필요 도구: [uv](https://docs.astral.sh/uv/) (Python 3.12는 uv가 설치), git

```bash
make setup   # 의존성 설치 + git 훅(pre-commit, 커밋 메시지 검사) 설치
make check   # lint + 테스트 (PR 올리기 전)
make format  # 자동 수정
```

비밀값은 `.env.example`을 복사한 `.env`에 넣는다. `.env`는 커밋되지 않는다.

## 레포 구조

```text
apps/diagnostic-agent/   # 진단 에이전트 (Python, uv workspace 멤버)
docs/                    # 기획·계획·산출물·ADR·학습 노트
```

실행 계획서 §1.2의 구조(`deploy/`, `scenarios/`, `eval/` 등)는 주차 작업에 맞춰 추가한다.

## 기여

브랜치·커밋·PR 규칙은 [CONTRIBUTING.md](CONTRIBUTING.md). 기본 브랜치는 `develop`이다.
