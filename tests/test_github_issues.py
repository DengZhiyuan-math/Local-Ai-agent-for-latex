"""The issue adapter with only the existing gh transport replaced."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import github
import github_issues


def response(value, code=0, stderr=""):
    return subprocess.CompletedProcess(["fixture-gh"], code, json.dumps(value), stderr)


def issue(number=1, full="Owner/paper", **extra):
    return {"id": number + 1000, "number": number, "title": "A topic", "state": "open",
            "body": "A continuing record", "html_url": f"https://github.com/{full}/issues/{number}", **extra}


def comment(identity=1, number=3, full="Owner/paper", **extra):
    return {"id": identity, "body": "An external discussion", "user": {"id": 22, "login": "coauthor", "type": "User"},
            "html_url": f"https://github.com/{full}/issues/{number}#issuecomment-{identity}",
            "issue_url": f"https://api.github.com/repos/{full}/issues/{number}",
            "created_at": "2026-10-29T00:00:00Z", "updated_at": "2026-10-29T00:00:00Z", **extra}


class IssueAdapter(unittest.TestCase):
    def connection(self, *results):
        connection = mock.Mock()
        connection.status.return_value = {"gh": "fixture-gh", "logged_in": True, "account": "author"}
        connection._gh.side_effect = list(results)
        self.enterContext(mock.patch.object(github, "GITHUB", connection))
        return connection

    def test_repository_uses_the_existing_dynamic_connection_and_pinned_host(self):
        connection = mock.Mock()
        connection.status.return_value = {"gh": "fixture-gh", "logged_in": True, "account": "author"}
        connection._gh.return_value = response({"id": 12, "full_name": "Owner/paper",
                                                "html_url": "https://github.com/Owner/paper"})
        with mock.patch.object(github, "GITHUB", connection):
            found = github_issues.IssueAdapter().repository("Owner/paper")
        self.assertEqual(found, {"id": 12, "full_name": "Owner/paper",
                                 "html_url": "https://github.com/Owner/paper"})
        args, options = connection._gh.call_args
        self.assertEqual(args[:6], ("fixture-gh", "api", "--hostname", "github.com", "--method", "GET"))
        self.assertIn("repos/Owner/paper", args)
        self.assertLessEqual(options["timeout"], 30)
        connection.login.assert_not_called()

    def test_user_resolves_a_human_account_id_through_the_existing_pinned_connection(self):
        connection = self.connection(response({"id": 42, "login": "Author", "type": "User",
                                               "html_url": "https://github.com/Author", "email": "private@example.test"}))
        found = github_issues.IssueAdapter().user("author")
        self.assertEqual(found, {"id": 42, "login": "Author", "type": "User",
                                 "html_url": "https://github.com/Author"})
        args, options = connection._gh.call_args
        self.assertEqual(args[:7], ("fixture-gh", "api", "--hostname", "github.com", "--method", "GET", "users/author"))
        self.assertIn("X-GitHub-Api-Version: 2026-03-10", args)
        self.assertLessEqual(options["timeout"], 30)
        connection.login.assert_not_called()

    def test_user_lookup_rejects_nonhuman_malformed_or_different_account_evidence(self):
        account = {"id": 42, "login": "author", "type": "User", "html_url": "https://github.com/author"}
        bad_rows = [None, [], {},
                    *({**account, "id": identity} for identity in (True, False, 0, -1, "42", 42.0, None)),
                    *({**account, "type": kind} for kind in ("Bot", "Organization", "user", None)),
                    {**account, "login": "renamed-author", "html_url": "https://github.com/renamed-author"},
                    *({**account, "html_url": url} for url in (None, "https://evil.test/author",
                       "https://github.com/other", "https://github.com/author/", "https://github.com/author?tab=repos",
                       "https://github.com@evil.test/author", "http://github.com/author"))]
        self.connection(*(response(row) for row in bad_rows))
        adapter = github_issues.IssueAdapter()
        for row in bad_rows:
            with self.subTest(row=row):
                with self.assertRaises(github_issues.IssueError) as error:
                    adapter.user("author")
                self.assertFalse(error.exception.uncertain)

    def test_user_lookup_refuses_unsafe_or_unbounded_names_before_authentication(self):
        connection = self.connection()
        adapter = github_issues.IssueAdapter()
        for login in (None, True, 42, "", "https://github.com/author", "../author", "author/other",
                      "author?x=y", "author#fragment", "author%2fother", "author\\other", "-author",
                      "a" * 101, "作者", "author\x00", " author", "author "):
            with self.subTest(login=login), self.assertRaises(github_issues.IssueError):
                adapter.user(login)
        connection.status.assert_not_called()
        connection._gh.assert_not_called()

    def test_user_lookup_does_not_accept_unicode_as_an_ascii_account_alias(self):
        self.connection(response({"id": 42, "login": "a\u212a", "type": "User", "html_url": "https://github.com/ak"}),
                        response({"id": 42, "login": "ak", "type": "User", "html_url": "https://github.com/a\u212a"}))
        for _ in range(2):
            with self.assertRaises(github_issues.IssueError):
                github_issues.IssueAdapter().user("ak")

    def test_user_lookup_honors_pause_logout_and_response_limits_without_writing(self):
        active = [False]
        connection = self.connection()
        adapter = github_issues.IssueAdapter(allowed=lambda: active[0])
        with self.assertRaises(github_issues.IssueError):
            adapter.user("author")
        connection.status.assert_not_called()
        active[0] = True
        def close_during_status():
            active[0] = False
            return {"gh": "fixture-gh", "logged_in": True}
        connection.status.side_effect = close_during_status
        with self.assertRaises(github_issues.IssueError):
            adapter.user("author")
        connection._gh.assert_not_called()
        active[0] = True
        connection.status.side_effect = None
        connection.status.return_value = {"gh": "fixture-gh", "logged_in": False}
        with self.assertRaises(github_issues.IssueError):
            adapter.user("author")
        connection._gh.assert_not_called()
        connection.login.assert_not_called()
        connection.status.return_value["logged_in"] = True
        connection._gh.side_effect = [subprocess.TimeoutExpired("secret-token", 20),
                                      subprocess.CompletedProcess([], 0, "secret-token", "")]
        for _ in range(2):
            with self.assertRaises(github_issues.IssueError) as error:
                adapter.user("author")
            self.assertFalse(error.exception.uncertain)
            self.assertNotIn("secret-token", str(error.exception))
        connection._gh.side_effect = [response({"id": 42, "login": "author", "type": "User",
                                                "html_url": "https://github.com/author"})]
        with mock.patch.object(github_issues, "MAX_RESPONSE", 20), self.assertRaises(github_issues.IssueError) as error:
            adapter.user("author")
        self.assertFalse(error.exception.uncertain)

    def test_a_successful_comment_with_invalid_body_is_an_unknown_result(self):
        connection = mock.Mock()
        connection.status.return_value = {"gh": "fixture-gh", "logged_in": True}
        connection._gh.return_value = response({"id": 4, "body": False,
            "user": {"id": 2, "login": "coauthor", "type": "User"},
            "html_url": "https://github.com/Owner/paper/issues/3#issuecomment-4",
            "issue_url": "https://api.github.com/repos/Owner/paper/issues/3",
            "created_at": "2026-10-29T00:00:00Z", "updated_at": "2026-10-29T00:00:00Z"})
        with mock.patch.object(github, "GITHUB", connection), \
                self.assertRaises(github_issues.IssueError) as error:
            github_issues.IssueAdapter().create_comment("Owner/paper", 3, "A note")
        self.assertTrue(error.exception.uncertain)

    def test_invalid_unicode_is_refused_before_any_connection_call(self):
        connection = mock.Mock()
        connection.status.return_value = {"gh": "fixture-gh", "logged_in": True}
        with mock.patch.object(github, "GITHUB", connection), \
                self.assertRaises(github_issues.IssueError) as error:
            github_issues.IssueAdapter().create_issue("Owner/paper", "Topic", "Invalid \ud800")
        self.assertFalse(error.exception.uncertain)
        connection.status.assert_not_called()
        connection._gh.assert_not_called()

    def test_old_repository_name_can_resolve_to_its_canonical_identity(self):
        connection = self.connection(response({"id": 12, "full_name": "Owner/renamed-paper",
                                               "html_url": "https://github.com/Owner/renamed-paper"}))
        adapter = github_issues.IssueAdapter()
        result = adapter.repository("Owner/paper")
        self.assertEqual(result["id"], 12)
        self.assertEqual(result["full_name"], "Owner/renamed-paper")
        self.assertIn("repos/Owner/paper", connection._gh.call_args.args)

    def test_unbound_hosts_paths_and_nonpositive_numbers_never_run_a_process(self):
        connection = self.connection()
        adapter = github_issues.IssueAdapter()
        for full in ("https://github.com/owner/paper", "owner/../paper", "owner/paper?x=y", "../paper",
                     "owner/..", "owner/paper/issue", "owner\\paper", "-owner/paper"):
            with self.subTest(full=full), self.assertRaises(github_issues.IssueError):
                adapter.repository(full)
        for number in (True, 0, -1, "3"):
            with self.subTest(number=number), self.assertRaises(github_issues.IssueError):
                adapter.list_comments("Owner/paper", number)
        connection.status.assert_not_called()
        connection._gh.assert_not_called()

    def test_issue_pagination_uses_raw_page_length_and_excludes_pull_requests(self):
        first = [issue(n) for n in range(1, 100)] + [issue(100, pull_request={"url": "unused"})]
        connection = self.connection(response(first), response([issue(101)]))
        rows = github_issues.IssueAdapter().list_issues("Owner/paper")
        self.assertEqual([row["number"] for row in rows], [*range(1, 100), 101])
        endpoints = [call.args[6] for call in connection._gh.call_args_list]
        self.assertEqual(endpoints, ["repos/Owner/paper/issues?state=all&sort=created&direction=asc&per_page=100&page=1",
                                     "repos/Owner/paper/issues?state=all&sort=created&direction=asc&per_page=100&page=2"])

    def test_comments_preserve_actual_authorship_and_versions_across_all_pages(self):
        first = [comment(n) for n in range(1, 101)]
        connection = self.connection(response(first), response([comment(101, body="$x$ was discussed.")]))
        rows = github_issues.IssueAdapter().list_comments("Owner/paper", 3)
        self.assertEqual(len(rows), 101)
        self.assertEqual(rows[-1]["user"], {"id": 22, "login": "coauthor", "type": "User"})
        self.assertNotEqual(rows[-1]["user"]["login"], connection.status()["account"])
        self.assertEqual(rows[-1]["body"], "$x$ was discussed.")
        self.assertEqual(rows[-1]["updated_at"], "2026-10-29T00:00:00Z")
        self.assertIn("repos/Owner/paper/issues/3/comments?per_page=100&page=2", connection._gh.call_args.args)

    def test_closing_during_a_page_prevents_the_next_transport_request(self):
        active = [True]
        connection = self.connection()
        def page(*args, **kwargs):
            if connection._gh.call_count == 1:
                active[0] = False
                return response([issue(number) for number in range(1, 101)])
            return response([issue(101)])
        connection._gh.side_effect = page
        adapter = github_issues.IssueAdapter()
        adapter.allowed = lambda: active[0]
        with self.assertRaises(github_issues.IssueError):
            adapter.list_issues("Owner/paper")
        self.assertEqual(connection._gh.call_count, 1)

    def test_closing_during_auth_check_prevents_a_new_mutating_request(self):
        active = [True]
        connection = self.connection()
        def login():
            active[0] = False
            return {"gh": "fixture-gh", "logged_in": True}
        connection.status.side_effect = login
        adapter = github_issues.IssueAdapter(allowed=lambda: active[0])
        with self.assertRaises(github_issues.IssueError) as error:
            adapter.create_issue("Owner/paper", "Topic", "Note")
        self.assertFalse(error.exception.uncertain)
        connection._gh.assert_not_called()

    def test_create_sends_literal_utf8_json_through_stdin_without_shell_text(self):
        body = "Literal `backticks`, $(commands), quotes \" and 中文\nA new line."
        connection = self.connection(response(issue(7, body=body)), response(comment(8, number=7, body=body)))
        adapter = github_issues.IssueAdapter()
        self.assertEqual(adapter.create_issue("Owner/paper", "主题", body)["number"], 7)
        self.assertEqual(adapter.create_comment("Owner/paper", 7, body)["id"], 8)
        for called in connection._gh.call_args_list:
            self.assertNotIn(body, called.args)
            self.assertIn("--input", called.args)
            self.assertEqual(json.loads(called.kwargs["input"])["body"], body)
            self.assertIn("X-GitHub-Api-Version: 2026-03-10", called.args)

    def test_issue_and_comment_context_mismatches_fail_closed(self):
        adapter = github_issues.IssueAdapter()
        invalid_issues = (issue(full="Other/paper"), issue(2), issue(1, id=True), issue(1, body=False),
                          issue(1, pull_request={}), issue(1, html_url="https://example.com/Owner/paper/issues/1"))
        connection = self.connection(*(response(row) for row in invalid_issues))
        for row in invalid_issues:
            with self.subTest(row=row), self.assertRaises(github_issues.IssueError) as error:
                adapter.get_issue("Owner/paper", 1)
            self.assertFalse(error.exception.uncertain)
        self.assertEqual(connection._gh.call_count, len(invalid_issues))
        invalid_comments = (comment(1, full="Other/paper"), comment(1, number=4), comment(1, user={"login": "coauthor"}),
                            comment(1, issue_url="https://example.com/Owner/paper/issues/3"))
        connection._gh.side_effect = [response([row]) for row in invalid_comments]
        for row in invalid_comments:
            with self.subTest(row=row), self.assertRaises(github_issues.IssueError):
                adapter.list_comments("Owner/paper", 3)

    def test_page_limit_or_malformed_page_never_returns_a_partial_list(self):
        connection = self.connection(response([issue(1)] * 100), response([issue(1)] * 100), response({"not": "a page"}))
        adapter = github_issues.IssueAdapter()
        with mock.patch.object(github_issues, "MAX_PAGES", 2), self.assertRaises(github_issues.IssueError):
            adapter.list_issues("Owner/paper")
        with self.assertRaises(github_issues.IssueError):
            adapter.list_issues("Owner/paper")
        self.assertEqual(connection._gh.call_count, 3)

    def test_combined_pages_have_a_bounded_collection_size(self):
        connection = self.connection(response([issue(1)]))
        with mock.patch.object(github_issues, "MAX_COLLECTION", 20, create=True), \
                self.assertRaises(github_issues.IssueError):
            github_issues.IssueAdapter().list_issues("Owner/paper")
        self.assertEqual(connection._gh.call_count, 1)

    def test_mutating_timeouts_and_invalid_successes_are_uncertain(self):
        connection = self.connection(subprocess.TimeoutExpired("fixture-gh", 20), response({"message": "not an issue"}))
        adapter = github_issues.IssueAdapter()
        for _ in range(2):
            with self.assertRaises(github_issues.IssueError) as error:
                adapter.create_issue("Owner/paper", "Topic", "Note")
            self.assertTrue(error.exception.uncertain)
        connection._gh.side_effect = [subprocess.TimeoutExpired("fixture-gh", 20)]
        with self.assertRaises(github_issues.IssueError) as error:
            adapter.list_issues("Owner/paper")
        self.assertFalse(error.exception.uncertain)

    def test_io_failure_after_transport_started_is_not_treated_as_not_applied(self):
        self.connection(OSError("controlled output read failure"))
        with self.assertRaises(github_issues.IssueError) as error:
            github_issues.IssueAdapter().create_issue("Owner/paper", "Topic", "A note")
        self.assertTrue(error.exception.uncertain)

    def test_definite_refusal_is_distinct_from_unknown_failure_and_never_echoes_secrets(self):
        connection = self.connection(response({}, 1, "secret-token https://token@github.com/o/r (HTTP 403)"),
                                     response({}, 1, "secret-token HTTP 502"), response({}, 1, "HTTP 408"))
        for uncertain in (False, True, True):
            with self.assertRaises(github_issues.IssueError) as error:
                github_issues.IssueAdapter().create_comment("Owner/paper", 3, "A note")
            self.assertEqual(error.exception.uncertain, uncertain)
            self.assertNotIn("secret", str(error.exception))
            self.assertNotIn("token@", str(error.exception))
        self.assertEqual(connection._gh.call_count, 3)

    def test_request_text_cannot_fake_a_definite_http_failure(self):
        self.connection(response({}, 1, 'gh: echo of user text HTTP 403; upstream unavailable (HTTP 502)'))
        with self.assertRaises(github_issues.IssueError) as error:
            github_issues.IssueAdapter().create_issue("Owner/paper", "Topic", "Note")
        self.assertTrue(error.exception.uncertain)

    def test_missing_login_does_not_start_transport_or_authorization(self):
        connection = self.connection()
        connection.status.return_value = {"gh": "fixture-gh", "logged_in": False, "account": None}
        with self.assertRaises(github_issues.IssueError) as error:
            github_issues.IssueAdapter().create_issue("Owner/paper", "Topic", "A note")
        self.assertFalse(error.exception.uncertain)
        connection._gh.assert_not_called()
        connection.login.assert_not_called()

    def test_unreadable_or_oversized_success_is_uncertain_only_for_mutations(self):
        connection = self.connection(subprocess.CompletedProcess([], 0, "not-json", ""),
                                     subprocess.CompletedProcess([], 0, "x" * 30, ""))
        with self.assertRaises(github_issues.IssueError) as error:
            github_issues.IssueAdapter().create_issue("Owner/paper", "Topic", "A note")
        self.assertTrue(error.exception.uncertain)
        with mock.patch.object(github_issues, "MAX_RESPONSE", 20), self.assertRaises(github_issues.IssueError) as error:
            github_issues.IssueAdapter().repository("Owner/paper")
        self.assertFalse(error.exception.uncertain)

    def test_invalid_unicode_in_a_json_response_is_not_returned_as_discussion_data(self):
        self.connection(response([comment(body="Invalid \ud800")]))
        with self.assertRaises(github_issues.IssueError) as error:
            github_issues.IssueAdapter().list_comments("Owner/paper", 3)
        self.assertFalse(error.exception.uncertain)

    def test_fake_preserves_unknown_applied_mutations_and_known_failure_has_no_effect(self):
        from fake_github_issues import FakeIssueAdapter
        adapter = FakeIssueAdapter()
        adapter.offline = True
        with self.assertRaises(github_issues.IssueError) as error:
            adapter.create_issue("fixture/paper", "Topic", "Note")
        self.assertFalse(error.exception.uncertain)
        self.assertEqual(adapter.issue_rows, [])
        adapter.offline = False
        adapter.unknown_after_apply.add("create_issue")
        with self.assertRaises(github_issues.IssueError) as error:
            adapter.create_issue("fixture/paper", "Topic", "Note")
        self.assertTrue(error.exception.uncertain)
        self.assertEqual(len(adapter.issue_rows), 1)
        adapter.unknown_after_apply.add("create_comment")
        with self.assertRaises(github_issues.IssueError):
            adapter.create_comment("fixture/paper", 1, "Update")
        self.assertEqual(len(adapter.comment_rows[1]), 1)
        rows = adapter.list_issues("fixture/paper")
        rows[0]["body"] = "Modified returned copy"
        self.assertEqual(adapter.issue_rows[0]["body"], "Note")

    def test_fake_user_lookup_returns_only_explicit_stable_account_rows(self):
        from fake_github_issues import FakeIssueAdapter
        adapter = FakeIssueAdapter()
        self.assertEqual(adapter.user("Author"), {"id": 42, "login": "author", "type": "User",
                                                 "html_url": "https://github.com/author"})
        self.assertEqual(adapter.calls, [{"method": "user", "login": "Author"}])
        adapter.users["coauthor"] = {"id": 55, "login": "coauthor", "type": "User",
                                      "html_url": "https://github.com/coauthor"}
        found = adapter.user("coauthor")
        found["id"] = 987
        self.assertEqual(adapter.users["coauthor"]["id"], 55)
        with self.assertRaises(github_issues.IssueError):
            adapter.user("unknown-account")

    def test_fake_user_lookup_uses_the_shared_failure_and_override_controls(self):
        from fake_github_issues import FakeIssueAdapter
        adapter = FakeIssueAdapter(allowed=lambda: False)
        with self.assertRaises(github_issues.IssueError):
            adapter.user("author")
        self.assertEqual(adapter.calls, [])
        adapter.allowed = None
        for field in ("offline", "logged_in"):
            setattr(adapter, field, field == "offline")
            with self.assertRaises(github_issues.IssueError) as error:
                adapter.user("author")
            self.assertFalse(error.exception.uncertain)
            setattr(adapter, field, field == "logged_in")
        adapter.next_error["user"] = github_issues.IssueError("Controlled failure")
        with self.assertRaisesRegex(github_issues.IssueError, "Controlled failure"):
            adapter.user("author")
        self.assertEqual(adapter.user("author")["id"], 42)
        adapter.overrides["user"] = {"id": 0, "login": "wrong-context"}
        self.assertEqual(adapter.user("author"), {"id": 0, "login": "wrong-context"})


if __name__ == "__main__":
    unittest.main()
