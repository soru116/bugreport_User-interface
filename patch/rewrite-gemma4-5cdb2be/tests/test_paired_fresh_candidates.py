from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1'))
from prepare_paired_fresh_candidates import select_candidates


def ticket(i,repo='new/repo',text=None):
    return dict(ticket_id=repo.replace('/','__')+'-'+str(i),repo=repo,base_commit=str(i).zfill(40),
                problem_statement=text or ('Public parsing issue '+str(i)+' should preserve the empty string'))


class PairedCandidatesTests(unittest.TestCase):
    sampling=dict(target_cases=24,per_repository_max=2,minimum_description_chars=20)

    def test_short_description_excluded_without_filling_with_known_repo(self):
        pool=[ticket(1,text='short'),ticket(2,'known/repo')]
        selected,excluded,unsampled,_=select_candidates(pool,[ticket(5,'known/repo')],set(),self.sampling,'fixed')
        self.assertEqual(selected,[])
        self.assertEqual({r['reason'] for r in excluded},{'below_frozen_minimum_description_chars','development_repository'})
        self.assertEqual(unsampled,[])

    def test_every_source_case_accounted_for_and_repo_cap_retained(self):
        pool=[ticket(i) for i in range(1,7)]
        selected,excluded,unsampled,_=select_candidates(pool,[],set(),self.sampling,'fixed')
        self.assertEqual(len(selected),2)
        self.assertEqual(len(unsampled),4)
        groups=[{r['ticket_id'] for r in group} for group in (selected,excluded,unsampled)]
        self.assertEqual(len(set.union(*groups)),len(pool))
        self.assertFalse(groups[0]&groups[1] or groups[0]&groups[2] or groups[1]&groups[2])

    def test_stable_source_order_and_local_exposure_exclusion(self):
        pool=[ticket(i,'repo'+str(i%3)+'/lib') for i in range(1,9)]
        exposed={pool[0]['ticket_id']}
        a=select_candidates(pool,[],exposed,self.sampling,'fixed')
        b=select_candidates(list(reversed(pool)),[],exposed,self.sampling,'fixed')
        self.assertEqual(a[0],b[0])
        self.assertNotIn(pool[0]['ticket_id'],{r['ticket_id'] for r in a[0]})


if __name__=='__main__':unittest.main()
