[선별]
점검 결과 게이트 경로 $gate_path로 조사가 시작되었습니다 (A: 규칙 적발, B: 규칙 통과 + 증상(API 시나리오 실패 또는 사용자 입력)).

신호판 (서비스별 규칙 판정):
$board

사전 확인 사실 (가설 없이 도구로 확인함):
$facts

실패한 API 시나리오 (점검 때 근거 번호):
$symptoms

사용자 증상: $symptom

할 일:
1. 사용자 영향 순으로 문제를 최대 3건 고릅니다. 영향 등급: core_path_broken(핵심 기능 전체 실패) > partial_users(일부 사용자) > latent_risk(잠재 위험) > improvement(개선 권고).
2. 같은 원인에서 나온 신호는 한 문제로 묶습니다. 개선 권고만 있는 문제는 고르지 않습니다.
3. 1순위 문제에 맞는 조사 지침 이름 하나를 고릅니다 (지침 목록의 [지침: 이름]).

아래 JSON 하나로만 답합니다.
{"issues": [{"rank": 1, "title": "문제 한 줄", "impact": "core_path_broken", "confidence": "high", "evidence_ids": ["ev-1"]}], "runbook": "지침 이름"}
