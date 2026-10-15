"""프롬프트 로더. 프롬프트는 버전 폴더의 prompts/<이름>.md에 둔다.

자리표시자는 $name 형식이다 (string.Template). 보고서 예시 JSON의 중괄호는 그대로 쓴다.
"""

from pathlib import Path
from string import Template


def load_prompt(prompts_dir: Path | None, name: str, **values: str) -> str:
    if prompts_dir is None:
        raise ValueError("AgentSpec.prompts_dir가 없습니다")
    template = Template((prompts_dir / f"{name}.md").read_text(encoding="utf-8"))
    try:
        return template.substitute(values)
    except KeyError as e:
        raise KeyError(f"프롬프트 자리표시자에 값이 없음: {e.args[0]}") from e
