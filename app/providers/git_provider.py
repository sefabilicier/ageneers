"""
Git Provider abstraction layer.

Defines the GitProvider interface that all Git hosting providers must implement.
Currently GitHub is the only implementation, but GitLab and Bitbucket can be
added without changing any agent code.

Usage in agents:
    from app.providers.git_provider import get_git_provider
    provider = get_git_provider()
    pr = provider.create_pull_request(...)
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass


# ─────────────────────────────────────────────────────────────────────────────
# Data classes (provider-agnostic)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PullRequestResult:
    """Provider-agnostic PR creation result."""
    number: int
    url: str
    title: str
    branch: str


@dataclass
class BranchInfo:
    """Result of a branch existence check."""
    exists: bool
    name: str


# ─────────────────────────────────────────────────────────────────────────────
# Abstract interface
# ─────────────────────────────────────────────────────────────────────────────

class GitProvider(ABC):
    """
    Abstract base class for Git hosting providers.

    Any provider (GitHub, GitLab, Bitbucket, Gitea, …) must implement
    these methods. Agents interact only with this interface — they never
    import GitHub-specific libraries directly.
    """

    @abstractmethod
    def get_repo_slug(self, url: str) -> str:
        """
        Extract the owner/repo identifier from a repository URL.
        GitHub: 'https://github.com/org/repo' → 'org/repo'
        GitLab: 'https://gitlab.com/org/repo' → 'org/repo'
        """

    @abstractmethod
    def branch_exists(self, repo_slug: str, branch_name: str) -> BranchInfo:
        """Check whether a branch exists on the remote."""

    @abstractmethod
    def create_pull_request(
        self,
        repo_slug: str,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
    ) -> PullRequestResult:
        """Open a pull request and return the result."""

    @abstractmethod
    def get_open_pull_request(
        self,
        repo_slug: str,
        head: str,
    ) -> PullRequestResult | None:
        """
        Return an existing open PR for the given head branch, or None.
        Used for duplicate PR detection.
        """


# ─────────────────────────────────────────────────────────────────────────────
# GitHub implementation
# ─────────────────────────────────────────────────────────────────────────────

class GitHubProvider(GitProvider):
    """
    GitHub implementation using PyGithub.

    All GitHub-specific API calls are confined to this class.
    """

    def __init__(self, token: str) -> None:
        from github import Auth, Github
        self._gh = Github(auth=Auth.Token(token))

    def get_repo_slug(self, url: str) -> str:
        import re
        match = re.search(r"github\.com[/:]([^/]+/[^/]+?)(?:\.git)?/?$", url)
        if not match:
            raise ValueError(f"Cannot extract repo slug from GitHub URL: {url!r}")
        return match.group(1)

    def branch_exists(self, repo_slug: str, branch_name: str) -> BranchInfo:
        try:
            repo = self._gh.get_repo(repo_slug)
            repo.get_branch(branch_name)
            return BranchInfo(exists=True, name=branch_name)
        except Exception as exc:
            if "404" in str(exc) or "Not Found" in str(exc):
                return BranchInfo(exists=False, name=branch_name)
            # Any other error → assume doesn't exist (safe default)
            return BranchInfo(exists=False, name=branch_name)

    def create_pull_request(
        self,
        repo_slug: str,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
    ) -> PullRequestResult:
        from github import GithubException
        repo = self._gh.get_repo(repo_slug)
        try:
            pr = repo.create_pull(
                title=title,
                body=body,
                base=base,
                head=head,
                draft=draft,
            )
            return PullRequestResult(
                number=pr.number,
                url=pr.html_url,
                title=pr.title,
                branch=head,
            )
        except GithubException as exc:
            raise RuntimeError(
                f"GitHub API error creating PR: {exc.status} {exc.data}"
            ) from exc

    def get_open_pull_request(
        self,
        repo_slug: str,
        head: str,
    ) -> PullRequestResult | None:
        repo  = self._gh.get_repo(repo_slug)
        owner = repo_slug.split("/")[0]
        pulls = list(repo.get_pulls(state="open", head=f"{owner}:{head}"))
        if not pulls:
            return None
        pr = pulls[0]
        return PullRequestResult(
            number=pr.number,
            url=pr.html_url,
            title=pr.title,
            branch=head,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Factory — agents call this, never instantiate providers directly
# ─────────────────────────────────────────────────────────────────────────────

def get_git_provider() -> GitProvider:
    """
    Return the configured Git provider based on environment variables.

    GIT_PROVIDER=github  (default) → GitHubProvider
    GIT_PROVIDER=gitlab            → GitLabProvider  (not yet implemented)
    GIT_PROVIDER=bitbucket         → BitbucketProvider (not yet implemented)

    This factory is the single integration point. Adding a new provider
    requires only:
      1. Implementing GitProvider subclass
      2. Adding a case here
    No agent code needs to change.
    """
    provider = os.getenv("GIT_PROVIDER", "github").lower()
    token    = os.getenv("GITHUB_TOKEN", "")

    if provider == "github":
        if not token:
            raise RuntimeError(
                "GITHUB_TOKEN is not set. "
                "Set it in your .env file to use the GitHub provider."
            )
        return GitHubProvider(token)

    # Future providers — uncomment when implemented:
    # if provider == "gitlab":
    #     return GitLabProvider(token=os.getenv("GITLAB_TOKEN", ""))
    # if provider == "bitbucket":
    #     return BitbucketProvider(...)

    raise ValueError(
        f"Unknown GIT_PROVIDER: {provider!r}. "
        f"Supported: 'github'. "
        f"Set GIT_PROVIDER in your .env file."
    )