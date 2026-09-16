#!/usr/bin/env python3
"""Keep the OMM roadmap project and its README aligned with repository issues."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Any, Protocol


DEFAULT_GRAPHQL_URL = "https://api.github.com/graphql"
REQUEST_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class IssueItem:
    content_id: str
    number: int
    title: str
    state: str
    repository: str
    project_item_id: str | None = None
    status: str | None = None


@dataclass(frozen=True)
class ProjectSnapshot:
    project_id: str
    readme: str
    status_field_id: str
    status_options: dict[str, str]
    items: tuple[IssueItem, ...]


@dataclass(frozen=True)
class SyncResult:
    added: int
    status_updated: int
    readme_updated: bool
    readme: str


class ProjectApi(Protocol):
    def fetch_project(self, owner: str, project_number: int) -> ProjectSnapshot: ...

    def fetch_open_issues(self, owner: str, repository: str) -> list[IssueItem]: ...

    def add_issue(self, project_id: str, content_id: str) -> str: ...

    def set_status(
        self,
        project_id: str,
        item_id: str,
        field_id: str,
        option_id: str,
    ) -> None: ...

    def update_readme(self, project_id: str, readme: str) -> None: ...


PROJECT_METADATA_QUERY = """
query($owner: String!, $number: Int!) {
  organization(login: $owner) {
    projectV2(number: $number) {
      id
      readme
      fields(first: 100) {
        nodes {
          __typename
          ... on ProjectV2SingleSelectField {
            id
            name
            options { id name }
          }
        }
      }
    }
  }
}
"""


PROJECT_ITEMS_QUERY = """
query($owner: String!, $number: Int!, $cursor: String) {
  organization(login: $owner) {
    projectV2(number: $number) {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          content {
            __typename
            ... on Issue {
              id
              number
              title
              state
              repository { nameWithOwner }
            }
          }
          fieldValues(first: 20) {
            nodes {
              __typename
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field {
                  ... on ProjectV2SingleSelectField { name }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


OPEN_ISSUES_QUERY = """
query($owner: String!, $repository: String!, $cursor: String) {
  repository(owner: $owner, name: $repository) {
    issues(
      first: 100,
      after: $cursor,
      states: OPEN,
      orderBy: {field: CREATED_AT, direction: ASC}
    ) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        number
        title
        state
        repository { nameWithOwner }
      }
    }
  }
}
"""


ADD_ITEM_MUTATION = """
mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) {
    item { id }
  }
}
"""


SET_STATUS_MUTATION = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(
    input: {
      projectId: $project,
      itemId: $item,
      fieldId: $field,
      value: {singleSelectOptionId: $option}
    }
  ) {
    projectV2Item { id }
  }
}
"""


UPDATE_README_MUTATION = """
mutation($project: ID!, $readme: String!) {
  updateProjectV2(input: {projectId: $project, readme: $readme}) {
    projectV2 { id }
  }
}
"""


class GraphQLProjectApi:
    def __init__(self, token: str, endpoint: str = DEFAULT_GRAPHQL_URL) -> None:
        self._token = token
        self._endpoint = endpoint

    def _execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "omm-project-readme-sync",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub GraphQL HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GitHub GraphQL request failed: {exc.reason}") from exc

        if payload.get("errors"):
            messages = "; ".join(
                str(error.get("message", "unknown GraphQL error"))
                for error in payload["errors"]
            )
            raise RuntimeError(f"GitHub GraphQL error: {messages}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("GitHub GraphQL response did not contain data")
        return data

    def fetch_project(self, owner: str, project_number: int) -> ProjectSnapshot:
        metadata = self._execute(
            PROJECT_METADATA_QUERY, {"owner": owner, "number": project_number}
        )
        organization = metadata.get("organization")
        project = organization and organization.get("projectV2")
        if not project:
            raise RuntimeError(f"Project {owner}#{project_number} was not found")

        status_fields = [
            node
            for node in project["fields"]["nodes"]
            if node
            and node.get("__typename") == "ProjectV2SingleSelectField"
            and node.get("name") == "Status"
        ]
        if len(status_fields) != 1:
            raise RuntimeError("Project must contain exactly one single-select Status field")
        status_field = status_fields[0]
        status_options = {
            option["name"]: option["id"] for option in status_field["options"]
        }
        missing = {"Todo", "Done"} - status_options.keys()
        if missing:
            raise RuntimeError(
                "Project Status field is missing required option(s): "
                + ", ".join(sorted(missing))
            )

        items: list[IssueItem] = []
        cursor: str | None = None
        while True:
            page = self._execute(
                PROJECT_ITEMS_QUERY,
                {"owner": owner, "number": project_number, "cursor": cursor},
            )
            page_organization = page.get("organization")
            page_project = page_organization and page_organization.get("projectV2")
            if not page_project:
                raise RuntimeError(f"Project {owner}#{project_number} disappeared")
            connection = page_project["items"]
            for node in connection["nodes"]:
                content = node.get("content") or {}
                if content.get("__typename") != "Issue":
                    continue
                status = None
                for value in node["fieldValues"]["nodes"]:
                    if not value or value.get("__typename") != "ProjectV2ItemFieldSingleSelectValue":
                        continue
                    field = value.get("field") or {}
                    if field.get("name") == "Status":
                        status = value.get("name")
                        break
                items.append(
                    IssueItem(
                        content_id=content["id"],
                        project_item_id=node["id"],
                        number=content["number"],
                        title=content["title"],
                        state=content["state"],
                        repository=content["repository"]["nameWithOwner"],
                        status=status,
                    )
                )
            page_info = connection["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]

        return ProjectSnapshot(
            project_id=project["id"],
            readme=project.get("readme") or "",
            status_field_id=status_field["id"],
            status_options=status_options,
            items=tuple(items),
        )

    def fetch_open_issues(self, owner: str, repository: str) -> list[IssueItem]:
        issues: list[IssueItem] = []
        cursor: str | None = None
        while True:
            page = self._execute(
                OPEN_ISSUES_QUERY,
                {"owner": owner, "repository": repository, "cursor": cursor},
            )
            repo = page.get("repository")
            if not repo:
                raise RuntimeError(f"Repository {owner}/{repository} was not found")
            connection = repo["issues"]
            for node in connection["nodes"]:
                issues.append(
                    IssueItem(
                        content_id=node["id"],
                        number=node["number"],
                        title=node["title"],
                        state=node["state"],
                        repository=node["repository"]["nameWithOwner"],
                    )
                )
            page_info = connection["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]
        return issues

    def add_issue(self, project_id: str, content_id: str) -> str:
        data = self._execute(
            ADD_ITEM_MUTATION, {"project": project_id, "content": content_id}
        )
        return data["addProjectV2ItemById"]["item"]["id"]

    def set_status(
        self,
        project_id: str,
        item_id: str,
        field_id: str,
        option_id: str,
    ) -> None:
        self._execute(
            SET_STATUS_MUTATION,
            {
                "project": project_id,
                "item": item_id,
                "field": field_id,
                "option": option_id,
            },
        )

    def update_readme(self, project_id: str, readme: str) -> None:
        self._execute(
            UPDATE_README_MUTATION, {"project": project_id, "readme": readme}
        )


def _escape_markdown_text(text: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _issue_line(owner: str, repository: str, item: IssueItem) -> str:
    title = _escape_markdown_text(item.title)
    url = f"https://github.com/{owner}/{repository}/issues/{item.number}"
    return f"- [#{item.number} {title}]({url})"


def render_readme(
    owner: str,
    repository: str,
    project_number: int,
    items: list[IssueItem],
) -> str:
    target = f"{owner}/{repository}"
    scoped = [item for item in items if item.repository == target]
    in_progress = sorted(
        (item for item in scoped if item.state == "OPEN" and item.status == "In Progress"),
        key=lambda item: item.number,
    )
    todo = sorted(
        (item for item in scoped if item.state == "OPEN" and item.status == "Todo"),
        key=lambda item: item.number,
    )
    other = sorted(
        (
            item
            for item in scoped
            if item.state == "OPEN" and item.status not in {"In Progress", "Todo"}
        ),
        key=lambda item: item.number,
    )
    done_count = sum(item.status == "Done" for item in scoped)

    lines = [
        "# OMM Roadmap",
        "",
        "> 이 README는 `omm-hippo/omm` 이슈와 Project `Status`에서 자동 생성됩니다.",
        "> 우선순위와 실제 착수 여부는 각 이슈의 본문과 토론을 기준으로 판단하세요.",
        "",
        "## 상태 기준",
        "",
        "- **Todo**: 방향 검토, 논의, 조사 기록 또는 착수 전",
        "- **In Progress**: 구현, 검증, 외부 승인 대기 등 실제 진행 중",
        "- **Done**: 이슈가 닫혔고 추적할 작업이 완료됨",
        "",
        f"## 현재 진행 중 ({len(in_progress)})",
        "",
    ]
    lines.extend(
        [_issue_line(owner, repository, item) for item in in_progress] or ["- 없음"]
    )
    lines.extend(["", f"## 검토·대기 중 ({len(todo)})", ""])
    lines.extend([_issue_line(owner, repository, item) for item in todo] or ["- 없음"])
    if other:
        lines.extend(["", f"## 기타 열린 상태 ({len(other)})", ""])
        lines.extend(_issue_line(owner, repository, item) for item in other)
    project_url = f"https://github.com/orgs/{owner}/projects/{project_number}"
    lines.extend(
        [
            "",
            "## 완료",
            "",
            f"`Done` 항목은 {done_count}개입니다. 전체 이력은 [Project #{project_number}]({project_url})에서 확인하세요.",
            "",
        ]
    )
    return "\n".join(lines)


def synchronize(
    api: ProjectApi,
    owner: str,
    repository: str,
    project_number: int,
    *,
    dry_run: bool = False,
) -> SyncResult:
    snapshot = api.fetch_project(owner, project_number)
    target_repository = f"{owner}/{repository}"
    items = list(snapshot.items)
    by_content_id = {
        item.content_id: item for item in items if item.repository == target_repository
    }
    added = 0
    status_updated = 0

    for issue in api.fetch_open_issues(owner, repository):
        if issue.content_id in by_content_id:
            continue
        item_id = f"dry-run:{issue.content_id}"
        if not dry_run:
            item_id = api.add_issue(snapshot.project_id, issue.content_id)
            api.set_status(
                snapshot.project_id,
                item_id,
                snapshot.status_field_id,
                snapshot.status_options["Todo"],
            )
        added += 1
        new_item = replace(issue, project_item_id=item_id, status="Todo")
        items.append(new_item)
        by_content_id[issue.content_id] = new_item

    normalized: list[IssueItem] = []
    for item in items:
        desired = item.status
        if item.repository == target_repository:
            if item.state == "CLOSED" and item.status != "Done":
                desired = "Done"
            elif item.state == "OPEN" and item.status in {None, "Done"}:
                desired = "Todo"
        if desired != item.status:
            if not item.project_item_id:
                raise RuntimeError(f"Project item id is missing for issue #{item.number}")
            if not dry_run:
                api.set_status(
                    snapshot.project_id,
                    item.project_item_id,
                    snapshot.status_field_id,
                    snapshot.status_options[desired],
                )
            status_updated += 1
            item = replace(item, status=desired)
        normalized.append(item)

    readme = render_readme(owner, repository, project_number, normalized)
    readme_updated = readme != snapshot.readme
    if readme_updated and not dry_run:
        api.update_readme(snapshot.project_id, readme)

    return SyncResult(
        added=added,
        status_updated=status_updated,
        readme_updated=readme_updated,
        readme=readme,
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--project-number", required=True, type=int)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read live state and print the desired README without mutating GitHub.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GH_TOKEN or GITHUB_TOKEN is required", file=sys.stderr)
        return 2
    endpoint = os.environ.get("GITHUB_GRAPHQL_URL", DEFAULT_GRAPHQL_URL)
    api = GraphQLProjectApi(token, endpoint)
    try:
        result = synchronize(
            api,
            args.owner,
            args.repository,
            args.project_number,
            dry_run=args.dry_run,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    mode = "Dry run" if args.dry_run else "Sync complete"
    print(
        f"{mode}: added={result.added}, "
        f"status_updated={result.status_updated}, "
        f"readme_updated={str(result.readme_updated).lower()}"
    )
    if args.dry_run:
        print("\n" + result.readme)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
