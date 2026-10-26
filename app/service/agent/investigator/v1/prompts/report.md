[보고서 작성]
조사한 문제: $issue

사전 확인 사실:
$facts

지금까지의 조사 대화(도구 결과 포함)를 바탕으로 보고서를 씁니다.
- conclusion: 원인 1순위를 한 문장으로. 원인 리소스(kind/name)와 그 안에서 잘못된 설정 항목 또는 동작(예: 이미지 참조, env 값, selector, quota 응답, 연결 차단(출발 서비스 → 도착 서비스))을 구체적으로 적고 사용자에게 어떤 영향이 있는지 씁니다. "롤아웃이 멈춤" 같은 상태만 적지 않습니다. 관찰하지 않은 설정 항목을 지어내지 않습니다.
- 모든 사실 문장과 결론은 실제 도구 결과의 근거 번호(ev-N)를 evidence_ids로 인용합니다. 없는 번호를 만들지 않습니다.
- 근거가 없는 판단은 hypotheses에 status "inconclusive"로 두고 reason에 "추정"이라고 씁니다.
- actions: 운영자가 실행할 조치 (명령어 수준, 자동 실행하지 않음). verification: 조치 후 확인 방법.
- issues[].cause_resource: 원인으로 지목한 리소스 {"kind", "name"}. 1순위는 반드시 적고, 모르면 null.
- hypotheses: 배제(refuted)한 가설은 reason에 배제한 이유와 그 근거를 씁니다.
- coverage: 구동(run)·설정(config)·연결(wiring)·로그(logs) 네 영역 모두에 status(checked|not_checked|not_applicable), summary, evidence_ids를 씁니다. checked는 본 것과 근거 번호를 씁니다. 확인할 필요가 없거나 확인할 수 없는 영역(예: 새 Pod가 한 번도 뜨지 않아 볼 로그가 없음)은 not_applicable으로 두고 그 사유를 summary에 씁니다. not_checked는 확인했어야 하는데 하지 못한 영역이며 그 이유를 summary에 씁니다.
- 원인 리소스의 설정은 get_resource로 직접 본 근거가 있어야 결론으로 올립니다. 없으면 hypotheses에 inconclusive로 둡니다.

아래 형식의 JSON 하나로만 답합니다.
{"conclusion": "...", "confidence": "high|medium|low", "impact_scope": "영향 범위",
 "issues": [{"rank": 1, "title": "...", "impact": "core_path_broken", "confidence": "high", "evidence_ids": ["ev-1"], "cause_resource": {"kind": "Deployment", "name": "..."}}],
 "preliminary_facts": [{"statement": "...", "evidence_ids": ["ev-1"]}],
 "evidence_ids": ["결론을 뒷받침하는 근거 번호"],
 "hypotheses": [{"statement": "...", "status": "supported|refuted|inconclusive", "reason": "...", "evidence_ids": ["ev-2"]}],
 "coverage": {"run": {"status": "checked", "summary": "...", "evidence_ids": ["ev-1"]}, "config": {"status": "checked", "summary": "...", "evidence_ids": ["ev-2"]}, "wiring": {"status": "not_checked", "summary": "확인하지 못한 이유", "evidence_ids": []}, "logs": {"status": "not_applicable", "summary": "해당 없는 이유(예: 새 Pod가 뜨지 않아 로그 없음)", "evidence_ids": []}},
 "actions": ["..."], "verification": "..."}
