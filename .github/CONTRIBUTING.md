# 개발 규칙

KubeGuardian 레포의 브랜치·커밋·PR 규칙이다. 1인 PoC(2026-10-12 ~ 11-20)에 맞춰 단순하게 유지한다.

## 1. 브랜치 규칙

### 1.1 전략: develop 기본 + main 안정판

| 브랜치 | 역할 | 병합 |
|---|---|---|
| `develop` | **기본 브랜치.** 개발 통합. 작업 브랜치는 여기서 따고 여기로 PR한다 | 작업 브랜치 → `develop` (squash merge) |
| `main` | 안정판. `develop`에서 안정이 확인된 상태만 옮긴다 | `develop` → `main` (merge commit), 안정 확인 시 |

- 모든 작업은 `develop`에서 작업 브랜치를 따서 하고, PR로 `develop`에 병합한다.
- `develop`·`main`에 직접 push하지 않는다.
- `develop`이 안정되면 `develop` → `main` PR을 올린다. **안정**은 아래를 모두 만족하는 상태다.
  - `develop`의 CI(lint·테스트)가 통과한다
  - 지금까지 만든 시나리오의 `make eval`에서 회귀가 없다 (eval이 생긴 W1부터)
  - 진행 중이던 기능이 반쯤 들어간 상태가 아니다
- 주차 Exit 조건 통과는 좋은 옮길 시점이지만, 그 사이에도 안정되면 옮겨도 된다.
- `main`에 병합한 뒤 `v0.N` 태그를 단다 (예: `v0.1`).
- 병합한 작업 브랜치는 삭제한다.

### 1.2 이름 규칙

```text
<type>/<설명>
<type>/w<주차>-<설명>      # 주차 계획(02-execution-plan.md)의 작업일 때
```

- 설명은 **영문 소문자 kebab-case**로 2~5단어 쓴다. 한글·공백·대문자는 쓰지 않는다.
- 주차 작업이면 `w1`~`w5` 접두사를 붙인다.

| type | 용도 | 예 |
|---|---|---|
| `feat` | 기능 추가 | `feat/w2-detectors`, `feat/w3-mcp-server` |
| `fix` | 버그 수정 | `fix/redact-event-message` |
| `docs` | 문서·산출물·ADR·학습 노트 | `docs/deliverables`, `docs/adr-002-agent-framework` |
| `scenario` | 장애 시나리오 추가·수정 | `scenario/c1-quota-exhausted` |
| `eval` | 평가 러너·채점·리포트 | `eval/w7-llm-judge` |
| `spike` | 비교·검증용 실험 (병합하지 않을 수도 있음) | `spike/w3-langgraph`, `spike/w6-code-mode` |
| `refactor` | 동작 변화 없는 구조 개선 | `refactor/tool-layer` |
| `chore` | 빌드·설정·의존성·CI | `chore/makefile-eval-target` |

## 2. 커밋 규칙

### 2.1 형식: Conventional Commits

```text
<type>(<scope>): <제목>

<본문 — 왜 바꿨는지>

<꼬리말>
```

- **type**은 영문으로 쓴다. 브랜치 type과 같다: `feat` `fix` `docs` `scenario` `eval` `spike` `refactor` `test` `chore`
- **scope**는 선택이다. 바뀐 모듈을 쓴다.

| scope | 대상 |
|---|---|
| `detectors` `symptoms` `gate` | 결정적 점검 계층 (`service/checks/`) |
| `sensors` `tools` `mcp` | 사실 수집·에이전트 도구·MCP 서버 |
| `agent` `codemode` `guard` `knowledge` | 에이전트 코어·가드레일·조사 지침 |
| `pipeline` `alerts` `notify` | 점검 실행 흐름·문제 지문·Slack 알림 |
| `api` `ui` `slack` `cli` | 화면 라우트·보고서 화면 템플릿·Slack 명령 수신·CLI |
| `core` `db` `model` | 공통 기반 (설정·K8s 클라이언트·LLM 생성·에러 코드·추적)·SQLite 저장소·도메인 모델 |
| `deploy` `scenarios` `eval` | 배포 매니페스트·장애 시나리오·평가 |
| `adr` `learning` `deliverables` | 문서 종류 |

- **제목**
  - 한국어로 쓴다. 50자 이내, 끝에 마침표를 붙이지 않는다.
  - "~ 추가", "~ 수정"처럼 무엇을 했는지로 끝낸다.
- **본문**은 선택이다. 무엇을 했는지보다 **왜** 했는지를 쓴다. 72자에서 줄을 바꾼다.
- **꼬리말**: 관련 이슈는 `Refs #12`, 이슈를 닫으면 `Closes #12`로 쓴다.
- 커밋 하나에는 논리적 변경 하나만 담는다.

**예시**

```text
feat(detectors): S04 env URL 미해결 감지기 추가

원본 템플릿 매니페스트에 서비스 주소가 없어 gateway가 localhost를
호출하는 문제(T1)를 선언 단계에서 잡기 위함. A5 시나리오로 검증.

Refs #8
```

```text
docs(deliverables): 시나리오 수립 문서 추가
```

## 3. PR 규칙

- **제목**은 커밋 제목과 같은 형식으로 쓴다: `feat(detectors): N·P 카테고리 감지기 추가`
- **본문**은 [PR 템플릿](pull_request_template.md)을 채운다.
- **크기**: 한 PR은 한 주제만 담는다. 주차 작업이 크면 나눠서 올린다.
- **대상 브랜치**: 작업 PR은 `develop`으로 올린다. `main`으로는 `develop` → `main` PR만 올린다.
- **병합 방식**: 작업 PR은 **Squash merge**를 쓰고, squash 커밋 메시지는 PR 제목을 그대로 쓴다. `develop` → `main`은 merge commit을 쓰고 제목은 `release: v0.N <요약>`으로 쓴다 (예: `release: v0.2 규칙 점검 계층`).
- **병합 조건**
  - 기능·시나리오 변경이면 관련 시나리오의 `make eval` 결과를 PR에 붙인다.
  - 프롬프트나 조사 지침을 바꿨으면 전체 eval로 회귀를 확인한다 (실행 계획서 §4).
  - 설계 결정이 있으면 ADR을 같은 PR에 넣는다.
- `spike/` 브랜치는 PR 없이 남겨 둬도 된다. 결론은 ADR로 남긴다.

## 4. 문서 규칙

| 종류 | 위치 | 파일명 |
|---|---|---|
| 기획·계획 | `docs/` | `NN-<영문-kebab>.md` (예: `05-evaluation-report.md`) |
| 과제 산출물 | `docs/산출물/` | `NN-<한글제목>.md` |
| 과제 산출물 (제출 양식 그대로 요약한 버전) | `docs/산출물/양식/` | 위와 같은 파일명 |
| 설계 결정 기록 | `docs/adr/` | `ADR-NNN-<영문-kebab>.md` |
| 주간 학습 노트 | `docs/learning/` | `wN-<영문-kebab>.md` |

## 5. 이슈 규칙

작업은 모두 GitHub Issue로 관리한다. 코드가 아닌 작업(승인 요청, 설치)도 이슈로 만든다.

### 5.1 단위

| 단위 | 크기 | 무엇 | 관리 |
|---|---|---|---|
| 마일스톤 | 1주 | 주차(W1~W5, 발표 준비)와 Exit 조건 | 실행 계획서 §2 |
| **이슈** | **2~6시간** | 기능 하나. **PR 하나** | Project에서 상태·예상·실제 시간 |
| 체크리스트 항목 | 0.5~2시간 | 이슈 본문의 `- [ ]` 한 줄. **커밋 하나** | 이슈 안 진행률 |
| 하위 이슈 (예외) | — | 한 기능을 PR 여러 개로 나눠야 할 때만 GitHub sub-issue로 만든다 | 상위 이슈 진행률 |

- 실행 계획서 작업 표의 여러 행을 기능 단위로 묶어 이슈 하나로 만들고, 각 행은 체크리스트 항목이 된다.
- 6시간을 넘으면 기능을 나누거나 하위 이슈를 만든다. 다른 레포(템플릿 포크)에서 하는 작업은 체크리스트로 둔다.
- 진행 중 범위가 커지면 이슈를 키우지 말고 새 이슈를 만든다.

### 5.2 종류와 템플릿

| 종류 | 템플릿 | 라벨 | 언제 |
|---|---|---|---|
| 작업 | `task` | `type:<브랜치 type>` 1개 | 실행 계획서의 작업, 진행 중 새로 생긴 작업 |
| 버그 | `bug` | `type:fix` | 기대와 다르게 동작하는 것 |
| 스파이크 | `spike` | `type:spike` | 비교·검증이 필요한 질문 (결론은 ADR) |
| 운영 작업 | `task` | `type:ops` | 승인 요청, 도구 설치, 환경 구성 |

추가 라벨: `blocked`(외부 요인으로 진행 불가, 이유를 코멘트로), `carried-over`(이전 주차에서 이월).

### 5.3 제목

```text
<scope>: <무엇을 하는지>
```

- scope는 §2.1의 scope를 쓴다. 코드 밖 작업은 `env`(환경·승인)·`scenarios`·`eval`·`docs`를 쓴다.
- 한국어, 40자 이내. 예: `sensors: 사실 수집 (수집기·Evidence 저장소)`, `env: Slack 앱 생성 승인 요청`

### 5.4 본문

템플릿을 채운다. 빠뜨리면 안 되는 것:

- **완료 조건**: 확인할 수 있는 문장 (예: "A5 상태에서 `kg check`가 S04를 장애 신호로 출력"). 실행 계획서의 Exit 조건 문장을 그대로 옮긴다.
- **체크리스트**: 커밋 단위 작업. 각 항목에 예상 시간을 붙인다.
- **예상 시간**: 체크리스트 합계. Project의 `Estimate`에도 같은 값을 넣는다.

### 5.5 이슈와 브랜치·커밋·PR

- 브랜치는 이슈 하나에 하나: `<type>/w<주차>-<설명>`.
- 체크리스트 항목 하나를 끝낼 때마다 커밋하고(`Refs #N`) 체크 표시를 갱신한다.
- PR 본문에 `Closes #N`. 병합하면 이슈가 닫히고 Project에서 Done이 된다.
- 하루를 마칠 때 끝나지 않은 이슈에는 진행 상황을 코멘트로 남긴다.

## 6. 진행 관리 (GitHub Project)

전체 진행은 [GitHub Project](https://github.com/users/skax-jihyeryu/projects/2)에서 본다.

| 항목 | 규칙 |
|---|---|
| 마일스톤 | `W1`~`W5`, `발표 준비`. 마감은 각 주 일요일 (발표 준비는 11/20) |
| Status | `Backlog`(등록) → `Ready`(오늘 할 일) → `In progress`(브랜치 생성) → `In review`(PR 열림) → `Done`(병합) |
| Priority | `P0` 그 주 Exit 조건에 직결 · `P1` 그 주 계획 작업 · `P2` 미뤄도 되는 것 |
| Estimate / Actual | 예상·실제 시간(시간 단위). Actual은 하루를 마칠 때 누적해서 적는다 |

- 금요일에 마일스톤의 Actual·Estimate 합계와 Exit 조건을 확인한다. 못 채운 분량은 다음 주로 이월한다 (실행 계획서 §0.1, §5).

