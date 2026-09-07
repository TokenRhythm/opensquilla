from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

DUMMY_MERGED_DEV_PR = 9001
DUMMY_CLOSED_PR = 9002
DUMMY_OPEN_PR = 9003
DUMMY_MERGED_MAIN_PR = 9004
DUMMY_MERGED_DEV_PR_URL = "https://example.invalid/pulls/9001"
DUMMY_CLOSED_PR_URL = "https://example.invalid/pulls/9002"
DUMMY_OPEN_PR_URL = "https://example.invalid/pulls/9003"
DUMMY_MERGED_MAIN_PR_URL = "https://example.invalid/pulls/9004"


def _load_sync_module():
    script = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "issue_link_sync.py"
    spec = importlib.util.spec_from_file_location("issue_link_sync", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordingClient:
    def __init__(self, comments: list[dict[str, Any]] | None = None) -> None:
        self.comments = comments or []
        self.calls: list[tuple[Any, ...]] = []

    def add_labels(self, issue_number: int, labels: list[str]) -> None:
        self.calls.append(("add_labels", issue_number, tuple(labels)))

    def remove_label(self, issue_number: int, label: str) -> None:
        self.calls.append(("remove_label", issue_number, label))

    def list_comments(self, issue_number: int) -> list[dict[str, Any]]:
        self.calls.append(("list_comments", issue_number))
        return self.comments

    def create_comment(self, issue_number: int, body: str) -> None:
        self.calls.append(("create_comment", issue_number, body))


class PaginatedGitHubClient:
    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.paths: list[str] = []

    def request_json(self, method: str, path: str) -> list[dict[str, Any]]:
        assert method == "GET"
        self.paths.append(path)
        return self.pages.pop(0)


def test_migrated_repository_resolves_its_historical_issue_references() -> None:
    sync = _load_sync_module()
    parsed = sync.parse_linked_issues(
        "\n".join(
            [
                "Fixes opensquilla/opensquilla#100",
                "Closes TokenRhythm/opensquilla#100",
                "Fixes #100",
                "Resolves https://github.com/OpenSquilla/OpenSquilla/issues/101",
                "Refs https://github.com/TokenRhythm/opensquilla/issues/102",
                "Refs opensquilla/opensquilla#103",
                "Fixes unrelated/opensquilla#999",
                "Fixes opensquilla/runtime-packs#998",
                "Fixes TokenRhythm/runtime-packs#997",
            ]
        ),
        owner="TokenRhythm",
        repo="opensquilla",
    )
    assert parsed.closing == (100, 101)
    assert parsed.references == (102, 103)


def test_repository_alias_is_not_applied_to_forks_or_companion_repositories() -> None:
    sync = _load_sync_module()
    for owner, repo in (("unrelated", "opensquilla"), ("TokenRhythm", "runtime-packs")):
        parsed = sync.parse_linked_issues(
            "Fixes opensquilla/opensquilla#100\nRefs opensquilla/runtime-packs#101\nFixes #102",
            owner=owner,
            repo=repo,
        )
        assert parsed.closing == (102,)
        assert parsed.references == ()


def test_parse_linked_issues_splits_closing_and_reference_keywords() -> None:
    sync = _load_sync_module()

    parsed = sync.parse_linked_issues(
        "\n".join(
            [
                "Fixes #100",
                "Closes: opensquilla/opensquilla#101",
                "resolves https://github.com/opensquilla/opensquilla/issues/102",
                "Refs #200",
                "References opensquilla/opensquilla#201",
                "Fixes other/project#999",
            ]
        ),
        owner="opensquilla",
        repo="opensquilla",
    )

    assert parsed.closing == (100, 101, 102)
    assert parsed.references == (200, 201)
    assert parsed.all == (100, 101, 102, 200, 201)


def test_parse_linked_issues_deduplicates_and_ignores_pr_style_urls() -> None:
    sync = _load_sync_module()

    parsed = sync.parse_linked_issues(
        "\n".join(
            [
                "Fixes #100",
                "fixes #100",
                "Refs https://github.com/opensquilla/opensquilla/pull/9999",
                "Fixes https://github.com/opensquilla/opensquilla/issues/101",
            ]
        ),
        owner="opensquilla",
        repo="opensquilla",
    )

    assert parsed.closing == (100, 101)
    assert parsed.references == ()
    assert parsed.all == (100, 101)


def test_plan_merged_dev_pr_updates_only_closing_issues() -> None:
    sync = _load_sync_module()

    actions = sync.plan_issue_sync_actions(
        {
            "action": "closed",
            "pull_request": {
                "number": DUMMY_MERGED_DEV_PR,
                "merged": True,
                "body": "Fixes #100\nRefs #200",
                "base": {"ref": "dev"},
                "html_url": DUMMY_MERGED_DEV_PR_URL,
            },
            "repository": {
                "full_name": "opensquilla/opensquilla",
            },
        }
    )

    assert actions == (
        sync.IssueSyncAction(
            issue_number=100,
            kind="merged_to_dev",
            pr_number=DUMMY_MERGED_DEV_PR,
            pr_url=DUMMY_MERGED_DEV_PR_URL,
        ),
        sync.IssueSyncAction(
            issue_number=200,
            kind="remove_linked_pr",
            pr_number=DUMMY_MERGED_DEV_PR,
            pr_url=DUMMY_MERGED_DEV_PR_URL,
        ),
    )


def test_plan_open_or_updated_pr_adds_linked_pr_label_to_all_linked_issues() -> None:
    sync = _load_sync_module()

    for action_name in ["opened", "reopened", "edited"]:
        actions = sync.plan_issue_sync_actions(
            {
                "action": action_name,
                "pull_request": {
                    "number": DUMMY_OPEN_PR,
                    "merged": False,
                    "body": "Fixes #100\nRefs #200",
                    "base": {"ref": "dev"},
                    "html_url": DUMMY_OPEN_PR_URL,
                },
                "repository": {
                    "full_name": "opensquilla/opensquilla",
                },
            }
        )

        assert actions == (
            sync.IssueSyncAction(
                issue_number=100,
                kind="linked_pr_open",
                pr_number=DUMMY_OPEN_PR,
                pr_url=DUMMY_OPEN_PR_URL,
            ),
            sync.IssueSyncAction(
                issue_number=200,
                kind="linked_pr_open",
                pr_number=DUMMY_OPEN_PR,
                pr_url=DUMMY_OPEN_PR_URL,
            ),
        )


def test_plan_merged_main_pr_removes_linked_pr_label_without_dev_status() -> None:
    sync = _load_sync_module()

    actions = sync.plan_issue_sync_actions(
        {
            "action": "closed",
            "pull_request": {
                "number": DUMMY_MERGED_MAIN_PR,
                "merged": True,
                "body": "Fixes #100\nRefs #200",
                "base": {"ref": "main"},
                "html_url": DUMMY_MERGED_MAIN_PR_URL,
            },
            "repository": {
                "full_name": "opensquilla/opensquilla",
            },
        }
    )

    assert actions == (
        sync.IssueSyncAction(
            issue_number=100,
            kind="remove_linked_pr",
            pr_number=DUMMY_MERGED_MAIN_PR,
            pr_url=DUMMY_MERGED_MAIN_PR_URL,
        ),
        sync.IssueSyncAction(
            issue_number=200,
            kind="remove_linked_pr",
            pr_number=DUMMY_MERGED_MAIN_PR,
            pr_url=DUMMY_MERGED_MAIN_PR_URL,
        ),
    )


def test_plan_closed_unmerged_pr_removes_linked_pr_label_from_all_linked_issues() -> None:
    sync = _load_sync_module()

    actions = sync.plan_issue_sync_actions(
        {
            "action": "closed",
            "pull_request": {
                "number": DUMMY_CLOSED_PR,
                "merged": False,
                "body": "Fixes #100\nRefs #200",
                "base": {"ref": "dev"},
                "html_url": DUMMY_CLOSED_PR_URL,
            },
            "repository": {
                "full_name": "opensquilla/opensquilla",
            },
        }
    )

    assert actions == (
        sync.IssueSyncAction(
            issue_number=100,
            kind="closed_unmerged",
            pr_number=DUMMY_CLOSED_PR,
            pr_url=DUMMY_CLOSED_PR_URL,
        ),
        sync.IssueSyncAction(
            issue_number=200,
            kind="closed_unmerged",
            pr_number=DUMMY_CLOSED_PR,
            pr_url=DUMMY_CLOSED_PR_URL,
        ),
    )


def test_comment_marker_is_pr_scoped_for_idempotent_merged_to_dev_comments() -> None:
    sync = _load_sync_module()

    marker = sync.comment_marker(kind="merged_to_dev", pr_number=DUMMY_MERGED_DEV_PR)

    assert marker == "<!-- opensquilla-issue-link-sync:merged-to-dev:pr-9001 -->"
    assert sync.has_marker([{"body": f"{marker}\nThis is already posted."}], marker)
    assert not sync.has_marker(
        [{"body": "<!-- opensquilla-issue-link-sync:merged-to-dev:pr-9000 -->"}],
        marker,
    )


def test_apply_merged_dev_action_labels_removes_open_pr_label_and_comments_once() -> None:
    sync = _load_sync_module()
    action = sync.IssueSyncAction(
        issue_number=100,
        kind="merged_to_dev",
        pr_number=DUMMY_MERGED_DEV_PR,
        pr_url=DUMMY_MERGED_DEV_PR_URL,
    )
    client = RecordingClient()

    sync.apply_action(client, action)

    assert client.calls == [
        ("add_labels", 100, ("merged-to-dev", "needs-verification")),
        ("remove_label", 100, "has-linked-pr"),
        ("list_comments", 100),
        (
            "create_comment",
            100,
            "<!-- opensquilla-issue-link-sync:merged-to-dev:pr-9001 -->\n"
            "The linked fix for this issue has merged to `dev` via #9001 "
            f"({DUMMY_MERGED_DEV_PR_URL}). "
            "Keeping it open for verification before release.",
        ),
    ]

    marker = sync.comment_marker(kind="merged_to_dev", pr_number=DUMMY_MERGED_DEV_PR)
    client = RecordingClient(comments=[{"body": marker}])

    sync.apply_action(client, action)

    assert client.calls == [
        ("add_labels", 100, ("merged-to-dev", "needs-verification")),
        ("remove_label", 100, "has-linked-pr"),
        ("list_comments", 100),
    ]


def test_apply_open_linked_pr_action_adds_open_pr_label() -> None:
    sync = _load_sync_module()
    action = sync.IssueSyncAction(
        issue_number=100,
        kind="linked_pr_open",
        pr_number=DUMMY_OPEN_PR,
        pr_url=DUMMY_OPEN_PR_URL,
    )
    client = RecordingClient()

    sync.apply_action(client, action)

    assert client.calls == [("add_labels", 100, ("has-linked-pr",))]


def test_apply_remove_linked_pr_action_only_removes_open_pr_label() -> None:
    sync = _load_sync_module()
    action = sync.IssueSyncAction(
        issue_number=200,
        kind="remove_linked_pr",
        pr_number=DUMMY_MERGED_MAIN_PR,
        pr_url=DUMMY_MERGED_MAIN_PR_URL,
    )
    client = RecordingClient()

    sync.apply_action(client, action)

    assert client.calls == [("remove_label", 200, "has-linked-pr")]


def test_apply_closed_unmerged_action_only_removes_open_pr_label() -> None:
    sync = _load_sync_module()
    action = sync.IssueSyncAction(
        issue_number=200,
        kind="closed_unmerged",
        pr_number=DUMMY_CLOSED_PR,
        pr_url=DUMMY_CLOSED_PR_URL,
    )
    client = RecordingClient()

    sync.apply_action(client, action)

    assert client.calls == [("remove_label", 200, "has-linked-pr")]


class ClassifyingClient:
    """get_issue stub: maps issue number -> API response (or None)."""

    def __init__(self, targets: dict[int, dict[str, Any] | None]) -> None:
        self.targets = targets
        self.lookups: list[int] = []

    def get_issue(self, issue_number: int) -> dict[str, Any] | None:
        self.lookups.append(issue_number)
        return self.targets.get(issue_number)


def test_classify_sync_target_distinguishes_issue_pr_and_missing() -> None:
    sync = _load_sync_module()
    client = ClassifyingClient(
        {
            100: {"number": 100},
            535: {"number": 535, "pull_request": {"url": "https://example.invalid/pulls/535"}},
        }
    )

    assert sync.classify_sync_target(client, 100) == "issue"
    assert sync.classify_sync_target(client, 535) == "pull_request"
    assert sync.classify_sync_target(client, 999) == "missing"


class StatusRaisingClient:
    """request_json stub that answers every mutation with a fixed HTTP status."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.calls: list[tuple[str, str]] = []

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        ignore_statuses: set[int] | None = None,
    ) -> Any:
        self.calls.append((method, path))
        if self.status in (ignore_statuses or set()):
            return None
        raise RuntimeError(f"GitHub API {method} {path} failed: {self.status}")

    def ensure_label(self, name: str) -> None:
        self.calls.append(("ENSURE", name))


def test_label_and_comment_mutations_tolerate_403_and_410() -> None:
    """A PR-numbered target or a locked/deleted issue must not fail the job."""
    sync = _load_sync_module()

    for status in (403, 404, 410):
        client = StatusRaisingClient(status)
        sync.GitHubClient.add_labels(client, 535, ["has-linked-pr"])
        sync.GitHubClient.remove_label(client, 535, "has-linked-pr")
        sync.GitHubClient.create_comment(client, 535, "body")
        assert ("POST", "/issues/535/labels") in client.calls
        assert ("POST", "/issues/535/comments") in client.calls


def test_ensure_label_tolerates_create_race() -> None:
    """A 422 from a concurrent run creating the same label is not an error."""
    sync = _load_sync_module()

    class LabelRaceClient(StatusRaisingClient):
        def request_json(self, method: str, path: str, **kwargs: Any) -> Any:
            if method == "GET":
                self.calls.append((method, path))
                return None  # label not found -> triggers POST /labels
            return super().request_json(method, path, **kwargs)

    client = LabelRaceClient(422)
    sync.GitHubClient.ensure_label(client, "has-linked-pr")
    assert ("POST", "/labels") in client.calls


def test_list_comments_reads_all_pages_before_idempotency_check() -> None:
    sync = _load_sync_module()
    client = PaginatedGitHubClient(
        [
            [{"body": f"old comment {index}"} for index in range(100)],
            [{"body": "<!-- opensquilla-issue-link-sync:merged-to-dev:pr-9001 -->"}],
        ]
    )

    comments = sync.GitHubClient.list_comments(client, 100)

    assert sync.has_marker(
        comments,
        "<!-- opensquilla-issue-link-sync:merged-to-dev:pr-9001 -->",
    )
    assert client.paths == [
        "/issues/100/comments?per_page=100&page=1",
        "/issues/100/comments?per_page=100&page=2",
    ]
