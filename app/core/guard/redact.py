"""비밀값 마스킹. 수집·전송·표시하는 모든 문자열에 적용하는 순수 함수다 (I/O 없음).

규칙 (기획서 §6.5):
- URL 자격 증명 `scheme://user:pass@host`, `redis://:pass@host` → 비밀번호만 `***`로 바꾼다.
- PEM 개인 키 → BEGIN·END 줄은 두고 본문을 `***`로 바꾼다.
  로그가 잘려 BEGIN·END 중 한쪽이 없어도 본문을 가린다. BEGIN만 있으면 뒤를 모두,
  END만 있으면 END 앞에 이어진 base64 줄을 가린다. 둘 다 없이 본문만 남은 경우는
  **정확히 64자인 base64 줄이 2줄 이상 연속**일 때 그 줄들(바로 뒤에 `=` 패딩으로 끝나는
  마지막 줄이 있으면 그것도)을 가린다. 64자 한 줄은 일반 값(해시·ID)과 구분하지 못해 두고,
  64자 16진수만으로 된 줄(sha256 다이제스트)과 64자가 아닌 줄은 본문으로 보지 않는다.
  패딩 없는 짧은 마지막 줄은 일반 줄과 구분할 수 없어 남는다 (과잉 마스킹 방지).
  들여쓴 본문은 들여쓰기를 남기고 가린다. 인증서·공개 키 BEGIN/END 블록 안의 본문은 두며,
  줄 앞 접두사(타임스탬프)와 JSON 이스케이프(`\\n`) 본문은 다루지 않는다.
- `Bearer <토큰>`(8자 이상), `Authorization: [Basic|Bearer|...] <값>` → 값을 `***`로 바꾼다.
- 형식이 알려진 토큰(Slack `xox?-`, GitHub `ghp_`, `sk-`, JWT) → 통째로 `***`로 바꾼다.
- `이름=값`, `이름: 값`, `"이름": "값"`, `이름 => 값`, `--이름 값`에서 이름이 민감하면 값을 가린다.
  값은 같은 줄에서만 찾는다 (YAML의 `secret:` 다음 줄은 값이 아니다).
  `<set to the key ...>`처럼 `<`로 시작하는 값은 참조 설명이라 두고, 이스케이프된 JSON
  (`\\"password\\":\\"x\\"`)과 닫히지 않은 따옴표(잘린 로그)도 가린다.

- `Authorization`·`Proxy-Authorization` 헤더는 값을(`Basic` 같은 방식 이름은 둔다),
  `Cookie`·`Set-Cookie` 헤더는 줄 끝까지(`;`·`=`가 든 값 전체)를 `***`로 바꾼다.
  매핑 키로 쓰인 경우도 같다. 이름 전체가 일치할 때만이라 `cookie_name`·`cookieConsent`는
  가리지 않는다. `X-Api-Key`는 `api_key`와 같은 규칙으로 가린다.

이름이 민감한지는 `_`·`-`·`.`·공백·camelCase로 나눈 단어로 판단한다.
- `password`·`passwd`·`passphrase`·`pass`·`secret`·`token`·`apikey`·`credential(s)` 단어가
  있거나, 단어가 `password`·`passwd`·`secret`·`token`으로 끝나면 민감하다
  (`DB_PASSWORD`, `PGPASSWORD`, `client_secret`, `accessToken`).
- `key`·`pwd`는 다른 단어와 붙었을 때만 민감하다 (`api_key`, `MYSQL_PWD`).
  단독 `key`는 Secret·ConfigMap 참조의 키 이름(`secretKeyRef.key`)이고, 단독 `PWD`는
  현재 디렉터리라 가리지 않는다. `public`이 붙은 `key`(`public_key`)도 가리지 않는다.
- 마지막 단어가 `name`·`ref`·`id`·`type`·`count`·`ttl`·`expiry`·`url`·`path`·`file`이면
  비밀값이 아니라 그 비밀값을 가리키는 정보라 가리지 않는다 (`secretName`, `token_ttl`).
- 단어 일부만 같은 경우(`keyboard`, `monkey`, `tokenizer`)와 복수형(`max_tokens`)은
  민감하지 않다.

같은 입력을 두 번 마스킹해도 결과가 같다 (멱등). 긴 줄에서도 선형 시간에 끝난다.
"""

import re
from bisect import bisect_right
from typing import Any

MASK = "***"

_SENSITIVE_WORDS = {
    "password",
    "passwd",
    "passphrase",
    "pass",
    "secret",
    "token",
    "apikey",
    "credential",
    "credentials",
}
_SENSITIVE_SUFFIXES = ("password", "passwd", "secret", "token")
_COMPOUND_ONLY_WORDS = {"key", "pwd"}
# 이름 전체가 이 값일 때만 민감한 HTTP 헤더 (`cookie_name`·`cookieConsent`는 해당 없음).
_SENSITIVE_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie"}
_NON_VALUE_SUFFIXES = {"name", "ref", "id", "type", "count", "ttl", "expiry", "url", "path", "file"}

# END만 있는 경우 (로그를 앞에서 잘라 BEGIN이 없다):
# END 바로 앞에 이어진 base64 줄과 END 줄을 가린다.
# 본문이 짧은 마지막 줄(16자 미만)은 일반 줄과 구분하지 못해 남는다.
_PEM_END_LINE = re.compile(r"^-----END [A-Z ]*PRIVATE KEY-----", re.MULTILINE)
_BASE64_LINE = re.compile(r"[A-Za-z0-9+/=]{16,}\r?")
_BASE64_LAST_LINE = re.compile(r"[A-Za-z0-9+/=]+\r?")  # PEM의 마지막 줄은 짧을 수 있다
# BEGIN만 있고 END가 없는 경우(`\Z`)는 뒤를 모두 가린다. 로그가 중간에서 잘렸을 때 본문이
# 어디까지인지 알 수 없어, 비밀값을 놓치느니 뒤의 일반 줄을 잃는 쪽을 택한다.
_PEM = re.compile(
    r"(?P<head>-----BEGIN [A-Z ]*PRIVATE KEY-----).*?(?P<tail>-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
# BEGIN·END가 모두 잘려 본문만 남은 경우: 64자 base64 줄이 2줄 이상 연속이면 가린다.
# 줄 시작에서만 시도하고 줄마다 한 번씩만 읽어 선형이다. 64자 16진수 줄(다이제스트)은 제외한다.
# 줄 앞 들여쓰기(YAML 블록 스칼라)는 허용하고 첫 줄의 들여쓰기는 남긴다.
# 줄 앞 접두사(타임스탬프 등)와 JSON 이스케이프된 `\n` 본문은 다루지 않는다.
_PEM_BODY_LINE = r"[ \t]*(?![0-9a-fA-F]{64}\r?$)[A-Za-z0-9+/]{64}"
_PEM_BODY = re.compile(
    rf"^(?P<indent>[ \t]*)(?![0-9a-fA-F]{{64}}\r?$)[A-Za-z0-9+/]{{64}}"
    rf"(?:\r?\n{_PEM_BODY_LINE})+(?:\r?\n[ \t]*[A-Za-z0-9+/]*={{1,2}})?(?=\r?$)",
    re.MULTILINE,
)
# 개인 키가 아닌 BEGIN/END 블록(인증서·공개 키)의 본문은 비밀이 아니라서 가리지 않는다.
_NON_KEY_BEGIN = re.compile(r"-----BEGIN (?![A-Z ]*PRIVATE KEY)[A-Z ]+-----")
_ANY_END = re.compile(r"-----END [A-Z ]+-----")
_URL_USERINFO = re.compile(
    r"(?P<head>(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]*://[^\s/:@]*:)[^\s/@]+(?=@)"
)
_KNOWN_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_\-])(?:xox[abposr]-[A-Za-z0-9\-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}"
    # sk- 키는 `-` 없는 영숫자다. `sk-hynix-batch` 같은 K8s 리소스 이름과 구분하기 위함이다.
    r"|sk-(?:proj-|ant-[a-z0-9]+-)?[A-Za-z0-9_]{20,}|eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+)"
)
_BEARER = re.compile(r"(?P<head>\bBearer[ \t]+)[A-Za-z0-9\-._~+/]{8,}=*", re.IGNORECASE)
_AUTH_HEADER = re.compile(
    r"(?P<head>\bAuthorization\\?[\"']?[ \t]*[:=][ \t]*\\?[\"']?"
    r"(?:(?:Basic|Bearer|Digest|Token|ApiKey|Negotiate)[ \t]+)?)[^\s\"'\\,;]+",
    re.IGNORECASE,
)
# Cookie·Set-Cookie 값은 `;`·`=`·공백을 담으므로 `:`·`=` 구분자 모두 구분자 뒤를 통째로 가린다.
# 값은 RFC 6265상 `"`·`\`·줄바꿈을 담지 못하므로 거기서 멈춘다. 따옴표로 열린 값은 같은 따옴표에서,
# 따옴표 없는 값은 (`Cookie: a=1; b=2`처럼 공백을 담으므로) 줄 끝이나 `"`에서 끝난다.
_COOKIE = re.compile(
    r"(?P<head>(?<![A-Za-z0-9_.\-])\\?[\"']?(?:Set-)?Cookie\\?[\"']?[ \t]*[:=][ \t]*"
    r"(?:\\?(?P<q>[\"']))?)"
    r"(?(q)(?:(?!(?P=q))[^\r\n\"\\])+|[^\r\n\"\\]+)",
    re.IGNORECASE,
)
_CLI_FLAG = re.compile(
    r"(?<![^\s\"'\[,])--(?P<key>[A-Za-z0-9_.\-]+)[ \t]+(?P<val>[^\s\"'\-][^\s\"',]*)"
)
# 이름+구분자만 찾고 값은 따로 읽는다. 앞의 값에 가려 뒤의 이름을 놓치지 않기 위함이다.
# 이름 앞을 경계로 고정해 긴 줄에서 시작 위치마다 다시 훑지 않는다 (선형 시간).
_KEY_SEP = re.compile(
    r"(?<![A-Za-z0-9_.\-])(?P<key>[A-Za-z0-9_.\-]+)\\?[\"']?[ \t]*(?:=>|[:=])[ \t]*"
)
_VALUE = re.compile(
    r"\\(?P<eq>[\"'])(?P<eqval>[^\n]*?)\\(?P=eq)"
    r"|(?P<q>[\"'])(?P<qval>(?:\\.|(?!(?P=q))[^\\\n])*)(?P=q)"
    r"|\\?[\"']?(?P<val>[^\s\"'\\,;&}\]]+)"
)
_FLAG_ITEM = re.compile(r"--?(?P<key>[A-Za-z0-9_.\-]+)")
_WORD_SPLIT = re.compile(r"[_.\-\s]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def is_sensitive_name(name: str) -> bool:
    """이름(키·환경변수 이름·CLI 플래그)이 비밀값을 담는 이름인지 판단한다."""
    words = [w.lower() for w in _WORD_SPLIT.split(name) if w]
    if not words or words[-1] in _NON_VALUE_SUFFIXES:
        return False
    if any(w in _SENSITIVE_WORDS or w.endswith(_SENSITIVE_SUFFIXES) for w in words):
        return True
    return (
        len(words) > 1 and any(w in _COMPOUND_ONLY_WORDS for w in words) and "public" not in words
    )


def redact_text(text: str) -> str:
    """문자열 안의 비밀값을 `***`로 바꾼다."""
    text = _PEM.sub(rf"\g<head>{MASK}\g<tail>", text)
    text = _mask_orphan_pem_end(text)
    text = _mask_pem_body(text)
    text = _URL_USERINFO.sub(rf"\g<head>{MASK}", text)
    text = _KNOWN_TOKEN.sub(MASK, text)
    text = _BEARER.sub(rf"\g<head>{MASK}", text)
    text = _AUTH_HEADER.sub(rf"\g<head>{MASK}", text)
    text = _COOKIE.sub(rf"\g<head>{MASK}", text)
    text = _CLI_FLAG.sub(_mask_cli_flag, text)
    return _redact_key_values(text)


def _non_key_pem_spans(text: str) -> list[tuple[int, int]]:
    """인증서·공개 키 BEGIN~END 구간. 구간 안은 건너뛰고 찾아 선형이다."""
    spans: list[tuple[int, int]] = []
    pos = 0
    while (begin := _NON_KEY_BEGIN.search(text, pos)) is not None:
        end = _ANY_END.search(text, begin.end())
        if end is None:
            break
        spans.append((begin.start(), end.end()))
        pos = end.end()
    return spans


def _mask_pem_body(text: str) -> str:
    """BEGIN·END 없는 PEM 본문을 가린다. 인증서·공개 키 블록 안의 본문은 둔다."""
    if "-----BEGIN" not in text:
        return _PEM_BODY.sub(lambda m: m.group("indent") + MASK, text)
    spans = _non_key_pem_spans(text)

    starts = [a for a, _ in spans]

    def repl(m: re.Match[str]) -> str:
        i = bisect_right(starts, m.start()) - 1
        if i >= 0 and m.start() < spans[i][1]:
            return m.group(0)
        return m.group("indent") + MASK

    return _PEM_BODY.sub(repl, text)


def _mask_orphan_pem_end(text: str) -> str:
    """BEGIN 없는 END 줄과 그 앞의 base64 줄을 가린다.

    END 바로 앞 한 줄은 base64 문자로만 되어 있으면 길이와 상관없이(`abc=`) 가리고,
    그 위는 16자 이상 base64 줄만 이어서 가린다.
    위에서 마스킹한 PEM의 END는 줄 앞이 아니라서 걸리지 않는다. END마다 위로만 훑어 선형이다.
    """
    out_end = len(text)
    pieces: list[str] = []
    matches = list(_PEM_END_LINE.finditer(text))
    for m in reversed(matches):
        start = m.start()
        line_re = _BASE64_LAST_LINE
        while start > 0:
            line_start = text.rfind("\n", 0, start - 1) + 1
            if not line_re.fullmatch(text, line_start, start - 1):
                break
            start = line_start
            line_re = _BASE64_LINE
        pieces.append(text[m.end() : out_end])
        pieces.append(MASK)
        out_end = start
    pieces.append(text[:out_end])
    return "".join(reversed(pieces))


def _mask_cli_flag(m: re.Match[str]) -> str:
    if not is_sensitive_name(m.group("key")):
        return m.group(0)
    return m.group(0)[: m.start("val") - m.start()] + MASK


def _redact_key_values(text: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _KEY_SEP.finditer(text):
        if m.start() < pos or not is_sensitive_name(m.group("key")):
            continue
        if _is_url_host(text, m.start()) and _PORT.match(text, m.end("key")):
            continue  # URL의 host:port (`http://token-service:8080`)는 이름=값이 아니다
        if text.startswith(("<", "-----BEGIN"), m.end()):
            continue
        v = _VALUE.match(text, m.end())
        if v is None:
            continue
        group = next(g for g in ("eqval", "qval", "val") if v.group(g) is not None)
        out.append(text[pos : v.start(group)])
        out.append(MASK)
        pos = v.end(group)
    out.append(text[pos:])
    return "".join(out)


_URL_AUTHORITY_TAIL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://(?:[^\s/@]*@)?\Z")
_PORT = re.compile(r":\d{1,5}(?=$|[/?#\s\"'])")  # host 바로 뒤의 `:포트`만 예외로 둔다
_AUTHORITY_LOOKBACK = 256  # 긴 줄에서 선형 시간을 지키려고 뒤돌아보는 길이를 제한한다


def _is_url_host(text: str, pos: int) -> bool:
    """pos가 `scheme://` 또는 `scheme://userinfo@` 바로 뒤(URL 호스트 자리)인지."""
    if not text.endswith(("://", "@"), 0, pos):
        return False  # 대부분의 키는 여기서 끝난다
    return _URL_AUTHORITY_TAIL.search(text, max(0, pos - _AUTHORITY_LOOKBACK), pos) is not None


def redact_mapping(data: Any) -> Any:
    """dict·list 안의 문자열을 재귀로 마스킹한 새 객체를 돌려준다 (입력은 바꾸지 않는다).

    - 키 이름이 민감하면 문자열·숫자 값(리스트면 그 항목)을 `***`로 바꾼다.
      값이 dict면 통째로 가리지 않고 재귀한다 (`secret: {secretName: ...}` 같은 참조를 남기기 위함).
    - env 항목(`{"name": "DB_PASSWORD", "value": ...}`)은 `name`이 민감하면 `value`를 가린다.
    - 리스트의 `["--db-password", "x"]`처럼 민감한 플래그 다음 항목을 가린다 (컨테이너 args).
    - 그 밖의 문자열은 `redact_text`로 마스킹한다.
    """
    if isinstance(data, dict):
        env_name = data.get("name")
        mask_env_value = isinstance(env_name, str) and is_sensitive_name(env_name)
        out = {}
        for k, v in data.items():
            sensitive = (
                str(k).lower() in _SENSITIVE_HEADERS
                or is_sensitive_name(str(k))
                or (mask_env_value and k == "value")
            )
            out[k] = _mask_value(v) if sensitive else redact_mapping(v)
        return out
    if isinstance(data, list | tuple):
        return type(data)(_redact_items(data))
    if isinstance(data, str):
        return redact_text(data)
    return data


def _mask_value(v: Any) -> Any:
    if _is_scalar_value(v):
        return MASK
    if isinstance(v, list | tuple):
        return type(v)(MASK if _is_scalar_value(x) else redact_mapping(x) for x in v)
    return redact_mapping(v)


def _redact_items(items: list | tuple) -> list:
    out = []
    mask_next = False
    for item in items:
        if mask_next and _is_scalar_value(item):
            out.append(MASK)
            mask_next = False
            continue
        flag = _FLAG_ITEM.fullmatch(item) if isinstance(item, str) else None
        mask_next = flag is not None and is_sensitive_name(flag.group("key"))
        out.append(redact_mapping(item))
    return out


def _is_scalar_value(v: Any) -> bool:
    return isinstance(v, str | int | float) and not isinstance(v, bool)
