---
name: issue-work
description: Use when starting implementation of a KubeGuardian GitHub issue — 이슈 진행, 이슈 작업, #N 처리, 테스크 시작, or opening a PR for an issue. Accepts an issue number.
---

# issue-work — 이슈 하나를 브랜치부터 PR까지

## 개요
이슈 하나 = 브랜치 하나 = PR 하나. 이슈 본문의 체크리스트 항목 하나 = 커밋 하나. 개발 방식은 superpowers 스킬을, 브랜치·커밋·PR 형식은 `.github/CONTRIBUTING.md`를 따른다.
Project 조작: `bash .claude/skills/_shared/board.sh`.

## 1. 시작
1. `gh issue view <#>`로 목적·완료 조건·체크리스트·산출물·예상 시간을 읽는다. 구조·계약은 실행 계획서 §1.2·§1.3, 규칙은 `docs/05-rules.md`를 따른다. 완료 조건이 확인할 수 없는 문장이면 먼저 고쳐 달라고 묻는다.
2. 작업 트리가 깨끗한지 확인하고 `git switch develop && git pull --ff-only`.
3. 브랜치: `<type>/w<주차>-<영문-kebab 2~5단어>` (type은 이슈의 `type:` 라벨, 주차는 마일스톤).
4. `board.sh status <#> "In progress"`.

## 2. 개발 (superpowers)
| 조건 | 사용할 스킬 |
|---|---|
| 인터페이스·설계가 이슈와 문서에 정해져 있지 않음 | superpowers:brainstorming (설계 결정은 ADR 후보로 표시) |
| 예상 1시간 초과 또는 파일 3개 이상 변경 | superpowers:writing-plans → superpowers:executing-plans |
| 코드 변경 | superpowers:test-driven-development |
| 버그·테스트 실패 | superpowers:systematic-debugging |
| PR 전 | superpowers:verification-before-completion (`make check`, 관련 시나리오 eval) |
| 리뷰 필수 대상 (CONTRIBUTING §3.1: 100줄 초과, 프롬프트·지침, 규칙 판정, 가드레일, RBAC·Secret, 기반 계약) | superpowers:requesting-code-review |
| 리뷰 지적을 받음 | superpowers:receiving-code-review + CONTRIBUTING §3.1 "리뷰 지적 처리 기준" (심각·중요는 같은 PR에서 수정, 사소는 범위 안 몇 줄만, 명세·설계 변경은 사용자에게 묻기, 재리뷰는 한 번) |

- 계획 문서는 `.claude/plans/<이슈#>-<설명>.md`에 저장한다 (커밋하지 않음).
- 작업 중 범위 밖 문제를 발견하면 고치지 말고 `issue-create`로 등록한다.
- 체크리스트 항목 하나를 끝낼 때마다 커밋한다: `<type>(<scope>): <한국어 제목>` + 꼬리말 `Refs #<#>`. 커밋 후 이슈 본문의 해당 항목을 체크한다 (`gh issue edit <#> --body-file`).

## 3. PR
1. `make check` 통과와 체크리스트 전부 완료를 확인한다. 기능·시나리오 변경이면 관련 `make eval S=<ID>` 결과를 준비한다.
2. `git push -u origin <브랜치>`.
3. `gh pr create --base develop --title "<커밋과 같은 형식>" --body-file <파일>`. 본문은 `.github/pull_request_template.md`를 채운다: 주차, 시나리오, `Closes #<#>`, 검증 결과, 체크리스트.
4. `board.sh status <#> "In review"`.
5. 리뷰 필수 대상이면 리뷰를 실행하고, 결과와 처리 내역을 PR 코멘트로 남긴다 (`gh pr comment`).
6. CI 결과와 병합 조건(CONTRIBUTING §3) 확인 결과를 사용자에게 알린다. **병합은 사용자가 승인한 뒤** squash merge로 한다 (`gh pr merge --squash --delete-branch`). `develop`은 squash만 허용되도록 보호되어 있다.

## 자주 하는 실수
- `develop`에서 바로 커밋 → pre-commit이 막는다. 항상 브랜치에서.
- 이슈 두 개를 한 PR에 → 나눈다.
- 완료 조건을 확인하지 않고 PR → PR 본문 "검증"에 완료 조건별 확인 결과를 적는다.
