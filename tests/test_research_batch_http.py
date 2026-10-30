"""Batch and direct human decisions through the editor's HTTP boundary."""
import json
import unittest

import test_research_decisions_http as single
import test_research_guard_http as guard
import test_research_http as base
from test_agent import call_chunks


class ResearchBatchHTTP(unittest.TestCase):
    setUp = single.ResearchDecisionsHTTP.setUp
    tearDown = single.ResearchDecisionsHTTP.tearDown
    start = single.ResearchDecisionsHTTP.start
    stop = single.ResearchDecisionsHTTP.stop
    request = single.ResearchDecisionsHTTP.request
    initialize = single.ResearchDecisionsHTTP.initialize
    turn = single.ResearchDecisionsHTTP.turn
    events = single.ResearchDecisionsHTTP.events
    finish = single.ResearchDecisionsHTTP.finish
    requests = single.ResearchDecisionsHTTP.requests
    reply = single.ResearchDecisionsHTTP.reply

    def state(self):
        code, state = self.request('/api/research')
        self.assertEqual(code, 200, state)
        return state

    def propose_batch(self, names=('z', 'w', 'v')):
        calls = [(name, 'propose_notation', {**guard.PROPOSAL, 'notation': name}) for name in names]
        events = self.finish(self.turn([[guard.call(*calls)]]))
        return next(e['session_id'] for e in events if e['t'] == 'init'), events

    def test_one_completion_saves_each_candidate_before_ending_the_turn(self):
        self.initialize()
        original = (self.root / 'main.tex').read_bytes()
        _session, events = self.propose_batch()
        self.assertEqual([p['canonical'] for p in self.state()['proposals']], ['z', 'w', 'v'])
        self.assertEqual([e['proposal']['canonical'] for e in events if e['t'] == 'notation_proposal'], ['z', 'w', 'v'])
        self.assertEqual((self.root / 'main.tex').read_bytes(), original)
        self.assertEqual(len(self.requests()), 1)

    def test_complete_direct_instruction_needs_no_proposal_or_second_approval(self):
        self.initialize()
        original = (self.root / 'main.tex').read_text()
        changed = original.replace(r"$\ell+\ell_+$", "$h$")
        code, started = self.reply("Use $h$ for $\\ell+\\ell_+$.", None, [
            call_chunks('direct', 'write_file', {'path': 'main.tex', 'content': changed}),
            [base.say('The author specified $h$.')]])
        self.assertEqual(code, 200, started)
        events = self.finish(started['job'])
        self.assertEqual((self.root / 'main.tex').read_text(), changed)
        state = self.state()
        self.assertEqual(state['proposals'], [])
        self.assertEqual(state['adoptions'][0]['canonical'], 'h')
        self.assertEqual(state['adoptions'][0]['actor'], 'human')
        self.assertFalse(any(e['t'] == 'notation_proposal' for e in events))

    def test_each_batch_clause_decides_only_its_item_and_replacement(self):
        self.initialize()
        session, _ = self.propose_batch()
        code, started = self.reply('Approve $z$, reject $w$, and use $q$ instead of $v$.', session,
                                   [[base.say('Continue with the original estimate.')]])
        self.assertEqual(code, 200, started)
        self.finish(started['job'])
        state = self.state()
        self.assertEqual({p['canonical']: p['status'] for p in state['proposals']},
                         {'z': 'approved', 'w': 'rejected', 'v': 'rejected'})
        self.assertIn('q', {p['canonical'] for p in state['symbols']})
        self.assertEqual([a['canonical'] for a in state['adoptions']], ['q'])
        self.assertNotIn('q', {p['canonical'] for p in state['proposals']})

    def test_delayed_direct_instruction_cannot_release_source_or_chat(self):
        self.initialize()
        original = (self.root / 'main.tex').read_text()
        changed = original.replace(r"$\ell+\ell_+$", "$h$")
        code, started = self.reply(r"Use $h$ for $\ell+\ell_+$ only after I approve it.", None, [
            call_chunks('delayed', 'write_file', {'path': 'main.tex', 'content': changed}),
            [base.say('The estimate uses $h$.')]])
        self.assertEqual(code, 200, started)
        events = self.finish(started['job'])
        self.assertEqual((self.root / 'main.tex').read_text(), original)
        self.assertEqual(self.state()['adoptions'], [])
        self.assertTrue(any(e['t'] == 'notation_clarification' for e in events))
        self.assertNotIn('$h$', ''.join(e.get('text', '') for e in events if e['t'] == 'delta'))
        # A later unconditional instruction still grants the same notation.
        self.human(r"Use $h$ for $\ell+\ell_+$.", script=[
            call_chunks('approved', 'write_file', {'path': 'main.tex', 'content': changed})])
        self.assertEqual((self.root / 'main.tex').read_text(), changed)

    def test_partial_ambiguity_does_not_expand_or_discard_other_decisions(self):
        self.initialize()
        session, _ = self.propose_batch()
        code, started = self.reply('Approve $z$; maybe approve $w$.', session,
                                   [[base.say('The remaining items await a decision.')]])
        self.assertEqual(code, 200, started)
        events = self.finish(started['job'])
        self.assertEqual({p['canonical']: p['status'] for p in self.state()['proposals']},
                         {'z': 'approved', 'w': 'pending', 'v': 'pending'})
        self.assertTrue(any(e['t'] == 'notation_clarification' for e in events))
        code, started = self.reply('Reject $w$.', session, [[base.say('Keep its original expression.')]])
        self.assertEqual(code, 200, started)
        self.finish(started['job'])
        self.assertEqual({p['canonical']: p['status'] for p in self.state()['proposals']},
                         {'z': 'approved', 'w': 'rejected', 'v': 'pending'})

    def test_expression_refusal_blocks_renaming_in_proposals_source_and_chat(self):
        self.initialize()
        session, _ = self.propose_batch(('z',))
        original = (self.root / 'main.tex').read_text()
        code, started = self.reply('Reject $z$ and keep its expression expanded.', session,
                                   [[base.say('Keep the expression expanded.')]])
        self.assertEqual(code, 200, started)
        self.finish(started['job'])
        rejected = self.state()
        self.assertEqual(rejected['rejections'][0]['action']['rejection_scope'], 'expression')
        for script in (
            [call_chunks('rename', 'propose_notation', {**guard.PROPOSAL, 'notation': 'q', 'source_expression': ''})],
            [call_chunks('raw-write', 'write_file', {'path': 'main.tex', 'content': original.replace(r"$\ell+\ell_+$", '$q$')})],
            [[base.say(r'The abbreviation is $q=\ell+\ell_+$.')]],
        ):
            with self.subTest(script=script):
                events = self.finish(self.turn(script, mode='edit'))
                self.assertEqual((self.root / 'main.tex').read_text(), original)
                self.assertFalse(any(e['t'] == 'notation_proposal' for e in events))
                self.assertNotIn('$q', ''.join(e.get('text', '') for e in events if e['t'] == 'delta'))
                self.assertEqual(len(self.state()['proposals']), 1)

    def human(self, text, session=None, script=None, **extra):
        code, started = self.reply(text, session, script or [[base.say('Continue the existing work.')]], **extra)
        self.assertEqual(code, 200, started)
        return self.finish(started['job'])

    def test_yes_cannot_approve_remaining_batch_items_after_a_partial_reply(self):
        self.initialize()
        session, _ = self.propose_batch()
        self.human('Approve $z$; reject $w$.', session)
        events = self.human('Yes', session)
        self.assertEqual({p['canonical']: p['status'] for p in self.state()['proposals']},
                         {'z': 'approved', 'w': 'rejected', 'v': 'pending'})
        self.assertTrue(any(e['t'] == 'notation_clarification' for e in events))

    def test_finite_family_allows_only_declared_complete_variants(self):
        self.initialize()
        self.human('Use $a_i$ for $x+x$ with i in {1,2,3}.')
        state = self.state()
        self.assertEqual(state['families'][0]['rule'], {'base': 'a', 'indices': ['1', '2', '3']})
        self.assertEqual(state['proposals'], [])
        original = (self.root / 'main.tex').read_text()
        changed = original.replace(r"$\ell+\ell_+$", '$a_{2}$')
        self.human('Use the allowed member.', script=[call_chunks('family', 'write_file', {'path': 'main.tex', 'content': changed}), [base.say('The existing $a_2$ is available.')]])
        self.assertEqual((self.root / 'main.tex').read_text(), changed)
        for notation in ('a_4', 'a_2^+', "a'_2", 'b_2'):
            events = self.finish(self.turn([[base.say('A draft uses $' + notation + '$.')]]))
            self.assertNotIn('$' + notation + '$', ''.join(e.get('text', '') for e in events if e['t'] == 'delta'))
            self.assertTrue(any(e['t'] == 'notation_proposal' for e in events))

    def test_name_rejection_allows_other_proposals_but_only_explicit_reopen_reconsiders(self):
        self.initialize()
        session, _ = self.propose_batch(('z',))
        self.human('Reject $z$.', session)
        self.human('Approve $z$.', session)
        self.assertEqual(self.state()['proposals'][0]['status'], 'rejected')
        events = self.finish(self.turn([call_chunks('other', 'propose_notation', {**guard.PROPOSAL, 'notation': 'q'})]))
        self.assertTrue(any(e['t'] == 'notation_proposal' for e in events))
        # A different Conversation can explicitly reopen the original; it is still unapproved.
        events = self.human('Reopen $z$.')
        new_session = next(e['session_id'] for e in events if e['t'] == 'init')
        self.assertEqual(self.state()['proposals'][0]['status'], 'pending')
        self.assertNotIn('z', {s['canonical'] for s in self.state()['symbols']})
        self.human('Yes', new_session)
        self.assertEqual(self.state()['proposals'][0]['status'], 'approved')
        self.assertTrue(any(r.get('decision') == 'rejected' for r in self.state()['decisions']))

    def test_batch_save_failure_is_atomic_and_does_not_start_model(self):
        self.initialize()
        session, _ = self.propose_batch()
        before = (self.root / 'research/knowledge.json').read_bytes()
        requests = len(self.requests())
        (self.work / 'fail-persistence').touch()
        code, failed = self.reply('Approve $z$; reject $w$; use $q$ instead of $v$.', session, [[base.say('Must not run.')]])
        self.assertEqual(code, 409, failed)
        self.assertEqual((self.root / 'research/knowledge.json').read_bytes(), before)
        self.assertEqual(len(self.requests()), requests)
        (self.work / 'fail-persistence').unlink()
        self.human('Approve $z$; reject $w$.', session)
        self.assertEqual(self.state()['proposals'][0]['status'], 'approved')

    def test_family_and_direct_save_failure_never_release_source(self):
        self.initialize()
        original = (self.root / 'main.tex').read_bytes()
        for text in ('Use $h$ for $x+x$.', 'Use $a_i$ for $x+x$ with i in {1,2,3}.'):
            (self.work / 'fail-persistence').touch()
            requests = len(self.requests())
            code, failed = self.reply(text, None, [[base.say('Must not run.')]])
            self.assertEqual(code, 409, failed)
            self.assertEqual(len(self.requests()), requests)
            self.assertEqual((self.root / 'main.tex').read_bytes(), original)
            (self.work / 'fail-persistence').unlink()
        self.assertEqual(self.state()['adoptions'], [])
        self.assertEqual(self.state()['families'], [])

    def test_rules_and_rejection_survive_restart_model_and_conversation_changes(self):
        self.initialize()
        session, _ = self.propose_batch(('z',))
        self.human('Reject $z$ and keep its expression expanded.', session)
        self.human('Use $a_i$ for $x+x$ with i in {1,2,3}.')
        before = self.state()
        self.stop(); self.start()
        self.assertEqual(self.state(), before)
        self.human('Discuss existing $a_2$.', model='another-model', script=[[base.say('Existing $a_2$ remains available.')]])
        events = self.finish(self.turn([call_chunks('blocked', 'propose_notation', {**guard.PROPOSAL, 'notation': 'r'})]))
        self.assertFalse(any(e['t'] == 'notation_proposal' for e in events))
        self.assertEqual(self.state()['rejections'][0]['human_text'], 'Reject $z$ and keep its expression expanded.')

    def test_tampered_family_and_adoption_mirrors_do_not_grant_permission(self):
        self.initialize()
        self.human('Use $a_i$ for $x+x$ with i in {1,2,3}.')
        self.human('Use $h$ for $x+x$.')
        trusted = self.state()
        record = self.root / 'research/knowledge.json'
        data = json.loads(record.read_text())
        for r in data['decisions']:
            if r.get('kind') == 'family':r['rule']['indices'].append('9')
            if r.get('kind') == 'adopt':r['canonical'] = r['notation'] = 'q'
        data['families'] = [{'rule': {'base': 'a', 'indices': ['9']}}]
        data['adoptions'] = [{'notation': 'q', 'canonical': 'q'}]
        record.write_text(json.dumps(data))
        raw = record.read_bytes()
        self.assertEqual(self.state()['families'], trusted['families'])
        self.assertEqual(self.state()['adoptions'], trusted['adoptions'])
        self.assertEqual(record.read_bytes(), raw)
        for notation in ('a_9', 'q'):
            events = self.finish(self.turn([[base.say('Draft $' + notation + '$.')]]))
            self.assertNotIn('$' + notation + '$', ''.join(e.get('text', '') for e in events if e['t'] == 'delta'))
            self.assertNotIn(notation, {row['canonical'] for row in self.state()['symbols']})
            self.assertEqual(record.read_bytes(), raw)

    def test_quoted_conditional_or_model_direct_instructions_cannot_adopt(self):
        self.initialize()
        for raw in ('> Use $h$ for $x+x$.', 'If needed, use $h$ for $x+x$.', 'The model says Use $h$ for $x+x$.'):
            self.human(raw, script=[[base.say('Discuss the existing expression.')]])
        self.human('Discuss existing work.', script=[[base.say('Use $h$ for $x+x$.')]])
        self.assertEqual(self.state()['adoptions'], [])

    def test_standalone_expression_refusal_needs_no_fake_proposal(self):
        self.initialize()
        self.human('保持 $x+x$ 展开，不要简称。')
        self.assertEqual(self.state()['proposals'], [])
        self.assertEqual(self.state()['rejections'][0]['kind'], 'refuse_expression')
        self.assertEqual(self.state()['rejections'][0]['decision'], 'rejected')
        events = self.finish(self.turn([[base.say('Let $r=x+x$.')]]))
        self.assertFalse(any(e['t'] == 'notation_proposal' for e in events))
        self.assertNotIn('$r', ''.join(e.get('text', '') for e in events if e['t'] == 'delta'))

    def test_expanded_expression_cannot_be_replaced_by_equivalent_braced_known_member(self):
        self.initialize()
        session, _ = self.propose_batch(('z',))
        self.human('Reject $z$ and keep its expression expanded.', session)
        self.human('Use $a_i$ for $x+x$ with i in {1,2,3}.')
        original = (self.root / 'main.tex').read_text()
        changed = original.replace(r"$\ell+\ell_+$", '$a_{2}$')
        events = self.finish(self.turn([call_chunks('braced', 'write_file', {'path': 'main.tex', 'content': changed})], mode='edit'))
        self.assertEqual((self.root / 'main.tex').read_text(), original)
        self.assertFalse(any(e['t'] == 'notation_proposal' for e in events))

    def test_expression_refusal_checks_reversed_definitions_before_chat_display(self):
        self.initialize()
        session, _ = self.propose_batch(('z',))
        self.human('Reject $z$ and keep its expression expanded.', session)
        self.human('Use $a_i$ for $x+x$ with i in {1,2,3}.')
        text = r'Use the abbreviation $\ell+\ell_+=a_2$.'
        events = self.finish(self.turn([[base.say(text)]]))
        self.assertNotIn(text, ''.join(e.get('text', '') for e in events if e['t'] == 'delta'))
        self.assertFalse(any(e['t'] == 'notation_proposal' for e in events))

    def test_project_record_reordering_cannot_revoke_a_later_expression_refusal(self):
        self.initialize()
        self.human(r'Use $h$ for $\ell+\ell_+$.')
        self.human(r'Keep $\ell+\ell_+$ expanded; do not abbreviate it.')
        self.assertEqual(len(self.state()['rejections']), 1)
        record = self.root / 'research/knowledge.json'
        data = json.loads(record.read_text()); data['decisions'].reverse(); record.write_text(json.dumps(data))
        self.assertEqual(len(self.state()['rejections']), 1)
        original = (self.root / 'main.tex').read_text()
        self.finish(self.turn([call_chunks('reordered', 'write_file', {'path': 'main.tex', 'content': original.replace(r"$\ell+\ell_+$", '$h$')})], mode='edit'))
        self.assertEqual((self.root / 'main.tex').read_text(), original)

    def test_later_expression_refusal_also_withholds_an_earlier_adopted_abbreviation(self):
        self.initialize()
        self.human('Use h for x+x.')
        self.human('Keep $x+x$ expanded; do not abbreviate it.')
        events = self.finish(self.turn([[base.say('Continue the abbreviation $h$.')]]))
        self.assertNotIn('$h$', ''.join(e.get('text', '') for e in events if e['t'] == 'delta'))
        self.assertFalse(any(e['t'] == 'notation_proposal' for e in events))
        self.assertEqual(self.state()['adoptions'], [])

    def test_same_message_withdrawal_cannot_leave_a_clear_clause_approved(self):
        self.initialize()
        self.human("Use $h$ for $x+x$; don't use $h$ after all.")
        self.assertNotIn('h', {s['canonical'] for s in self.state()['symbols']})
        self.assertEqual(self.state()['adoptions'], [])
        session, _ = self.propose_batch(('z',))
        self.human("Approve $z$; don't approve $z$.", session)
        self.assertEqual(self.state()['proposals'][0]['status'], 'pending')

    def test_unselected_stale_item_does_not_block_other_clear_batch_or_direct_actions(self):
        self.initialize()
        session, _ = self.propose_batch(('z', 'w'))
        self.finish(self.turn([call_chunks('revise', 'propose_notation', {**guard.PROPOSAL, 'definition': 'A revised definition.'})]))
        self.human('Approve $w$.', session)
        self.human('Use h for x+x.', session)
        state = self.state()
        self.assertEqual(state['proposals'][0]['status'], 'pending')
        self.assertEqual(state['proposals'][1]['status'], 'approved')
        self.assertIn('h', {s['canonical'] for s in state['symbols']})

    def test_pending_mirror_cannot_erase_a_signed_rejection_or_replace_reopen(self):
        self.initialize()
        session, _ = self.propose_batch(('z',))
        self.human('Reject $z$.', session)
        record = self.root / 'research/knowledge.json'
        data = json.loads(record.read_text()); data['proposals'][0]['status'] = 'pending'; record.write_text(json.dumps(data))
        self.human('Approve $z$.', session)
        self.assertEqual(self.state()['proposals'][0]['status'], 'rejected')
        self.assertNotIn('z', {s['canonical'] for s in self.state()['symbols']})
        self.human('Reopen $z$.', session)
        self.human('Yes', session)
        self.assertEqual(self.state()['proposals'][0]['status'], 'approved')

    def test_batch_proposals_precede_sibling_write_and_compile(self):
        self.initialize()
        original = (self.root / 'main.tex').read_bytes()
        events = self.finish(self.turn([[guard.call(
            ('write-first', 'write_file', {'path': 'main.tex', 'content': '$q$'}),
            ('one', 'propose_notation', guard.PROPOSAL),
            ('compile', 'compile', {}),
            ('two', 'propose_notation', {**guard.PROPOSAL, 'notation': 'w'}),
        )]], mode='edit'))
        self.assertEqual((self.root / 'main.tex').read_bytes(), original)
        self.assertEqual([p['canonical'] for p in self.state()['proposals']], ['z', 'w'])
        self.assertFalse(any(e['t'] == 'build' for e in events))
        self.assertEqual(len(self.requests()), 1)

    def test_invalid_second_proposal_or_failed_save_never_persists_half_a_batch(self):
        self.initialize()
        before = (self.root / 'research/knowledge.json').read_bytes()
        for invalid in ({**guard.PROPOSAL, 'notation': 'w', 'definition': ''}, 'not an object'):
            events = self.finish(self.turn([[guard.call(
                ('one', 'propose_notation', guard.PROPOSAL),
                ('two', 'propose_notation', invalid),
            )]]))
            self.assertEqual((self.root / 'research/knowledge.json').read_bytes(), before)
            self.assertFalse(any(e['t'] == 'notation_proposal' for e in events))
        (self.work / 'fail-persistence').touch()
        _session, events = self.propose_batch(('z','w'))
        self.assertEqual((self.root / 'research/knowledge.json').read_bytes(), before)
        self.assertFalse(any(e['t'] == 'notation_proposal' for e in events))
        (self.work / 'fail-persistence').unlink()


if __name__ == '__main__':
    unittest.main()
