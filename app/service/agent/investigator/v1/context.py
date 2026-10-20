"""프롬프트에 넣을 맥락: 지식(환경 설명·조사 지침), 신호판, 사전 확인 사실."""

import json
from pathlib import Path

from service.checks.detectors.catalog import CATALOG
from service.model import SignalBoard
from service.model.report import Fact

_VERDICT = {"detected": "적발", "observing": "관찰 중", "unknown": "확인하지 못함"}


def load_knowledge(knowledge_dir: Path) -> str:
    """환경 설명 + 조사 지침 전체 (기본 주입 방식, 상세설계 §4.1). 템플릿은 뺀다."""
    parts = [(knowledge_dir / "environment.md").read_text(encoding="utf-8").strip()]
    for path in sorted((knowledge_dir / "runbooks").glob("*.md")):
        if path.name.startswith("_"):
            continue
        parts.append(f"[지침: {path.stem}]\n{path.read_text(encoding='utf-8').strip()}")
    return "\n\n---\n\n".join(parts)


def render_board(board: SignalBoard) -> str:
    """서비스별로 적발·관찰 중·확인하지 못함 신호만. 통과는 뺀다."""
    lines = []
    for service, signals in sorted(board.by_service.items()):
        shown = [s for s in signals if s.verdict in _VERDICT]
        if not shown:
            continue
        lines.append(f"[{service}]")
        for s in shown:
            label = _VERDICT[s.verdict] + (f"({s.severity})" if s.severity else "")
            rule = CATALOG[s.rule_id]
            line = f"- {s.rule_id} {rule.name} · {label} · {s.subject}"
            if s.evidence_id:
                line += f" [{s.evidence_id}]"
            if s.values:
                line += " · " + json.dumps(s.values, ensure_ascii=False, sort_keys=True)
            lines.append(line)
    return "\n".join(lines) or "(적발·관찰 중인 신호 없음)"


def render_facts(facts: list[Fact]) -> str:
    return "\n".join(f"- {f.statement} [{', '.join(f.evidence_ids)}]" for f in facts) or "(없음)"
