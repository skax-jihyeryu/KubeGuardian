#!/usr/bin/env bash
# GitHub Project #2 조작 도우미. 스킬(week-plan, daily-check, issue-create, issue-work)이 공용으로 쓴다.
#
#   bash .claude/skills/_shared/board.sh add <이슈#> [Status] [P0|P1|P2] [Estimate]
#   bash .claude/skills/_shared/board.sh status <이슈#> <Backlog|Ready|In progress|In review|Done>
#   bash .claude/skills/_shared/board.sh priority <이슈#> <P0|P1|P2>
#   bash .claude/skills/_shared/board.sh estimate <이슈#> <시간>
#   bash .claude/skills/_shared/board.sh actual-add <이슈#> <시간>     # 기존 Actual에 더한다
#   bash .claude/skills/_shared/board.sh list [마일스톤 접두사, 예: W1]  # 탭 구분: #, 상태, 우선순위, 예상, 실제, 마일스톤, 제목
#   bash .claude/skills/_shared/board.sh week [날짜 YYYY-MM-DD]         # 그 날짜의 주차(W1~W5) 출력
set -euo pipefail

OWNER=skax-jihyeryu
NUM=2
REPO=skax-jihyeryu/KubeGuardian
PROJECT_ID=PVT_kwHODcryEs4Bm8T3
F_STATUS=PVTSSF_lAHODcryEs4Bm8T3zhljLvo
F_PRIORITY=PVTSSF_lAHODcryEs4Bm8T3zhljMO8
F_ESTIMATE=PVTF_lAHODcryEs4Bm8T3zhljMPE
F_ACTUAL=PVTF_lAHODcryEs4Bm8T3zhlkESA
W1_START=2026-10-12

status_opt() {
  case "$1" in
    Backlog) echo f75ad846 ;; Ready) echo 08afe404 ;; "In progress") echo 47fc9ee4 ;;
    "In review") echo 4cc61d42 ;; Done) echo 98236657 ;;
    *) echo "알 수 없는 Status: $1" >&2; exit 1 ;;
  esac
}
priority_opt() {
  case "$1" in
    P0) echo 79628723 ;; P1) echo 0a877460 ;; P2) echo da944a9c ;;
    *) echo "알 수 없는 Priority: $1" >&2; exit 1 ;;
  esac
}

# 이슈를 Project에 넣고 item id를 돌려준다. 이미 들어 있으면 기존 item id가 나온다.
item_id() {
  gh project item-add "$NUM" --owner "$OWNER" --url "https://github.com/$REPO/issues/$1" --format json --jq .id
}
set_select() { gh project item-edit --project-id "$PROJECT_ID" --id "$1" --field-id "$2" --single-select-option-id "$3" >/dev/null; }
set_number() { gh project item-edit --project-id "$PROJECT_ID" --id "$1" --field-id "$2" --number "$3" >/dev/null; }

cmd=${1:-}; shift || true
case "$cmd" in
  add)
    id=$(item_id "$1")
    [ -n "${2:-}" ] && set_select "$id" "$F_STATUS" "$(status_opt "$2")"
    [ -n "${3:-}" ] && set_select "$id" "$F_PRIORITY" "$(priority_opt "$3")"
    [ -n "${4:-}" ] && set_number "$id" "$F_ESTIMATE" "$4"
    echo "#$1 → $id" ;;
  status)   set_select "$(item_id "$1")" "$F_STATUS" "$(status_opt "$2")"; echo "#$1 Status=$2" ;;
  priority) set_select "$(item_id "$1")" "$F_PRIORITY" "$(priority_opt "$2")"; echo "#$1 Priority=$2" ;;
  estimate) set_number "$(item_id "$1")" "$F_ESTIMATE" "$2"; echo "#$1 Estimate=$2" ;;
  actual-add)
    id=$(item_id "$1")
    cur=$(gh api graphql -f id="$id" -f query='query($id:ID!){node(id:$id){... on ProjectV2Item{fieldValueByName(name:"Actual"){... on ProjectV2ItemFieldNumberValue{number}}}}}' \
      --jq '.data.node.fieldValueByName.number // 0')
    new=$(python3 -c "print(round($cur + $2, 2))")
    set_number "$id" "$F_ACTUAL" "$new"; echo "#$1 Actual=$cur+$2=$new" ;;
  list)
    gh project item-list "$NUM" --owner "$OWNER" --limit 300 --format json | python3 -c '
import json, sys
prefix = sys.argv[1]
for it in json.load(sys.stdin)["items"]:
    c = it.get("content", {})
    ms = (it.get("milestone") or {}).get("title", "")
    if c.get("type") != "Issue" or (prefix and not ms.startswith(prefix)):
        continue
    print("\t".join(str(x) for x in [
        "#%s" % c.get("number"), it.get("status", "-"), it.get("priority", "-"),
        it.get("estimate", "-"), it.get("actual", "-"), ms.split(" ")[0] or "-", c.get("title")]))
' "${1:-}" ;;
  week)
    python3 -c "
import datetime as d, sys
day = d.date.fromisoformat(sys.argv[1]) if sys.argv[1] else d.date.today()
n = (day - d.date.fromisoformat('$W1_START')).days // 7 + 1
print(f'W{n}' if 1 <= n <= 5 else f'범위 밖({n})')
" "${1:-}" ;;
  *) sed -n '2,11p' "$0"; exit 1 ;;
esac
