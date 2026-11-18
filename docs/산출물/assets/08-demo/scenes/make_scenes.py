"""08 시연영상스크립트의 장면 합성 사진(L2·L3·L4·L5)을 만든다.

실행: python make_scenes.py   (Pillow 필요, 저장소 어디서든 같은 결과)
- 브라우저 영역은 assets/08-demo의 기존 캡처를 1280x1080 안에 비율 유지로 넣는다.
- 터미널 영역은 로그 줄 데이터로 그린다. 로그 줄은 08 문서의 실측 출력(Job 로그,
  A7·C8 리허설 발췌, 로그 샘플 사진 12~14)이거나 scenarios/*의 스크립트가 echo하는 문구다.
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


def browser_area(src):
    """원본 비율을 유지해 1280x1080 안에 넣고 남는 곳은 흰색."""
    area = Image.new("RGB", (1280, 1080), "white")
    im = Image.open(ASSETS / src).convert("RGB")
    s = min(1280 / im.width, 1080 / im.height)
    im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    area.paste(im, ((1280 - im.width) // 2, (1080 - im.height) // 2))
    return area


def slack_area(heading, notes):
    area = Image.new("RGB", (1280, 1080), (225, 227, 230))
    d = ImageDraw.Draw(area)
    big, mid = font(UI, 44, 0), font(UI, 34, 0)
    d.text((60, 60), "Slack 테스트 채널 (캡처 없음)", font=big, fill=(60, 60, 60))
    d.text((60, 140), heading, font=mid, fill=(90, 90, 90))
    y = 240
    for note, hl in notes:
        top = y
        for r in wrap(note, mid, 1100):
            d.text((60, y), r, font=mid, fill=(30, 30, 30))
            y += 52
        if hl:
            dashed_rect(d, (40, top - 10, 1240, y), (230, 170, 0))
        y += 36
    return area


def compose(name, left, right):
    img = Image.new("RGB", (1920, 1080), "white")
    img.paste(left, (0, 0))
    img.paste(right, (1280, 0))
    img.save(HERE / name)


def l2(name, browser, title, command, lines):
    compose(name, browser_area(browser), terminal(640, 1080, title, command, lines, 16, 10))


def l3(name, left, command, lines):
    compose(name, left, terminal(640, 1080, "② 작업자 로그", command, lines, 16, 10))


def l5(name, left_cmd, left_lines, right_cmd, right_lines):
    """왼쪽 ③' 점검 Job 로그 960 + 오른쪽 ④ 서비스 로그 960, 둘 다 터미널."""
    img = Image.new("RGB", (1920, 1080), "white")
    img.paste(terminal(960, 1080, "③' 점검 Job 로그", left_cmd, left_lines, 16, 10), (0, 0))
    img.paste(terminal(960, 1080, "④ 서비스 로그", right_cmd, right_lines, 16, 10), (960, 0))
    img.save(HERE / name)


def l4(name, command, lines):
    terminal(1920, 1080, "② 작업자 로그", command, lines, 22, 14).save(HERE / name)


# ---- 로그 줄 데이터 ----
GREP_ALL = (
    "$ kubectl -n kubeguardian logs -f deploy/diagnostic-agent -c worker --since=1s | grep "
    "--line-buffered -E '\\[조사|\\[작업\\]|\\[알림\\]'"
)
GREP_SUM = (
    "$ kubectl -n kubeguardian logs -f deploy/diagnostic-agent -c worker --since=1s | grep "
    "--line-buffered -E '근거 요약|\\[작업\\]|\\[알림\\]'"
)
JOB_CMD = (
    "$ make kg-cron-run && J=$(kubectl -n kubeguardian get jobs ...) && until kubectl -n "
    "kubeguardian logs -f job/$J ...; do sleep 1; done"
)

R2 = [
    (
        "A7 주입(dot): dev/Deployment/gateway AGENT_SERVICE_URL http://agent:8006 → "
        "http://agent.:8006",
        True,
    ),
    ("$ kubectl get pods -n dev -w", False),
    ("NAME                       READY   STATUS              RESTARTS   AGE", False),
    ("gateway-<해시>-<새>        0/1     ContainerCreating   0          1s", False),
    ("gateway-<해시>-<새>        1/1     Running             0          6s", True),
    ("gateway-<해시>-<이전>      1/1     Terminating         0          3d", False),
    ("(위 3줄은 형식 예시)", False),
]
R3 = [
    ("점검 #N  dev  2026-11-…", False),
    ("03:26:32  게이트: 경로 A (critical) — 적발: S04", True),
    ("03:26:32  이슈 #19 새로 열림 (규칙 진단)", True),
    ("03:26:32  [알림] M1 발송 이슈 #19 (Slack)", False),
]
R5 = [
    ("03:26:35  근거 요약: ev-0632 done 1.7초 입력 339 출력 93", True),
    ("03:26:38  근거 요약: ev-0631 done 1.9초 입력 397 출력 101", True),
]
R7 = [
    ("OK", False),
    ("$ bash scenarios/C8/inject.sh", False),
    ("C8 injected: test deployment_name changed; original saved; credentials unchanged", False),
    ("$ bash scenarios/C8/verify_symptom.sh", False),
    (
        '{"valid": true, "target_pods_ready": true, "chat_failed": true, "upstream_error": "403 '
        'deployment_not_allowed", "server_log_matches": true}',
        True,
    ),
]
R8A = [
    ("03:27:12  게이트: 경로 B (critical) — 증상: chat 2단계 실패", True),
    ("03:27:12  이슈 #20 새로 열림 (조사 대기)", True),
    ("03:27:12  [알림] M1 발송 이슈 #20 (Slack)", False),
]
SVC_CMD = (
    "$ kubectl -n dev logs -f deploy/llm-gateway --since=1s | grep --line-buffered -E "
    "'chat/completions|gpt-4.1-next'"
)
# 형식 예시: scenarios/C8/README.md "확인 상태" 절의 기록만 근거. 모르는 부분은 ...
R8A_SVC = [
    ("(형식 예시 — README 기록 기반, 리허설 후 실측으로 교체)", False),
    ("POST /api/v1/chat/completions ... 500", True),
    (
        "... INSERT ... 'deployment_name': 'gpt-4.1-next' ... 'response_status': 'error' ... "
        "'log_data': '... (N characters truncated) ...'",
        False,
    ),
]
R8B = [
    ("03:27:15  [조사 #19] 시작: 이슈 #20 경로 B", True),
    ("03:27:16  [조사 #19] 사전 확인: 근거 8건", False),
]
R9 = [
    ("03:27:16  [조사 #19] 사전 확인: 근거 8건", False),
    (
        "03:27:24  [조사 #19] 선별: 문제 1개 — 외부 모델 배포 거부로 채팅 응답이 실패합니다 "
        "[ev-0637, ev-…",
        False,
    ),
    (
        "03:27:36  [조사 #19] 계획: 가설 3개 — 추정: 모델 배포 설정이 허용되지 않은 배포 이름으로 "
        "변경되었을 가능성… / 추정: 외부 호출 경로의 "
        "배포 허용 정책이 요청한 모델 배포를 차단했을… / 추정: 외부 API 인증 정보가 해당 모델 "
        "배포에 접근할 권한이 없는 …",
        True,
    ),
    (
        "03:27:39  [조사 #19] 도구 get_pod_logs llm-gateway-66b994f766-xnks8 llm-gateway 100 "
        "deployment_not… → 근거 ev-0643",
        False,
    ),
    (
        "03:27:44  [조사 #19] 도구 query_app_api gateway "
        "/api/v1/llm-gateway/deployments/assignments/org/${o… → 근거 ev-0644",
        False,
    ),
    ("03:27:44  [조사 #19] 도구 get_resource Deployment/llm-gateway → 근거 ev-0645", False),
    ("03:29:01  [조사 #19] 재조사: 빈틈 1개", True),
    (
        "03:29:04  [조사 #19] 도구 get_pod_logs llm-gateway-66b994f766-xnks8 llm-gateway 2 Azure "
        "OpenAI err… → 근거 ev-0651",
        False,
    ),
    ("03:29:07  [조사 #19] 도구 get_topology → 근거 ev-0652", False),
    (
        "03:29:12  [조사 #19] 도구 get_pod_logs llm-gateway-66b994f766-xnks8 llm-gateway 6 "
        "atl_gateway|upst… → 근거 ev-0653",
        False,
    ),
    (
        "03:30:19  [조사 #19] 가설 판정: 현재 할당된 외부 배포 호출의 거부가 조사한 채팅 실패를 "
        "일으켰습니다. 지지 / 외부 ATL 게이트웨이의 자체 "
        "배포 허용 정책이 최종 거부 원인입니다. 판단 불가 / 최근 모델 배포 설정이 허용되지 않은 "
        "이름으로 변경되었습니다. 판단 불가 / 외부 인증 정보가 해당 "
        "배포에 접근할 수 없는 인증 대상으로 변경되었습… 판단 불가 / llm-gateway 내부에서 외부 "
        "응답 없이 배포 거부 오류를 생성했… 반증 / "
        "llm-gateway의 다운이나 반복 재시작이 이번 실패의 직접 원인입… 반증 / 할당량 초과나 요청 "
        "시간 초과가 조사한 요청의 직접 실패 사유입니다. 반증",
        False,
    ),
    (
        "03:30:19  [조사 #19] 결론: Deployment/llm-gateway의 외부 배포 gpt-4.1-next 호출이 <주소 "
        "가림> … (판단 수준 "
        "후보, 183s, 도구 19회)",
        True,
    ),
]
R10 = [
    ("[알림] M1u 발송 이슈 #12 (Slack)", True),
    ("[알림] M9 발송 이슈 #12 (Slack)", True),
    ("[작업] 이슈 #12 조사 마침: done", True),
]


def main():
    l2(
        "R2-a7-inject.png",
        "01-normal-checks.png",
        "③ 명령 터미널",
        "$ KG_TYPO=dot bash scenarios/A7/inject.sh",
        R2,
    )
    l2("R3-a7-check-run.png", "01-normal-checks.png", "③ 명령 터미널", JOB_CMD, R3)
    l3(
        "R5-a7-slack-evidence-summary.png",
        slack_area(
            "A7 1차 알림 (규칙 기반 진단 결과)",
            [
                ("적발 규칙 ID, 이름, 위치 (최대 3줄)", False),
                ("규칙 기반 진단 결과입니다. 근본 원인은 확인하지 않았습니다.", True),
            ],
        ),
        GREP_SUM,
        R5,
    )
    l2(
        "R7-c8-inject.png",
        "01-normal-checks.png",
        "③ 명령 터미널",
        "$ bash scenarios/H0/verify_symptom.sh",
        R7,
    )
    l5("R8a-c8-check-run.png", JOB_CMD, R8A, SVC_CMD, R8A_SVC)
    l3("R8b-c8-investigation-start.png", browser_area("05-c8-check-92-judge.png"), GREP_ALL, R8B)
    l4("R9-c8-investigation-log.png", GREP_ALL, R9)
    l3(
        "R10-c8-slack-update.png",
        slack_area(
            "C8 1차 알림 갱신과 스레드 답글",
            [
                ("1차 알림이 원인 문구로 갱신된다 (M1 -> M1u)", True),
                ("스레드에 후속 답글이 붙는다 (M9)", True),
            ],
        ),
        GREP_ALL,
        R10,
    )


if __name__ == "__main__":
    main()
