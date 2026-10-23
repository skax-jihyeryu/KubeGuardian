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

반복 점검이 끝난 뒤 게이트가 A·B면 `run_investigation`으로 조사하고, 보고서 결론을 `judge.py`(gpt-4.1, 조사 모델과 다름)가 `expected.yaml`의 `expected_top1`과 같은 원인인지 채점한다. 통과 = 규칙 채점 통과 + judge match + `conclusion_supported`가 true(결론이 인용한 근거가 있음). 근거 없는 결론이 우연히 맞은 것은 통과가 아니다. 사람이 `[ai]` 줄이나 `report.json`으로 결론·기대를 나란히 확인한다.

- judge가 판정하지 못하면(match `null`) 무효(2)다
- 기대 게이트가 C이면 조사를 건너뛰었을 때 통과, 조사가 돌았으면 채점 없이 실패다
- 판정 우선순위는 무효 > 실패 > 통과다
- `result.json`의 `ai` 블록: `status`, `conclusion`, `expected_top1`, `judge`(match·reason, 채점 실패는 match null), `usage`(토큰·도구 호출·초), `trace_id`(Phoenix에서 찾는 키, 추적이 꺼져 있으면 null), `passed`, `conclusion_supported`, `demoted`(근거 검증에서 강등된 항목), `fail_reason`(실패 사유), `invalid`(무효 사유)
- history에는 `report.json`(보고서 전체)과 `steps.json`(조사 단계 기록)을 저장한다. Phoenix 추적은 `.env`의 `OTEL_EXPORTER_OTLP_ENDPOINT`로 켜지고 port-forward가 필요하다
- 러너가 로컬에서 돌면 클러스터 DNS를 찾지 못해 HTTP 도구(probe·run_api_scenario·query_app_api)가 실패한다. k8s 조회 도구는 동작한다

### 클러스터 안 실행 (`--in-cluster`)

로컬(Mac)에서 점검하면 클러스터 주소(`<svc>.dev.svc`)를 못 찾아 증상 감지(API 시나리오)와 AI의 HTTP 도구가 실패한다. `--in-cluster`는 **점검(반복 점검 포함)과 AI 조사만** kubeguardian 네임스페이스의 runner Pod에서 실행한다. 주입·증상 확인·원복·judge 채점은 Mac에서 그대로 한다 (쓰기는 Mac의 사용자 kubeconfig로만, runner는 조회 전용).

```bash
make kg-up                                          # 이미지 빌드 + runner Pod (설정·코드를 바꿀 때마다 다시)
make eval S=A1 ARGS="--in-cluster"                  # 규칙 조건
make eval S=A1 ARGS="--in-cluster --condition ai"   # AI 조건
```

- 러너가 `kubectl exec <pod> -- python -m runner_step check|investigate ... --json`을 부르고, Pod는 결과를 stdout 한 줄 JSON으로 낸다 (`runner_step.py`). 점검 DB는 Pod의 `/data/<실행 이름>/kg.db`에 쌓이고, 실행이 끝나면 `kubectl cp`로 `eval/history/<실행 이름>/kg.db`에 가져온다. `result.json`·`report.json`·`steps.json` 형식은 로컬 실행과 같고, `result.json`에 `in_cluster.pod`가 더해진다
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

전제는 진단 대상이 정상 기준선(H0)인 상태다 (환경 설치 가이드 10단계). 주입이 실패해도 원복을 실행하므로, `.state/`에 남은 이전 주입 기록이 있으면 그 기록으로 되돌린다.

## 채점 (`scoring.py`, 기준 조건)

- 통과 = `expected.yaml`의 `must_signals`를 모두 적발 + 게이트 경로가 `gate_path`와 같음 + `forbidden_signals`를 하나도 적발하지 않음
- 정답 파일의 신호 구분 (05-rules §0.1.1): `must_signals` 반드시, `allowed_signals` 나와도 됨(오탐 아님), `forbidden_signals` 나오면 오탐(`"*"`는 반드시·허용 신호를 뺀 장애 신호 전부, 개선 권고 H는 제외)
- `must_check`(`run`·`config`·`wiring`·`logs`별 확인할 사실)와 `not_a_cause`(원인으로 지목하면 안 되는 것)는 채점하지 않고 `result.json`의 `expected` 블록에 기록만 한다 (보고서 대조·판정은 #21)
- 개선 권고(H 규칙)는 장애 신호로 세지 않고 `advice`로 따로 남긴다
- 금지 적발(`forbidden_hit`)은 실패 사유다. 허용·금지 밖의 적발(`unexpected`, 오탐 후보), 관찰 중(`observing`, 지속 시간 전), 확인하지 못함(`unknown`)은 기록한다
- 마지막 점검으로 채점한다. 상한까지 지속 시간을 채우지 못한 규칙은 "관찰 중"으로 남는다
