# eval

평가 러너, 채점, 리포트. `kg` 점검 흐름과 SQLite 결과만 사용한다.

- `make eval S=<ID>`: 시나리오 하나 (주입 → 증상 확인 → 점검·게이트 → 채점 → 원복). 기본은 규칙만이고, AI 조건은 `make eval S=A5 ARGS="--condition ai"` (아래 "AI 조건")
- `make eval S=<ID> ARGS="--in-cluster"`: 점검·조사를 클러스터 안 runner Pod에서 실행 (아래 "클러스터 안 실행")
- `make eval`: 전체 시나리오 × 2조건 × 3회 (W2 이후)
- 결과는 `eval/history/<시각>-<ID>/`에 `result.json`과 점검 DB로 쌓는다 (커밋하지 않음)

## 러너 (`run.py`)

| 단계 | 하는 일 | 실패하면 |
|---|---|---|
| 기준선 확인 | 주입 전 H0 `verify_symptom.sh` (로그인 → 채팅) | 무효 (주입하지 않음) |
| 주입 | `scenarios/<ID>/inject.sh` | 무효 |
| 증상 확인 | `verify_symptom.sh`가 성공할 때까지 반복 (기본 180초) | 무효 (증상이 안 나면 그 실행은 믿을 수 없다) |
| 점검·게이트 | 같은 DB로 `run_check` 반복 (아래 "반복 점검") | 어느 점검에서든 수집 실패가 있으면 무효 (확인하지 못함이 경로 C로 가서 정상처럼 보이므로) |
| 채점 | `scoring.py` | — |
| 원복 | `reset.sh` 후 H0 `verify_symptom.sh`(로그인 → 채팅)로 복구 확인 | 무효 |

증상 감지(API 시나리오)는 클러스터 DNS(`<service>.<namespace>.svc`)로 호출한다. `--in-cluster` 없이 Mac에서 돌리면 증상 감지가 클러스터 DNS에 닿지 않아 무효이거나 경로가 어긋난다. 클러스터 안에서 실행한다.

종료 코드(`run.py` 기준): 0 통과, 1 실패, 2 무효. 예외(설정·kubeconfig·저장소 오류)도 무효다. `make eval`로 실행하면 make가 실패를 모두 2로 바꾸므로, 결과는 `result.json`의 `exit_code`·`invalid`로 본다.

### 반복 점검

지속 시간이 있는 규칙(P03 1시간, P06·W01·W02·S02 15분)은 앞 점검의 관찰 기록이 있어야 적발된다. 그래서 운영 CronJob과 같게 **같은 SQLite DB**로 점검을 반복한다 (`check_repeatedly`).

- 첫 점검은 증상 확인 직후. 이후 첫 점검 시작 시각 기준 `--check-interval`(기본 300초)마다 점검한다
- **한 점검에서** 기대 신호(`must_signals`)를 모두 적발하면 멈춘다. 채점은 마지막 점검으로 하기 때문이다
- 다음 점검 시작이 `--check-max`(기본 2700초 = 45분, 주입 시각 기준)를 넘으면 멈추고 마지막 점검으로 채점한다
  - 기본값을 W02 적발 시점(약 26분)보다 넉넉히 잡은 이유: 첫 점검 시각(증상 확인이 끝나는 시점)에 따라 5분 일정이 밀리고, W02는 Deployment ProgressDeadline(롤아웃 정지 판정)이 지난 뒤부터 15분을 세므로 그 시각과 점검 시각이 엇갈리면 한 주기(5분)가 더 걸린다. 경계에서 몇 초 차이로 지속 시간을 못 채우는 지터도 있다. 35분이면 한 주기만 밀려도 상한에 걸린다
  - P03(1시간)을 기대 신호로 넣는 시나리오는 `--check-max`를 4000초 이상으로 잡는다 (1시간 + 점검 주기·첫 점검 지연)
- 기대 신호가 없는 시나리오(H0, B·C 시나리오)는 한 번만 점검한다. 즉시 판정 시나리오(A1·A5)도 첫 점검에서 끝난다
- 게이트 경로는 마지막 점검의 경로로 비교한다
- 반복 중 **수집 실패가 한 번이라도 나면** 그 자리에서 멈추고 실행 전체가 무효다 (35분 넘게 돈 실행도 마찬가지)
- 점검마다 `[check N] <경과>s 경로 <X> 적발 [...] 관찰 중 [...]`을 출력한다

예: `uv run python eval/run.py A3 --config kubeguardian.yaml --check-interval 300 --check-max 2700`

### AI 조건 (`--condition ai`)

반복 점검이 끝난 뒤 게이트가 A·B면 `run_investigation`으로 조사하고, 조사가 낸 보고서를 네 층으로 채점해 실패를 분류한다. judge는 `judge.py`(gpt-4.1, 조사 모델과 다름)다.

**통과 = 규칙 채점 통과 + 실패 분류가 `pass`.** 사람이 `[ai]` 줄이나 `report-<i>.json`으로 결론·기대를 나란히 확인한다.

| 층 | 질문 | 방법 |
|---|---|---|
| 결과 | 1순위 원인이 정답인가 | `cause_resource`를 `expected_cause`와 대조(`cause_match`, 코드) + 결론 judge(`judge_match`). 둘 다 맞아야 `outcome_ok` |
| 과정 | 결정적 근거를 손에 넣었는가 | `key_evidence`를 에이전트가 고른 도구 단계의 근거와 대조(`key_found`·`key_missing`·`key_all_found`, 코드). preliminary·점검에서 받은 근거는 제외 |
| 근거 | 인용한 근거가 그 문장을 뒷받침하는가 | 결론·1순위 문제·supported/refuted 가설을 주장별 LLM 판정 (`support_rate`, `conclusion_support`) |
| 금지 | 원인이 아닌 것을 원인으로 올렸는가 | `not_a_cause.resource`를 `cause_resource`와 대조(`forbidden_cause`, 코드) |

실행마다 위에서부터 처음 맞는 분류 하나를 `ai.failure`에 남긴다 (통과한 실행도 F5 검사를 받는다).

| 분류 | 조건 | 고칠 곳 |
|---|---|---|
| F0 무효 | 기존 무효 조건, judge 채점 실패, 점검 DB를 못 가져왔거나 근거 id가 DB에 없음 | 환경·러너 |
| F1 도구 한계 | `outcome_ok`가 false이고, 시나리오의 `key_evidence`가 모두 `obtainable: false` | 도구 추가 또는 범위 밖으로 명시 |
| F2 탐색 실패 | `outcome_ok`가 false이고, obtainable인 key evidence 중 손에 넣지 못한 것이 있음 | 계획·조사 지침·예산 |
| F3 추론 실패 | `outcome_ok`가 false이고 key evidence는 다 있음. `cause_match`가 false이거나 `forbidden_cause`가 true | 가설 판정·보고서 프롬프트 |
| F4 보고 실패 | `cause_match`는 true인데 `judge_match`가 false | 보고서 프롬프트 (결론 문장) |
| F5 근거 불일치 | `outcome_ok`가 true인데 `conclusion_support`가 `unsupported` | 근거 인용 규칙 |
| 통과 | `outcome_ok`가 true이고 F5가 아님 | — |

- 결과값은 `pass`, `F0`~`F5`, `error`(조사가 예외로 실패했거나 보고서가 없음. 에이전트 코드 결함이라 튜닝 대상이 아님)다. 리포트는 여기에 `skip`(기대 게이트 C에서 조사를 정상적으로 건너뜀, 통과로 센다), `gate_fail`(기대 C인데 조사가 돌았음), `not_run`(조사가 안 돌았음)을 더해 센다
- 조사가 상한에 닿아 끝나면(`incomplete`) 분류와 별도로 `limited: true`를 남긴다. F2가 상한 때문인지 집계에서 보기 위해서다
- 옛 통과 기준(judge match + `conclusion_supported`)은 `ai.legacy_passed`로 함께 남긴다. W2~W3 결과와 비교하기 위한 값이며 통과 판정에는 쓰지 않는다
- judge가 판정하지 못하면(match `null`) 그 회차는 F0 무효(2)다
- 기대 게이트가 C이면 조사를 건너뛰었을 때 통과, 조사가 돌았으면 채점 없이 실패다
- 판정 우선순위는 무효 > 실패 > 통과다. 조사 회차 중 하나라도 무효면 실행 전체가 무효, 모두 통과해야 통과다
- 채점은 둘로 나뉜다. 조사 직후에는 결과·금지 층만 채점하고, 원복 뒤 점검 DB를 가져온 다음(로컬 실행은 바로) 과정·근거 층과 실패 분류, 종료 코드를 정한다. DB를 가져오지 못하면 F0 무효다
- `--ai-repeats N`(기본 1): 반복 점검이 끝난 뒤 **같은 장애 상태에서 조사를 N회** 연달아 돌린다. 각 조사는 새 `RunContext`(근거·예산·사용량 분리)로 돈다. 주입·적발 대기를 반복하지 않고 에이전트의 편차만 재기 위해서다. 장애가 원복 전까지 유지되는 시나리오가 대상이다
- `--batch <이름>`: 실행을 묶음으로 표시한다 (`result.json`의 `batch`, 아래 "묶음 실행과 리포트")
- `result.json`의 `ai_runs`: 조사 회차별 `ai` 블록의 배열. 블록 키는 `status`, `conclusion`, `expected_top1`, `judge`(match·reason), `judge_match`, `usage`(토큰·도구 호출·초), `trace_id`(Phoenix에서 찾는 키, 추적이 꺼져 있으면 null), `passed`, `legacy_passed`, `failure`, `limited`, `conclusion_supported`, `support_rate`, `conclusion_support`, `claims`, `demoted`, `eval`(`cause_match`·`forbidden_cause`·`key_found`·`key_missing`·`key_all_found` 등), `must_check`·`cause_resource`(기록), `fail_reason`, `invalid`
- 회차별 산출물은 history에 `report-<i>.json`(보고서 전체)과 `steps-<i>.json`(조사 단계 기록)으로 저장한다. `i`는 0부터 시작한다. Phoenix 추적은 `.env`의 `OTEL_EXPORTER_OTLP_ENDPOINT`로 켜지고 port-forward가 필요하다
- 정답 파일의 `${injected_target}`(B1처럼 실행 때 정해지는 원인)은 `inject.sh`가 `scenarios/<ID>/.state/target`에 적은 Pod 이름으로 바뀌고, 이 항목은 소유 관계 규칙 없이 이름이 정확히 같아야 맞다. 대상을 읽지 못하면 F0 무효다
- W2~W3 결과(`ai` 단일 블록)는 다시 채점하지 않는다. `report.py`는 `ai_runs`가 있는 결과만 읽는다
- 러너가 로컬에서 돌면 클러스터 DNS를 찾지 못해 HTTP 도구(probe·run_api_scenario·query_app_api)가 실패한다. k8s 조회 도구는 동작한다

### 클러스터 안 실행 (`--in-cluster`)

로컬(Mac)에서 점검하면 클러스터 주소(`<svc>.dev.svc`)를 못 찾아 증상 감지(API 시나리오)와 AI의 HTTP 도구가 실패한다. `--in-cluster`는 **점검(반복 점검 포함)과 AI 조사만** kubeguardian 네임스페이스의 runner Pod에서 실행한다. 주입·증상 확인·원복·judge 채점은 Mac에서 그대로 한다 (쓰기는 Mac의 사용자 kubeconfig로만, runner는 조회 전용).

```bash
make kg-up                                          # 이미지 빌드 + runner Pod (설정·코드를 바꿀 때마다 다시)
make eval S=A1 ARGS="--in-cluster"                  # 규칙 조건
make eval S=A1 ARGS="--in-cluster --condition ai"   # AI 조건
```

- 러너가 `kubectl exec <pod> -- python -m runner_step check|investigate ... --json`을 부르고, Pod는 결과를 stdout 한 줄 JSON으로 낸다 (`runner_step.py`). 점검 DB는 Pod의 `/data/<실행 이름>/kg.db`에 쌓이고, 실행이 끝나면 `kubectl cp`로 `eval/history/<실행 이름>/kg.db`에 가져온다. `result.json`·`report-<i>.json`·`steps-<i>.json` 형식은 로컬 실행과 같고, `result.json`에 `in_cluster.pod`가 더해진다
- 점검 설정은 ConfigMap `kubeguardian-config`(`make kg-up` 때의 `kubeguardian.yaml`)를 쓴다. `--config`는 Mac 쪽 읽기에만 쓰이므로 설정을 바꾸면 `make kg-up`을 다시 한다
- 무효(2): runner Pod가 없거나 실행 중 **Pod가 바뀜**(emptyDir DB가 사라져 지속 시간 규칙을 이어 볼 수 없음), exec 비정상 종료, 출력이 JSON이 아님. 사유는 마스킹해 `result.json`의 `invalid`에 남는다. 점검 DB를 가져오지 못하면 `db_fetch_failed`에 남는다
- `ai.trace_id`는 Pod 안 조사의 추적 id다. 추적은 클러스터 안 주소로 Phoenix에 보내므로 port-forward 없이 남는다
- 조사 도구(`probe`·`run_api_scenario`·`query_app_api`)가 클러스터 주소로 동작한다 (위 AI 조건의 "로컬이면 실패" 제약이 없다)

### 결과 (`result.json`)

| 키 | 내용 |
|---|---|
| `check_interval_s` · `check_max_s` | 이 실행의 점검 간격·상한 |
| `checks` | 점검이 끝날 때마다 바로 쌓는다 (반복 중 예외가 나도 남는다). 점검별 `n`, `elapsed_s`(주입 시각 기준 초, 점검이 끝난 시각), `gate_path`, `detected`(장애 신호), `observing`, `collect_failed` |
| `first_detected_s` | 기대 신호별 첫 적발 경과 초. 적발하지 못했으면 `null` |
| `score` | 마지막 점검의 채점 결과 |
| `check_run_id` | 마지막 점검의 run id (점검 DB `kg.db`) |
| `batch` | `--batch`로 준 묶음 이름. 없으면 키가 없다 |
| `ai_runs` | AI 조건의 조사 회차별 `ai` 블록 배열 (`--ai-repeats` 횟수만큼) |

전제는 진단 대상이 정상 기준선(H0)인 상태다 (환경 설치 가이드 10단계). 주입이 실패해도 원복을 실행하므로, `.state/`에 남은 이전 주입 기록이 있으면 그 기록으로 되돌린다.

## 채점 (`scoring.py`, 기준 조건)

- 통과 = `expected.yaml`의 `must_signals`를 모두 적발 + 게이트 경로가 `gate_path`와 같음 + `forbidden_signals`를 하나도 적발하지 않음
- 정답 파일의 신호 구분 (05-rules §0.1.1): `must_signals` 반드시, `allowed_signals` 나와도 됨(오탐 아님), `forbidden_signals` 나오면 오탐(`"*"`는 반드시·허용 신호를 뺀 장애 신호 전부, 개선 권고 H는 제외)
- AI 조건의 새 항목(통과 조건, 위 "AI 조건"): `expected_cause`(1순위 원인으로 받아들이는 리소스 목록), `key_evidence`(원인 쪽 결정적 근거 1~3개. `kind` 일치 + `subject` glob + `contains` 부분 문자열(대소문자 무시)이 맞으면 손에 넣은 것), `not_a_cause[].resource`(리소스가 있는 항목만 `cause_resource`와 대조해 `forbidden_cause`를 남김). 작성 규칙은 `scenarios/_template/expected.yaml` 주석을 따른다
  - 원인 리소스 대조는 kind 대소문자를 무시하고 소유 관계를 허용한다 (`Deployment/agent`는 `Pod/agent-<해시>-<해시>`, `ReplicaSet/agent-<해시>`도 맞음)
- `must_check`(`run`·`config`·`wiring`·`logs`별 확인할 사실)와 서술형 `not_a_cause`(문자열 항목)는 채점하지 않고 `result.json`의 `expected` 블록에 기록만 한다. AI 조건에서는 `ai.must_check`에 영역마다 보고서 `coverage`의 상태와 근거 유무(`status`·`has_evidence`)를 함께 기록하고, 1순위 원인 리소스는 `ai.cause_resource`에 남긴다 (통과 조건이 아니다)
- 개선 권고(H 규칙)는 장애 신호로 세지 않고 `advice`로 따로 남긴다
- 금지 적발(`forbidden_hit`)은 실패 사유다. 허용·금지 밖의 적발(`unexpected`, 오탐 후보), 관찰 중(`observing`, 지속 시간 전), 확인하지 못함(`unknown`)은 기록한다
- 마지막 점검으로 채점한다. 상한까지 지속 시간을 채우지 못한 규칙은 "관찰 중"으로 남는다

## 묶음 실행과 리포트

시나리오 여러 개 × 조사 N회를 한 묶음으로 돌려 집계한다. history 디렉터리 구조는 그대로이고 각 `result.json`에 `batch`만 더해진다.

```bash
make eval-batch B=w4-base S="A1 A3 A5 B1 C1 C2" ARGS="--in-cluster --condition ai --ai-repeats 3"
make eval-report B=w4-base                 # 이미 쌓인 결과로 리포트만 다시
make eval-report B=w4-new BASE=w4-base     # baseline 대비 회귀 표 포함
```

- `eval-batch`는 시나리오를 차례로 실행하고(한 시나리오가 실패·무효여도 다음으로 간다) 끝에 `eval-report`를 부른다. `eval-report`는 `uv run python eval/report.py <묶음> [--baseline <묶음>]`이며 `eval/report.md`를 만든다
- 리포트 내용: 시나리오별 표(통과 k/N, 실패 분류 분포, `cause_match`·`judge_match`·`key_all_found` 비율, `support_rate` 평균, 시간·토큰 중앙값), 레벨별(L1~L3)·경로별(A·B) 통과율과 Wilson 95% 신뢰구간, 일관성(N회가 모두 같은 분류인 시나리오 비율), KPI(원인 정확도, 원인 규명 커버리지, 근거 일치율, 조사 시간·토큰 중앙값)
- `--baseline`: 시나리오별 통과 수 변화와 실패 분류 분포 변화를 회귀 표로 낸다. 통과 수가 2 이상 줄면 회귀로 표시한다
- 상한에 닿아 끝난(`limited`) 실패 회차 수를 시나리오별 표에 따로 센다

### judge 보정과 라벨

LLM이 판정하는 것은 결론 judge와 근거 judge 둘이다. 사람 판정과 일치율을 재어 90% 이상이면 LLM 판정을 지표로 쓰고, 미만이면 그 judge의 지표는 사람 판정이 있는 표본에서만 보고한 뒤 judge 프롬프트를 고쳐 같은 표본으로 다시 잰다.

- 사람 판정은 `eval/labels/<history 디렉터리 이름>.yaml`에 커밋한다 (history는 커밋하지 않으므로). 형식은 `eval/labels/README.md`
- `index`는 `ai_runs`의 0부터 시작하는 순번이다 (`report-<i>.json`의 `<i>`)
- `report.py`는 라벨이 있는 회차에서 사람 판정을 우선해 다시 분류하고, judge와의 일치율을 표에 함께 낸다

### 튜닝 채택 기준 (W4)

1. **정답 파일 보강** (튜닝 전, 결과를 보지 않고): A1·A3·A5·B1·C1·C2의 `expected_cause`·`key_evidence`·`not_a_cause.resource`를 `root_cause`에서 도출해 쓴다
2. **기준 묶음**: 6개 시나리오 × 조사 3회 = 18건. 실패 분류 분포를 본다
3. **고칠 것 고르기**: 분류별 "고칠 곳"(위 표) 중 실패 건수가 가장 많은 것부터 고친다. 한 번에 한 종류만 바꾼다
4. **확인**: 바뀐 것이 겨냥한 시나리오를 3회 실행한다. 좋아졌으면 6개 전체 묶음을 `--baseline`으로 다시 돌려 회귀를 확인한다
5. **채택 기준**: 겨냥한 시나리오의 통과 수가 늘었고, 다른 어떤 시나리오도 통과 수가 2 이상 줄지 않았고, L1(A1·A3·A5) 통과 합계가 줄지 않았을 때 채택한다. 3회 표본에서 1회 차이는 편차와 구분할 수 없으므로 회귀로 보지 않는다

C5(W4에 만듦)는 보류 세트다. 튜닝에 쓰지 않고, 튜닝을 마친 뒤 한 번만 3회 실행해 튜닝 세트 통과율과 비교한다.
