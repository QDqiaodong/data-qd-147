
import copy,json,unittest,tempfile,subprocess,sys,hashlib,random
from pathlib import Path
from fractions import Fraction
from water_rights.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_field(self):
        d=sample();d['extra']=1;self.bad(d)

    def test_proportional_allocation(self):
        r=analyze(sample())['basins'][0];d={c['id']:c['allocated'] for c in r['claims']};self.assertEqual(d,{'farm-a':50,'farm-b':50,'town':0});self.assertEqual(r['allocated'],100)
    def test_largest_remainder_id_tie(self):
        d={'basins':[{'id':'x','cap':2}],'claims':[{'id':'c','basin':'x','priority':1,'requested':1},{'id':'a','basin':'x','priority':1,'requested':1},{'id':'b','basin':'x','priority':1,'requested':1}]};r=analyze(d)['basins'][0];self.assertEqual({x['id']:x['allocated'] for x in r['claims']},{'a':1,'b':1,'c':0})
    def test_priority(self):
        d=sample();d['claims'][2]['priority']=0;r=analyze(d)['basins'][0];v={x['id']:x['allocated'] for x in r['claims']};self.assertEqual(v['town'],50);self.assertEqual(v['farm-a']+v['farm-b'],50)
    def test_zero_capacity(self):
        d=sample();d['basins'][0]['cap']=0;self.assertEqual(analyze(d)['basins'][0]['allocated'],0)
    def test_zero_demand(self):
        d=sample();d['claims'][0]['requested']=0;self.assertEqual(sum(c['allocated'] for c in analyze(d)['basins'][0]['claims']),100)
    def test_unknown_basin(self):
        d=sample();d['claims'][0]['basin']='x';self.bad(d)
    def test_duplicate_claim(self):
        d=sample();d['claims'].append(d['claims'][0]);self.bad(d)
    def test_order(self):
        d=sample();d['claims'].reverse();self.assertEqual(analyze(d),analyze(sample()))

class Command(unittest.TestCase):
    def call(self,source,output):
        return subprocess.run([sys.executable,'-m','water_rights.cli','--source',str(source),'--output',str(output)],capture_output=True,text=True)
    def test_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();self.assertEqual(json.loads(first)['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"a":1,"a":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_output_safety(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
