from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'experiments/generalization_v1'),str(ROOT/'src'),str(ROOT)]
from prepare_fresh_live_candidates import project,choose


def task(n,repo='new/library'):
    return dict(repo=repo,instance_id=repo.replace('/','__')+'-'+str(n),base_commit=str(n).zfill(40),
        problem_statement='Parsing error '+str(n)+' should retain an empty string.',created_at='2025-06-01')


class FreshCandidatesTests(unittest.TestCase):
    def test_public_projection_discards_answers_hints_difficulty(self):
        a=task(1);b=dict(a,patch='secret answer',test_patch='assert answer',hints_text='solve',difficulty={'lines':1})
        self.assertEqual(project(a,'source','month.parquet'),project(b,'source','month.parquet'))
        self.assertNotIn('patch',project(b,'source','month.parquet'))

    def test_wrong_identity_or_missing_public_text_rejected(self):
        for changed in ({'base_commit':'invalid'},{'repo':'../outside'},{'instance_id':'../bad'},{'problem_statement':None}):
            with self.assertRaises(ValueError):project(dict(task(1),**changed),'source','month.parquet')

    def test_local_references_and_development_repo_excluded(self):
        pool=[project(task(i,repo),'source','month.parquet') for i,repo in ((1,'old/lib'),(2,'new/lib'),(3,'other/lib'))]
        selected,excluded,_=choose(pool,[project(task(5,'old/lib'),'source','month.parquet')],{'new__lib-2'},12,2,'fixed')
        self.assertEqual([r['ticket_id'] for r in selected],['other__lib-3'])
        self.assertEqual({r['reason'] for r in excluded},{'local_history_reference','development_repository'})

    def test_sampling_stable_under_source_order_and_per_repo_limit(self):
        pool=[project(task(i,'repo'+str(i%3)+'/lib'),'source','month.parquet') for i in range(1,10)]
        a=choose(pool,[],set(),5,2,'fixed')[0];b=choose(list(reversed(pool)),[],set(),5,2,'fixed')[0]
        self.assertEqual(a,b);self.assertEqual(len(a),5)
        self.assertLessEqual(max(sum(t['repo']==r['repo'] for t in a) for r in a),2)

    def test_duplicate_ids_or_invalid_budget_refused(self):
        row=project(task(1),'source','month.parquet')
        with self.assertRaises(ValueError):choose([row,row],[],set(),2,2,'fixed')
        with self.assertRaises(ValueError):choose([row],[],set(),0,2,'fixed')


if __name__=='__main__':unittest.main()
