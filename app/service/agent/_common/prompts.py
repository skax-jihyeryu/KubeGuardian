"""프롬프트 로더. 프롬프트는 버전 폴더의 prompts/<이름>.md에 둔다.

자리표시자는 {name} 형식이다. 본문에 중괄호를 그대로 쓰려면 {{ }}로 적는다.
"""

from pathlib import Path


def load_prompt(prompts_dir: Path, name: str, **values: str) -> str:
    template = (prompts_dir / f"{name}.md").read_text(encoding="utf-8")
    return template.format_map(_Strict(values))


class _Strict(dict):
    def __missing__(self, key: str) -> str:
        raise KeyError(f"프롬프트 자리표시자에 값이 없음: {key}")
