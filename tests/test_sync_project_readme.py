from __future__ import annotations

from dataclasses import replace

from scripts.sync_project_readme import (
    IssueItem,
    ProjectSnapshot,
    render_readme,
    synchronize,
)


OWNER = "omm-hippo"
REPOSITORY = "omm"
PROJECT_ID = "project-id"
STATUS_FIELD_ID = "status-field-id"


def issue(
    number: int,
    *,
    state: str = "OPEN",
    status: str | None = "Todo",
    title: str | None = None,
    repository: str = "omm-hippo/omm",
) -> IssueItem:
    return IssueItem(
        content_id=f"issue-{number}",
        project_item_id=f"item-{number}",
        number=number,
        title=title or f"Issue {number}",
        state=state,
        repository=repository,
        status=status,
    )


class FakeApi:
    def __init__(
        self,
        project_items: list[IssueItem],
        open_issues: list[IssueItem],
        *,
        readme: str = "old readme",
    ) -> None:
        self.snapshot = ProjectSnapshot(
            project_id=PROJECT_ID,
            readme=readme,
            status_field_id=STATUS_FIELD_ID,
            status_options={"Todo": "todo-id", "In Progress": "progress-id", "Done": "done-id"},
            items=tuple(project_items),
        )
        self.open_issues = open_issues
        self.added: list[str] = []
        self.status_changes: list[tuple[str, str]] = []
        self.readmes: list[str] = []

    def fetch_project(self, owner: str, project_number: int) -> ProjectSnapshot:
        assert owner == OWNER
        assert project_number == 1
        return self.snapshot

    def fetch_open_issues(self, owner: str, repository: str) -> list[IssueItem]:
        assert owner == OWNER
        assert repository == REPOSITORY
        return self.open_issues

    def add_issue(self, project_id: str, content_id: str) -> str:
        assert project_id == PROJECT_ID
        self.added.append(content_id)
        return f"added-{content_id}"

    def set_status(
        self,
        project_id: str,
        item_id: str,
        field_id: str,
        option_id: str,
    ) -> None:
        assert project_id == PROJECT_ID
        assert field_id == STATUS_FIELD_ID
        self.status_changes.append((item_id, option_id))

    def update_readme(self, project_id: str, readme: str) -> None:
        assert project_id == PROJECT_ID
        self.readmes.append(readme)


def test_render_readme_groups_sorts_and_escapes_titles() -> None:
    readme = render_readme(
        OWNER,
        REPOSITORY,
        1,
        [
            issue(20, state="CLOSED", status="Done"),
            issue(347, status="In Progress", title="Docs [sync]\nnow"),
            issue(29, status="Todo"),
            issue(88, status="In Progress"),
            issue(5, repository="other/repo"),
        ],
    )

    assert "## 현재 진행 중 (2)" in readme
    assert readme.index("#88 Issue 88") < readme.index(r"#347 Docs \[sync\] now")
    assert "## 검토·대기 중 (1)" in readme
    assert "`Done` 항목은 1개입니다." in readme
    assert "other/repo" not in readme
    assert not readme.endswith("\n\n")


def test_synchronize_adds_missing_issue_and_normalizes_obvious_status_drift() -> None:
    project_items = [
        issue(20, state="CLOSED", status="In Progress"),
        issue(24, state="OPEN", status="Done"),
        issue(88, state="OPEN", status="In Progress"),
    ]
    missing = replace(issue(347), project_item_id=None, status=None)
    api = FakeApi(project_items, [issue(24), issue(88), missing])

    result = synchronize(api, OWNER, REPOSITORY, 1)

    assert result.added == 1
    assert result.status_updated == 2
    assert api.added == ["issue-347"]
    assert ("added-issue-347", "todo-id") in api.status_changes
    assert ("item-20", "done-id") in api.status_changes
    assert ("item-24", "todo-id") in api.status_changes
    assert ("item-88", "progress-id") not in api.status_changes
    assert result.readme_updated is True
    assert api.readmes == [result.readme]


def test_synchronize_is_noop_when_project_and_readme_are_current() -> None:
    project_items = [issue(29), issue(88, status="In Progress")]
    expected = render_readme(OWNER, REPOSITORY, 1, project_items)
    api = FakeApi(project_items, [issue(29), issue(88)], readme=expected)

    result = synchronize(api, OWNER, REPOSITORY, 1)

    assert result.added == 0
    assert result.status_updated == 0
    assert result.readme_updated is False
    assert api.added == []
    assert api.status_changes == []
    assert api.readmes == []

def test_dry_run_reports_changes_without_writing() -> None:
    project_items = [issue(20, state="CLOSED", status="In Progress")]
    missing = replace(issue(347), project_item_id=None, status=None)
    api = FakeApi(project_items, [missing])

    result = synchronize(api, OWNER, REPOSITORY, 1, dry_run=True)

    assert result.added == 1
    assert result.status_updated == 1
    assert result.readme_updated is True
    assert api.added == []
    assert api.status_changes == []
    assert api.readmes == []
