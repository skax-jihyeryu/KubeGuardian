# KubeGuardian

AI 기반 Kubernetes 서비스 진단 Agent (1인 8주 PoC). 기획은 `docs/01-project-proposal.md`, 일정은 `docs/02-execution-plan.md`.

## 작업 규칙

브랜치·커밋·PR은 [CONTRIBUTING.md](CONTRIBUTING.md)를 따른다.

- 기본 브랜치는 `develop`이다. 작업 브랜치는 `develop`에서 따고 PR도 `develop`으로 올린다. `main`은 주차 종료 시 `develop`을 병합하는 안정판이다.
- 브랜치: `<type>/<영문-kebab>` 또는 `<type>/w<주차>-<영문-kebab>`. `develop`·`main`에 직접 push하지 않는다.
- 커밋: `<type>(<scope>): <한국어 제목>` (Conventional Commits)
- PR: `.github/pull_request_template.md`를 채운다. 병합은 squash merge.
- 문서는 한국어로 쓴다.
