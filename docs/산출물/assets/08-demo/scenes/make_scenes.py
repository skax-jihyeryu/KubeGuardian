"""08 시연영상스크립트의 장면 합성 사진(L2·L3·L4)을 만든다.

실행: python make_scenes.py   (Pillow 필요, 저장소 어디서든 같은 결과)
- 브라우저 영역은 assets/08-demo의 기존 캡처를 칸 안에 비율 유지로 넣는다.
- L2는 브라우저 1280 + 오른쪽 위 ④ 서비스 로그 640x540 + 아래 ② 작업자 로그 640x540,
  L3는 ② 작업자 로그 1280 + 브라우저 640이다.
- L4(운영 화면)는 Slack 1280 + 오른쪽 위 ③ 명령 터미널 640x540 + 오른쪽 아래 ⑤ Pod 감시 640x540이다.
- 터미널 영역은 로그 줄 데이터로 그린다. 로그 줄은 08 문서의 실측 출력(11/16 새 로그 형식 실측,
  A7·C8 리허설 발췌를 새 형식 접두로 옮긴 줄)이거나 scenarios/*의 스크립트가 echo하는 문구다.
  "형식 예시"는 출처가 없어 형식만 보인 줄이다(문서에도 그렇게 적는다).
- 강조할 줄은 (텍스트, True)로 표시한다.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent
MONO = str(Path.home() / "Library/Fonts/D2Coding-Ver1.3.2-20180524.ttf")
UI = "/System/Library/Fonts/AppleSDGothicNeo.ttc"

BG = (30, 30, 30)
FG = (230, 230, 230)
DIM = (150, 150, 150)
YELLOW = (255, 214, 10)
BAR = (58, 58, 60)


def font(path, size, index=0):
    return ImageFont.truetype(path, size, index=index)


def wrap(text, f, width):
    """픽셀 폭 기준으로 줄을 접는다(가능하면 공백에서). 이어지는 줄은 두 칸 들여 쓴다."""
    out, cur = [], ""
    for ch in text:
        if f.getlength(cur + ch) > width and cur:
            cut = cur.rfind(" ")
            if cut > len(cur) // 2:
                out.append(cur[:cut])
                cur = "  " + cur[cut + 1 :] + ch
            else:
                out.append(cur)
                cur = "  " + ch
        else:
            cur += ch
    out.append(cur)
    return out


PIPE_FONT = "/System/Library/Fonts/SFNSMono.ttf"


def draw_mono(d, xy, text, mono, size, fill):
    """D2Coding의 | 는 끊긴 막대로 보여 이 글자만 SF Mono로 그린다."""
    x, y = xy
    pipe = font(PIPE_FONT, size)
    parts = text.split("|")
    for i, part in enumerate(parts):
        d.text((x, y), part, font=mono, fill=fill)
        x += mono.getlength(part)
        if i < len(parts) - 1:
            d.text((x, y), "|", font=pipe, fill=fill)
            x += mono.getlength("|")


def dashed_rect(d, box, color, dash=8, gap=5, width=3):
    x0, y0, x1, y1 = box
    for x in range(x0, x1, dash + gap):
        d.line([(x, y0), (min(x + dash, x1), y0)], fill=color, width=width)
        d.line([(x, y1), (min(x + dash, x1), y1)], fill=color, width=width)
    for y in range(y0, y1, dash + gap):
        d.line([(x0, y), (x0, min(y + dash, y1))], fill=color, width=width)
        d.line([(x1, y), (x1, min(y + dash, y1))], fill=color, width=width)


def terminal(w, h, title, command, lines, size, gap):
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    mono = font(MONO, size)
    ui = font(UI, size - 1, 0)
    d.rectangle([0, 0, w, size * 2 + 6], fill=BAR)
    d.text((14, 8), title, font=ui, fill=FG)
    pad = 14
    y = size * 2 + 6 + 12
    for cmd_line in wrap(command, mono, w - pad * 2):
        draw_mono(d, (pad, y), cmd_line, mono, size, DIM)
        y += size + 6
    y += 8
    lh = size + 7
    for text, hl in lines:
        rows = wrap(text, mono, w - pad * 2)
        top = y
        for r in rows:
            draw_mono(d, (pad, y), r, mono, size, FG)
            y += lh
        if hl:
            dashed_rect(d, (4, top - 4, w - 6, y - 3), YELLOW)
        y += gap
    if y > h:
        raise SystemExit(f"{title}: 줄이 영역({h}px)을 넘는다 ({y}px)")
    return img


def browser_area(src, w=1280, h=1080):
    """원본 비율을 유지해 w x h 안에 넣고 남는 곳은 흰색."""
    area = Image.new("RGB", (w, h), "white")
    im = Image.open(ASSETS / src).convert("RGB")
    s = min(w / im.width, h / im.height)
    im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    area.paste(im, ((w - im.width) // 2, (h - im.height) // 2))
    return area


def slack_area(top):
    """가림 처리한 실제 Slack 캡처(16-slack-example.png)를 1280x1080 비율로 잘라 넣는다."""
    im = Image.open(ASSETS / "16-slack-example.png").convert("RGB")
    h = round(im.width * 1080 / 1280)
    return im.crop((0, top, im.width, top + h)).resize((1280, 1080), Image.LANCZOS)


def l2(name, browser, svc_cmd, svc_lines, wk_cmd, wk_lines):
    """브라우저 1280 + 오른쪽 위 ④ 서비스 로그 640x540 + 아래 ② 작업자 로그 640x540(16pt)."""
    img = Image.new("RGB", (1920, 1080), "white")
    if isinstance(browser, str):
        browser = browser_area(browser)
    img.paste(browser, (0, 0))
    img.paste(terminal(640, 540, "④ 서비스 로그", svc_cmd, svc_lines, 16, 6), (1280, 0))
    img.paste(terminal(640, 540, "② 작업자 로그", wk_cmd, wk_lines, 16, 6), (1280, 540))
    img.save(HERE / name)


def l4(name, slack, cmd, out, pods):
    """Slack 왼쪽 1280x1080 + 오른쪽 위 ③ 명령 터미널 640x540 + 아래 ⑤ Pod 감시 640x540(16pt)."""
    img = Image.new("RGB", (1920, 1080), "white")
    img.paste(slack, (0, 0))
    img.paste(terminal(640, 540, "③ 명령 터미널", cmd, out, 16, 6), (1280, 0))
    img.paste(terminal(640, 540, "⑤ Pod 감시", PODS_CMD, pods, 16, 4), (1280, 540))
    img.save(HERE / name)


def l3(name, wk_cmd, wk_lines, browser):
    """② 작업자 로그 왼쪽 1280x1080(20pt) + 브라우저 오른쪽 640x1080."""
    img = Image.new("RGB", (1920, 1080), "white")
    img.paste(terminal(1280, 1080, "② 작업자 로그", wk_cmd, wk_lines, 20, 10), (0, 0))
    img.paste(browser, (1280, 0))
    img.save(HERE / name)


# ---- 로그 줄 데이터 ----
# 새 로그 형식(#275): "YYYY-MM-DD HH:MM:SS INFO [역할] 내용". 역할은 web·worker·listener.
WK_CMD = "$ kubectl -n kubeguardian logs -f deploy/diagnostic-agent --all-containers --since=1s"
GW_CMD = (
    "$ kubectl -n dev logs -f deploy/gateway --since=1s | grep --line-buffered -v -E "
    "'GET /(health|readyz)'"
)
SVC_CMD = (
    "$ kubectl -n dev logs -f deploy/llm-gateway --since=1s | grep --line-buffered -E "
    "'chat/completions|gpt-4.1-next' | perl -pe 'BEGIN{$|=1} s#https?://[^/\\s\"]+#<외부 호스트>#g'"
)
W = "INFO [worker] "  # 형식 변환 줄의 접두(날짜·시각은 앞에 붙임)
B = "INFO [web] "
CONV = "2026-11-12 03:"  # 옛 형식 리허설 원문(03:27 등)을 새 형식으로 옮긴 줄의 날짜·시각 접두

NO_GW = (
    "(리허설에서 확인 — 장애 때는 `-> http://agent.:8006/...`처럼 오타 주소가 프록시 요청 줄에 "
    "보일 것으로 예상, 실측 없음)"
)

# 실측: 11/16 18:57 배포 후 새 형식, 18:35 줄은 옛 형식 실측을 새 형식으로 옮긴 것
R2_SVC = [(NO_GW, False)]
R3_WK = [
    (
        '2026-11-16 18:57:57 INFO [web] 127.0.0.1:60386 - "GET /llm-gateway/checks HTTP/1.1" 200',
        False,
    ),
    ("2026-11-16 18:35:58 INFO [web] [알림] M1 발송 이슈 #21 (Slack)", True),
    (
        "(위 줄은 옛 형식 실측을 새 형식으로 옮김. 직접 점검은 web 컨테이너에서 돌아 "
        "Job 로그·규칙 결과 줄은 나오지 않음)",
        False,
    ),
]
R8A_SVC = [
    ("(형식 예시 — C8 README 기록 기반, 리허설에서 확인)", False),
    ("POST /api/v1/chat/completions ... 500", True),
    ("... 'deployment_name': 'gpt-4.1-next' ... 'response_status': 'error' ...", False),
]
R8A_WK = [
    (CONV + "27:12 " + W + "[작업] 조사 대기 집음: 이슈 #20 (점검 #92)", True),
    ("(형식 변환 — 리허설 원문은 옛 형식 `[작업] 조사 대기 집음` 줄)", False),
]
R8B_WK = [
    (CONV + "27:15 " + W + "[조사 #19] 시작: 이슈 #20 경로 B", True),
    (CONV + "27:16 " + W + "[조사 #19] 사전 확인: 근거 8건", False),
    ("(형식 변환 — 리허설 원문은 옛 형식)", False),
]
R9_WK = [
    (CONV + "27:16 " + W + "[조사 #19] 사전 확인: 근거 8건", False),
    (
        CONV + "27:24 " + W + "[조사 #19] 선별: 문제 1개 — 외부 모델 배포 거부로 채팅 응답이 "
        "실패합니다 [ev-0637, ev-…",
        False,
    ),
    (
        CONV + "27:36 " + W + "[조사 #19] 계획: 가설 3개 — 추정: 모델 배포 설정이 허용되지 않은 "
        "배포 이름으로 변경되었을 가능성… / 추정: 외부 호출 경로의 배포 허용 정책이 요청한 모델 "
        "배포를 차단했을… / 추정: 외부 API 인증 정보가 해당 모델 배포에 접근할 권한이 없는 …",
        True,
    ),
    (
        CONV + "27:39 " + W + "[조사 #19] 도구 get_pod_logs llm-gateway-66b994f766-xnks8 "
        "llm-gateway 100 deployment_not… → 근거 ev-0643",
        False,
    ),
    (
        CONV + "27:44 " + W + "[조사 #19] 도구 query_app_api gateway "
        "/api/v1/llm-gateway/deployments/assignments/org/${o… → 근거 ev-0644",
        False,
    ),
    (
        CONV + "27:44 " + W + "[조사 #19] 도구 get_resource Deployment/llm-gateway → 근거 ev-0645",
        False,
    ),
    (CONV + "29:01 " + W + "[조사 #19] 재조사: 빈틈 1개", True),
    (
        CONV + "29:04 " + W + "[조사 #19] 도구 get_pod_logs llm-gateway-66b994f766-xnks8 "
        "llm-gateway 2 Azure OpenAI err… → 근거 ev-0651",
        False,
    ),
    (CONV + "29:07 " + W + "[조사 #19] 도구 get_topology → 근거 ev-0652", False),
    (
        CONV + "29:12 " + W + "[조사 #19] 도구 get_pod_logs llm-gateway-66b994f766-xnks8 "
        "llm-gateway 6 atl_gateway|upst… → 근거 ev-0653",
        False,
    ),
    (
        CONV + "30:19 " + W + "[조사 #19] 가설 판정: 현재 할당된 외부 배포 호출의 거부가 조사한 "
        "채팅 실패를 일으켰습니다. 지지 / 외부 ATL 게이트웨이의 자체 배포 허용 정책이 최종 거부 "
        "원인입니다. 판단 불가 / … 반증 / … 반증",
        False,
    ),
    (
        CONV + "30:19 " + W + "[조사 #19] 결론: Deployment/llm-gateway의 외부 배포 gpt-4.1-next "
        "호출이 <주소 가림> … (판단 수준 후보, 183s, 도구 19회)",
        True,
    ),
    ("(형식 변환 — 리허설 원문은 옛 형식, 가설 판정은 일부 줄임)", False),
]


# ---- L4 (운영 화면): ③ 명령 터미널과 ⑤ Pod 감시 ----
PODS_CMD = "$ kubectl get pods -n dev -w"
PODS_HEAD = "NAME                  READY  STATUS             RESTARTS  AGE"
EXAMPLE = "(형식 예시 — Pod 이름·시각은 지어낸 값, 리허설에서 확인)"


def pod(name, ready="1/1", status="Running", age="3h"):
    return f"{name:<21} {ready:<6} {status:<18} 0         {age}"


BASE_PODS = [
    pod("admin-xxxxx"),
    pod("agent-xxxxx"),
    pod("agent-xxxxx"),
    pod("agent-xxxxx"),
    pod("front-admin-xxxxx"),
    pod("front-chat-xxxxx"),
    pod("llm-gateway-xxxxx"),
    pod("postgres-xxxxx"),
    pod("redis-xxxxx"),
]
P_IDLE = [(PODS_HEAD, False)] + [(p, False) for p in BASE_PODS] + [(pod("gateway-xxxxx"), False)]
P_ROLLOUT = (
    [(PODS_HEAD, False)]
    + [(p, False) for p in BASE_PODS]
    + [
        (pod("gateway-xxxxx"), False),
        (pod("gateway-yyyyy", "0/1", "ContainerCreating", "0s"), True),
        (pod("gateway-yyyyy", "1/1", "Running", "5s"), True),
        (pod("gateway-xxxxx", "1/1", "Terminating"), True),
        (EXAMPLE, False),
    ]
)
P_DONE = (
    [(PODS_HEAD, False)]
    + [(p, False) for p in BASE_PODS]
    + [(pod("gateway-yyyyy", "1/1", "Running", "4m"), False), (EXAMPLE, False)]
)
P_NOCHANGE = P_IDLE + [("(Pod 변화 없음 — 새 줄이 올라오지 않음)", True), (EXAMPLE, False)]

PROMPT = "$"
R2_CMD = "$ KG_TYPO=dot bash scenarios/A7/inject.sh"
R2_OUT = [
    (
        "A7 주입(dot): dev/Deployment/gateway AGENT_SERVICE_URL "
        "http://agent:8006 → http://agent.:8006",
        True,
    ),
    ("(inject.sh의 echo 문구. 주소 값은 스크립트 기본 형식)", False),
]
R7_CMD = "$ bash scenarios/C8/inject.sh"
R7_OUT = [
    ("C8 injected: test deployment_name changed; original saved; credentials unchanged", True),
    ("$ bash scenarios/C8/verify_symptom.sh", False),
    (
        '{"valid": true, "target_pods_ready": true, "chat_failed": true, '
        '"upstream_error": "403 deployment_not_allowed", "server_log_matches": true}',
        True,
    ),
    ("(scenario.py가 print하는 문구와 JSON. 실제 실행 출력은 확인하지 못함)", False),
]


def main():
    slack_a7, slack_c8 = slack_area(159), slack_area(0)  # A·B·C가 보이는 아래쪽, D가 보이는 위쪽
    l4("R2-a7-inject.png", slack_a7, R2_CMD, R2_OUT, P_ROLLOUT)
    l2("R3-a7-check-run.png", "02-a7-check-90-judge.png", GW_CMD, R2_SVC, WK_CMD, R3_WK)
    l4("R5-a7-slack-evidence-summary.png", slack_a7, PROMPT, [], P_DONE)
    l4("R7-c8-inject.png", slack_c8, R7_CMD, R7_OUT, P_NOCHANGE)
    l2("R8a-c8-check-run.png", "05-c8-check-92-judge.png", SVC_CMD, R8A_SVC, WK_CMD, R8A_WK)
    l2(
        "R8b-c8-investigation-start.png",
        "05-c8-check-92-judge.png",
        SVC_CMD,
        R8A_SVC,
        WK_CMD,
        R8B_WK,
    )
    l3(
        "R9-c8-investigation-log.png",
        WK_CMD,
        R9_WK,
        browser_area("05-c8-check-92-judge.png", 640, 1080),
    )
    l4("R10-c8-slack-update.png", slack_c8, PROMPT, [], P_NOCHANGE[:-2] + [(EXAMPLE, False)])


if __name__ == "__main__":
    main()
