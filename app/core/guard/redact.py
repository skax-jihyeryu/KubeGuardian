"""비밀값 마스킹. 수집·전송·표시하는 모든 문자열에 적용하는 순수 함수다 (I/O 없음).

규칙 (기획서 §6.5):
- URL 자격 증명 `scheme://user:pass@host`, `redis://:pass@host` → 비밀번호만 `***`로 바꾼다.
- PEM 개인 키 → BEGIN·END 줄은 두고 본문을 `***`로 바꾼다.
- `Bearer <토큰>`(8자 이상), `Authorization: [Basic|Bearer|...] <값>` → 값을 `***`로 바꾼다.
- 형식이 알려진 토큰(Slack `xox?-`, GitHub `ghp_`, `sk-`, JWT) → 통째로 `***`로 바꾼다.
- `이름=값`, `이름: 값`, `"이름": "값"`, `이름 => 값`, `--이름 값`에서 이름이 민감하면 값을 가린다.
  값은 같은 줄에서만 찾는다 (YAML의 `secret:` 다음 줄은 값이 아니다).
  `<set to the key ...>`처럼 `<`로 시작하는 값은 참조 설명이라 두고, 이스케이프된 JSON
  (`\\"password\\":\\"x\\"`)과 닫히지 않은 따옴표(잘린 로그)도 가린다.

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
_NON_VALUE_SUFFIXES = {"name", "ref", "id", "type", "count", "ttl", "expiry", "url", "path", "file"}

_PEM = re.compile(
    r"(?P<head>-----BEGIN [A-Z ]*PRIVATE KEY-----).*?(?P<tail>-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_URL_USERINFO = re.compile(
    r"(?P<head>(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]*://[^\s/:@]*:)[^\s/@]+(?=@)"
)
_KNOWN_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_\-])(?:xox[abposr]-[A-Za-z0-9\-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|sk-[A-Za-z0-9_\-]{20,}|eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+)"
)
_BEARER = re.compile(r"(?P<head>\bBearer[ \t]+)[A-Za-z0-9\-._~+/]{8,}=*", re.IGNORECASE)
_AUTH_HEADER = re.compile(
    r"(?P<head>\bAuthorization\\?[\"']?[ \t]*[:=][ \t]*\\?[\"']?"
    r"(?:(?:Basic|Bearer|Digest|Token|ApiKey|Negotiate)[ \t]+)?)[^\s\"'\\,;]+",
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
    text = _URL_USERINFO.sub(rf"\g<head>{MASK}", text)
    text = _KNOWN_TOKEN.sub(MASK, text)
    text = _BEARER.sub(rf"\g<head>{MASK}", text)
    text = _AUTH_HEADER.sub(rf"\g<head>{MASK}", text)
    text = _CLI_FLAG.sub(_mask_cli_flag, text)
    return _redact_key_values(text)


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
            sensitive = is_sensitive_name(str(k)) or (mask_env_value and k == "value")
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
