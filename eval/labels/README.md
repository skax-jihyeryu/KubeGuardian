# 사람 판정 라벨

회차별로 사람이 LLM judge 판정을 다시 매긴 파일을 둔다. 파일 이름은 `eval/history/` 아래 디렉터리 이름에 `.yaml`을 붙인 것이다(예: `20261029-1010-A1.yaml`). 형식은 `runs: [{index, top1_match, claims: [{source, verdict}], note}]`이며, `index`는 0부터 시작하는 회차 번호(`result.json`의 `ai_runs` 위치이자 `report-<i>.json`의 `<i>`), `top1_match`는 1순위 원인이 정답과 같은지, `claims`는 `source`(conclusion·issue·hypothesis 등)별 `supported`·`partial`·`unsupported` 판정이다. `eval/report.py`가 라벨이 있는 회차의 judge 값을 이 값으로 덮어 다시 분류하고, 원래 judge와의 일치율을 계산한다.
