"""Usage refresh is read-only, reports failures, and never presents stale data as fresh."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
import usage


class UsageRefresh(unittest.TestCase):
    def test_fresh_read_only_query_normalizes_five_hour_and_seven_day(self):
        payload = {"rate_limits_available": True, "rate_limits": {
            "five_hour": {"utilization": 42, "resets_at": "2030-01-01T12:00:00+00:00"},
            "seven_day": {"utilization": 12.5, "resets_at": "2030-01-07T12:00:00+00:00"}}}
        with mock.patch.object(usage, "control_query", return_value=payload) as query:
            cache = usage.UsageCache()
            result = cache.get("claude", ("profile-a",), force=True)
        self.assertAlmostEqual(result["rate"]["unifiedWindows"]["five_hour"]["utilization"], .42)
        self.assertEqual(result["rate"]["unifiedWindows"]["seven_day"]["resetsAt"], 1894017600)
        self.assertEqual(result["last_success"], result["rate"]["at"])
        self.assertFalse(result["stale"])
        query.assert_called_once_with("claude", {"subtype": "get_usage", "skip_behaviors": True})

    def test_force_refresh_queries_again_while_normal_read_uses_cache(self):
        payload = {"rate_limits_available": True, "rate_limits": {
            "five_hour": {"utilization": 10, "resets_at": "2030-01-01T12:00:00Z"}}}
        cache = usage.UsageCache()
        with mock.patch.object(usage, "control_query", return_value=payload) as query:
            cache.get("claude", ("a",), force=True)
            cached = cache.get("claude", ("a",), force=False)
            self.assertTrue(cached["cached"])
            cache.get("claude", ("a",), force=True)
        self.assertEqual(query.call_count, 2)

    def test_failure_retains_timestamp_but_marks_previous_snapshot_stale(self):
        payload = {"rate_limits_available": True, "rate_limits": {
            "five_hour": {"utilization": 10, "resets_at": "2030-01-01T12:00:00Z"}}}
        cache = usage.UsageCache()
        with mock.patch.object(usage, "control_query", return_value=payload):
            good = cache.get("claude", ("a",))
        with mock.patch.object(usage, "control_query", side_effect=ValueError("offline")):
            failed = cache.get("claude", ("a",))
            changed_account = cache.get("claude", ("b",))
        self.assertEqual(failed["last_success"], good["last_success"])
        self.assertEqual(failed["rate"], good["rate"])
        self.assertTrue(failed["stale"])
        self.assertEqual(failed["error"], "offline")
        self.assertIsNone(changed_account["rate"])
        self.assertIsNone(changed_account["last_success"])

    def test_elapsed_reset_and_ttl_trigger_new_query(self):
        payload = {"rate_limits_available": True, "rate_limits": {
            "five_hour": {"utilization": 10, "resets_at": "2030-01-01T12:00:00Z"}}}
        cache = usage.UsageCache()
        with mock.patch.object(usage, "control_query", return_value=payload) as query:
            with mock.patch.object(usage.time, "time", return_value=1000):
                cache.get("claude", ("a",))
            with mock.patch.object(usage.time, "time", return_value=1061):
                cache.get("claude", ("a",), force=False)
            self.assertEqual(query.call_count, 2)
        self.assertTrue(usage.expired({"at": 1000, "unifiedWindows": {
            "five_hour": {"utilization": .5, "resetsAt": 1005}}}, 1006))

    def test_invalid_or_unavailable_usage_never_counts_as_a_success(self):
        for payload in ({}, {"rate_limits_available": False},
                        {"rate_limits_available": True, "rate_limits": {}},
                        {"rate_limits_available": True, "rate_limits": {"five_hour": {"utilization": True}}},
                        {"rate_limits_available": True, "rate_limits": {"five_hour": {"utilization": 101}}},
                        {"rate_limits_available": True, "rate_limits": {"five_hour": {"utilization": 0, "resets_at": "bad"}}}):
            with self.subTest(payload=payload):
                with mock.patch.object(usage, "control_query", return_value=payload):
                    result = usage.UsageCache().get("claude", ("a",))
                self.assertIn("error", result)
                self.assertIsNone(result["rate"])
                self.assertIsNone(result["last_success"])


@unittest.skipIf(os.name == "nt", "executable fixture uses a POSIX shebang")
class ReadOnlyControl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def cli(self, behavior):
        path = self.root / "claude"
        path.write_text("#!" + sys.executable + "\n" + behavior, encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def test_actual_process_uses_only_control_messages_and_no_user_prompt(self):
        log = self.root / "requests.jsonl"
        exe = self.cli("""import json, sys
with open(""" + repr(str(log)) + """, 'w') as out:
    out.write(json.dumps(sys.argv[1:]) + '\\n')
    for line in sys.stdin:
        request = json.loads(line)
        out.write(line); out.flush()
        if request['request']['subtype'] == 'initialize':
            payload = {'models': [{'value': 'm'}]}
        else:
            payload = {'rate_limits_available': True, 'rate_limits': {
                'five_hour': {'utilization': 23, 'resets_at': '2030-01-01T12:00:00Z'}}}
        print(json.dumps({'type': 'control_response', 'response': {
            'subtype': 'success', 'request_id': request['request_id'], 'response': payload}}), flush=True)
""")
        response = usage.control_query(exe, {"subtype": "get_usage", "skip_behaviors": True})
        self.assertEqual(response["rate_limits"]["five_hour"]["utilization"], 23)
        messages = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(messages[1]["request"]["subtype"], "initialize")
        self.assertEqual(messages[2]["request"], {"subtype": "get_usage", "skip_behaviors": True})
        self.assertTrue(all(m["type"] == "control_request" for m in messages[1:]))
        self.assertNotIn("--model", messages[0])

    def test_initialize_only_returns_models_without_model_turn(self):
        exe = self.cli("""import json, sys, time
request = json.loads(sys.stdin.readline())
print(json.dumps({'type':'control_response','response':{'subtype':'success',
    'request_id':request['request_id'],'response':{'models':[{'value':'dynamic'}]}}}), flush=True)
time.sleep(30)
""")
        self.assertEqual(usage.control_query(exe)["models"], [{"value": "dynamic"}])

    def test_unsupported_or_missing_response_fails_explicitly(self):
        exe = self.cli("""import json, sys
request = json.loads(sys.stdin.readline())
print(json.dumps({'type':'control_response','response':{'subtype':'error',
    'request_id':request['request_id'],'error':'get_usage is not supported'}}), flush=True)
""")
        with self.assertRaisesRegex(ValueError, "not supported"):
            usage.control_query(exe)
        exe = self.cli("import sys\nprint('bad-json', flush=True)\n")
        with self.assertRaisesRegex(ValueError, "did not answer"):
            usage.control_query(exe)

    def test_hung_cli_has_bounded_timeout(self):
        exe = self.cli("import time\ntime.sleep(30)\n")
        with self.assertRaisesRegex(ValueError, "timed out"):
            usage.control_query(exe, timeout=.1)


if __name__ == "__main__":
    unittest.main()
