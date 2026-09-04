#!/usr/bin/env python3
"""Synchronize OpenSquilla issue state after pull request lifecycle changes.

This script is intentionally safe for `pull_request_target`: it treats pull
request text as data only and never executes code from the pull request head.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, NamedTuple
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

HAS_LINKED_PR_LABEL = "has-linked-pr"
MERGED_TO_DEV_LABEL = "merged-to-dev"
NEEDS_VERIFICATION_LABEL = "needs-verification"

LABEL_DEFINITIONS = {
    HAS_LINKED_PR_LABEL: {
        "color": "C5DEF5",
        "description": "An open pull request is linked to this issue",
    },
    MERGED_TO_DEV_LABEL: {
        "color": "5319E7",
        "description": "A related fix has merged to dev but has not necessarily shipped",
    },
    NEEDS_VERIFICATION_LABEL: {
        "color": "C5DEF5",
        "description": "Maintainers or reporters should retest the current behavior",
    },
}

CLOSING_KEYWORDS = frozenset(
    {
        "close",
        "closes",
        "closed",
        "fix",
        "fixes",
        "fixed",
        "resolve",
        "resolves",
        "resolved",
    }
)
#: Mutation statuses that describe the target's state or the token's scope on
#: that one target (labeling a pull request needs pull-requests write scope
#: this workflow does not request; locked or deleted issues answer 403/410)
#: rather than a broken sync setup — the affected action is logged and
#: skipped instead of failing every remaining action in the job.
TOLERATED_MUTATION_STATUSES = frozenset({403, 404, 410})
REFERENCE_KEYWORDS = frozenset({"ref", "refs", "reference", "references"})
OPEN_PR_ACTIONS = frozenset({"opened", "reopened", "edited"})
KEYWORD_RE = re.compile(
    r"\b(?P<keyword>close|closes|closed|fix|fixes|fixed|resolve|resolves|"
    r"resolved|ref|refs|reference|references)\s*:?\s+(?P<ref>"
    r"#\d+|"
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+|"
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+"
    r")\b",
    re.IGNORECASE,
)
LOCAL_REF_RE = re.compile(r"^#(?P<number>\d+)$")
REPO_REF_RE = re.compile(
    r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)#(?P<number>\d+)$"
)
ISSUE_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/"
    r"issues/(?P<number>\d+)$"
)


class ParsedIssueLinks(NamedTuple):
    closing: tuple[int, ...]
    references: tuple[int, ...]

    @property
    def all(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys((*self.closing, *self.references)))


class IssueSyncAction(NamedTuple):
    issue_number: int
    kind: str
    pr_number: int
    pr_url: str


class GitHubClient:
    def __init__(self, *, token: str, repository: str) -> None:
        self._token = token
        self._repository = repository
        self._api_root = f"https://api.github.com/repos/{repository}"

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        ignore_statuses: set[int] | None = None,
    ) -> Any:
        ignore_statuses = ignore_statuses or set()
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(
            f"{self._api_root}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read()
        except HTTPError as exc:
            if exc.code in ignore_statuses:
                return None
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API {method} {path} failed: {exc.code} {body}") from exc

        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))

    def get_issue(self, issue_number: int) -> dict[str, Any] | None:
        """Fetch an issue, or None when it is missing or unreadable.

        Pull requests are returned too (the REST issues endpoint covers
        both) — callers use the ``pull_request`` key to tell them apart.
        """
        found = self.request_json(
            "GET",
            f"/issues/{issue_number}",
            ignore_statuses=TOLERATED_MUTATION_STATUSES,
        )
        return found if isinstance(found, dict) else None

    def ensure_label(self, name: str) -> None:
        definition = LABEL_DEFINITIONS[name]
        label_name = quote(name, safe="")
        found = self.request_json(
            "GET",
            f"/labels/{label_name}",
            ignore_statuses={404},
        )
        if found is not None:
            return
        self.request_json(
            "POST",
            "/labels",
            payload={
                "name": name,
                "color": definition["color"],
                "description": definition["description"],
            },
            # 422: another run created the label between the GET and here.
            ignore_statuses=TOLERATED_MUTATION_STATUSES | {422},
        )

    def add_labels(self, issue_number: int, labels: list[str]) -> None:
        for label in labels:
            self.ensure_label(label)
        self.request_json(
            "POST",
            f"/issues/{issue_number}/labels",
            payload={"labels": labels},
            ignore_statuses=TOLERATED_MUTATION_STATUSES,
        )

    def remove_label(self, issue_number: int, label: str) -> None:
        label_name = quote(label, safe="")
        self.request_json(
            "DELETE",
            f"/issues/{issue_number}/labels/{label_name}",
            ignore_statuses=TOLERATED_MUTATION_STATUSES,
        )

    def list_comments(self, issue_number: int) -> list[dict[str, Any]]:
        all_comments: list[dict[str, Any]] = []
        page = 1
        while True:
            comments = self.request_json(
                "GET",
                f"/issues/{issue_number}/comments?per_page=100&page={page}",
            )
            if not isinstance(comments, list) or not comments:
                return all_comments
            all_comments.extend(comments)
            if len(comments) < 100:
                return all_comments
            page += 1

    def create_comment(self, issue_number: int, body: str) -> None:
        self.request_json(
            "POST",
            f"/issues/{issue_number}/comments",
            payload={"body": body},
            # Locked conversations reject comments with 403; the labels above
            # still applied, so the sync outcome is preserved.
            ignore_statuses=TOLERATED_MUTATION_STATUSES,
        )


def _normalize_repo_ref(raw_ref: str, *, owner: str, repo: str) -> int | None:
    local = LOCAL_REF_RE.match(raw_ref)
    if local is not None:
        return int(local.group("number"))

    accepted_owners = {owner.lower()}
    if (owner.lower(), repo.lower()) == ("tokenrhythm", "opensquilla"):
        # Historical issue references follow this repository's organization move.
        accepted_owners.add("opensquilla")

    repo_ref = REPO_REF_RE.match(raw_ref)
    if repo_ref is not None:
        if repo_ref.group("owner").lower() not in accepted_owners:
            return None
        if repo_ref.group("repo").lower() != repo.lower():
            return None
        return int(repo_ref.group("number"))

    issue_url = ISSUE_URL_RE.match(raw_ref)
    if issue_url is not None:
        if issue_url.group("owner").lower() not in accepted_owners:
            return None
        if issue_url.group("repo").lower() != repo.lower():
            return None
        return int(issue_url.group("number"))

    return None


def parse_linked_issues(body: str | None, *, owner: str, repo: str) -> ParsedIssueLinks:
    closing: list[int] = []
    references: list[int] = []

    for match in KEYWORD_RE.finditer(body or ""):
        issue_number = _normalize_repo_ref(match.group("ref"), owner=owner, repo=repo)
        if issue_number is None:
            continue
        keyword = match.group("keyword").lower()
        if keyword in CLOSING_KEYWORDS:
            closing.append(issue_number)
        elif keyword in REFERENCE_KEYWORDS:
            references.append(issue_number)

    return ParsedIssueLinks(
        closing=tuple(dict.fromkeys(closing)),
        references=tuple(dict.fromkeys(references)),
    )


def plan_issue_sync_actions(event: dict[str, Any]) -> tuple[IssueSyncAction, ...]:
    action = event.get("action")
    pr = event.get("pull_request") or {}
    repository = event.get("repository") or {}
    full_name = repository.get("full_name") or ""
    if "/" not in full_name:
        return ()
    owner, repo = full_name.split("/", 1)

    pr_number = int(pr["number"])
    pr_url = str(pr.get("html_url") or "")
    parsed = parse_linked_issues(pr.get("body"), owner=owner, repo=repo)

    if action in OPEN_PR_ACTIONS:
        return tuple(
            IssueSyncAction(
                issue_number=issue_number,
                kind="linked_pr_open",
                pr_number=pr_number,
                pr_url=pr_url,
            )
            for issue_number in parsed.all
        )

    if action != "closed":
        return ()

    if pr.get("merged") is True and (pr.get("base") or {}).get("ref") == "dev":
        closing_actions = tuple(
            IssueSyncAction(
                issue_number=issue_number,
                kind="merged_to_dev",
                pr_number=pr_number,
                pr_url=pr_url,
            )
            for issue_number in parsed.closing
        )
        closing_issue_numbers = set(parsed.closing)
        reference_cleanup_actions = tuple(
            IssueSyncAction(
                issue_number=issue_number,
                kind="remove_linked_pr",
                pr_number=pr_number,
                pr_url=pr_url,
            )
            for issue_number in parsed.references
            if issue_number not in closing_issue_numbers
        )
        return (*closing_actions, *reference_cleanup_actions)

    if pr.get("merged") is not True:
        return tuple(
            IssueSyncAction(
                issue_number=issue_number,
                kind="closed_unmerged",
                pr_number=pr_number,
                pr_url=pr_url,
            )
            for issue_number in parsed.all
        )

    return tuple(
        IssueSyncAction(
            issue_number=issue_number,
            kind="remove_linked_pr",
            pr_number=pr_number,
            pr_url=pr_url,
        )
        for issue_number in parsed.all
    )


def comment_marker(*, kind: str, pr_number: int) -> str:
    marker_kind = kind.replace("_", "-")
    return f"<!-- opensquilla-issue-link-sync:{marker_kind}:pr-{pr_number} -->"


def has_marker(comments: list[dict[str, Any]], marker: str) -> bool:
    return any(marker in str(comment.get("body") or "") for comment in comments)


def _merged_to_dev_comment(action: IssueSyncAction) -> str:
    marker = comment_marker(kind=action.kind, pr_number=action.pr_number)
    return (
        f"{marker}\n"
        f"The linked fix for this issue has merged to `dev` via #{action.pr_number} "
        f"({action.pr_url}). Keeping it open for verification before release."
    )


def apply_action(client: GitHubClient, action: IssueSyncAction) -> None:
    if action.kind == "linked_pr_open":
        client.add_labels(action.issue_number, [HAS_LINKED_PR_LABEL])
        return

    if action.kind == "merged_to_dev":
        client.add_labels(
            action.issue_number,
            [MERGED_TO_DEV_LABEL, NEEDS_VERIFICATION_LABEL],
        )
        client.remove_label(action.issue_number, HAS_LINKED_PR_LABEL)
        marker = comment_marker(kind=action.kind, pr_number=action.pr_number)
        if not has_marker(client.list_comments(action.issue_number), marker):
            client.create_comment(action.issue_number, _merged_to_dev_comment(action))
        return

    if action.kind in {"closed_unmerged", "remove_linked_pr"}:
        client.remove_label(action.issue_number, HAS_LINKED_PR_LABEL)
        return

    raise ValueError(f"Unsupported issue sync action: {action.kind}")


def classify_sync_target(client: GitHubClient, issue_number: int) -> str:
    """Classify a parsed ``#N`` reference before mutating it.

    ``#N`` is textual and shares one number space between issues and pull
    requests, so a PR body saying "the main path was fixed in #535" parses as
    an issue link even though 535 is a pull request. Labeling a PR needs a
    scope this workflow does not hold (and is not the sync's job), so only
    ``"issue"`` targets are acted on; ``"pull_request"`` and ``"missing"``
    are skipped by the caller.
    """
    found = client.get_issue(issue_number)
    if found is None:
        return "missing"
    if found.get("pull_request"):
        return "pull_request"
    return "issue"


def _load_event(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main() -> int:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    repository = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")

    if not event_path:
        print("GITHUB_EVENT_PATH is required.", file=sys.stderr)
        return 2
    if not repository:
        print("GITHUB_REPOSITORY is required.", file=sys.stderr)
        return 2
    if not token:
        print("GITHUB_TOKEN is required.", file=sys.stderr)
        return 2

    event = _load_event(event_path)
    actions = plan_issue_sync_actions(event)
    if not actions:
        print("No issue sync actions required.")
        return 0

    client = GitHubClient(token=token, repository=repository)
    for action in actions:
        target_kind = classify_sync_target(client, action.issue_number)
        if target_kind != "issue":
            print(
                f"Skipping {action.kind} for #{action.issue_number} "
                f"from PR #{action.pr_number}: target is {target_kind}."
            )
            continue
        print(
            f"Applying {action.kind} for issue #{action.issue_number} "
            f"from PR #{action.pr_number}."
        )
        apply_action(client, action)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
