# KubeGuardian

AI 기반 Kubernetes 서비스 진단 Agent (1인 5주 PoC). 기획은 `docs/01-project-proposal.md`, 일정은 `docs/02-execution-plan.md`.

## 작업 규칙

브랜치·커밋·PR은 [CONTRIBUTING.md](.github/CONTRIBUTING.md)를 따른다.

- 기본 브랜치는 `develop`이다. 작업 브랜치는 `develop`에서 따고 PR도 `develop`으로 올린다. `main`은 `develop`이 안정되면(CI 통과, eval 회귀 없음) 옮기는 안정판이다.
- 브랜치: `<type>/<영문-kebab>` 또는 `<type>/w<주차>-<영문-kebab>`. `develop`·`main`에 직접 push하지 않는다.
- 커밋: `<type>(<scope>): <한국어 제목>` (Conventional Commits)
- PR: `.github/pull_request_template.md`를 채운다. 병합은 squash merge.
- 문서는 한국어로 쓴다.

## 작업 흐름

테스크는 GitHub Issue(마일스톤 W1~W5), 진행은 [Project](https://github.com/users/skax-jihyeryu/projects/2)로 관리한다. 규칙은 CONTRIBUTING §5·§6.

| 언제 | 스킬 |
|---|---|
| 주차 시작·마감 | `week-plan` |
| 아침·퇴근 전 (예약 실행) | `daily-check` |
| 버그·추가 작업 발견 | `issue-create` |
| 이슈 구현 → PR | `issue-work` |
| 주간·기간 리뷰 | `work-review` (개인 스킬) |

- 코드 작성은 superpowers 스킬을 따른다. 어떤 조건에서 어떤 스킬을 쓰는지는 `issue-work` 스킬에 정리했다.
- superpowers 계획 문서는 `.claude/plans/`에 둔다 (커밋하지 않음). 별도 worktree는 쓰지 않는다.
- 작업 중 범위 밖 문제는 고치지 말고 이슈로 등록한다.

## 코드 규칙

- 구조는 [실행 계획서 §1.2](docs/02-execution-plan.md#12-kubeguardian-레포). 템플릿 backend-agent와 같은 FastAPI 계층(routes → service → infra)이다.
- `app/`이 Python 루트다. import에 `app.` 접두사를 쓰지 않는다 (`from core.config import get_setting`).
- 에이전트는 `service/agent/<이름>/v<N>/`에 두고 `registry.py`로 고른다.
- `ui/`는 `app/`을 import하지 않고 API를 HTTP로 호출한다.
