"""An issue/comment adapter with no process, account, or network access."""
import copy

from fake_github import FakeGitHub
from github_issues import IssueError, repository_name, user_login


class FakeIssueAdapter(FakeGitHub):
    def __init__(self, full="fixture/paper", repo_id=1, account="fixture-author", logged_in=True, *, allowed=None):
        super().__init__(account=account, logged_in=logged_in)
        self.repo = {"id": repo_id, "full_name": repository_name(full), "html_url": "https://github.com/" + full}
        self.aliases = {full.casefold()}
        self.allowed = allowed
        self.issue_rows = []
        self.comment_rows = {}
        self.users = {"author": {"id": 42, "login": "author", "type": "User",
                                  "html_url": "https://github.com/author"}}
        self.calls = []
        self.offline = False
        self.next_error = {}
        self.unknown_after_apply = set()
        self.overrides = {}

    def _before(self, method, full=None, **values):
        if self.allowed is not None and not self.allowed():
            raise IssueError("The controlled GitHub check is paused.")
        self.calls.append({"method": method, **({"full": full} if full is not None else {}), **values})
        if not self.logged_in or self.offline:
            raise IssueError("The controlled GitHub adapter is offline.")
        error = self.next_error.pop(method, None)
        if error is not None:
            raise error
        if (full is not None and full.casefold() != self.repo["full_name"].casefold()
                and not (method == "repository" and full.casefold() in self.aliases)):
            raise IssueError("The controlled GitHub repository is not bound.")

    def _answer(self, method, value):
        if method in self.unknown_after_apply:
            self.unknown_after_apply.remove(method)
            raise IssueError("The controlled response was lost after application.", uncertain=True)
        return copy.deepcopy(self.overrides.get(method, value))

    def repository(self, full):
        self._before("repository", full)
        return self._answer("repository", self.repo)

    def user(self, login):
        login = user_login(login)
        self._before("user", login=login)
        if login.casefold() not in self.users:
            raise IssueError("The controlled GitHub account does not exist.")
        return self._answer("user", self.users[login.casefold()])

    def list_issues(self, full):
        self._before("list_issues", full)
        return self._answer("list_issues", self.issue_rows)

    def get_issue(self, full, number):
        self._before("get_issue", full, number=number)
        row = next((row for row in self.issue_rows if row["number"] == number), None)
        if row is None:
            raise IssueError("The controlled issue does not exist.")
        return self._answer("get_issue", row)

    def list_comments(self, full, number):
        self._before("list_comments", full, number=number)
        if not any(row["number"] == number for row in self.issue_rows):
            raise IssueError("The controlled issue does not exist.")
        return self._answer("list_comments", self.comment_rows.get(number, []))

    def create_issue(self, full, title, body):
        self._before("create_issue", full, title=title, body=body)
        number = max((row["number"] for row in self.issue_rows), default=0) + 1
        row = {"id": self.repo["id"] * 1000000 + number, "number": number,
               "title": title, "body": body, "state": "open",
               "html_url": f"https://github.com/{full}/issues/{number}"}
        self.issue_rows.append(row)
        self.comment_rows[number] = []
        return self._answer("create_issue", row)

    def create_comment(self, full, number, body):
        self._before("create_comment", full, number=number, body=body)
        if not any(row["number"] == number for row in self.issue_rows):
            raise IssueError("The controlled issue does not exist.")
        identity = max((row["id"] for rows in self.comment_rows.values() for row in rows), default=100) + 1
        stamp = f"2026-10-29T00:{identity // 60 % 60:02d}:{identity % 60:02d}Z"
        row = {"id": identity, "body": body, "user": {"id": 7, "login": self.account, "type": "User"},
               "html_url": f"https://github.com/{full}/issues/{number}#issuecomment-{identity}",
               "issue_url": f"https://api.github.com/repos/{full}/issues/{number}",
               "created_at": stamp, "updated_at": stamp}
        self.comment_rows.setdefault(number, []).append(row)
        return self._answer("create_comment", row)
