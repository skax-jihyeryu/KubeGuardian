[보고서 작성]
조사한 문제: $issue

사전 확인 사실:
$facts

지금까지의 조사 대화(도구 결과 포함)를 바탕으로 보고서를 씁니다.
- conclusion: 원인 1순위를 한 문장으로. 무엇이 어떻게 잘못되어 사용자에게 어떤 영향이 있는지.
- 모든 사실 문장과 결론은 실제 도구 결과의 근거 번호(ev-N)를 evidence_ids로 인용합니다. 없는 번호를 만들지 않습니다.
- 근거가 없는 판단은 hypotheses에 status "inconclusive"로 두고 reason에 "추정"이라고 씁니다.
- actions: 운영자가 실행할 조치 (명령어 수준, 자동 실행하지 않음). verification: 조치 후 확인 방법.
- checked_scope: 확인한 것(checked)과 권한·범위 밖이라 확인하지 못한 것(not_checked).

아래 형식의 JSON 하나로만 답합니다.
{"conclusion": "...", "confidence": "high|medium|low", "impact_scope": "영향 범위",
 "issues": [{"rank": 1, "title": "...", "impact": "core_path_broken", "confidence": "high", "evidence_ids": ["ev-1"]}],
 "preliminary_facts": [{"statement": "...", "evidence_ids": ["ev-1"]}],
 "evidence_ids": ["결론을 뒷받침하는 근거 번호"],
 "hypotheses": [{"statement": "...", "status": "supported|refuted|inconclusive", "reason": "...", "evidence_ids": ["ev-2"]}],
 "checked_scope": {"checked": ["..."], "not_checked": ["..."]},
 "actions": ["..."], "verification": "..."}
