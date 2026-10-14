---
name: issue-create
description: Use when registering a new KubeGuardian issue — 버그 발견, 추가 작업, 이슈 등록, 스파이크 질문, or when work uncovers something out of the current issue's scope.
---

# issue-create — 이슈 등록

## 개요
버그·추가 작업·스파이크를 CONTRIBUTING §5 규칙대로 등록하고 Project에 넣는다.
규칙의 정본: `.github/CONTRIBUTING.md` §5·§6. 템플릿: `.github/ISSUE_TEMPLATE/{task,bug,spike}.yml`. Project 조작: `bash .claude/skills/_shared/board.sh`.

## 절차
1. **종류 판별**: 기대와 다른 동작 → bug(`type:fix`) / 비교·검증 질문 → spike(`type:spike`) / 나머지 → task(`type:<브랜치 type>`, 코드 밖이면 `type:ops`).
2. **중복 확인**: `gh issue list --state all --search "<핵심어>"`. 비슷한 이슈가 있으면 새로 만들지 않고 그 이슈에 코멘트할지 묻는다.
3. **본문 작성**: 템플릿의 항목을 `### 항목` 섹션으로 채운다. 필수 항목(task: 목적·완료 조건·예상 시간 / bug: 현상·재현·기대 동작 / spike: 질문·판단 기준·시간 상한)이 비면 사용자에게 묻는다. 로그의 비밀값은 마스킹한다.
4. **배정**
   | 상황 | 마일스톤 | Priority |
   |---|---|---|
   | 이번 주 Exit 조건을 막는 버그 | 이번 주차 (`board.sh week`) | P0 |
   | 이번 주에 할 작업 | 이번 주차 | P1 |
   | 나중에 해도 되는 것 | 다음 주차, 마지막 주면 없음 | P2 |
   3.5시간을 넘으면 나누자고 제안한다 (§5.3).
5. **확인 후 생성**: 제목·라벨·마일스톤·Priority·Estimate를 한 줄씩 보여주고 승인받는다. 사용자가 "바로 등록"이라고 했으면 묻지 않는다.
6. `gh issue create ...` → `board.sh add <#> Backlog <P> <Estimate>`. 이번 주 P0 버그면 Status를 `Ready`로 둔다.
7. 진행 중인 이슈에서 나온 것이면 원래 이슈에 `Refs #<새 이슈>` 코멘트를 남긴다.

## 제목
`<scope>: <무엇을>` (§5.2). 예: `check: S04가 포트 없는 URL을 미해결로 판정`, `spike: MCP 서버 경유 시 도구 호출 지연`.

## 자주 하는 실수
- 지금 작업 중인 이슈의 범위를 넓혀서 처리하기 → 범위 밖이면 새 이슈로 등록한다.
- 재현 방법 없이 버그 등록 → 재현이 안 되면 "재현 안 됨"이라고 적는다.
