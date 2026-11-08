from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1'))
from paired_dispatch_store import DispatchStore,ReplayBlocked,atomic_json,exclusive_run,digest
from run_paired_bounded import summarize,validate_completed


class PairedDispatchTests(unittest.TestCase):
    def store(self,root,cap=3,total=6):return DispatchStore(root,['a|A3','a|A3_summary'],cap,total,1)
    def payload(self,n=1):return dict(prompt='code '+str(n),suffix='end',options={'seed':42})
    def reply(self):return dict(response='saved code',done=True,done_reason='stop',prompt_eval_count=10,eval_count=2)

    def test_restart_replays_exact_reply_without_new_send(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);store=self.store(root);calls=[]
            store.dispatch('a|A3',self.payload(),lambda p:calls.append(p) or self.reply())
            resumed=self.store(root)
            result=resumed.dispatch('a|A3',self.payload(),lambda p:self.fail('redispatched'))
            self.assertEqual(result,self.reply());self.assertEqual(len(calls),1);self.assertEqual(len(resumed.events),1)

    def test_per_unit_cap_shared_over_restarts(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);store=self.store(root)
            for n in range(3):store.dispatch('a|A3',self.payload(n),lambda p:self.reply())
            resumed=self.store(root)
            for n in range(3):resumed.dispatch('a|A3',self.payload(n),lambda p:self.fail('replay sent'))
            with self.assertRaises(ReplayBlocked):resumed.dispatch('a|A3',self.payload(4),lambda p:self.fail('cap sent'))

    def test_total_cap_and_probe_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            store=self.store(Path(temp),total=1)
            store.dispatch(None,self.payload(0),lambda p:self.reply())
            store.dispatch('a|A3',self.payload(),lambda p:self.reply())
            with self.assertRaises(ReplayBlocked):store.dispatch('a|A3_summary',self.payload(),lambda p:self.fail('total sent'))
            with self.assertRaises(ReplayBlocked):store.dispatch(None,self.payload(2),lambda p:self.fail('probe sent'))

    def test_interrupted_before_reply_never_resent(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);store=self.store(root)
            def interrupted(payload):raise KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):store.dispatch('a|A3',self.payload(),interrupted)
            resumed=self.store(root)
            with self.assertRaises(ReplayBlocked):resumed.dispatch('a|A3',self.payload(),lambda p:self.fail('uncertain sent'))
            self.assertEqual(len(resumed.events),1)

    def test_reply_saved_before_ledger_return_marker_recovers(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);store=self.store(root)
            def interrupted(payload):
                response=self.reply()
                atomic_json(root/'responses/0001.json',dict(request_sha256=digest(payload),response=response,response_sha256=digest(response)))
                raise KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):store.dispatch('a|A3',self.payload(),interrupted)
            resumed=self.store(root);resumed.dispatch('a|A3',self.payload(),lambda p:self.fail('saved reply sent'))
            self.assertEqual(resumed.events[0]['status'],'returned')

    def test_network_failure_is_uncertain_not_auto_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            store=self.store(Path(temp))
            def failed(payload):raise TimeoutError('local timeout')
            with self.assertRaises(ReplayBlocked):store.dispatch('a|A3',self.payload(),failed)
            self.assertEqual(store.events[0]['status'],'uncertain')

    def test_changed_replay_payload_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);store=self.store(root);store.dispatch('a|A3',self.payload(),lambda p:self.reply())
            with self.assertRaises(ReplayBlocked):self.store(root).dispatch('a|A3',self.payload(2),lambda p:self.fail('changed sent'))

    def test_changed_response_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);store=self.store(root);store.dispatch('a|A3',self.payload(),lambda p:self.reply())
            atomic_json(root/'responses/0001.json',dict(request_sha256=digest(self.payload()),response={'response':'changed'},response_sha256='old'))
            with self.assertRaises(ReplayBlocked):self.store(root).dispatch('a|A3',self.payload(),lambda p:self.fail('tampered sent'))

    def test_changed_ledger_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);atomic_json(root/'dispatch_ledger.json',dict(events=[],events_sha256='invalid'))
            with self.assertRaises(ValueError):self.store(root)

    def test_scope_and_actual_summary_header_recorded(self):
        with tempfile.TemporaryDirectory() as temp:
            store=self.store(Path(temp));payload=self.payload()
            payload['prompt']='# OBSERVED ORIGINAL BODY (AST facts, not a repair specification):\ncode'
            store.dispatch('a|A3_summary',payload,lambda p:self.reply(),scope='body')
            self.assertTrue(store.events[0]['source_summary_header_present']);self.assertEqual(store.events[0]['requested_scope'],'body')

    def test_exclusive_process_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with exclusive_run(root):
                with self.assertRaises(ValueError):
                    with exclusive_run(root):pass
            with exclusive_run(root):pass

    def test_paired_summary_keeps_blocked_cases_and_full_denominator(self):
        config=dict(full_denominator=2,seed=42,planned=dict(arms=['A3','A3_summary'],selected_ids=['a','b']))
        records=[dict(ticket_id=key,arm=arm,status='generated' if key=='a' and arm=='A3_summary' else 'blocked',
                      selected_candidate=key=='a' and arm=='A3_summary') for key in ('a','b') for arm in ('A3','A3_summary')]
        summary=summarize(records,SimpleNamespace(events=[]),config)
        self.assertEqual(summary['paired_outcomes'],{'summary_only':1,'neither':1})
        self.assertEqual(summary['paired_delta'],0.5);self.assertEqual(summary['arms']['A3_summary']['generation_rate_full_cohort'],0.5)

    def test_generated_and_selected_are_reported_separately(self):
        config=dict(full_denominator=1,seed=42,planned=dict(arms=['A3','A3_summary'],selected_ids=['a']))
        records=[dict(ticket_id='a',arm=arm,status='no_selected',selected_candidate=False,
                      generated_before_validation=arm=='A3_summary') for arm in ('A3','A3_summary')]
        result=summarize(records,SimpleNamespace(events=[]),config)
        self.assertEqual(result['arms']['A3_summary']['generated_cases'],1)
        self.assertEqual(result['arms']['A3_summary']['selected_cases'],0)

    def test_completed_artifact_change_stops_resume(self):
        from hashlib import sha256
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);file=root/'tickets/a/A3/error.json'
            atomic_json(file,dict(status='error'))
            record=dict(ticket_id='a',arm='A3',status='error',selected_candidate=False,repair_dispatches=0,
                        evidence_path='tickets/a/A3/error.json',evidence_sha256=sha256(file.read_bytes()).hexdigest())
            validate_completed(root,[record],SimpleNamespace(events=[]))
            atomic_json(file,dict(status='changed'))
            with self.assertRaises(ValueError):validate_completed(root,[record],SimpleNamespace(events=[]))


if __name__=='__main__':unittest.main()
