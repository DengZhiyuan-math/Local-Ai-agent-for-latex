"""Bound issue/comment operations through the editor's existing gh connection.

The caller owns durable publication intent and reconciliation. An uncertain
mutation must be looked up before the caller decides whether to send it again.
"""
from __future__ import annotations

import json
import re
import subprocess

import github

MAX_BODY = 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024
MAX_COLLECTION = 32 * 1024 * 1024
MAX_PAGES = 100
API_VERSION = "2026-03-10"
FULL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}")
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}")


class IssueError(ValueError):
    def __init__(self, message, *, uncertain=False):
        self.uncertain = bool(uncertain)
        super().__init__(message)


def repository_name(value):
    if (not isinstance(value, str) or not FULL_NAME.fullmatch(value)
            or value.split("/")[1] in (".", "..")):
        raise IssueError("Choose a GitHub repository as owner/name.")
    return value


def user_login(value):
    if not isinstance(value, str) or not LOGIN.fullmatch(value):
        raise IssueError("Choose a designated GitHub human account by login.")
    return value


def _id(value):
    return type(value) is int and value > 0


def _text(value, limit=MAX_BODY, *, empty=True):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value or (not empty and not value.strip()):
        return False
    try:
        value.encode("utf-8")
        return True
    except UnicodeError:
        return False


def _url(value, expected):
    return isinstance(value, str) and value.casefold() == expected.casefold()


def _number(value):
    if not _id(value):
        raise IssueError("A GitHub issue needs a positive issue number.")
    return value


def _user(value):
    if (not isinstance(value, dict) or not _id(value.get("id"))
            or not _text(value.get("login"), 100, empty=False)
            or not _text(value.get("type"), 40, empty=False)):
        raise IssueError("GitHub returned invalid comment authorship.")
    return {key: value[key] for key in ("id", "login", "type")}


class IssueAdapter:
    def __init__(self, *, allowed=None):
        self.allowed = allowed

    def _check_active(self):
        if self.allowed is not None and not self.allowed():
            raise IssueError("GitHub discussion checking is paused.")

    def _request(self, full, suffix="", *, body=None):
        return self._api(f"repos/{repository_name(full)}{suffix}", body=body)

    def _api(self, path, *, body=None):
        self._check_active()
        method = "POST" if body is not None else "GET"
        args = ["api", "--hostname", "github.com", "--method", method,
                path, "-H", "Accept: application/vnd.github+json",
                "-H", "X-GitHub-Api-Version: " + API_VERSION]
        options = {"timeout": 20}
        if body is not None:
            try:
                raw = json.dumps(body, ensure_ascii=False)
                length = len(raw.encode("utf-8"))
            except (TypeError, ValueError, UnicodeError):
                raise IssueError("The GitHub research update contains invalid text.") from None
            if length > MAX_BODY:
                raise IssueError("The GitHub research update is too large.")
            args += ["--input", "-"]
            options["input"] = raw
        connection = github.GITHUB
        try:
            login = connection.status()
        except (OSError, ValueError, subprocess.SubprocessError):
            raise IssueError("The GitHub connection could not be checked; retry when it is available.") from None
        if (not isinstance(login, dict) or login.get("logged_in") is not True
                or not _text(login.get("gh"), 4096, empty=False)):
            raise IssueError("Log in to GitHub from Home before synchronizing research discussions.")
        self._check_active()
        try:
            result = connection._gh(login["gh"], *args, **options)
        except OSError:
            # An I/O error can occur after gh sent the request, not only at exec.
            raise IssueError("The GitHub connection failed; its result must be checked.",
                             uncertain=body is not None) from None
        except subprocess.SubprocessError:
            raise IssueError("The GitHub request did not complete; its result must be checked.",
                             uncertain=body is not None) from None
        if result.returncode:
            # Do not echo CLI diagnostics: they can contain credentials or request data.
            diagnostic = result.stderr if isinstance(result.stderr, str) else ""
            status = re.search(r"\(HTTP\s+(\d{3})\)\s*$", diagnostic)
            definite = bool(status and int(status[1]) in (400, 401, 403, 404, 405, 409, 410, 415, 422, 429))
            raise IssueError("GitHub refused the request." if definite else "The GitHub request failed; its result must be checked.",
                             uncertain=body is not None and not definite)
        try:
            if not isinstance(result.stdout, str) or len(result.stdout.encode("utf-8")) > MAX_RESPONSE:
                raise ValueError
            return json.loads(result.stdout)
        except (ValueError, UnicodeError):
            raise IssueError("GitHub returned an unreadable response.", uncertain=body is not None) from None

    def user(self, login):
        login = user_login(login)
        data = self._api("users/" + login)
        if (not isinstance(data, dict) or not _id(data.get("id")) or data.get("type") != "User"
                or not isinstance(data.get("login"), str) or not LOGIN.fullmatch(data["login"])
                or data["login"].casefold() != login.casefold()
                or not isinstance(data.get("html_url"), str) or not data["html_url"].isascii()
                or not _url(data.get("html_url"), "https://github.com/" + login)):
            raise IssueError("GitHub returned a different or invalid designated human account.")
        return {key: data[key] for key in ("id", "login", "type", "html_url")}

    def repository(self, full):
        full = repository_name(full)
        data = self._request(full)
        if not isinstance(data, dict) or not _id(data.get("id")):
            raise IssueError("GitHub returned a different or invalid repository.")
        canonical = repository_name(data.get("full_name"))
        # GitHub can resolve an old repository name to its canonical name. The
        # caller binds its numeric ID and retains the requested GitSync target.
        if not _url(data.get("html_url"), "https://github.com/" + canonical):
            raise IssueError("GitHub returned a different or invalid repository.")
        return {key: data[key] for key in ("id", "full_name", "html_url")}

    @staticmethod
    def _issue(full, data, number=None):
        if (not isinstance(data, dict) or "pull_request" in data
                or not _id(data.get("id")) or not _id(data.get("number"))
                or (number is not None and data["number"] != number)
                or not _text(data.get("title"), 1024, empty=False)
                or data.get("state") not in ("open", "closed")
                or "body" not in data or (data["body"] is not None and not _text(data["body"]))
                or not _url(data.get("html_url"), f"https://github.com/{full}/issues/{data.get('number')}")):
            raise IssueError("GitHub returned a different or invalid research issue.")
        result = {key: data[key] for key in ("id", "number", "title", "state", "html_url")}
        result["body"] = data.get("body") or ""
        return result

    @staticmethod
    def _comment(full, number, data):
        if (not isinstance(data, dict) or not _id(data.get("id"))
                or "body" not in data or (data["body"] is not None and not _text(data["body"]))
                or not _url(data.get("html_url"), f"https://github.com/{full}/issues/{number}#issuecomment-{data.get('id')}")
                or not _url(data.get("issue_url"), f"https://api.github.com/repos/{full}/issues/{number}")
                or not _text(data.get("created_at"), 100, empty=False)
                or not _text(data.get("updated_at"), 100, empty=False)):
            raise IssueError("GitHub returned a comment for a different or invalid issue.")
        result = {key: data[key] for key in ("id", "html_url", "issue_url", "created_at", "updated_at")}
        result.update(body=data.get("body") or "", user=_user(data.get("user")))
        return result

    def _pages(self, full, suffix):
        rows, size = [], 0
        for page in range(1, MAX_PAGES + 1):
            join = "&" if "?" in suffix else "?"
            data = self._request(full, suffix + f"{join}per_page=100&page={page}")
            if not isinstance(data, list) or len(data) > 100:
                raise IssueError("GitHub returned an invalid discussion page.")
            try:
                size += len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
            except (TypeError, ValueError):
                raise IssueError("GitHub returned unreadable discussion data.") from None
            if size > MAX_COLLECTION:
                raise IssueError("The GitHub discussion size limit was reached; no complete result was accepted.")
            rows.extend(data)
            if len(data) < 100:
                return rows
        raise IssueError("The GitHub discussion page limit was reached; no complete result was accepted.")

    def list_issues(self, full):
        full = repository_name(full)
        rows = self._pages(full, "/issues?state=all&sort=created&direction=asc")
        return [self._issue(full, row) for row in rows
                if not (isinstance(row, dict) and "pull_request" in row)]

    def get_issue(self, full, number):
        full, number = repository_name(full), _number(number)
        return self._issue(full, self._request(full, f"/issues/{number}"), number)

    def list_comments(self, full, number):
        full, number = repository_name(full), _number(number)
        return [self._comment(full, number, row)
                for row in self._pages(full, f"/issues/{number}/comments")]

    def create_issue(self, full, title, body):
        full = repository_name(full)
        if not _text(title, 256, empty=False) or not _text(body):
            raise IssueError("A GitHub research issue needs a bounded title and body.")
        result = self._request(full, "/issues", body={"title": title, "body": body})
        try:
            return self._issue(full, result)
        except IssueError as error:
            raise IssueError(str(error), uncertain=True) from None

    def create_comment(self, full, number, body):
        full, number = repository_name(full), _number(number)
        if not _text(body, empty=False):
            raise IssueError("A GitHub research update needs a bounded nonempty body.")
        result = self._request(full, f"/issues/{number}/comments", body={"body": body})
        try:
            return self._comment(full, number, result)
        except IssueError as error:
            raise IssueError(str(error), uncertain=True) from None
