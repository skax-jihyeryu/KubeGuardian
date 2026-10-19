"""진단용 Azure OpenAI 연결 확인 (이슈 #8 스파이크).

build_chat_model로 배포 하나를 호출해 응답과 걸린 시간을 출력한다.
로컬: uv run --env-file .env python scripts/llm-spike/llm_smoke.py [모델]
Pod:  scripts/llm-spike/run.sh [모델]
모델(Azure 배포 이름)은 인자로 받는다. 기본값 gpt-4.1.
모델별 제약(temperature, Responses API)은 core/llm.py의 MODEL_OPTIONS가 적용한다.
"""

import os
import sys
import time
from pathlib import Path

# 로컬에서는 레포의 app/을, Pod에서는 같은 폴더에 복사된 core/를 import한다
here = Path(__file__).resolve()
for root in (here.parent.parent.parent / "app", here.parent):
    if (root / "core").is_dir():
        sys.path.insert(0, str(root))
        break

from core.llm import build_chat_model  # noqa: E402

deployment = sys.argv[1] if len(sys.argv) > 1 else "gpt-4.1"
endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
print(f"endpoint={endpoint} deployment={deployment}")
print(f"SSL_CERT_FILE={os.environ.get('SSL_CERT_FILE') or '(기본 인증서 저장소)'}")

started = time.monotonic()
try:
    llm = build_chat_model(deployment)
    reply = llm.invoke("한 단어로만 답하세요: ping")
except Exception as e:  # 스파이크: 실패 원인(인증서·네트워크·인증)을 그대로 보여준다
    print(f"FAIL {type(e).__name__}: {e}")
    cause = e.__cause__ or e.__context__
    if cause:
        print(f"  cause {type(cause).__name__}: {cause}")
    sys.exit(1)

print(f"OK {time.monotonic() - started:.1f}s reply={reply.text!r}")
