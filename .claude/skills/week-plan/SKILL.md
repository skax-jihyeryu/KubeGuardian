---
name: week-plan
description: Use when starting or closing a KubeGuardian week (W1~W5) — 주차 시작, 주차 계획, 이슈 일괄 생성, 주차 마감, Exit 조건 점검, 이월 처리. Accepts a week argument like W2.
---

# week-plan — 주차 계획·마감

## 개요
실행 계획서(`docs/02-execution-plan.md` §2)의 주차 작업 표를 GitHub Issue로 옮기고, 지난 주차를 마감한다.
상세 이슈는 **이번 주와 다음 주만** 만든다 (M2·M3 체크포인트 결과로 이후 계획이 바뀔 수 있음).

규칙의 정본: `.github/CONTRIBUTING.md` §5(이슈) §6(Project). Project 조작은 `bash .claude/skills/_shared/board.sh`.

## 입력
- 인자 `W<N>`: 계획할 주차. 없으면 `board.sh week`로 오늘 주차를 쓴다.

## 절차

### 1. 지난 주차 마감 (N ≥ 2이고 W<N-1>에 열린 이슈가 있을 때)
1. `board.sh list W<N-1>`로 열린 이슈와 Estimate·Actual 합계를 본다.
2. 실행 계획서의 W<N-1> **Exit 조건**을 항목별로 표로 보여주고, 각 항목 충족 여부를 사용자에게 묻는다. 직접 판정하지 않는다.
3. 열린 이슈마다 사용자에게 **이월 / 닫기**를 묻는다. 이월이면 마일스톤을 W<N>으로 바꾸고 `carried-over` 라벨을 단다.
4. Actual 합계가 22시간을 넘었고 그 전 주도 넘었으면 실행 계획서 §5 축소 순서의 다음 항목을 제안한다.

### 2. 이슈 초안 만들기 (W<N>, W<N+1>)
작업 표 한 행 = 이슈 하나. 이미 같은 제목의 이슈가 그 마일스톤에 있으면 건너뛴다 (`gh issue list --milestone "<제목>" --state all`).

| 필드 | 채우는 법 |
|---|---|
| 제목 | `<scope>: <작업>` (CONTRIBUTING §5.2). 코드 밖 작업은 scope `env` |
| 라벨 | `type:<type>` 1개. 승인·설치·환경은 `type:ops`, 시나리오는 `type:scenario` |
| 마일스톤 | `gh api repos/skax-jihyeryu/KubeGuardian/milestones --jq '.[]|"\(.number) \(.title)"'`의 해당 주차 |
| 본문 | `.github/ISSUE_TEMPLATE/task.yml`의 항목(목적·완료 조건·산출물·예상 시간·관련 시나리오·참고)을 `### 제목` 섹션으로 쓴다. 완료 조건은 그 행과 연결된 **Exit 조건 문장**을 체크박스로 옮긴다 |
| Priority | Exit 조건에 직결되면 `P0`, 나머지 `P1`. 사용자가 미뤄도 된다고 한 것은 `P2` |
| Estimate | 작업 표의 시간 |

3.5시간을 넘는 행은 하위 이슈로 나눌지 초안에 표시한다.

### 3. 확인 후 생성
1. 초안을 표(제목·라벨·Priority·Estimate)로 보여주고 승인을 받는다. 승인 전에는 아무것도 만들지 않는다.
2. 승인되면 `gh issue create --title ... --body-file ... --label ... --milestone ...`로 만들고, 각 이슈를 `board.sh add <#> Backlog <P> <Estimate>`로 Project에 넣는다.
3. 마지막에 `board.sh list W<N>`으로 결과를 보여준다.

## 자주 하는 실수
- 작업 표에 없는 작업을 지어내기 → 표에 없는 것은 `issue-create`로 따로 등록한다.
- 다음 주 이슈를 Ready로 올리기 → 새 이슈는 모두 `Backlog`. Ready는 `daily-check`가 정한다.
- Exit 조건 충족을 스스로 판정하기 → 항상 사용자에게 묻는다.
