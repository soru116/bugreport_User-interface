from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1'))
from prepare_independent_candidates import select_candidates, scan_exposure, duplicate_reason


def ticket(n,repo='other/project'):
    return dict(ticket_id=repo.replace('/','__')+'-'+str(n),repo=repo,base_commit=str(n).zfill(40),
                title='Bug '+str(n),description='Description '+str(n))


class IndependentCandidatesTests(unittest.TestCase):
    def test_dev_id_repo_and_history_excluded(self):
        dev=[ticket(1,'known/project')]
        pool=[*dev,ticket(2,'known/project'),ticket(3),ticket(4)]
        selected,excluded,_=select_candidates(pool,dev,{ticket(3)['ticket_id']})
        self.assertEqual([r['ticket_id'] for r in selected],[ticket(4)['ticket_id']])
        self.assertEqual({e['reason'] for e in excluded},{'fixed_development_id','repository_overlaps_fixed36','local_artifact_reference'})

    def test_gold_success_and_hints_do_not_affect_selection(self):
        pool=[ticket(i) for i in range(1,9)]
        altered=[dict(r,patch='answer',hints_text='solve here',resolved=True,test_patch='assert') for r in pool]
        a=select_candidates(pool,[],set())[0];b=select_candidates(altered,[],set())[0]
        self.assertEqual(a,b);self.assertTrue(all('patch' not in r and 'hints_text' not in r for r in b))

    def test_duplicate_public_text_and_base(self):
        a=ticket(1);b=ticket(2)
        a['description']=b['description']=' '.join('token'+str(i) for i in range(50));a['title']=b['title']='same'
        self.assertEqual(duplicate_reason(b,[a])['reason'],'normalized_public_text_duplicate')
        b['description']='different';b['base_commit']=a['base_commit']
        self.assertEqual(duplicate_reason(b,[a])['reason'],'same_repo_base_commit')

    def test_short_text_not_duplicate_by_empty_fields(self):
        self.assertIsNone(duplicate_reason(ticket(2),[ticket(1)]))

    def test_plans_are_conservative_exposure_reserved_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'plan.json').write_text('{"selected_ids":["other__project-1"]}')
            (root/'sealed').mkdir();(root/'sealed'/'result.json').write_text('other__project-2')
            evidence,inventory,skipped=scan_exposure([root],{'other__project-1','other__project-2'})
            self.assertEqual(set(evidence),{'other__project-1'})
            self.assertEqual(len(inventory),1)
            self.assertFalse(evidence['other__project-1'][0]['execution_or_human_view_verified'])
            self.assertEqual(skipped[0]['reason'],'reserved_history_not_read')

    def test_missing_history_is_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            _,_,skipped=scan_exposure([Path(temp)/'absent'],set())
            self.assertEqual(skipped[0]['reason'],'missing_history_root')

    def test_duplicate_ids_refused_and_seed_stable(self):
        with self.assertRaises(ValueError):select_candidates([ticket(1),ticket(1)],[],set())
        pool=[ticket(i) for i in range(1,6)]
        self.assertEqual(select_candidates(pool,[],set())[0],select_candidates(list(reversed(pool)),[],set())[0])


if __name__=='__main__':unittest.main()
