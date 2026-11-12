[보고서 작성]
조사한 문제: $issue

사전 확인 사실:
$facts

지금까지의 조사 대화(도구 결과 포함)를 바탕으로 보고서를 씁니다.
- conclusion: 원인 1순위를 한 문장으로. 원인 리소스(kind/name)와 그 안에서 잘못된 설정 항목 또는 동작(예: 이미지 참조, env 값, selector, quota 응답, 연결 차단(출발 서비스 → 도착 서비스))을 구체적으로 적고 사용자에게 어떤 영향이 있는지 씁니다. "롤아웃이 멈춤" 같은 상태만 적지 않습니다. 관찰하지 않은 설정 항목을 지어내지 않습니다.
- 모든 사실 문장과 결론은 실제 도구 결과의 근거 번호(ev-N)를 evidence_ids로 인용합니다. 없는 번호를 만들지 않습니다.
- 근거가 없는 판단은 hypotheses에 status "inconclusive"로 두고 reason에 "추정"이라고 씁니다.
- 요청 시간 초과와 정상 Pod 상태만으로 원인 리소스를 확정하지 않습니다. 하위 의존성의 응답 대기가 의심되면 가설을 판단 보류로 남기고, 확인하지 못한 호출 구간과 운영자가 확인할 연결·응답 시간 항목을 적습니다.
- actions: 운영자가 실행할 조치 (명령어 수준, 자동 실행하지 않음). verification: 조치 후 확인 방법.
- issues[].cause_resource: 원인으로 지목한 리소스 {"kind", "name"}. 1순위는 반드시 적고, 모르면 null.
- cause_chain: 증상이 보인 호출자에서 오류 출처 쪽으로 거슬러 올라간 순서로 씁니다. 문제를 보고했다면 빈 목록으로 생략하지 않습니다. 각 지점은 service({"kind", "name"}, 실제 관찰한 Pod/Deployment/Service 등 리소스), observation(관찰 내용), evidence_ids(그 관찰의 실제 근거), error_from_downstream(하위 호출에서 받은 오류를 전달했는지)입니다. 마지막 service는 1순위 cause_resource와 같아야 합니다. 호출자 로그에 하위 호출의 오류가 있으면 호출자 내부 오류 생성·변환으로 단정하지 않습니다. 서버의 주변 성공 로그만으로도 변환을 증명할 수 없습니다. 오류 출처가 불명확하면 확인한 전달 지점을 남기고 error_from_downstream=true와 미해결 가설로 표시합니다. quota 오류 본문만으로 실제 quota 소진을 확정하지 않습니다.
- hypotheses: 배제(refuted)한 가설은 reason에 배제한 이유와 그 근거를 씁니다.
- coverage: 구동(run)·설정(config)·연결(wiring)·로그(logs) 네 영역 모두에 status(checked|not_checked|not_applicable), summary, evidence_ids를 씁니다. checked는 본 것과 근거 번호를 씁니다. 확인할 필요가 없거나 확인할 수 없는 영역(예: 새 Pod가 한 번도 뜨지 않아 볼 로그가 없음)은 not_applicable으로 두고 그 사유를 summary에 씁니다. not_checked는 확인했어야 하는데 하지 못한 영역이며 그 이유를 summary에 씁니다.
- 설정 비교의 변경 구간은 두 스냅샷 관측 사이입니다. 수정 시각·주체·장애 원인으로 단정하지 않습니다. Pod 생성은 컨테이너가 읽은 env 값이나 앱 재로딩을 입증하지 않습니다. 기능 점검 시각, 로그 이벤트 시각, 로그 수집 시각을 구분합니다.
- 원인 리소스의 설정은 get_resource로 직접 본 근거가 있어야 결론으로 올립니다. 없으면 hypotheses에 inconclusive로 둡니다.

아래 형식의 JSON 하나로만 답합니다.
{"conclusion": "...", "confidence": "high|medium|low", "impact_scope": "영향 범위",
 "issues": [{"rank": 1, "title": "...", "impact": "core_path_broken", "confidence": "high", "evidence_ids": ["ev-1"], "cause_resource": {"kind": "Deployment", "name": "..."}}],
 "preliminary_facts": [{"statement": "...", "evidence_ids": ["ev-1"]}],
 "evidence_ids": ["결론을 뒷받침하는 근거 번호"],
 "hypotheses": [{"statement": "...", "status": "supported|refuted|inconclusive", "reason": "...", "evidence_ids": ["ev-2"]}],
 "cause_chain": [{"service": {"kind": "Deployment", "name": "..."}, "observation": "실제 관찰 내용", "evidence_ids": ["ev-1"], "error_from_downstream": false}],
 "coverage": {"run": {"status": "checked", "summary": "...", "evidence_ids": ["ev-1"]}, "config": {"status": "checked", "summary": "...", "evidence_ids": ["ev-2"]}, "wiring": {"status": "not_checked", "summary": "확인하지 못한 이유", "evidence_ids": []}, "logs": {"status": "not_applicable", "summary": "해당 없는 이유(예: 새 Pod가 뜨지 않아 로그 없음)", "evidence_ids": []}},
 "actions": ["..."], "verification": "..."}
