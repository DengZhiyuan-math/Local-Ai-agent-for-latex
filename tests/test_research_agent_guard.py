"""Manager-level research boundaries without starting providers or native CLIs."""
import copy
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "prism_local"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import backends
from research import RECORD, ResearchKnowledge
from research_access import NotationStop, ResearchAccess
from test_agent import manager
from test_research import project_with


class ResearchAgentGuard(unittest.TestCase):
    def setUp(self):
        self.project = project_with({"main.tex": "$x$"})
        self.knowledge = ResearchKnowledge(self.project)

    def backend(self, kind, provider=None):
        backend = backends.Backend(provider or kind)
        backend.kind = kind
        backend.unavailable = mock.Mock(return_value=None)
        backend.preflight = mock.Mock(return_value=None)
        backend.check_session = mock.Mock(return_value=None)
        backend.run = mock.Mock(side_effect=AssertionError("No provider may run in this test"))
        return backend

    def managed(self, backend, knowledge=True):
        result = manager(self.project.root, lambda: ["main.tex"], **{backend.id: backend})
        if knowledge:
            result.research_fn = lambda: self.knowledge
        return result

    def start_without_provider(self, managed, *args, **kwargs):
        # Exercise actual start, snapshots and Job construction. Leave the backend
        # thread unstarted so no CLI or inference request can be made.
        with mock.patch("agent.threading.Thread") as thread:
            result = managed.start(*args, **kwargs)
        return result, thread

    def test_initialized_cli_rejects_ask_edit_resume_and_historical_absolute_paths(self):
        self.knowledge.initialize()
        source = (self.project.root / "main.tex").read_bytes()
        record = (self.project.root / RECORD).read_bytes()
        for kind in ("claude", "codex", "deepcode"):
            for mode in ("ask", "edit"):
                for session in (None, "restored-session"):
                    with self.subTest(kind=kind, mode=mode, session=session):
                        backend = self.backend(kind)
                        managed = self.managed(backend)
                        result, thread = self.start_without_provider(managed,
                            "Earlier history names /old/checkout/main.tex. Continue that work.", session, mode,
                            scope=["main.tex"], active_file="main.tex")
                        self.assertIn("does not yet protect", result["error"])
                        self.assertIn("API provider", result["error"])
                        self.assertIsNone(managed.active)
                        self.assertEqual(managed.jobs, {})
                        thread.assert_not_called()
                        backend.run.assert_not_called()
        self.assertEqual((self.project.root / "main.tex").read_bytes(), source)
        self.assertEqual((self.project.root / RECORD).read_bytes(), record)

    def test_guard_uses_backend_kind_even_when_provider_id_looks_like_api(self):
        self.knowledge.initialize()
        backend = self.backend("codex", provider="openai")
        result, thread = self.start_without_provider(self.managed(backend), "Continue", "session", "ask")
        self.assertIn("does not yet protect", result["error"])
        thread.assert_not_called()

    def test_uninitialized_cli_preserves_scope_session_and_start_behavior(self):
        for kind in ("claude", "codex", "deepcode"):
            for mode in ("ask", "edit"):
                with self.subTest(kind=kind, mode=mode):
                    backend = self.backend(kind)
                    managed = self.managed(backend)
                    result, thread = self.start_without_provider(managed, "Read the project.", "restored-session",
                        mode, scope=["main.tex"], active_file="main.tex")
                    self.assertNotIn("error", result)
                    job = managed.jobs[result["job"]]
                    self.assertEqual(job.session_id, "restored-session")
                    self.assertEqual(job.mode, mode)
                    self.assertEqual(job.scope, ["main.tex"] if mode == "edit" else None)
                    self.assertIsNone(job.research_access)
                    self.assertIn("Scope for this turn", job.prompt)
                    backend.preflight.assert_called_once_with(self.project.root)
                    backend.check_session.assert_called_once_with(self.project.root, "restored-session")
                    thread.return_value.start.assert_called_once_with()
                    backend.run.assert_not_called()
        self.assertFalse((self.project.root / RECORD).exists())

    def test_manager_without_research_hook_preserves_existing_cli_start(self):
        self.knowledge.initialize()
        backend = self.backend("codex")
        managed = self.managed(backend, knowledge=False)
        result, thread = self.start_without_provider(managed, "Read", None, "ask")
        self.assertNotIn("error", result)
        self.assertIsNone(managed.jobs[result["job"]].research_access)
        thread.return_value.start.assert_called_once_with()

    def test_api_receives_access_and_its_cancel_event_stops_recording_and_writes(self):
        self.knowledge.initialize()
        backend = self.backend("openai", provider="custom-api")
        managed = self.managed(backend)
        result, thread = self.start_without_provider(managed, "Continue", "api-session", "edit", scope=["main.tex"])
        self.assertNotIn("error", result)
        job = managed.jobs[result["job"]]
        self.assertIsInstance(job.research_access, ResearchAccess)
        self.assertIs(job.research_access.knowledge, self.knowledge)
        self.assertIs(job.research_access.cancel, job.cancel)
        self.assertTrue(job.research_access.initialized)
        self.assertEqual(job.scope, ["main.tex"])
        thread.return_value.start.assert_called_once_with()
        source = (self.project.root / "main.tex").read_bytes()
        record = (self.project.root / RECORD).read_bytes()
        self.assertEqual(managed.stop(job.id), {"ok": True})
        self.assertTrue(job.cancel.is_set())
        with self.assertRaises(NotationStop):
            job.research_access.propose({"notation": "z", "definition": "An error.",
                                        "scope": "The proof.", "reason": "Readability."})
        with self.assertRaises(NotationStop):
            job.research_access.apply_write(self.project.root / "main.tex", "$x+z$", source)
        self.assertEqual((self.project.root / RECORD).read_bytes(), record)
        self.assertEqual((self.project.root / "main.tex").read_bytes(), source)
        backend.run.assert_not_called()

    def test_each_api_turn_refreshes_baseline_instead_of_reusing_prior_access(self):
        self.knowledge.initialize()
        backend = self.backend("openai")
        managed = self.managed(backend)
        self.project.turn = managed
        with mock.patch.object(self.knowledge, "refresh", wraps=self.knowledge.refresh) as refresh:
            first, _thread = self.start_without_provider(managed, "First", None, "ask")
            first_job = managed.jobs[first["job"]]
            original = copy.deepcopy(first_job.research_access.state)
            first_job.done = True
            self.knowledge.correct(notes="The author corrected the baseline between Turns.")
            (self.project.root / "main.tex").write_text("$x+z$", encoding="utf-8")
            second, _thread = self.start_without_provider(managed, "Resume", "api-session", "ask")
        second_job = managed.jobs[second["job"]]
        self.assertEqual(refresh.call_count, 2)
        self.assertIsNot(first_job.research_access, second_job.research_access)
        self.assertEqual(first_job.research_access.state, original)
        current = second_job.research_access.state
        self.assertEqual(current["corrections"][-1]["notes"], "The author corrected the baseline between Turns.")
        self.assertEqual([item["canonical"] for item in current["unverified_symbols"]], ["z"])
        self.assertIn("The author corrected the baseline between Turns.", second_job.research_access.context())
        self.assertIsNot(first_job.cancel, second_job.cancel)
        self.assertIs(second_job.research_access.cancel, second_job.cancel)

    def test_api_hook_is_created_before_initialization_and_sees_later_initialization(self):
        backend = self.backend("openai")
        managed = self.managed(backend)
        self.project.turn = managed
        first, _thread = self.start_without_provider(managed, "Read", None, "ask")
        first_job = managed.jobs[first["job"]]
        self.assertFalse(first_job.research_access.initialized)
        first_job.done = True
        self.knowledge.initialize()
        second, _thread = self.start_without_provider(managed, "Now continue", "api-session", "ask")
        second_access = managed.jobs[second["job"]].research_access
        self.assertTrue(second_access.initialized)
        self.assertEqual([s["canonical"] for s in second_access.state["symbols"]], ["x"])
        self.assertFalse(first_job.research_access.initialized)

    def test_unreadable_knowledge_fails_closed_before_creating_a_job(self):
        for kind in ("openai", "codex"):
            with self.subTest(kind=kind):
                backend = self.backend(kind)
                managed = self.managed(backend)
                broken = mock.Mock()
                broken.status.side_effect = ValueError("damaged record")
                broken.refresh.side_effect = OSError("unreadable record")
                managed.research_fn = lambda: broken
                result, thread = self.start_without_provider(managed, "Continue", None, "ask")
                self.assertIn("Research baseline could not be read", result["error"])
                self.assertIsNone(managed.active)
                thread.assert_not_called()

    def test_backend_launch_failure_does_not_publish_unchecked_mathematical_diagnostics(self):
        self.knowledge.initialize()
        backend = self.backend('openai')
        backend.run.side_effect = ValueError('The protected process failed at $z$.')
        managed = self.managed(backend)
        started = managed.start('Read the existing manuscript.', None, 'ask')
        self.assertNotIn('error', started)
        job = managed.jobs[started['job']]
        with job.cond:
            self.assertTrue(job.cond.wait_for(lambda: job.done, timeout=3))
        visible = '\n'.join(str(e.get(k, '')) for e in job.events for k in ('message','error','text'))
        self.assertNotIn('$z$', visible)
        self.assertTrue(any(e['t'] == 'error' for e in job.events))

    def test_safety_cancellation_reports_failure_instead_of_a_human_stop(self):
        self.knowledge.initialize()
        backend = self.backend('openai')
        def fail(job):
            job.cancel.set()
            return {'is_error': True, 'cancelled_by_backend': True,
                    'error': 'The protected transport failed.'}
        backend.run.side_effect = fail
        managed = self.managed(backend)
        started = managed.start('Read the existing manuscript.', None, 'ask')
        self.assertNotIn('error', started)
        job = managed.jobs[started['job']]
        with job.cond:
            self.assertTrue(job.cond.wait_for(lambda: job.done, timeout=3))
        done = next(e for e in job.events if e['t'] == 'done')
        self.assertEqual(done['outcome'], 'failed')
        self.assertTrue(done['is_error'])
        self.assertEqual((self.project.root/'main.tex').read_text(), '$x$')

    def test_controlled_cli_preserves_external_saves_outside_its_write_authority(self):
        self.knowledge.initialize()
        target = self.project.root/'sections/intro.tex'
        target.parent.mkdir()
        target.write_text('original prose')
        for mode, scope in (('ask', None), ('edit', ['main.tex'])):
            with self.subTest(mode=mode):
                backend = self.backend('openai')
                backend.enforces_scope = backend.read_only_ask = False
                def run(job):
                    job.controlled_cli = True
                    target.write_text('human prose preserved')
                    return {'exit': 0}
                backend.run.side_effect = run
                managed = self.managed(backend)
                started = managed.start('Read this manuscript.', None, mode, scope=scope)
                self.assertNotIn('error', started)
                job = managed.jobs[started['job']]
                with job.cond:
                    self.assertTrue(job.cond.wait_for(lambda: job.done, timeout=3))
                done = next(e for e in job.events if e['t']=='done')
                self.assertEqual(target.read_text(), 'human prose preserved')
                self.assertEqual(done['reverted'], [])
                self.assertNotIn('sections/intro.tex', [c['path'] for c in done['changed']])
                managed.undo(job.id)
                self.assertEqual(target.read_text(), 'human prose preserved')
                target.write_text('original prose')

    def test_research_preflight_is_selected_only_for_the_controlled_path(self):
        backend = self.backend('deepcode')
        backend.research_support = True
        backend.preflight.return_value = 'Legacy settings cannot isolate external MCP.'
        backend.research_preflight = mock.Mock(return_value=None)
        protected = self.managed(backend)
        result, _thread = self.start_without_provider(protected, 'Read existing material.', None, 'ask')
        self.assertNotIn('error', result)
        self.assertIsInstance(protected.jobs[result['job']].research_access, ResearchAccess)
        legacy = self.managed(backend, knowledge=False)
        result, _thread = self.start_without_provider(legacy, 'Read existing material.', None, 'ask')
        self.assertIn('Legacy settings', result['error'])
        self.assertIsNone(legacy.active)

    def test_controlled_cli_undo_contains_only_unchanged_parent_results(self):
        import snapshot
        self.knowledge.initialize()
        main = self.project.root/'main.tex'
        intro = self.project.root/'intro.tex'
        intro.write_text('original prose')
        for later_manual in (False, True):
            with self.subTest(later_manual=later_manual):
                main.write_text('$x$')
                intro.write_text('original prose')
                backend = self.backend('openai')
                backend.enforces_scope = backend.read_only_ask = False
                def run(job):
                    job.controlled_cli = True
                    main.write_text('$x+x$')
                    job.controlled_writes = {'main.tex': snapshot.file_state(main, budget=0)}
                    intro.write_text('human prose preserved')
                    if later_manual:
                        main.write_text('$x+x$\nHuman addition.\n')
                    return {'exit': 0}
                backend.run.side_effect = run
                managed = self.managed(backend)
                started = managed.start('Edit the known estimate.', None, 'edit')
                job = managed.jobs[started['job']]
                with job.cond:
                    self.assertTrue(job.cond.wait_for(lambda: job.done, timeout=3))
                done = next(e for e in job.events if e['t']=='done')
                self.assertEqual([c['path'] for c in done['changed']], [] if later_manual else ['main.tex'])
                managed.undo(job.id)
                self.assertEqual(intro.read_text(), 'human prose preserved')
                self.assertEqual(main.read_text(), '$x+x$\nHuman addition.\n' if later_manual else '$x$')


if __name__ == "__main__":
    unittest.main()
