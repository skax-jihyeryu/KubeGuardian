# #173 직접 점검의 이슈·조사·알림 연결 검증

## 구현

화면 지금 점검과 CLI kg check는 기존 schedule과 같은 opened/continued/changed/resolved 추적을 사용한다. 첫 알림을 시도한 뒤 기존 조사 큐·worker로 처리하며, 알림 실패도 예약을 막지 않는다. 명시적 kg investigate의 동기 실행은 유지한다. UI는 점검 완료→조사 전환에 머리·판정·이슈 링크를 갱신한다. 알림 전송과 큐 삽입 사이에는 기존 UI 점검 잠금으로 폴링을 유지한다. ADR-002와 기획서·UI·상세설계를 함께 수정했다.

## 테스트와 리뷰

- 전체 pytest 1,691개, Ruff check/format 통과.
- UI/CLI 신규 장애→worker 결과·이슈·알림, 실패 조사, 반복 UI/주기 점검 중복 제거, 지문 변경 시 요청 교체, 정상/확인 불가, 복구 M3 1회, 알림 초기화/전송 실패, 확인 버튼을 검증했다.
- GPT-6.1 Sol Medium 독립 리뷰 후 실제 화면에서 점검→조사 전환의 오래된 머리/판정 표시를 발견했다. 상태 변화에 따른 새로고침 회귀 테스트를 추가했다.
- 추가 Sol Medium 리뷰에서 알림 I/O 중 완료된 점검이 큐 삽입 전 폴링을 멈추는 P2를 재현했다. UI 점검 잠금의 run_id가 일치하면 준비 중 상태로 폴링을 유지하도록 수정했고, 발송기 생성 시점의 재현 테스트는 done/live=False에서 done/live=True로 바뀌었다. 재리뷰 잔여 P1/P2 없음.

## 실환경 검증 조건

기존 minikube dev의 A1 selector 장애를 사용했다. 임시 Deployment/ConfigMap kg-manual-173-smoke는 별도 emptyDir DB를 사용하며 운영 DB와 공유하지 않는다. 브라우저는 localhost:8081, 실제 K8s/HTTP·Azure gpt-6.1-sol 및 기존 worker를 사용했다. UI와 worker의 Sender만 DryRunSender로 고정해 실제 Slack 전송은 하지 않았다. 운영 worker/web/runner 이미지나 정기 점검 설정은 변경하지 않았다.

첫 이미지 r1(4b0f8d67...)에서 정상 직접 점검은 경로 C·이슈 0건이었다. A1 직접 점검은 경로 A·S01 → 이슈 1 → trigger=ui 조사 1건 → done/confirmed·Service/agent selector 불일치 진단 → M1/M1u/M9 dry-run으로 연결됐다. 조사 118.92초, 입력 153,220/출력 7,164 토큰, 도구 15회였다. 이후 A1을 원복했고 UI 전환 수정을 적용한 r2(7fc1f098...)로 재검증했다. r1 백업은 /tmp/kg-173-smoke-r1.db다.

r2에서 별도 수동 새로고침 없이 제목이 “조사 중”으로 바뀌고 점검 완료·S01 판정·열린 이슈·이슈 보고서 링크가 함께 나타났다. 두 실험은 같은 모델의 독립 성능 평가가 아니라 실제 직접 점검 연결과 UI 수정의 동작 검증이다. A3·C2 장시간 평가 재실행은 사용자 결정에 따라 생략했고, 프롬프트·조사 지침은 변경하지 않았다.

최종 r2 직접 점검 #1은 경로 A·S01 → 이슈 #1 → trigger=ui 조사 #1 → done/confirmed·Service/agent 원인 식별로 연결됐다. 조사 57.84초, 입력 78,924/출력 3,466 토큰, 도구 9회였다. M1/M1u/M9가 모두 dry-run으로 남았다. A1을 원복한 뒤 화면의 지금 점검으로 #2를 실행하자 경로 C·정상으로 같은 이슈 #1이 resolved가 됐고 M3 dry-run이 정확히 1회 추가됐다. 추가 조사는 없었다. 두 점검이 이슈 타임라인에 연결되고 기존 보고서·근거를 계속 확인할 수 있었다. r2 DB 백업은 /tmp/kg-173-smoke-r2.db다.

실제 화면에서 보고서의 selector 불일치 결론과 이슈 해결·원인 갱신·복구 알림 dry-run 타임라인을 확인하고 스크린샷을 저장했다. 실환경 확인 범위는 A1 대표 흐름이며 전체 장애 시나리오 회귀를 수행했다는 주장은 하지 않는다. 조사 실패·중복 제거 등 나머지 계약은 자동 회귀 테스트로 검증했다.

검증 후 A1 selector는 app=agent로 복구됐고 임시 Deployment/ConfigMap을 삭제했다. Chaos 잔존 없음·정기 점검 suspend=true를 확인했다. 정리 시 공유 클러스터의 운영 ConfigMap 버전은 1665158이고 diagnostic-agent의 3개 컨테이너 태그는 모두 kubeguardian:local이었다. 이전 작업 기록과 달라 전체 운영 환경이 동일하게 유지됐다고 주장하지 않는다. 이번 작업은 해당 운영 Deployment/ConfigMap을 갱신하거나 이전 상태로 되돌리지 않았다. 실측 비용은 성능 비교가 아닌 각 검증 실행의 기록이다.
