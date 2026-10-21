"""Offline parser/report demonstration. All values are synthetic, not actual API usage."""
import argparse
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent))
from tokenguard.usage import import_usage,write_bundle
from tokenguard.usage_compare import compare,export


def run(output: Path) -> Path:
    fixtures=ROOT/'usage_fixtures'
    bundles=[]
    for arm in ('baseline','optimized'):
        bundle=import_usage(fixtures/f'{arm}-responses.json',fixtures/f'{arm}-manifest.json','deepseek',fixtures/f'{arm}-billing.json')
        write_bundle(bundle,output/'imports')
        bundles.append(bundle)
    report=compare(bundles)
    pair=report['pairs'][0]
    assert pair['status']=='synthetic_only'
    assert pair['metrics']['total_tokens']['reduction_ratio']>0
    assert pair['reported_billing_change']['reduction_ratio']<0
    assert all(b['actual_billed_cost']=='unavailable' for b in bundles)
    folder=export(report,output)
    print(folder)
    return folder

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT.parent/'reports/usage-demo')
    run(parser.parse_args().output)
