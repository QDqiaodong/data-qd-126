
import copy,json,unittest,tempfile,subprocess,sys,hashlib
from pathlib import Path
from experiment_lift.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_top_field(self):
        d=sample();d['unexpected']=1;self.bad(d)

    def test_exact_effect(self):
        r=analyze(sample());self.assertEqual(r['arms']['C']['rate'],'1/4');self.assertEqual(r['arms']['T']['rate'],'1/2');self.assertEqual(r['comparison']['risk_difference'],'1/4');self.assertEqual(r['comparison']['risk_ratio'],'2');self.assertEqual(r['allocation_diagnostic']['pearson_chi_square'],'0')
    def test_wilson(self):
        r=analyze(sample())['arms']['C']['rate_interval_95'];self.assertAlmostEqual(r['lower'],0.04558726080970055);self.assertAlmostEqual(r['upper'],0.6993581574175981)
    def test_zero_control(self):
        d=sample();d['outcomes'][0]['converted']=0;r=analyze(d);self.assertIsNone(r['comparison']['risk_ratio']);self.assertEqual(r['comparison']['risk_difference'],'1/2')
    def test_missing_result(self):
        d=sample();d['outcomes'].pop();self.bad(d)
    def test_duplicate_assignment(self):
        d=sample();d['assignments'].append(d['assignments'][0]);self.bad(d)
    def test_unassigned_outcome(self):
        d=sample();d['outcomes'][0]['user']='unknown';self.bad(d)
    def test_boolean_outcome(self):
        d=sample();d['outcomes'][0]['converted']=True;self.bad(d)
    def test_order(self):
        d=sample();d['assignments'].reverse();d['outcomes'].reverse();self.assertEqual(analyze(d),analyze(sample()))

class Command(unittest.TestCase):
    def call(self,source,output):
        return subprocess.run([sys.executable,'-m','experiment_lift.cli','--source',str(source),'--output',str(output)],capture_output=True,text=True)
    def test_installed_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();report=json.loads(first);self.assertEqual(report['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"x":1,"x":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_output_safety(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
