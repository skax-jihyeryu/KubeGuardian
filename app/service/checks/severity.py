"""저장된 점검의 시점별 심각도 판정."""

from sqlalchemy import Integer, case, cast, exists, func, select, true

from core.db.tables import CheckResultRow, CheckRunRow, IncidentCheckRow, InvestigationRow
from service.checks.detectors.catalog import CATALOG

CHECK_SEVERITIES = {
    4: ("crit", "긴급"),
    3: ("warn", "주의"),
    2: ("risk", "잠재 위험"),
    1: ("tip", "개선 권고"),
}


def check_severity_rank():
    first_rank = {"critical": 4, "warning": 3, "info": 2}
    impact_rank = {"core_path_broken": 4, "partial_users": 3, "latent_risk": 2, "improvement": 1}
    latest_investigation = (
        select(InvestigationRow.id)
        .where(InvestigationRow.check_run_id == CheckRunRow.id)
        .order_by(InvestigationRow.id.desc())
        .limit(1)
        .correlate(CheckRunRow)
        .scalar_subquery()
    )
    reported_issues = func.json_each(InvestigationRow.report, "$.issues").table_valued("value")
    resolved = exists(
        select(1)
        .where(
            IncidentCheckRow.check_run_id == CheckRunRow.id,
            IncidentCheckRow.incident_id == InvestigationRow.incident_id,
            IncidentCheckRow.action == "resolved",
        )
        .correlate(CheckRunRow, InvestigationRow)
    )
    # 점검 상세처럼 이 점검의 최신 조사·1순위 문제만 사용한다.
    # 이슈 행의 최종 심각도는 나중 재조사 때 바뀌므로 과거 점검에 사용하지 않는다.
    report_rank = (
        select(case(impact_rank, value=func.json_extract(reported_issues.c.value, "$.impact")))
        .select_from(InvestigationRow)
        .join(reported_issues, true())
        .where(
            InvestigationRow.id == latest_investigation,
            ~resolved,
            func.json_type(InvestigationRow.report, "$.conclusion") == "text",
            func.json_type(InvestigationRow.report, "$.impact_scope") == "text",
            func.json_extract(InvestigationRow.report, "$.confidence").in_(
                ["high", "medium", "low"]
            ),
        )
        .order_by(cast(func.json_extract(reported_issues.c.value, "$.rank"), Integer))
        .limit(1)
        .correlate(CheckRunRow)
        .scalar_subquery()
    )
    rules = func.json_each(CheckResultRow.verdicts).table_valued("value").alias("rules")
    items = func.json_each(func.json_extract(rules.c.value, "$.items")).table_valued("value")
    advice_exists = exists(
        select(1)
        .select_from(rules.join(items, true()))
        .where(
            func.json_extract(rules.c.value, "$.rule_id").in_(
                [rid for rid, spec in CATALOG.items() if spec.grade == "advice"]
            ),
            func.json_extract(items.c.value, "$.verdict") == "detected",
        )
        .correlate(CheckResultRow)
    )
    return func.coalesce(
        report_rank,
        case(
            first_rank, value=CheckResultRow.first_severity, else_=case((advice_exists, 1), else_=0)
        ),
    )
