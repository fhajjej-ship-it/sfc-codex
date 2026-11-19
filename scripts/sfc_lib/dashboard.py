"""Default dashboard presentation, separate from canonical routing names."""
from .common import STATUSES
from .activity import FIELD_NAMES, activity_fields

VIEW_NAME = "🏭 Factory"
STAGE_STYLE = {
    "Backlog": ("GRAY", "📥 Ideas and work waiting to be shaped."),
    "Ready": ("BLUE", "🚀 Scoped, unblocked and ready for Codex."),
    "Building": ("ORANGE", "🛠️ Implementation in an isolated worktree."),
    "QA": ("YELLOW", "🧪 Tests, acceptance checks and security verification."),
    "Review": ("PURPLE", "🔍 Independent review before human merge."),
    "Blocked": ("RED", "🚧 A decision, dependency or fix is needed."),
    "Done": ("GREEN", "✅ Completed with verification evidence."),
}

DASHBOARD_QUERY = """query($project:ID!, $field:ID!, $fieldsAfter:String, $viewsAfter:String) {
 project:node(id:$project) { ... on ProjectV2 { url
 fields(first:100,after:$fieldsAfter) { pageInfo { hasNextPage endCursor } nodes {
 ... on ProjectV2Field { id name dataType }
 ... on ProjectV2SingleSelectField { id name dataType }
 ... on ProjectV2IterationField { id name dataType }
 ... on ProjectV2MultiSelectField { id name dataType }
 } }
 views(first:100,after:$viewsAfter) {
 pageInfo { hasNextPage endCursor } nodes { id number name layout filter
 configuration { visibleFields(first:100) { pageInfo { hasNextPage endCursor } nodes {
 ... on ProjectV2Field { id } ... on ProjectV2SingleSelectField { id }
 ... on ProjectV2IterationField { id }
 ... on ProjectV2MultiSelectField { id }
 } } }
 verticalGroupByFields(first:100) { pageInfo { hasNextPage endCursor } nodes {
 ... on ProjectV2Field { id } ... on ProjectV2SingleSelectField { id }
 ... on ProjectV2IterationField { id }
 ... on ProjectV2MultiSelectField { id }
 } } } } } }
 field:node(id:$field) { ... on ProjectV2SingleSelectField {
 id name options { id name color description }
 } }
}"""


def styled_options(options):
    """Preserve IDs, all existing options and their ordering."""
    result = []
    for option in options:
        item = {key: option[key] for key in ("id", "name", "color", "description")}
        if item["name"] in STAGE_STYLE:
            item["color"], item["description"] = STAGE_STYLE[item["name"]]
        result.append(item)
    return result


def assess_dashboard(field, view, project_url, fields):
    options = {option["name"]: option for option in field["options"]}
    style_ready = all(name in options and
                      (options[name]["color"], options[name]["description"]) == STAGE_STYLE[name]
                      for name in STATUSES)
    columns = [option["name"] for option in field["options"] if option["name"] in STATUSES]
    grouped = view and [item.get("id") for item in view["verticalGroupByFields"]["nodes"]] == [field["id"]]
    checks = {
        "stage_style": style_ready,
        "stage_order": columns == list(STATUSES),
        "factory_view": bool(view and view["name"] == VIEW_NAME and view["layout"] == "BOARD_LAYOUT"),
        "saved_columns": bool(grouped),
        "activity_fields": len(activity_fields(fields)) == len(FIELD_NAMES),
        "saved_activity_fields": bool(view and set(activity_fields(fields).values()) <=
                                      {f["id"] for f in view["configuration"]["visibleFields"]["nodes"]}
                                      and len(activity_fields(fields)) == len(FIELD_NAMES)),
    }
    url = f"{project_url}/views/{view['number']}" if view else project_url
    pending = []
    if not checks["activity_fields"] or not checks["saved_activity_fields"]:
        pending.append("Run dashboard --apply to add SFC Agent and SFC Activity text fields to the saved Factory cards.")
    if not checks["stage_style"] or not checks["factory_view"]:
        pending.append("Apply the default presentation with dashboard --apply.")
    if not checks["stage_order"]:
        pending.append("Order the existing stages as Backlog, Ready, Building, QA, Review, Blocked, Done; preserve option IDs.")
    if not checks["saved_columns"]:
        pending.append(f"Open {url}, choose View > Column by > {field['name']}, Save view, and confirm the shared-view save.")
    return {"ready": all(checks.values()), "checks": checks, "url": url,
            "view_id": view["id"] if view else None, "view_name": view["name"] if view else None,
            "status_field": field["name"], "columns": columns, "pending": pending}
