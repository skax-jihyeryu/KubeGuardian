# #183 이력 채팅 전송 피드백 검증

## 원인과 변경

POST chat/messages는 history_qa의 조회·AI 답변 생성·검증 후 질문과 답변 HTML을 함께 반환했다. 브라우저가 응답 전 질문을 대화에 넣지 않아 버튼을 누른 결과가 늦게 보였다. 기본 btn에는 채팅 전용 hover/active/sending 구분이 없었다.

프런트의 폼 단위 pending 상태로 제출 직후 질문을 textContent로 표시하고 답변 생성 중 상태를 같은 대화 영역에 추가한다. 입력을 비우고 읽기 전용으로 두며 중복 제출을 막는다. 서버 응답을 받으면 임시 질문/대기를 지워 기존 서버의 마스킹·검증된 질문/답변을 한 번 표시한다. HTTP 실패는 오류 안내와 질문 복구, AI 실패는 기존 서버 오류 조각과 질문 복구로 처리한다. 닫기/맥락 변경은 브라우저 XHR을 취소하고 이전 폼의 DOM 참조만 정리한다. 이미 서버에서 시작한 AI 계산까지 취소한다고 보장하지 않는다.

토큰 스트리밍은 추가하지 않았다. 원문 입력의 즉시 미리보기는 사용자 자신의 입력을 HTML 실행 없이 표시하는 것이며, 저장·AI 전달·최종 서버 출력은 기존 마스킹/근거 검증을 유지한다. 버튼 색상/키보드 초점/눌림/전송 중 상태와 reduced-motion을 적용하고 정적 캐시 버전을 변경했다.

## 검증

운영 DB와 분리한 복사 DB·localhost8082 검증 서버를 사용했다. 지연/오류 경로는 4초 지연 fixture로 제어했고 실제 이력 답변 1회는 기존 Azure history_qa(gpt-6.1-sol)를 호출했다. 운영 DB·Slack·클러스터 배포 설정은 UI 검증에 사용하지 않았다.

- 수정 전 응답 대기: 버튼 disabled=true인데 대화 질문 목록은 []로 재현.
- 수정 후 대기: 질문 즉시 1개, 입력 비움/readonly, aria-busy=true, 답변 생성 중 버튼/role=status 표시.
- 성공: 임시 상태 제거, 질문 1개/답변 1개, 버튼·입력 복귀.
- HTTP503: 대화 내 오류, 질문 복구, 대기 해제/재전송 가능.
- AI ServiceException: 기존 오류 조각 표시, 질문 복구, 대기 해제.
- HTML 문자열: img/onerror 입력은 텍스트로 표시되고 DOM img는 0개.
- 더블 클릭: pending 1개/질문 1개, 완료 후에도 질문 1개.
- 닫기/재열기 및 맥락 변경: 옛 pending/질문이 늦게 새 패널에 추가되지 않음.
- 버튼 색상: 기본 rgb(63,106,96), hover rgb(20,24,31), disabled rgb(228,231,238). Tab 초점은 focus-visible/solid outline. active 스타일은 scoped CSS와 리뷰로 확인.
- 실제 AI: 저장된 Service/agent selector 불일치 원인·조치·점검/이슈/근거 링크를 응답하고 질문은 한 번 표시.

관련 채팅 테스트 43개, 전체 pytest 1,753개, Ruff check/format 및 node --check 통과. GPT-6.1 Sol Medium 독립 리뷰에서 actionable P1/P2 없음. 프롬프트·에이전트·DB 계약은 변경하지 않았고 스트리밍/백엔드 취소를 구현했다고 주장하지 않는다.
