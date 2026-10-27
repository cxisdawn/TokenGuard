"""Pair caller-supplied usage exports without mixing estimates or synthetic evidence."""
import csv
from decimal import Decimal
import json
from pathlib import Path
import uuid

from .usage import (SCHEMA, METRICS, _integer, _text, billing_binding,
                    manifest_fields, normalize, validate_format_source)

UNAVAILABLE = 'unavailable'
IDENTITY = ('provider','model','agent_version','prompt_sha256','initial_snapshot_sha256','config_sha256','baseline_mode')


def _request_coverage(bundle: dict, manifest: dict, request_ids: set[str]) -> dict | str:
    """Recheck observed-but-unfinished requests rather than trusting task totals."""
    for field in ('duplicate_records_ignored', 'nonfinal_chunks_ignored'):
        if field in bundle:
            _integer(bundle[field], field)
    if 'request_coverage' not in bundle:
        # Legacy imports discarded early chunk identities. When any were seen,
        # their relation to finalized requests cannot now be reconstructed.
        # All legacy imports wrote this count, so absence is unknown, not zero.
        if 'nonfinal_chunks_ignored' not in bundle or bundle['nonfinal_chunks_ignored']:
            raise ValueError('Legacy streaming export lacks request coverage evidence; reimport source')
        return UNAVAILABLE
    coverage=bundle['request_coverage']
    if not isinstance(coverage,dict):
        raise ValueError('Invalid normalized request coverage')
    observed=_integer(coverage.get('observed_requests'),'observed_requests')
    finalized=_integer(coverage.get('finalized_requests'),'finalized_requests')
    unresolved=coverage.get('unresolved_request_ids')
    if not isinstance(unresolved,list):
        raise ValueError('Invalid unresolved request IDs')
    pending=[_text(value,'unresolved request ID') for value in unresolved]
    if len(set(pending))!=len(pending) or set(pending)&request_ids:
        raise ValueError('Conflicting unresolved request IDs')
    if finalized!=len(request_ids) or observed!=len(request_ids)+len(pending):
        raise ValueError('Normalized request coverage count differs')
    if (type(coverage.get('all_observed_requests_finalized')) is not bool
            or coverage['all_observed_requests_finalized']!=(not pending)):
        raise ValueError('Normalized request completion differs')
    if manifest['coverage']=='complete_task' and pending:
        raise ValueError('Observed requests are missing finalized usage; complete task unavailable')
    return {'observed_requests':observed, 'finalized_requests':finalized,
            'unresolved_request_ids':pending, 'all_observed_requests_finalized':not pending}


def _checked(bundle: dict) -> dict:
    if not isinstance(bundle,dict) or bundle.get('schema')!=SCHEMA:
        raise ValueError('Unsupported normalized usage schema')
    manifest=manifest_fields(bundle.get('manifest'))
    format_name=bundle.get('usage_format')
    if format_name not in ('deepseek','anthropic'):
        raise ValueError('Unsupported normalized usage format')
    validate_format_source(manifest,format_name)
    records=bundle.get('requests')
    if not isinstance(records,list) or not records:
        raise ValueError('Normalized requests required')
    seen=set()
    clean=[]
    for item in records:
        if not isinstance(item,dict) or not isinstance(item.get('metrics'),dict):
            raise ValueError('Invalid normalized request')
        values=item['metrics']
        if set(values)!=set(METRICS):
            raise ValueError('Normalized metric fields differ')
        if format_name=='deepseek':
            raw={'object':'chat.completion','id':item.get('request_id'),'model':item.get('model'),
                 'usage':{'prompt_tokens':values['input_tokens'],'completion_tokens':values['output_tokens'],
                          'total_tokens':values['total_tokens'],'prompt_cache_hit_tokens':values['cache_read_tokens'],
                          'prompt_cache_miss_tokens':values['uncached_input_tokens'],
                          'completion_tokens_details':{'reasoning_tokens':values['reasoning_tokens']}}}
        else:
            raw={'type':'message','stop_reason':'end_turn','id':item.get('request_id'),'model':item.get('model'),
                 'usage':{'input_tokens':values['uncached_input_tokens'],'output_tokens':values['output_tokens'],
                          'cache_read_input_tokens':values['cache_read_tokens'],'cache_creation_input_tokens':values['cache_write_tokens']}}
        validated=normalize(raw,format_name,manifest['model'])
        if validated!=item or item['request_id'] in seen:
            raise ValueError('Invalid or duplicate normalized request')
        seen.add(item['request_id']); clean.append(validated)
    if _integer(bundle.get('unique_requests'),'unique_requests')!=len(clean):
        raise ValueError('Normalized request count differs')
    coverage=_request_coverage(bundle,manifest,seen)
    if manifest['coverage']=='complete_task' and manifest['expected_request_count']!=len(clean):
        raise ValueError('Complete task request count differs')
    totals={k:sum(item['metrics'][k] for item in clean) if all(item['metrics'][k] is not None for item in clean) else UNAVAILABLE for k in METRICS}
    if totals!=bundle.get('imported_request_totals'):
        raise ValueError('Normalized totals do not match requests')
    expected=totals if manifest['coverage']=='complete_task' else UNAVAILABLE
    if expected!=bundle.get('complete_task_totals'):
        raise ValueError('Normalized task totals differ from coverage')
    billing=bundle.get('reported_billing')
    billing_status='unavailable'
    if billing!=UNAVAILABLE:
        if not isinstance(billing,dict) or not isinstance(billing.get('amount'),str) or len(billing['amount'])>100 or not isinstance(billing.get('currency'),str):
            raise ValueError('Invalid normalized billing')
        import re
        from decimal import InvalidOperation
        try:
            amount=Decimal(billing['amount'])
        except InvalidOperation:
            raise ValueError('Invalid normalized billing amount') from None
        if not amount.is_finite() or amount<0 or amount>Decimal('1000000000') or abs(amount.as_tuple().exponent)>12 or not re.fullmatch('[A-Z]{3}',billing['currency']):
            raise ValueError('Invalid normalized billing amount/currency')
        allowed=('synthetic_fixture',) if manifest['evidence_kind']=='synthetic' else ('provider_billing_export','invoice')
        if billing.get('source') not in allowed or manifest['coverage']!='complete_task':
            raise ValueError('Normalized billing evidence differs')
        binding_fields=('schema_version','scope','session_id','request_ids')
        if not any(field in billing for field in binding_fields):
            # Old bundles discarded the binding after import. Retain their usage
            # evidence, but do not use an unbound amount in a cost comparison.
            billing=UNAVAILABLE
            billing_status='binding_unavailable'
        else:
            binding=billing_binding(billing,manifest,seen)
            billing={**billing,**binding}
            billing_status='bound_caller_supplied'
    return {**bundle,'manifest':manifest,'requests':clean,'request_coverage':coverage,
            'imported_request_totals':totals,'complete_task_totals':expected,
            'reported_billing':billing,'billing_validation':billing_status,
            'actual_billed_cost':billing if manifest['evidence_kind']=='provider_export' and isinstance(billing,dict) else UNAVAILABLE}


def _delta(before, after) -> dict | str:
    if before==UNAVAILABLE or after==UNAVAILABLE or before is None or after is None:
        return UNAVAILABLE
    return {'baseline':before,'optimized':after,'optimized_minus_baseline':after-before,
            'reduction_ratio':(before-after)/before if before else UNAVAILABLE}


def _cost(before, after) -> dict | str:
    if not isinstance(before,dict) or not isinstance(after,dict) or before['currency']!=after['currency']:
        return UNAVAILABLE
    base,opt=Decimal(before['amount']),Decimal(after['amount'])
    return {'baseline':format(base,'f'),'optimized':format(opt,'f'),
            'optimized_minus_baseline':format(opt-base,'f'),'currency':before['currency'],
            'reduction_ratio':float((base-opt)/base) if base else UNAVAILABLE,
            'verification':'caller-supplied billing; not independently authenticated'}


def _quality(manifest: dict) -> bool:
    return all(manifest[k] for k in ('task_success','tests_passed','tests_unchanged'))


def compare(bundles: list[dict]) -> dict:
    if len(bundles)<2:
        raise ValueError('At least two normalized runs required')
    runs=[_checked(b) for b in bundles]
    groups={}
    session_uses={}
    request_uses={}
    for run in runs:
        m=run['manifest']
        key=(m['experiment_id'],m['task_id'],m['pair_id'])
        groups.setdefault(key,[]).append(run)
        session_uses[m['session_id']]=session_uses.get(m['session_id'],0)+1
        for request in run['requests']:
            request_key=(m['provider'],request['request_id'])
            request_uses[request_key]=request_uses.get(request_key,0)+1
    pairs=[]
    eligible=[]
    for key,group in groups.items():
        pair={'experiment_id':key[0],'task_id':key[1],'pair_id':key[2],
              'status':'invalid_pair','issues':[],'metrics':UNAVAILABLE,'reported_billing_change':UNAVAILABLE,
              'actual_task_savings_claim':'not_established'}
        base=[r for r in group if r['manifest']['arm']=='baseline']
        opt=[r for r in group if r['manifest']['arm']=='optimized']
        if len(base)!=1 or len(opt)!=1:
            pair['issues'].append('exactly_one_run_per_arm_required')
            pairs.append(pair); continue
        b,o=base[0],opt[0]
        bm,om=b['manifest'],o['manifest']
        pair.update(baseline_session=bm['session_id'],optimized_session=om['session_id'],
                    baseline_mode=bm['baseline_mode'],
                    evidence_kind=bm['evidence_kind'],model=bm['model'],
                    baseline_success=_quality(bm),optimized_success=_quality(om))
        for field in IDENTITY:
            if bm[field]!=om[field]: pair['issues'].append('mismatch_'+field)
        for field in ('evidence_kind',):
            if bm[field]!=om[field]: pair['issues'].append('mismatch_'+field)
        if b['usage_format']!=o['usage_format']: pair['issues'].append('mismatch_usage_format')
        if not bm['fresh_session'] or not om['fresh_session']: pair['issues'].append('fresh_sessions_required')
        if any(session_uses[m['session_id']]!=1 for m in (bm,om)): pair['issues'].append('session_reused')
        if {r['request_id'] for r in b['requests']} & {r['request_id'] for r in o['requests']}:
            pair['issues'].append('request_ids_shared_across_arms')
        if any(request_uses[(r['manifest']['provider'],request['request_id'])]>1 for r in (b,o) for request in r['requests']):
            pair['issues'].append('request_id_reused_across_runs')
        if pair['issues']:
            pairs.append(pair); continue
        if bm['coverage']!='complete_task' or om['coverage']!='complete_task':
            pair['status']='partial_usage'; pair['issues'].append('task_coverage_incomplete')
            pairs.append(pair); continue
        bt,ot=b['complete_task_totals'],o['complete_task_totals']
        metrics={field:_delta(bt[field],ot[field]) for field in METRICS}
        for field in ('elapsed_ms','tool_calls','recovery_count'):
            metrics[field]=_delta(bm[field],om[field])
        metrics['cache_hit_percentage_point_change']=((ot['cache_read_tokens']/ot['input_tokens']-bt['cache_read_tokens']/bt['input_tokens'])*100
            if all(isinstance(t[k],int) for t in (bt,ot) for k in ('input_tokens','cache_read_tokens')) and bt['input_tokens']>0 and ot['input_tokens']>0 else UNAVAILABLE)
        pair['metrics']=metrics
        pair['reported_billing_change']=_cost(b['reported_billing'],o['reported_billing'])
        for arm,run in (('baseline',b),('optimized',o)):
            if run['billing_validation']=='binding_unavailable':
                pair['issues'].append(arm+'_billing_binding_unavailable')
        if isinstance(b['reported_billing'],dict) and isinstance(o['reported_billing'],dict) and pair['reported_billing_change']==UNAVAILABLE:
            pair['issues'].append('billing_currency_mismatch')
        for arm,manifest in (('baseline',bm),('optimized',om)):
            for field in ('task_success','tests_passed','tests_unchanged'):
                if not manifest[field]:
                    pair['issues'].append(arm+'_'+field+'_failed')
        if _quality(bm) and not _quality(om):
            pair['status']='quality_regression'
        elif not (_quality(bm) and _quality(om)):
            pair['status']='task_quality_gate_failed'
        else:
            pair['status']='synthetic_only' if bm['evidence_kind']=='synthetic' else 'reported_complete_pair'
            eligible.append((b,o))
        pairs.append(pair)
    cohorts={}
    for b,o in eligible:
        m=b['manifest']
        key=tuple(m[k] for k in ('experiment_id','provider','model','agent_version','config_sha256','evidence_kind','baseline_mode'))+(b['usage_format'],)
        cohorts.setdefault(key,[]).append((b,o))
    aggregates=[]
    for key,entries in cohorts.items():
        item=dict(zip(('experiment_id','provider','model','agent_version','config_sha256','evidence_kind','baseline_mode','usage_format'),key))
        item['pairs']=len(entries)
        item['metrics']={}
        for field in METRICS:
            sides=[]
            for arm in (0,1):
                values=[entry[arm]['complete_task_totals'][field] for entry in entries]
                sides.append(sum(values) if all(isinstance(v,int) for v in values) else UNAVAILABLE)
            item['metrics'][field]=_delta(*sides)
        bills=[entry[arm]['reported_billing'] for entry in entries for arm in (0,1)]
        if all(isinstance(v,dict) for v in bills) and len({v['currency'] for v in bills})==1:
            totals=[{'amount':str(sum((Decimal(entry[arm]['reported_billing']['amount']) for entry in entries),Decimal(0))),
                     'currency':bills[0]['currency']} for arm in (0,1)]
            item['reported_billing_change']=_cost(*totals)
        else:
            item['reported_billing_change']=UNAVAILABLE
        aggregates.append(item)
    evaluated=[p for p in pairs if p['status']!='invalid_pair']
    quality={'evaluable_pairs':len(evaluated),
             'baseline_successes':sum(p['baseline_success'] for p in evaluated),
             'optimized_successes':sum(p['optimized_success'] for p in evaluated),
             'quality_regressions':sum(p['baseline_success'] and not p['optimized_success'] for p in evaluated),
             'verification':'caller-declared; not independently re-run'}
    return {'schema':'tokenguard.usage-comparison/v1','pairs':pairs,'cohorts':aggregates,'quality_summary':quality,
            'total_pairs':len(pairs),'eligible_pairs':len(eligible),'excluded_pairs':len(pairs)-len(eligible),
            'aggregation_policy':'only matched complete pairs where both arms pass task/test quality; synthetic evidence kept in separate cohorts; inspect quality_summary to avoid survivor bias',
            'authenticity':'caller-supplied exports and coverage declarations; not independently authenticated',
            'statistical_claim':'not_established; no causal or stable savings claim from imported files alone'}


def _cell(value):
    if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')):
        return "'"+value
    return value


def export(report: dict, parent: Path) -> Path:
    folder=parent/('comparison-'+uuid.uuid4().hex)
    folder.mkdir(parents=True,exist_ok=False)
    (folder/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=True)+'\n',encoding='utf-8')
    metric_names=(*METRICS,'elapsed_ms','tool_calls','recovery_count')
    fields=['experiment_id','task_id','pair_id','status','baseline_mode','evidence_kind','baseline_success','optimized_success']
    fields += [name for field in metric_names for name in ('baseline_'+field,'optimized_'+field,field+'_reduction_ratio')]
    fields += ['cache_hit_percentage_point_change','baseline_billing','optimized_billing','currency','billing_reduction_ratio','issues']
    with (folder/'report.csv').open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields); writer.writeheader()
        for pair in report['pairs']:
            row={k:pair.get(k,UNAVAILABLE) for k in fields}
            for field in metric_names:
                metric=pair['metrics'].get(field) if isinstance(pair['metrics'],dict) else None
                if isinstance(metric,dict):
                    row.update({"baseline_"+field:metric['baseline'],"optimized_"+field:metric['optimized'],field+"_reduction_ratio":metric['reduction_ratio']})
            if isinstance(pair['metrics'],dict):
                row['cache_hit_percentage_point_change']=pair['metrics']['cache_hit_percentage_point_change']
            cost=pair['reported_billing_change']
            if isinstance(cost,dict):
                row.update(baseline_billing=cost['baseline'],optimized_billing=cost['optimized'],currency=cost['currency'],billing_reduction_ratio=cost['reduction_ratio'])
            row['issues']=';'.join(pair['issues'])
            writer.writerow({k:_cell(v) for k,v in row.items()})
    lines=['# Offline usage comparison','',
           'Evidence is caller supplied. Synthetic fixture values are NOT real usage or billed savings.',
           'Complete-task coverage and export authenticity are not independently verified.','',
           '|Task|Pair|Baseline mode|Status|Baseline total tokens|Optimized total tokens|',
           '|---|---|---|---|---:|---:|']
    for pair in report['pairs']:
        metric=pair['metrics'].get('total_tokens') if isinstance(pair['metrics'],dict) else None
        before=metric['baseline'] if isinstance(metric,dict) else UNAVAILABLE
        after=metric['optimized'] if isinstance(metric,dict) else UNAVAILABLE
        task=pair['task_id'].replace('|','\\|'); label=pair['pair_id'].replace('|','\\|')
        lines.append(f"|{task}|{label}|{pair.get('baseline_mode',UNAVAILABLE)}|{pair['status']}|{before}|{after}|")
    for pair in report['pairs']:
        task=pair['task_id'].replace('|','\\|'); label=pair['pair_id'].replace('|','\\|')
        lines.extend(['',f"### {task} / {label} ({pair['status']})",'',
                      '|Metric|Baseline|Optimized|Reduction ratio|','|---|---:|---:|---:|'])
        for field in metric_names:
            metric=pair['metrics'].get(field) if isinstance(pair['metrics'],dict) else None
            if isinstance(metric,dict):
                ratio=metric['reduction_ratio']
                display=f'{ratio:.2%}' if isinstance(ratio,(int,float)) else UNAVAILABLE
                lines.append(f"|{field}|{metric['baseline']}|{metric['optimized']}|{display}|")
            else:
                lines.append(f'|{field}|unavailable|unavailable|unavailable|')
        cost=pair['reported_billing_change']
        if isinstance(cost,dict):
            ratio=cost['reduction_ratio']
            display=f'{ratio:.2%}' if isinstance(ratio,(int,float)) else UNAVAILABLE
            lines.append(f"|reported billing ({cost['currency']})|{cost['baseline']}|{cost['optimized']}|{display}|")
        else:
            lines.append('|reported billing|unavailable|unavailable|unavailable|')
        lines.append('')
        if pair['issues']:
            lines.append('Issues: '+', '.join(pair['issues']))
    lines.extend(['',f"Eligible pairs: {report['eligible_pairs']}; excluded pairs: {report['excluded_pairs']}.",
                  'Billing deltas, cache input/output breakdowns, time, tools and recoveries are in JSON/CSV.',
                  f"Quality successes: baseline={report['quality_summary']['baseline_successes']}, optimized={report['quality_summary']['optimized_successes']}, regressions={report['quality_summary']['quality_regressions']}. Inspect excluded failures before interpreting aggregate reductions.",
                  'No stable task-cost savings claim is established.'])
    (folder/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return folder
