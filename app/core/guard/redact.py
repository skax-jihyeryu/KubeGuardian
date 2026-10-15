"""비밀값 마스킹. 수집·전송·표시하는 모든 문자열에 적용하는 순수 함수다 (I/O 없음).

규칙 (기획서 §6.5):
- URL 자격 증명 `scheme://user:pass@host` → 비밀번호만 `***`로 바꾼다.
- `Bearer <토큰>`, `Authorization: Basic <값>` → 토큰·값을 `***`로 바꾼다.
- `이름=값`, `이름: 값`, `"이름": "값"` 형태에서 이름이 민감하면 값을 `***`로 바꾼다.

이름이 민감한지는 `_`·`-`·`.`·camelCase로 나눈 단어로 판단한다.
- `password`·`passwd`·`secret`·`token`·`apikey` 단어가 있으면 민감하다
  (`DB_PASSWORD`, `client_secret`, `accessToken`, `x-api-key`).
- `key`는 다른 단어와 붙었을 때만 민감하다 (`api_key`, `secretKey`).
  단독 `key`는 Secret·ConfigMap 참조의 키 이름(`secretKeyRef.key`)이라 가리지 않는다.
- 마지막 단어가 `name`·`ref`이면 객체를 가리키는 이름이라 가리지 않는다 (`secretName`).
- 단어 일부만 같은 경우(`keyboard`, `monkey`, `tokenizer`)와 복수형(`max_tokens`)은
  민감하지 않다.

같은 입력을 두 번 마스킹해도 결과가 같다 (멱등).
"""

import re
from typing import Any

MASK = "***"

_SENSITIVE_WORDS = {"password", "passwd", "secret", "token", "apikey"}
_REFERENCE_SUFFIXES = {"name", "ref"}

_URL_USERINFO = re.compile(r"(?P<head>\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s/:@]+:)[^\s/@]+(?=@)")
_BEARER = re.compile(r"(?P<head>\bBearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
_AUTH_HEADER = re.compile(
    r"(?P<head>\bAuthorization[\"']?\s*[:=]\s*[\"']?(?:Basic|Digest|Token)\s+)[^\s\"',;]+",
    re.IGNORECASE,
)
# 값은 따로 읽는다. 이름+구분자만 찾아야 앞의 값에 가려 뒤의 이름을 놓치지 않는다.
_KEY_SEP = re.compile(r"(?P<key>[A-Za-z0-9_.\-]+)[\"']?\s*[:=]\s*")
_VALUE = re.compile(r"(?P<q>[\"'])(?P<qval>.*?)(?P=q)|(?P<val>[^\s\"',;&}\]]+)")
_WORD_SPLIT = re.compile(r"[_.\-\s]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def is_sensitive_name(name: str) -> bool:
    """이름(키·환경변수 이름)이 비밀값을 담는 이름인지 판단한다."""
    words = [w.lower() for w in _WORD_SPLIT.split(name) if w]
    if not words or words[-1] in _REFERENCE_SUFFIXES:
        return False
    if any(w in _SENSITIVE_WORDS for w in words):
        return True
    return len(words) > 1 and "key" in words


def redact_text(text: str) -> str:
    """문자열 안의 비밀값을 `***`로 바꾼다."""
    text = _URL_USERINFO.sub(rf"\g<head>{MASK}", text)
    text = _BEARER.sub(rf"\g<head>{MASK}", text)
    text = _AUTH_HEADER.sub(rf"\g<head>{MASK}", text)
    return _redact_key_values(text)


def _redact_key_values(text: str) -> str:
    out: list[str] = []
    pos = 0
    for m in _KEY_SEP.finditer(text):
        if m.start() < pos or not is_sensitive_name(m.group("key")):
            continue
        v = _VALUE.match(text, m.end())
        if v is None:
            continue
        if v.group("q"):
            start, end = v.start("qval"), v.end("qval")
        else:
            start, end = v.start("val"), v.end("val")
        out.append(text[pos:start])
        out.append(MASK)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def redact_mapping(data: Any) -> Any:
    """dict·list 안의 문자열을 재귀로 마스킹한 새 객체를 돌려준다 (입력은 바꾸지 않는다).

    - 키 이름이 민감하면 문자열·숫자 값을 통째로 `***`로 바꾼다.
    - env 항목(`{"name": "DB_PASSWORD", "value": ...}`)은 `name`이 민감하면 `value`를 가린다.
    - 그 밖의 문자열은 `redact_text`로 마스킹한다.
    """
    if isinstance(data, dict):
        env_name = data.get("name")
        mask_env_value = isinstance(env_name, str) and is_sensitive_name(env_name)
        out = {}
        for k, v in data.items():
            sensitive = is_sensitive_name(str(k)) or (mask_env_value and k == "value")
            out[k] = MASK if sensitive and _is_scalar_value(v) else redact_mapping(v)
        return out
    if isinstance(data, list | tuple):
        return type(data)(redact_mapping(v) for v in data)
    if isinstance(data, str):
        return redact_text(data)
    return data


def _is_scalar_value(v: Any) -> bool:
    return isinstance(v, str | int | float) and not isinstance(v, bool)
