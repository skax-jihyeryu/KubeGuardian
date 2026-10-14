---
name: daily-check
description: Use when checking KubeGuardian daily progress — 아침 확인, 오늘 할 일, 퇴근 전 정리, 진행 상황, 하루 마감, or when a scheduled morning/evening check fires. Accepts start or end.
---

# daily-check — 하루 시작·마감 확인

## 개요
GitHub Project와 git 활동을 보고 오늘 할 일을 정하거나(start) 오늘 한 일을 정리한다(end).
같은 스킬을 예약 작업이 아침·저녁에 호출한다. 규칙의 정본은 `.github/CONTRIBUTING.md` §5·§6, Project 조작은 `bash .claude/skills/_shared/board.sh`.

## 모드
- 인자 `start` 또는 `end`를 따른다.
- 인자가 없으면 로컬 시각 13시 전은 `start`, 이후는 `end`.

## 공통 수집
1. `board.sh week` → 이번 주차 `W<N>`. `board.sh list W<N>` → 이슈별 Status·Priority·Estimate·Actual.
2. `git fetch -q` 후 `git log --all --since=<오늘 00:00> --oneline`, `gh pr list --state all --search "updated:>=<오늘>"`, `gh run list --limit 5`.
3. `blocked` 라벨 이슈.

## start
1. **현황 한 줄**: 주차, 남은 날(일요일까지), 남은 Estimate 합계, 하루 필요 시간(남은 Estimate ÷ 남은 날).
2. **오늘 할 일**을 이슈 1~2개와 **오늘 끝낼 체크리스트 항목**으로 고른다 (`gh issue view <#>`로 남은 항목 확인). 순서: 이미 `In progress` → `Ready`에 남은 것 → `P0` → `P1`. 선행 이슈가 안 끝난 것은 뺀다. 항목 시간 합계가 하루 필요 시간에 맞게 한다.
3. 고른 이슈를 `board.sh status <#> Ready`로 올린다.
4. 위험 신호가 있으면 적는다: 남은 Estimate가 남은 시간을 넘음, blocked, CI 실패, 열린 지 하루 넘은 PR.

## end
1. 오늘 `Ready`·`In progress`·`In review` 이슈마다 오늘의 커밋·PR과 체크한 항목을 붙여 정리한다.
2. 사용자에게 이슈별 **오늘 쓴 시간**을 묻고 `board.sh actual-add <#> <시간>`으로 기록한다. 예약 실행이라 답을 받을 수 없으면 질문만 남기고 기록하지 않는다.
3. 끝나지 않은 이슈에는 `gh issue comment <#>`로 진행 코멘트를 남긴다: 한 일 / 남은 일 / 막힌 점.
4. 오늘 대화·커밋에서 나온 새 버그·작업 후보를 나열하고, 등록할지 묻는다. 등록은 `issue-create`로 한다.
5. 내일 이어갈 이슈 후보를 적는다.

## 출력 형식
한국어, 화면 하나 분량. 순서: 현황 한 줄 → 이슈 표(#, 제목, Status, 예상/실제) → 위험 신호 → (end) 질문.
예약 실행이면 같은 내용을 알림으로 보낸다.

## 자주 하는 실수
- Actual을 추정해서 적기 → 사용자에게 받은 숫자만 적는다.
- 하루 필요 시간을 넘게 고르기 → 체크리스트 항목 단위로 맞춘다.
- 이슈를 닫거나 PR을 병합하기 → 이 스킬은 상태 확인과 Ready·Actual·코멘트만 바꾼다.
