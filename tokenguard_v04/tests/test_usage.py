import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tokenguard.usage import import_usage,normalize,parse,write_bundle
from tokenguard.usage_compare import compare,export

ROOT=Path(__file__).resolve().parent.parent
FIXTURES=ROOT/'benchmark/usage_fixtures'


class TestUsage(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.folder=Path(self.temp.name)
        self.manifest=json.loads((FIXTURES/'baseline-manifest.json').read_text())
        self.responses=json.loads((FIXTURES/'baseline-responses.json').read_text())
    def tearDown(self): self.temp.cleanup()
    def put(self,name,value):
        path=self.folder/name
        path.write_text(json.dumps(value),encoding='utf-8')
        return path
    def bundle(self,arm='baseline',**changes):
        m=json.loads((FIXTURES/f'{arm}-manifest.json').read_text())
        m.update(changes)
        return import_usage(FIXTURES/f'{arm}-responses.json',self.put(arm+'-manifest.json',m),'deepseek')
    def imported(self,rows=None,manifest=None,billing=None,format_name='deepseek'):
        return import_usage(self.put('responses.json',self.responses if rows is None else rows),
                            self.put('manifest.json',manifest or self.manifest),format_name,
                            self.put('billing.json',billing) if billing else None)
    def test_deepseek_cache_not_double_counted(self):
        b=self.imported()
        self.assertEqual(b['imported_request_totals']['input_tokens'],3000)
        self.assertEqual(b['imported_request_totals']['cache_read_tokens'],2700)
        self.assertEqual(b['imported_request_totals']['total_tokens'],3300)
        self.assertEqual(b['imported_request_totals']['cache_write_tokens'],'unavailable')
        self.assertEqual(b['cache_hit_ratio'],0.9)
        self.assertTrue(b['is_synthetic'])
    def test_missing_cache_is_unknown(self):
        rows=copy.deepcopy(self.responses)
        for row in rows:
            for key in ('prompt_cache_hit_tokens','prompt_cache_miss_tokens','prompt_tokens_details'):
                row['usage'].pop(key)
        b=self.imported(rows)
        self.assertEqual(b['imported_request_totals']['cache_read_tokens'],'unavailable')
        self.assertEqual(b['imported_request_totals']['input_tokens'],3000)
        self.assertEqual(b['cache_hit_ratio'],'unavailable')
    def test_cache_alias_and_conflicts(self):
        row=copy.deepcopy(self.responses[0])
        row['usage'].pop('prompt_cache_hit_tokens')
        self.assertEqual(normalize(row,'deepseek','deepseek-flash')['metrics']['cache_read_tokens'],900)
        row['usage']['prompt_cache_hit_tokens']=800
        with self.assertRaises(ValueError): normalize(row,'deepseek','deepseek-flash')
    def test_reasoning_included_in_completion_not_added(self):
        row=copy.deepcopy(self.responses[0]); row['usage']['completion_tokens_details']={'reasoning_tokens':80}
        result=normalize(row,'deepseek','deepseek-flash')
        self.assertEqual(result['metrics']['total_tokens'],1100)
        self.assertEqual(result['metrics']['reasoning_tokens'],80)
        row['usage']['completion_tokens_details']['reasoning_tokens']=101
        with self.assertRaises(ValueError): normalize(row,'deepseek','deepseek-flash')
    def test_inconsistent_native_totals_and_cache_rejected(self):
        for key,value in [('total_tokens',999),('prompt_cache_miss_tokens',20),('prompt_cache_hit_tokens',1200)]:
            row=copy.deepcopy(self.responses[0]); row['usage'][key]=value
            with self.assertRaises(ValueError): normalize(row,'deepseek','deepseek-flash')
    def test_invalid_numeric_counters_rejected(self):
        for value in (-1,True,1.5,'100',None,2**63):
            row=copy.deepcopy(self.responses[0]); row['usage']['prompt_tokens']=value
            with self.assertRaises(ValueError): normalize(row,'deepseek','deepseek-flash')
    def test_anthropic_disjoint_cache_accounting(self):
        row={'id':'msg-1','type':'message','stop_reason':'end_turn','model':'test-model',
             'usage':{'input_tokens':50,'cache_read_input_tokens':1000,'cache_creation_input_tokens':100,'output_tokens':20}}
        result=normalize(row,'anthropic','test-model')['metrics']
        self.assertEqual(result['input_tokens'],1150)
        self.assertEqual(result['total_tokens'],1170)
        row['usage'].pop('cache_creation_input_tokens')
        result=normalize(row,'anthropic','test-model')['metrics']
        self.assertIsNone(result['input_tokens'])
        self.assertIsNone(result['total_tokens'])
        row['stop_reason']=None
        with self.assertRaises(ValueError): normalize(row,'anthropic','test-model')
    def test_duplicate_final_request_counted_once(self):
        result=self.imported(self.responses+[copy.deepcopy(self.responses[0])])
        self.assertEqual(result['unique_requests'],2)
        self.assertEqual(result['duplicate_records_ignored'],1)
        self.assertEqual(result['imported_request_totals']['total_tokens'],3300)
    def test_conflicting_duplicate_rejected(self):
        row=copy.deepcopy(self.responses[0]); row['usage']['completion_tokens']+=1; row['usage']['total_tokens']+=1
        with self.assertRaises(ValueError): self.imported(self.responses+[row])
    def test_stream_final_usage_only(self):
        early={'object':'chat.completion.chunk','usage':None,'id':self.responses[0]['id'],
               'model':'deepseek-flash'}
        final=copy.deepcopy(self.responses[0]); final['object']='chat.completion.chunk'; final['choices']=[{'finish_reason':'stop'}]
        m={**self.manifest,'expected_request_count':1}
        result=self.imported([early,final,copy.deepcopy(final)],m)
        self.assertEqual(result['nonfinal_chunks_ignored'],1)
        self.assertEqual(result['unique_requests'],1)
        self.assertEqual(result['duplicate_records_ignored'],1)
        final['choices']=[{'finish_reason':None}]
        with self.assertRaises(ValueError): self.imported([final],m)
        with self.assertRaises(ValueError): self.imported([early],m)
    def test_final_usage_or_model_missing_rejected(self):
        for change in ({'usage':None},{'model':'other-model'},{'id':None},{'object':'result','total_cost_usd':123}):
            row={**self.responses[0],**change}
            with self.assertRaises(ValueError): self.imported([row])
    def test_complete_coverage_count_and_manifest_validation(self):
        for field,value in [('expected_request_count',3),('expected_request_count',None),('fresh_session',1),('arm','wrong'),('prompt_sha256','../bad'),('elapsed_ms',-1),('schema_version',True)]:
            with self.assertRaises(ValueError): self.imported(manifest={**self.manifest,field:value})
    def test_partial_import_never_claims_task_totals(self):
        result=self.imported([self.responses[0]],{**self.manifest,'coverage':'partial','expected_request_count':None})
        self.assertEqual(result['complete_task_totals'],'unavailable')
        self.assertEqual(result['imported_request_totals']['total_tokens'],1100)
    def test_unknown_private_fields_not_persisted_and_cli_cost_ignored(self):
        rows=copy.deepcopy(self.responses)
        rows[0].update(api_key='SYNTHETIC_SECRET',choices=[{'message':{'content':'PRIVATE_SOURCE_TEXT'}}],total_cost_usd=1234)
        result=self.imported(rows)
        serialized=json.dumps(result)
        self.assertNotIn('SYNTHETIC_SECRET',serialized)
        self.assertNotIn('PRIVATE_SOURCE_TEXT',serialized)
        self.assertNotIn('total_cost_usd',serialized)
        self.assertEqual(result['actual_billed_cost'],'unavailable')
    def test_jsonl_bom_and_strict_json(self):
        path=self.folder/'responses.jsonl'
        path.write_text('\ufeff'+'\n'.join(json.dumps(r) for r in self.responses),encoding='utf-8')
        result=import_usage(path,self.put('manifest.json',self.manifest),'deepseek')
        self.assertEqual(result['unique_requests'],2)
        for value in ('{"x":1,"x":2}','{"x":NaN}','{"x":Infinity}'):
            with self.assertRaises(ValueError): parse(value)
    def test_synthetic_billing_cannot_be_actual_billing(self):
        bill=json.loads((FIXTURES/'baseline-billing.json').read_text())
        result=self.imported(billing=bill)
        self.assertEqual(result['reported_billing']['amount'],'0.0100')
        self.assertEqual(result['actual_billed_cost'],'unavailable')
    def test_billing_scope_ids_and_estimates_rejected(self):
        original=json.loads((FIXTURES/'baseline-billing.json').read_text())
        for field,value in [('source','claude_cli_estimate'),('session_id','other'),('request_ids',[]),('amount','NaN'),('amount','-1'),('amount',1.5),('currency','usd'),('scope','project_total')]:
            with self.assertRaises(ValueError): self.imported(billing={**original,field:value})
    def test_declared_provider_billing_preserves_unverified_provenance(self):
        bill=json.loads((FIXTURES/'baseline-billing.json').read_text())
        bill['source']='provider_billing_export'
        result=self.imported(manifest={**self.manifest,'evidence_kind':'provider_export'},billing=bill)
        self.assertEqual(result['actual_billed_cost']['amount'],'0.0100')
        self.assertIn('not independently',result['actual_billed_cost']['verification'])
    def test_compare_tokens_fall_while_billing_increases(self):
        bundles=[import_usage(FIXTURES/f'{arm}-responses.json',FIXTURES/f'{arm}-manifest.json','deepseek',FIXTURES/f'{arm}-billing.json') for arm in ('baseline','optimized')]
        pair=compare(bundles)['pairs'][0]
        self.assertEqual(pair['status'],'synthetic_only')
        self.assertGreater(pair['metrics']['total_tokens']['reduction_ratio'],0)
        self.assertEqual(pair['reported_billing_change']['reduction_ratio'],-1.0)
        self.assertEqual(pair['metrics']['cache_hit_percentage_point_change'],-90.0)
        self.assertEqual(pair['actual_task_savings_claim'],'not_established')
    def test_compare_identity_mismatch_refused(self):
        baseline=self.bundle()
        for field,value in [('model','other'),('provider','other'),('prompt_sha256','a'*64),('initial_snapshot_sha256','b'*64),('config_sha256','c'*64),('agent_version','other'),('baseline_mode','no_hook')]:
            opt=self.bundle('optimized')
            opt['manifest'][field]=value
            if field=='model':
                for r in opt['requests']: r['model']=value
            report=compare([baseline,opt])
            self.assertEqual(report['eligible_pairs'],0)
            self.assertIn('mismatch_'+field,report['pairs'][0]['issues'])
    def test_compare_requires_fresh_unique_sessions_and_requests(self):
        baseline=self.bundle()
        for change in ({'fresh_session':False},{'session_id':baseline['manifest']['session_id']}):
            opt=self.bundle('optimized',**change)
            self.assertEqual(compare([baseline,opt])['eligible_pairs'],0)
        opt=self.bundle('optimized')
        opt['requests'][0]['request_id']=baseline['requests'][0]['request_id']
        self.assertIn('request_ids_shared_across_arms',compare([baseline,opt])['pairs'][0]['issues'])
    def test_compare_partial_quality_regression_and_missing_arm(self):
        baseline=self.bundle()
        opt=self.bundle('optimized',coverage='partial')
        self.assertEqual(compare([baseline,opt])['pairs'][0]['status'],'partial_usage')
        opt=self.bundle('optimized',tests_unchanged=False)
        report=compare([baseline,opt])
        self.assertEqual(report['pairs'][0]['status'],'quality_regression')
        self.assertEqual(report['eligible_pairs'],0)
        self.assertIn('optimized_tests_unchanged_failed',report['pairs'][0]['issues'])
        self.assertEqual(report['quality_summary']['quality_regressions'],1)
        second=self.bundle(session_id='other-baseline')
        self.assertEqual(compare([baseline,second])['pairs'][0]['status'],'invalid_pair')
    def test_comparison_detects_tampered_normalized_totals(self):
        baseline=self.bundle(); opt=self.bundle('optimized')
        opt['imported_request_totals']['total_tokens']=1
        with self.assertRaises(ValueError): compare([baseline,opt])
    def test_compare_no_synthetic_real_evidence_mixing(self):
        baseline=self.bundle(); opt=self.bundle('optimized',evidence_kind='provider_export')
        self.assertIn('mismatch_evidence_kind',compare([baseline,opt])['pairs'][0]['issues'])
        second=[]
        for arm in ('baseline','optimized'):
            bundle=self.bundle(arm,pair_id='pair02',session_id='provider-'+arm,evidence_kind='provider_export')
            for row in bundle['requests']: row['request_id']='provider-'+row['request_id']
            second.append(bundle)
        result=compare([baseline,self.bundle('optimized'),*second])
        self.assertEqual(len(result['cohorts']),2)
    def test_two_rounds_aggregate_exact_totals(self):
        bundles=[self.bundle(),self.bundle('optimized')]
        for arm in ('baseline','optimized'):
            bundle=self.bundle(arm,pair_id='pair02',session_id='round2-'+arm)
            for row in bundle['requests']: row['request_id']='round2-'+row['request_id']
            bundles.append(bundle)
        report=compare(bundles)
        self.assertEqual(report['cohorts'][0]['pairs'],2)
        self.assertEqual(report['cohorts'][0]['metrics']['total_tokens']['baseline'],6600)

    def test_baseline_modes_do_not_share_a_cohort(self):
        bundles=[self.bundle(baseline_mode='disabled_hook'),self.bundle('optimized',baseline_mode='disabled_hook')]
        for arm in ('baseline','optimized'):
            item=self.bundle(arm,pair_id='no-hook-pair',session_id='no-hook-'+arm,baseline_mode='no_hook')
            for row in item['requests']:row['request_id']='no-hook-'+row['request_id']
            bundles.append(item)
        result=compare(bundles)
        self.assertEqual(result['eligible_pairs'],2)
        self.assertEqual({c['baseline_mode'] for c in result['cohorts']},{'disabled_hook','no_hook'})

    def test_unknown_usage_baseline_mode_rejected(self):
        self.assertEqual(self.bundle()['manifest']['baseline_mode'],'unspecified')
        for mode in ('third_arm',True,None):
            with self.subTest(mode=mode),self.assertRaises(ValueError):self.bundle(baseline_mode=mode)
    def test_request_reuse_across_rounds_cannot_inflate_sample_size(self):
        bundles=[self.bundle(),self.bundle('optimized')]
        for arm in ('baseline','optimized'):
            # Distinct claimed sessions/pairs, but reuse the same provider request IDs.
            bundles.append(self.bundle(arm,pair_id='pair02',session_id='round2-'+arm))
        report=compare(bundles)
        self.assertEqual(report['eligible_pairs'],0)
        self.assertTrue(all('request_id_reused_across_runs' in p['issues'] for p in report['pairs']))
    def test_quality_failures_remain_visible_outside_savings_aggregate(self):
        baseline=self.bundle(); opt=self.bundle('optimized')
        second=[]
        for arm in ('baseline','optimized'):
            bundle=self.bundle(arm,pair_id='pair02',session_id='round2-'+arm,task_success=(arm=='baseline'))
            for request in bundle['requests']: request['request_id']='round2-'+request['request_id']
            second.append(bundle)
        report=compare([baseline,opt,*second])
        self.assertEqual(report['eligible_pairs'],1)
        self.assertEqual(report['quality_summary']['baseline_successes'],2)
        self.assertEqual(report['quality_summary']['optimized_successes'],1)
        self.assertEqual(report['quality_summary']['quality_regressions'],1)
    def test_zero_baseline_ratio_unavailable(self):
        rows=[]
        for arm in ('baseline','optimized'):
            row=copy.deepcopy(self.responses[0]); row['id']='zero-'+arm
            row['usage']={'prompt_tokens':0,'completion_tokens':0,'total_tokens':0}
            rows.append(self.imported([row],{**self.manifest,'arm':arm,'session_id':'zero-'+arm,'expected_request_count':1}))
        self.assertEqual(compare(rows)['pairs'][0]['metrics']['total_tokens']['reduction_ratio'],'unavailable')
    def test_currency_mismatch_no_cost_ratio(self):
        bundles=[import_usage(FIXTURES/f'{arm}-responses.json',FIXTURES/f'{arm}-manifest.json','deepseek',FIXTURES/f'{arm}-billing.json') for arm in ('baseline','optimized')]
        bundles[1]['reported_billing']['currency']='CNY'
        pair=compare(bundles)['pairs'][0]
        self.assertEqual(pair['reported_billing_change'],'unavailable')
        self.assertIn('billing_currency_mismatch',pair['issues'])
    def test_export_and_non_overwrite(self):
        bundle=self.bundle(); first=write_bundle(bundle,self.folder); second=write_bundle(bundle,self.folder)
        self.assertNotEqual(first,second)
        report=compare([bundle,self.bundle('optimized')])
        folder=export(report,self.folder)
        for name in ('report.json','report.csv','report.md'): self.assertTrue((folder/name).exists())
        import csv
        with (folder/'report.csv').open(newline='') as stream:
            row=next(csv.DictReader(stream))
        self.assertEqual(row['baseline_cache_read_tokens'],'2700')
        self.assertEqual(row['optimized_recovery_count'],'1')
        self.assertEqual(row['baseline_elapsed_ms'],'1000')
        self.assertIn('reported billing',(folder/'report.md').read_text())
    def test_cli_import_compare_and_synthetic_demo(self):
        imported=[]
        for arm in ('baseline','optimized'):
            run=subprocess.run([sys.executable,str(ROOT/'tg.py'),'usage','import','--source',str(FIXTURES/f'{arm}-responses.json'),
                                '--manifest',str(FIXTURES/f'{arm}-manifest.json'),'--format','deepseek','--output',str(self.folder)],capture_output=True,text=True,check=True)
            imported.append(run.stdout.strip())
        pair=subprocess.run([sys.executable,str(ROOT/'tg.py'),'usage','compare',*imported,'--output',str(self.folder)],capture_output=True,text=True,check=True)
        self.assertEqual(json.loads((Path(pair.stdout.strip())/'report.json').read_text())['eligible_pairs'],1)
        demo=subprocess.run([sys.executable,str(ROOT/'benchmark/usage_demo.py'),'--output',str(self.folder)],capture_output=True,text=True,check=True)
        report=json.loads((Path(demo.stdout.strip())/'report.json').read_text())
        self.assertEqual(report['pairs'][0]['status'],'synthetic_only')
    def test_native_export_provider_format_mismatch_refused(self):
        with self.assertRaises(ValueError):
            self.imported(manifest={**self.manifest,'evidence_kind':'provider_export','provider':'anthropic'})
        bundle=self.imported(manifest={**self.manifest,'evidence_kind':'gateway_export','provider':'custom-gateway'})
        self.assertEqual(bundle['actual_billed_cost'],'unavailable')
    def test_partial_and_failed_runs_export_reviewable_cli_report(self):
        base=write_bundle(self.bundle(),self.folder)
        optimized=write_bundle(self.bundle('optimized',coverage='partial'),self.folder)
        run=subprocess.run([sys.executable,str(ROOT/'tg.py'),'usage','compare',str(base),str(optimized),'--output',str(self.folder)],capture_output=True,text=True)
        self.assertEqual(run.returncode,1)
        report=json.loads((Path(run.stdout.strip())/'report.json').read_text())
        self.assertEqual(report['pairs'][0]['status'],'partial_usage')
        self.assertEqual(report['eligible_pairs'],0)
    def test_cli_rejects_bad_export_without_echoing_body(self):
        source=self.folder/'bad.json'; source.write_text('{PRIVATE_RESPONSE')
        run=subprocess.run([sys.executable,str(ROOT/'tg.py'),'usage','import','--source',str(source),'--manifest',str(FIXTURES/'baseline-manifest.json'),
                            '--format','deepseek','--output',str(self.folder)],capture_output=True,text=True)
        self.assertEqual(run.returncode,2)
        self.assertNotIn('PRIVATE_RESPONSE',run.stderr)

if __name__=='__main__': unittest.main()
