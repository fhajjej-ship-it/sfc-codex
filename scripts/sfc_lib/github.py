from __future__ import annotations

import json
import os
import re
from datetime import datetime
from urllib.parse import urlparse
from .common import Halt, STATUSES, git, run
from .dashboard import DASHBOARD_QUERY, VIEW_NAME, assess_dashboard, styled_options
from .activity import FIELD_NAMES, activity_fields

ITEMS_QUERY = """query($id:ID!, $after:String) {
 node(id:$id) { ... on ProjectV2 { items(first:100,after:$after) {
 pageInfo { hasNextPage endCursor }
 nodes { id fieldValues(first:100) { nodes {
 ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2SingleSelectField { name } } }
 } } content { ... on Issue {
 id number title body url state repository { nameWithOwner }
 labels(first:100) { nodes { name } totalCount }
 } } } } } } }"""

LOCAL_MERGE_QUERY = """query($owner:String!, $name:String!, $number:Int!,
 $issue:Int!, $base:String!, $head:String!) {
 repository(owner:$owner, name:$name) {
 id nameWithOwner isArchived viewerPermission
 base:ref(qualifiedName:$base) { ...MergeRef }
 head:ref(qualifiedName:$head) { ...MergeRef }
 pullRequest(number:$number) {
 id number url state isDraft headRefName headRefOid baseRefName baseRefOid mergedAt
 headRepository { id nameWithOwner } baseRepository { id nameWithOwner }
 mergeCommit { oid }
 closingIssuesReferences(first:100) { totalCount nodes { id } }
 }
 issue(number:$issue) { id number url state repository { nameWithOwner } }
 }
}
fragment MergeRef on Ref {
 id name prefix target { __typename oid ... on Commit { tree { oid } } }
 branchProtectionRule { id }
 rules(first:1) { totalCount }
}"""

LOCAL_MERGE_CAPABILITIES = """query {
 mutation:__type(name:"Mutation") { fields { name args { name } } }
 update:__type(name:"RefUpdate") { inputFields { name } }
 input:__type(name:"UpdateRefsInput") { inputFields { name } }
}"""

ATOMIC_MERGE = """mutation($input:UpdateRefsInput!) {
 updateRefs(input:$input) { clientMutationId }
}"""


class GitHub:
    def __init__(self, root, config):
        self.root, self.c = root, config
        self.project = self.c["project"]
        self._meta = None
        self.origin = self.origin_url(root)
        self.env = {**os.environ, "GH_HOST": urlparse(self.origin).hostname}
        self.env.pop("GH_REPO", None)

    @staticmethod
    def origin_url(root):
        origin = git(root, "remote", "get-url", "origin")
        # Normalize scp-style SSH URLs for gh's URL parser. Never rely on GH_REPO
        # or gh's selected default when git fetch/push operate on origin.
        ssh = re.fullmatch(r"git@([^:]+):(.+)", origin)
        if ssh:
            origin = f"https://{ssh[1]}/{ssh[2]}"
        parsed = urlparse(origin)
        if parsed.scheme not in ("https", "http", "ssh") or not parsed.hostname:
            raise Halt("origin must be a GitHub HTTPS or SSH repository URL")
        return origin

    def call(self, *args, input=None):
        from .rate_limit import read_only
        gate = getattr(self, 'read_wait', None)
        if gate and read_only(args, input):
            return gate.call(lambda: self._call(args, input, read=True), lambda: self.rate_headers(args))
        return self._call(args, input)

    def rate_headers(self, args):
        from .rate_limit import response, limited
        rest = args[0] == 'api' and args[1] != 'graphql'
        if rest:
            result = run(['gh', 'api', 'rate_limit', '--include'], self.root, env=self.env, check=False)
        else:
            # One read-only diagnostic when gh view/pagination hides HTTP headers.
            result = run(['gh', 'api', 'graphql', '--include', '--input', '-'], self.root,
                         env=self.env, input=json.dumps({'query': 'query { rateLimit { remaining resetAt } }'}), check=False)
        headers, body = response(result.stdout)
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError('Expected a diagnostic object')
            errors = data.get('errors', [])
            if not isinstance(errors, list) or (errors and 'message' in data):
                raise ValueError('Malformed or mixed diagnostic errors')
            # Even after a confirmed quota response, a failed diagnostic must not
            # turn permission/transport uncertainty into fallback waiting.
            if result.returncode or data.get('errors') or 'message' in data:
                if not limited(data, result.stderr, bool(result.returncode)):
                    raise ValueError('Diagnostic failed without an unambiguous quota response')
                return headers  # A confirmed quota diagnostic may use bounded fallback.
            if result.stderr.strip():
                raise ValueError('Unexpected diagnostic stderr')
            if rest:
                bucket = 'search' if args[1].lstrip('/').startswith('search/') else 'core'
                resource = data['resources'][bucket]
                reset = resource['reset']
                if type(reset) is not int or reset < 0:
                    raise ValueError('Invalid reset timestamp')
                # /rate_limit headers describe core, not necessarily this bucket.
                headers = {k: v for k, v in headers.items() if k == 'retry-after'}
            else:
                resource = data['data']['rateLimit']
                stamp = resource['resetAt']
                if not isinstance(stamp, str):
                    raise ValueError('Invalid reset timestamp')
                reset_at = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                if reset_at.tzinfo is None:
                    raise ValueError('Reset timestamp needs a timezone')
                reset = reset_at.timestamp()
            if type(resource['remaining']) is not int or resource['remaining'] < 0:
                raise ValueError('Invalid remaining quota')
            if resource['remaining'] == 0:
                headers.update({'x-ratelimit-remaining': '0', 'x-ratelimit-reset': str(reset)})
            return headers
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise Halt('Invalid or failed GitHub rate-limit diagnostic; no automatic retry') from exc

    def _call(self, args, input, *, read=False):
        from .rate_limit import (ConnectionReset, ReadConnectionReset, RateLimited,
                                 api_response, connection_reset, limited, quota_stderr)
        argv = ["gh", *map(str, args)]
        include = read and args[0] == 'api'
        if include:
            argv.append('--include')
        result = run(argv, self.root, env=self.env, input=input, check=False)
        body = result.stdout
        if result.returncode and not body.strip() and connection_reset(result.stderr):
            error = ReadConnectionReset if read else ConnectionReset
            raise error('GitHub connection reset before a response was confirmed')
        if result.returncode and not read:
            raise Halt(f'gh failed ({result.returncode}): {result.stderr[-2000:]}')
        # Non-API gh commands can hide the body. Only a complete, standalone
        # quota message may enter diagnostic recovery; JSON null/malformed JSON
        # and API responses must never use stderr to replace invalid evidence.
        if (read and args[0] != 'api' and not body.strip() and result.returncode
                and quota_stderr(result.stderr)):
            raise RateLimited({})
        try:
            if include:
                data, pages = api_response(body, slurp='--slurp' in args, paginate='--paginate' in args)
            else:
                data = json.loads(body) if body.strip() else None
                pages = [({}, data)]
        except ValueError as exc:
            raise Halt("GitHub returned invalid JSON; no automatic retry") from exc
        quotas = [(headers, page) for headers, page in pages
                  if read and limited(page, result.stderr, bool(result.returncode))]
        if quotas:
            # gh can fail on a later page after printing successful pages. Only
            # a final unambiguous quota page may authorize retrying the read.
            if (len(quotas) != 1 or pages[-1] != quotas[0] or any(
                    not isinstance(page, (dict, list)) or
                    isinstance(page, dict) and ('message' in page or page.get('errors'))
                    for _, page in pages[:-1])):
                raise Halt('Mixed GitHub paginated errors; no automatic retry')
            raise RateLimited(quotas[0][0])
        if result.returncode:
            raise Halt(f'gh failed ({result.returncode}): {result.stderr[-2000:]}')
        if any(isinstance(page, dict) and page.get('errors') for _, page in pages):
            raise Halt("GitHub GraphQL read/write failed; inspect remote state before retry")
        return data

    def graphql(self, query, variables):
        return self.call("api", "graphql", "--input", "-",
                         input=json.dumps({"query": query, "variables": variables}))["data"]

    def project_id(self):
        p = self.project
        project = self.call("project", "view", p["number"], "--owner", p["owner"], "--format", "json")
        project_id = project.get("id") if isinstance(project, dict) else None
        if not isinstance(project_id, str) or not project_id.strip():
            raise Halt("GitHub Project identity unavailable")
        return project_id

    def routing_inventory(self, project_id):
        """Batch fresh release routing reads for a canonical GitHub origin."""
        from .priority import FIELD_SELECTION, inventory

        origin = urlparse(self.origin)
        slug = origin.path.strip('/').removesuffix('.git')
        project = self.project
        if (origin.netloc.lower() not in ('github.com', 'git@github.com') or
                origin.params or origin.query or origin.fragment or slug.lower() != self.c['repo'].lower() or
                slug.count('/') != 1 or not re.fullmatch(r'[A-Za-z0-9-]+', project['owner'])):
            return None  # Preserve gh's origin resolution for other URL forms.
        owner, repo = slug.split('/')
        query = """query($owner:String!, $repo:String!, $login:String!, $number:Int!, $after:String) {
          repository(owner:$owner,name:$repo) { nameWithOwner }
          repositoryOwner(login:$login) {
            login
            ... on User { projectV2(number:$number) { ...RoutingFields } }
            ... on Organization { projectV2(number:$number) { ...RoutingFields } }
          }
        } fragment RoutingFields on ProjectV2 { id number """ + FIELD_SELECTION + ' }'
        data = self.graphql(query, {'owner': owner, 'repo': repo, 'login': project['owner'],
                                    'number': project['number'], 'after': None})
        repository = (data.get('repository') or {}).get('nameWithOwner')
        if not isinstance(repository, str) or repository.lower() != self.c['repo'].lower():
            raise Halt("origin repository does not match the configured GitHub repository")
        project_owner = data.get('repositoryOwner') or {}
        live = project_owner.get('projectV2') or {}
        login = project_owner.get('login')
        if (not isinstance(login, str) or login.lower() != project['owner'].lower() or
                live.get('id') != project_id or live.get('number') != project['number']):
            raise Halt('Configured Project identity changed; inspect before dispatch')
        if not isinstance(live.get('fields'), dict):
            raise Halt('Release Project field inventory unavailable')
        return inventory(self, project_id, first_page=live['fields'])

    def onboarding_identity(self):
        """Read origin and Project node IDs independently of the routing field."""
        origin = self.origin_url(self.root)
        if origin != self.origin:
            raise Halt("origin changed during onboarding; inspect and preview again")
        remote = self.call("repo", "view", origin, "--json", "id,nameWithOwner")
        if not isinstance(remote, dict) or not all(isinstance(remote.get(k), str) and remote[k].strip()
                                                   for k in ("id", "nameWithOwner")):
            raise Halt("GitHub repository identity unavailable")
        if remote["nameWithOwner"].lower() != self.c["repo"].lower():
            raise Halt("origin repository does not match the configured GitHub repository")
        return {"host": urlparse(origin).hostname, "repository": remote["nameWithOwner"],
                "repository_id": remote["id"], "project_id": self.project_id()}

    def metadata(self, create=False, *, project_id=None):
        if self._meta and project_id is None:
            return self._meta
        p = self.project
        target = project_id or self.project_id()
        from .priority import inventory
        fields = [v for v in inventory(self, target) if v["name"] == self.c["status_field"]]
        if not fields and create:
            if project_id is None:
                self.call("project", "field-create", p["number"], "--owner", p["owner"],
                          "--name", self.c["status_field"], "--data-type", "SINGLE_SELECT",
                          "--single-select-options", ",".join(STATUSES), "--format", "json")
                return self.metadata()
            result = self.graphql("""mutation($input:CreateProjectV2FieldInput!) {
              createProjectV2Field(input:$input) { projectV2Field {
                ... on ProjectV2SingleSelectField { id }
              } }
            }""", {"input": {"projectId": project_id, "name": self.c["status_field"], "dataType": "SINGLE_SELECT",
                            "singleSelectOptions": [{"name": name, "color": "GRAY", "description": ""}
                                                    for name in STATUSES]}})
            made = result["createProjectV2Field"]["projectV2Field"]["id"]
            metadata = self.metadata(project_id=project_id)
            if not made or metadata["field"] != made:
                raise Halt("Routing field creation not verified; inspect remote state before repair")
            return metadata
        if len(fields) != 1:
            raise Halt(f"Need exactly one project field named {self.c['status_field']!r}")
        field = fields[0]
        if len({v["name"] for v in field.get("options", [])}) != len(field.get("options", [])):
            raise Halt("Duplicate status option names; resolve ambiguity before changing the dashboard")
        options = {v["name"]: v["id"] for v in field.get("options", [])}
        if set(STATUSES) - options.keys():
            raise Halt("Project status options missing: " + ", ".join(sorted(set(STATUSES) - options.keys())))
        self._meta = {"id": target, "field": field["id"], "options": options}
        return self._meta

    def dashboard_data(self):
        metadata = self.metadata()
        data = self.graphql(DASHBOARD_QUERY, {"project": metadata["id"], "field": metadata["field"]})
        for name, variable in (("fields", "fieldsAfter"), ("views", "viewsAfter")):
            page = data["project"][name]
            nodes, cursors = list(page["nodes"]), set()
            while page["pageInfo"]["hasNextPage"]:
                cursor = page["pageInfo"].get("endCursor")
                if not cursor or cursor in cursors:
                    raise Halt("Dashboard inventory is truncated or pagination did not advance")
                cursors.add(cursor)
                page = self.graphql(DASHBOARD_QUERY, {"project": metadata["id"], "field": metadata["field"],
                                                     variable: cursor})["project"][name]
                nodes.extend(page["nodes"])
            if len({n["id"] for n in nodes}) != len(nodes):
                raise Halt("Duplicate dashboard inventory IDs; no changes attempted")
            data["project"][name] = {"nodes": nodes, "pageInfo": {"hasNextPage": False}}
        for view in data["project"]["views"]["nodes"]:
            for name in ("visibleFields", "verticalGroupByFields"):
                owner = view["configuration"] if name == "visibleFields" else view
                page = owner[name]
                nodes, cursors = list(page["nodes"]), set()
                while page.get("pageInfo", {}).get("hasNextPage"):
                    cursor = page["pageInfo"].get("endCursor")
                    if not cursor or cursor in cursors:
                        raise Halt("Dashboard field inventory is truncated or pagination did not advance")
                    cursors.add(cursor)
                    selection = name + """(first:100,after:$after) {
                      pageInfo { hasNextPage endCursor } nodes {
                        ... on ProjectV2Field { id } ... on ProjectV2SingleSelectField { id }
                        ... on ProjectV2IterationField { id } ... on ProjectV2MultiSelectField { id }
                      }
                    }"""
                    if name == "visibleFields":
                        selection = "configuration { " + selection + " }"
                    result = self.graphql("query($view:ID!, $after:String) { node(id:$view) { ... on ProjectV2View { " +
                                          selection + " } } }", {"view": view["id"], "after": cursor})["node"]
                    page = (result["configuration"] if name == "visibleFields" else result)[name]
                    nodes.extend(page["nodes"])
                if len({n["id"] for n in nodes}) != len(nodes):
                    raise Halt("Duplicate saved view field IDs")
                owner[name] = {"nodes": nodes, "pageInfo": {"hasNextPage": False}}
        return data

    @staticmethod
    def factory_view(data):
        views = [view for view in data["project"]["views"]["nodes"] if view["name"] == VIEW_NAME]
        if len(views) > 1:
            raise Halt("Multiple Factory views; select one explicitly before changing the dashboard")
        return views[0] if views else None

    def priority_setup(self, apply=False):
        from .priority import setup
        return setup(self, apply=apply)

    def type_setup(self, apply=False):
        from .work_types import setup
        return setup(self, apply=apply)

    def dashboard(self):
        data = self.dashboard_data()
        report = assess_dashboard(data["field"], self.factory_view(data), data["project"]["url"],
                                  data["project"]["fields"]["nodes"])
        report["priority"] = self.priority_setup()
        report["ready"] = report["ready"] and report["priority"]["ready"]
        report["work_types"] = self.type_setup()
        report["ready"] = report["ready"] and report["work_types"]["ready"]
        return report

    def configure_dashboard(self):
        """Explicit onboarding/repair writes only; repeated matching setup is read-only."""
        self.priority_setup()  # Refuse ambiguous conventions before presentation writes.
        self.type_setup()  # Discover labels/views and reject conflicts before writes.
        data = self.dashboard_data()
        view = self.factory_view(data)  # Refuse ambiguity before any write.
        fields = activity_fields(data["project"]["fields"]["nodes"])
        field = data["field"]
        options = styled_options(field["options"])
        if options != field["options"]:
            result = self.graphql("""mutation($input:UpdateProjectV2FieldInput!) {
              updateProjectV2Field(input:$input) { projectV2Field {
                ... on ProjectV2SingleSelectField { id options { id name color description } }
              } }
            }""", {"input": {"fieldId": field["id"], "singleSelectOptions": options}})
            changed = result["updateProjectV2Field"]["projectV2Field"]
            if changed["id"] != field["id"] or changed["options"] != options:
                raise Halt("Dashboard style write did not preserve the requested option IDs/values; inspect before retrying")
        if view is None:
            # Even "View 1" may contain a user's saved presentation. Create a
            # dedicated Factory view rather than guessing that it is disposable.
            self.graphql("""mutation($input:CreateProjectV2ViewInput!) {
              createProjectV2View(input:$input) { projectV2View { id } }
            }""", {"input": {"projectId": self.metadata()["id"], "name": VIEW_NAME, "layout": "BOARD_LAYOUT"}})
        elif view["layout"] != "BOARD_LAYOUT":
            self.graphql("""mutation($input:UpdateProjectV2ViewInput!) {
              updateProjectV2View(input:$input) { projectV2View { id } }
            }""", {"input": {"viewId": view["id"], "layout": "BOARD_LAYOUT"}})
        for name in FIELD_NAMES:
            if name not in fields:
                created = self.graphql("""mutation($input:CreateProjectV2FieldInput!) {
                  createProjectV2Field(input:$input) { projectV2Field {
                    ... on ProjectV2Field { id name dataType }
                  } }
                }""", {"input": {"projectId": self.metadata()["id"], "name": name, "dataType": "TEXT"}})
                made = created["createProjectV2Field"]["projectV2Field"]
                if made.get("name") != name or made.get("dataType") != "TEXT" or not made.get("id"):
                    raise Halt("Activity field creation not verified; inspect before retry")
                fields[name] = made["id"]
        fresh = self.dashboard_data()
        if activity_fields(fresh["project"]["fields"]["nodes"]) != fields:
            raise Halt("Activity fields changed during setup; inspect before retry")
        view = self.factory_view(fresh)
        visible = [f["id"] for f in view["configuration"]["visibleFields"]["nodes"]]
        wanted = visible + [fields[name] for name in FIELD_NAMES if fields[name] not in visible]
        if wanted != visible:
            self.graphql("""mutation($input:UpdateProjectV2ViewInput!) {
              updateProjectV2View(input:$input) { projectV2View { id } }
            }""", {"input": {"viewId": view["id"], "configuration": {"visibleFieldIds": wanted}}})
        final = self.dashboard_data()
        saved = self.factory_view(final)
        if (not saved or saved["id"] != view["id"] or
                [f["id"] for f in saved["configuration"]["visibleFields"]["nodes"]] != wanted or
                saved["filter"] != view["filter"] or
                saved["verticalGroupByFields"] != view["verticalGroupByFields"]):
            raise Halt("Saved activity visibility or view preservation not verified; inspect before retry")
        # GitHub's current public mutation exposes no column-by update. Read the
        # saved grouping and leave an explicit pending UI step until it matches.
        report = assess_dashboard(final["field"], saved, final["project"]["url"],
                                  final["project"]["fields"]["nodes"])
        report["priority"] = self.priority_setup(apply=True)
        report["ready"] = report["ready"] and report["priority"]["ready"]
        report["work_types"] = self.type_setup(apply=True)
        report["ready"] = report["ready"] and report["work_types"]["ready"]
        return report

    def activity_snapshot(self, card):
        """Read identity and only the two targeted text values before every write."""
        from .priority import inventory
        # Field identities/types must be fresh at both IO boundaries. Saved view
        # layout, filters and visibility are presentation, not write authority.
        fields = activity_fields(inventory(self))
        if len(fields) != len(FIELD_NAMES):
            raise Halt("Activity fields missing; explicitly upgrade with dashboard --apply")
        data = self.graphql("""query($item:ID!) {
          node(id:$item) { ... on ProjectV2Item {
            id project { id } content { ... on Issue { id number repository { nameWithOwner } } }
            fieldValues(first:100) { pageInfo { hasNextPage } nodes {
              ... on ProjectV2ItemFieldTextValue { text field { ... on ProjectV2Field { id } } }
            } }
          } }
        }""", {"item": card["item_id"]})["node"]
        project = self.metadata()["id"]
        if (not data or not data.get("content") or not data.get("project") or
                not card.get("id") or data["id"] != card["item_id"] or data["project"]["id"] != project or
                data["content"].get("id") != card["id"] or
                data["content"]["number"] != card["number"] or
                data["content"]["repository"]["nameWithOwner"] != self.c["repo"] or
                data["fieldValues"]["pageInfo"]["hasNextPage"]):
            raise Halt("Activity item identity/scope is missing, changed or truncated")
        text_values = [v for v in data["fieldValues"]["nodes"] if v.get("field")]
        values = {v["field"]["id"]: v["text"] for v in text_values}
        if len(values) != len(text_values) or any(not isinstance(value, str) for value in values.values()):
            raise Halt("Activity text values are ambiguous or malformed")
        return {"project": project, "fields": [fields[n] for n in FIELD_NAMES]}, [values.get(fields[n], "") for n in FIELD_NAMES]

    def write_activity(self, card, scope, values):
        # One request, two targeted updates. GraphQL is not transactional; the
        # caller persists intent and reads both back even after partial writes.
        result = self.graphql("""mutation($state:UpdateProjectV2ItemFieldValueInput!,
                                         $action:UpdateProjectV2ItemFieldValueInput!) {
          state:updateProjectV2ItemFieldValue(input:$state) { projectV2Item { id } }
          action:updateProjectV2ItemFieldValue(input:$action) { projectV2Item { id } }
        }""", {key: {"projectId": scope["project"], "itemId": card["item_id"],
                      "fieldId": field, "value": {"text": value}}
                 for key, field, value in zip(("state", "action"), scope["fields"], values)})
        if any(result[key]["projectV2Item"]["id"] != card["item_id"] for key in ("state", "action")):
            raise Halt("Activity mutation returned a different item; inspect before retry")

    def verify_repository(self):
        remote = self.call("repo", "view", self.origin, "--json", "nameWithOwner")["nameWithOwner"]
        if remote.lower() != self.c["repo"].lower():
            raise Halt("origin repository does not match the configured GitHub repository")

    def cards(self):
        metadata = self.metadata()
        after, result = None, []
        while True:
            args = ["api", "graphql", "-f", "query=" + ITEMS_QUERY, "-f", "id=" + metadata["id"]]
            if after:
                args += ["-f", "after=" + after]
            data = self.call(*args)["data"]["node"]["items"]
            for item in data["nodes"]:
                issue = item.get("content") or {}
                if not issue.get("number") or issue.get("repository", {}).get("nameWithOwner") != self.c["repo"]:
                    continue
                if issue["labels"]["totalCount"] > len(issue["labels"]["nodes"]):
                    raise Halt("Issue labels were truncated; refusing incomplete policy input")
                status = next((v["name"] for v in item["fieldValues"]["nodes"]
                               if v.get("field", {}).get("name") == self.c["status_field"]), "Backlog")
                result.append({**issue, "item_id": item["id"], "status": status,
                               "labels": [v["name"] for v in issue["labels"]["nodes"]]})
            if not data["pageInfo"]["hasNextPage"]:
                return result
            cursor = data["pageInfo"]["endCursor"]
            if not cursor or cursor == after:
                raise Halt("Project pagination did not advance")
            after = cursor

    def move(self, card, status):
        m = self.metadata()
        self.call("project", "item-edit", "--id", card["item_id"], "--project-id", m["id"],
                  "--field-id", m["field"], "--single-select-option-id", m["options"][status])
        card["status"] = status

    def done_status_snapshot(self, card):
        """Resolve GitHub's standard Status field for a ticket already in SFC Done."""
        from .priority import inventory, item_snapshot, validate_routing

        m = self.metadata()
        fields = inventory(self, m["id"])
        validate_routing(fields, self.c, m)
        candidates = [field for field in fields if field["name"] == "Status" and field["id"] != m["field"]]
        if not candidates:
            return None
        if len(candidates) != 1:
            raise Halt("GitHub Status field is ambiguous")
        field = candidates[0]
        choices = field.get("options", [])
        options = {choice["name"]: choice["id"] for choice in choices}
        if (field.get("dataType") != "SINGLE_SELECT" or len(choices) != 3 or
                set(options) != {"Todo", "In Progress", "Done"}):
            return None  # A custom field has its own workflow.
        if len(set(options.values())) != 3 or any(not value for value in options.values()):
            raise Halt("GitHub Status option identities are ambiguous")
        item = item_snapshot(self, card["item_id"])
        content, values = item["content"], item["values"]
        if (item["project"]["id"] != m["id"] or item["id"] != card["item_id"] or
                content["repository"]["nameWithOwner"] != self.c["repo"] or
                any(content.get(key) != card.get(key) for key in ("id", "number", "title", "body", "url")) or
                content.get("state") not in ("OPEN", "CLOSED") or
                (card.get("state") == "CLOSED" and content["state"] != "CLOSED") or
                values.get(m["field"]) != m["options"]["Done"]):
            raise Halt("GitHub Status requires the same ticket still in SFC Done")
        current = values.get(field["id"])
        if current is not None and current not in options.values():
            raise Halt("GitHub Status value changed or is unknown")
        return {"project": m["id"], "item": item["id"], "field": field["id"], "options": options}, current

    def write_done_status(self, scope):
        self.call("project", "item-edit", "--id", scope["item"], "--project-id", scope["project"],
                  "--field-id", scope["field"], "--single-select-option-id", scope["options"]["Done"])

    def issue_state(self, number):
        return self.call("issue", "view", number, "--repo", self.c["repo"], "--json", "state")["state"]

    def comment(self, card, body_path):
        self.call("api", f"repos/{self.c['repo']}/issues/{card['number']}/comments", "--method", "POST",
                  "-F", "body=@" + str(body_path))

    def pull_requests(self, branch):
        return self.call("pr", "list", "--repo", self.c["repo"], "--head", branch,
                         "--state", "all", "--json", "number,url,state,baseRefName,headRefOid")

    def pr_state(self, number):
        """Read only the state used to decide whether to inspect an external merge."""
        pr = self.call("pr", "view", number, "--repo", self.c["repo"], "--json", "number,state")
        if (not isinstance(pr, dict) or type(pr.get("number")) is not int or pr["number"] != number or
                pr.get("state") not in ("OPEN", "CLOSED", "MERGED")):
            raise Halt("PR identity or state could not be verified")
        return pr["state"]

    def chat_closing_pr(self, issue):
        """Read the single GitHub-linked PR that actually closed this issue."""
        data = self.graphql("""query($issue:ID!) {
          node(id:$issue) { ... on Issue {
            id number state repository { nameWithOwner }
            closedByPullRequestsReferences(first:1,includeClosedPrs:true) {
              totalCount
              nodes { id number url state baseRefName headRefOid isDraft repository { nameWithOwner } }
            }
            timelineItems(last:1,itemTypes:[CLOSED_EVENT]) {
              nodes { ... on ClosedEvent { closer { ... on PullRequest { id } } } }
            }
          } }
        }""", {"issue": issue["id"]})["node"]
        if (not isinstance(data, dict) or data.get("id") != issue["id"]
                or data.get("number") != issue["number"]
                or data.get("repository") != {"nameWithOwner": self.c["repo"]}):
            raise Halt("GitHub closing pull request read did not identify the tracked issue")
        if data["state"] != "CLOSED":
            return None
        links = data["closedByPullRequestsReferences"]
        closures = data["timelineItems"]["nodes"]
        if links["totalCount"] != 1 or len(links["nodes"]) != 1 or len(closures) != 1:
            return None
        pr = links["nodes"][0]
        closer = (closures[0] or {}).get("closer") or {}
        if (not isinstance(pr, dict) or not pr.get("id")
                or closer.get("id") != pr["id"] or pr.get("state") != "MERGED"):
            return None
        return pr

    def chat_pr(self, number):
        """Read the linked PR and its checks without listing its changed files."""
        return self.call("pr", "view", number, "--repo", self.c["repo"], "--json",
                         "number,url,state,baseRefName,headRefOid,isDraft,statusCheckRollup")

    def pr(self, number):
        pr = self.call("pr", "view", number, "--repo", self.c["repo"], "--json",
                       "id,number,url,body,state,headRefOid,baseRefName,headRefName,isCrossRepository,mergeStateStatus,isDraft,statusCheckRollup,additions,deletions")
        pages = self.call("api", f"repos/{self.c['repo']}/pulls/{number}/files", "--paginate", "--slurp")
        pr["files"] = [{"path": item["filename"]} for page in pages for item in page]
        if len(pr["files"]) >= 3000:
            raise Halt("PR file inventory may be truncated; automatic assessment refused")
        return pr

    def create_pr(self, card, branch, body_path):
        # API returns JSON, so no parsing of display output or invented PR number.
        return self.call("api", f"repos/{self.c['repo']}/pulls", "--method", "POST",
                         "-f", "title=" + card["title"], "-f", "head=" + branch,
                         "-f", "base=" + self.c["base_branch"], "-F", "draft=true",
                         "-F", "body=@" + str(body_path))

    def ready(self, number):
        # gh pr ready prints prose; use run rather than the JSON adapter.
        run(["gh", "pr", "ready", str(number), "--repo", self.c["repo"]], self.root, env=self.env)

    def update_pr_body(self, number, body):
        self.call("api", f"repos/{self.c['repo']}/pulls/{number}", "--method", "PATCH",
                  "--input", "-", input=json.dumps({"body": body}))

    def merge(self, number, sha):
        run(["gh", "pr", "merge", str(number), "--repo", self.c["repo"],
             "--squash", "--match-head-commit", sha], self.root, env=self.env)

    def local_merge_capabilities(self):
        data = self.graphql(LOCAL_MERGE_CAPABILITIES, {})
        try:
            mutations = {f["name"]: {a["name"] for a in f["args"]}
                         for f in data["mutation"]["fields"]}
            supported = (mutations.get("updateRefs") == {"input"} and
                         {"name", "beforeOid", "afterOid", "force"} <=
                         {f["name"] for f in data["update"]["inputFields"]} and
                         {"repositoryId", "refUpdates", "clientMutationId"} <=
                         {f["name"] for f in data["input"]["inputFields"]})
        except (KeyError, TypeError):
            supported = False
        if not supported:
            raise Halt("Server lacks the required atomic updateRefs/beforeOid protocol; no fallback")

    def local_merge_snapshot(self, number, issue, base, head):
        owner, name = self.c["repo"].split("/")
        data = self.graphql(LOCAL_MERGE_QUERY,
                            {"owner": owner, "name": name, "number": number, "issue": issue,
                             "base": base, "head": head})
        if not isinstance(data, dict) or not isinstance(data.get("repository"), dict):
            raise Halt("Local merge repository snapshot unavailable")
        return data["repository"]

    def local_merge_checks(self, head):
        """Enumerate latest checks/status contexts; never trust a truncated rollup."""
        prefix = f"repos/{self.c['repo']}/commits/{head}"
        runs = self.call("api", prefix + "/check-runs?per_page=100&filter=latest", "--paginate", "--slurp")
        statuses = self.call("api", prefix + "/status?per_page=100", "--paginate", "--slurp")
        result = []
        try:
            for pages, key in ((runs, "check_runs"), (statuses, "statuses")):
                if not isinstance(pages, list) or not pages:
                    raise ValueError("missing check inventory")
                entries = [entry for page in pages for entry in page[key]]
                totals = {page["total_count"] for page in pages}
                identities = [e["id"] if key == "check_runs" else e["context"] for e in entries]
                if (totals != {len(entries)} or len(set(identities)) != len(entries) or
                        any(not value for value in identities)):
                    raise ValueError("incomplete or changing check inventory")
                if key == "check_runs":
                    for entry in entries:
                        if entry["head_sha"] != head:
                            raise ValueError("check belongs to another commit")
                        result.append({"status": entry["status"].upper(),
                                       "conclusion": (entry["conclusion"] or "").upper()})
                else:
                    if any(page["sha"] != head for page in pages):
                        raise ValueError("statuses belong to another commit")
                    result.extend({"state": entry["state"].upper()} for entry in entries)
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise Halt("GitHub check inventory missing, incomplete or changed; no merge") from exc
        return result

    def atomic_fast_forward(self, intent):
        # One server transaction, including a compare-only update of the head.
        # Never replace this with updateRef, gh pr merge or a non-atomic push.
        data = self.graphql(ATOMIC_MERGE, {"input": {
            "repositoryId": intent["repository_id"], "clientMutationId": intent["id"],
            "refUpdates": [
                {"name": intent["head_ref"], "beforeOid": intent["head"],
                 "afterOid": intent["head"], "force": False},
                {"name": intent["base_ref"], "beforeOid": intent["base"],
                 "afterOid": intent["head"], "force": False}]}})
        if (not isinstance(data, dict) or not isinstance(data.get("updateRefs"), dict) or
                data["updateRefs"].get("clientMutationId") != intent["id"]):
            raise Halt("Atomic update response missing or mismatched; reconcile without retry")
