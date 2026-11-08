from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'experiments/generalization_v1'))
import prepare_next_sources as module
from core import dump,load,write_rows
from rebuild_saved_snapshots import explicit_snapshot


class NextAcquisitionTests(unittest.TestCase):
    def test_two_bases_in_same_repo_preserved_and_resume_does_not_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); remote=root/'remote'; remote.mkdir()
            def run(*args):
                return subprocess.check_output(['git','-C',str(remote),*args]).decode().strip()
            run('init'); run('config','user.name','fixture'); run('config','user.email','fixture@example.invalid')
            bases=[]
            for content in ('old','new'):
                (remote/'source.py').write_text(content)
                run('add','source.py'); run('commit','-m',content); bases.append(run('rev-parse','HEAD'))
            prepared=root/'prepared'; prepared.mkdir(); cache=root/'cache'
            tickets=[dict(ticket_id='fixture__repo-'+str(i),repo='fixture/repo',base_commit=base)
                     for i,base in enumerate(bases)]
            write_rows(prepared/'prospective_public_tickets.jsonl',tickets)
            dump(prepared/'source_plan.json',dict(repo_cache=str(cache),cases=tickets))
            dump(prepared/'freeze.json',dict(implementation_sha256={},prepared_artifact_sha256={},input_sha256={}))
            real_git=module.git; fetches=[]
            def local_git(path,*args):
                if args[0]=='fetch':
                    fetches.append(args[-1]); args=(*args[:-2],str(remote),args[-1])
                return real_git(path,*args)
            with patch.object(module,'git',side_effect=local_git):
                module.acquire(prepared,root/'first.json')
                first_fetch_count=len(fetches)
                module.acquire(prepared,root/'resumed.json')
            self.assertEqual(first_fetch_count,2)
            self.assertEqual(len(fetches),2)
            self.assertEqual(load(root/'resumed.json')['verified_bases'],2)
            for base in bases:
                self.assertEqual(real_git(cache/'fixture__repo','rev-parse','refs/next-bases/'+base).strip().decode(),base)
            for index,ticket in enumerate(tickets):
                snapshot=root/('snapshot-'+str(index))
                receipt=explicit_snapshot(cache/'fixture__repo',ticket,snapshot,['source.py'])
                self.assertEqual((snapshot/'source.py').read_text(),('old','new')[index])
                self.assertEqual(len(receipt['source_file_sha256']),1)


if __name__=='__main__':unittest.main()
