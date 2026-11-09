"""셸 스크립트의 `$변수` 바로 뒤 한글 검사.

bash 3.2(macOS 기본)는 `$NS의`에서 한글 바이트를 변수 이름으로 읽어
`set -u`에서 unbound variable로 죽는다. `${NS}의`로 감싸야 한다 (#58).
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERN = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*[가-힣]")


def _offenders(text: str) -> list[int]:
    return [i for i, line in enumerate(text.splitlines(), 1) if PATTERN.search(line)]


def test_pattern_detects_bare_variable_before_korean():
    assert _offenders('echo "$NS의"') == [1]
    assert _offenders('echo "${NS}의 $NS"') == []


def test_no_bare_variable_before_korean_in_shell_scripts():
    found = [
        f"{path.relative_to(ROOT)}:{n}"
        for top in ("scripts", "scenarios")
        for path in (ROOT / top).rglob("*.sh")
        for n in _offenders(path.read_text(encoding="utf-8"))
    ]
    assert not found, f"$변수 뒤 한글은 ${{변수}}로 감싼다: {found}"
