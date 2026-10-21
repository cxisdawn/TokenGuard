"""Offline, allowlisted provider usage import. Never reads credentials or calls APIs."""
import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import uuid
from typing import Any, Iterator

SCHEMA = 'tokenguard.usage/v1'
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
HASH = re.compile(r'[0-9a-f]{64}')
METRICS = ('input_tokens', 'uncached_input_tokens', 'cache_read_tokens', 'cache_write_tokens', 'output_tokens', 'total_tokens', 'reasoning_tokens')


def _unique(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def _bad_constant(value: str) -> None:
    raise ValueError('Nonfinite JSON number')


def parse(data: str) -> Any:
    return json.loads(data, object_pairs_hook=_unique, parse_constant=_bad_constant)


def load(path: Path) -> Any:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('Input exceeds 32 MiB limit; supply a usage-only export')
    return parse(path.read_text(encoding='utf-8-sig'))


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or any(ord(c)<32 for c in value):
        raise ValueError('Invalid ' + field)
    return value


def _integer(value: Any, field: str, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if type(value) is not int or not 0 <= value <= 2**63-1:
        raise ValueError('Invalid nonnegative integer: ' + field)
    return value


def manifest_fields(raw: Any) -> dict:
    if not isinstance(raw, dict) or type(raw.get('schema_version')) is not int or raw['schema_version'] != 1:
        raise ValueError('Manifest schema_version must be 1')
    fields = ('experiment_id','pair_id','task_id','session_id','provider','model','agent_version')
    result = {'schema_version':1, **{field:_text(raw.get(field),field) for field in fields}}
    for field in ('prompt_sha256','initial_snapshot_sha256','config_sha256'):
        value=raw.get(field)
        if not isinstance(value,str) or not HASH.fullmatch(value):
            raise ValueError('Invalid ' + field)
        result[field]=value
    for field in ('fresh_session','task_success','tests_passed','tests_unchanged'):
        if type(raw.get(field)) is not bool:
            raise ValueError('Boolean required: ' + field)
        result[field]=raw[field]
    for field, allowed in [('arm',('baseline','optimized')), ('coverage',('complete_task','partial')),
                           ('evidence_kind',('synthetic','provider_export','gateway_export','manual'))]:
        if raw.get(field) not in allowed:
            raise ValueError('Invalid ' + field)
        result[field]=raw[field]
    for field in ('expected_request_count','tool_calls','recovery_count','elapsed_ms'):
        result[field]=_integer(raw.get(field),field,optional=True)
    if result['coverage']=='complete_task' and result['expected_request_count'] is None:
        raise ValueError('Complete task requires expected_request_count')
    return result


def _records(path: Path) -> Iterator[dict]:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('Usage source exceeds 32 MiB limit')
    if path.suffix.lower()=='.jsonl':
        with path.open('r',encoding='utf-8-sig') as stream:
            while True:
                line=stream.readline(MAX_LINE_BYTES+1)
                if not line:
                    break
                if len(line.encode('utf-8')) > MAX_LINE_BYTES:
                    raise ValueError('Usage JSONL record exceeds limit')
                if line.strip():
                    row=parse(line)
                    if not isinstance(row,dict):
                        raise ValueError('Usage record must be an object')
                    yield row
    else:
        value=load(path)
        rows=value if isinstance(value,list) else [value]
        for row in rows:
            if not isinstance(row,dict):
                raise ValueError('Usage record must be an object')
            yield row


def normalize(row: dict, format_name: str, model: str) -> dict | None:
    # Only finalized native responses, not Claude transcripts or estimated CLI totals.
    kind=row.get('object') if format_name=='deepseek' else row.get('type')
    if format_name=='deepseek' and kind=='chat.completion.chunk' and row.get('usage') is None:
        return None
    if kind not in (('chat.completion','chat.completion.chunk') if format_name=='deepseek' else ('message',)):
        raise ValueError('Unsupported native response; supply finalized provider usage')
    if row.get('model') != model:
        raise ValueError('Response model differs from manifest')
    request_id=_text(row.get('id'),'request id')
    usage=row.get('usage')
    if not isinstance(usage,dict):
        raise ValueError('Final usage object missing')
    values={key:None for key in METRICS}
    if format_name=='deepseek':
        choices=row.get('choices',[])
        if not isinstance(choices,list):
            raise ValueError('Invalid choices array')
        if kind=='chat.completion.chunk' and not any(isinstance(c,dict) and c.get('finish_reason') is not None for c in choices):
            raise ValueError('Streaming usage must be a final chunk')
        incoming=_integer(usage.get('prompt_tokens'),'prompt_tokens')
        outgoing=_integer(usage.get('completion_tokens'),'completion_tokens')
        total=_integer(usage.get('total_tokens'),'total_tokens',optional=True)
        if total is not None and total != incoming+outgoing:
            raise ValueError('Provider total_tokens conflict')
        hit=_integer(usage.get('prompt_cache_hit_tokens'),'prompt_cache_hit_tokens',optional=True)
        details=usage.get('prompt_tokens_details',{})
        if not isinstance(details,dict):
            raise ValueError('Invalid prompt token details')
        alias=_integer(details.get('cached_tokens'),'cached_tokens',optional=True)
        if hit is not None and alias is not None and hit!=alias:
            raise ValueError('Conflicting cached token aliases')
        hit=hit if hit is not None else alias
        miss=_integer(usage.get('prompt_cache_miss_tokens'),'prompt_cache_miss_tokens',optional=True)
        if any(n is not None and n>incoming for n in (hit,miss)) or (hit is not None and miss is not None and hit+miss!=incoming):
            raise ValueError('Inconsistent DeepSeek cache accounting')
        reasoning_details=usage.get('completion_tokens_details',{})
        if not isinstance(reasoning_details,dict):
            raise ValueError('Invalid completion token details')
        reasoning=_integer(reasoning_details.get('reasoning_tokens'),'reasoning_tokens',optional=True)
        if reasoning is not None and reasoning>outgoing:
            raise ValueError('Reasoning tokens exceed output')
        values.update(input_tokens=incoming,uncached_input_tokens=miss,cache_read_tokens=hit,
                      output_tokens=outgoing,total_tokens=incoming+outgoing,reasoning_tokens=reasoning)
        # Cache misses are NOT a count of cache writes. Missing cache fields remain unknown.
    elif format_name=='anthropic':
        if not isinstance(row.get('stop_reason'),str) or not row['stop_reason']:
            raise ValueError('Anthropic response is not finalized')
        incoming=_integer(usage.get('input_tokens'),'input_tokens')
        outgoing=_integer(usage.get('output_tokens'),'output_tokens')
        hit=_integer(usage.get('cache_read_input_tokens'),'cache_read_input_tokens',optional=True)
        write=_integer(usage.get('cache_creation_input_tokens'),'cache_creation_input_tokens',optional=True)
        total_input=incoming+hit+write if hit is not None and write is not None else None
        values.update(input_tokens=total_input,uncached_input_tokens=incoming,cache_read_tokens=hit,
                      cache_write_tokens=write,output_tokens=outgoing,
                      total_tokens=total_input+outgoing if total_input is not None else None)
    else:
        raise ValueError('Unsupported usage format')
    return {'request_id':request_id,'model':model,'metrics':values}


def _billing(path: Path | None, manifest: dict, request_ids: set[str]) -> dict | str:
    if path is None:
        return 'unavailable'
    raw=load(path)
    if not isinstance(raw,dict) or raw.get('schema_version')!=1 or raw.get('scope')!='session_total':
        raise ValueError('Billing must be a versioned session_total record')
    allowed=('synthetic_fixture',) if manifest['evidence_kind']=='synthetic' else ('provider_billing_export','invoice')
    if raw.get('source') not in allowed:
        raise ValueError('Estimated or incompatible billing source rejected')
    if manifest['coverage']!='complete_task' or raw.get('session_id')!=manifest['session_id']:
        raise ValueError('Billing must cover the same complete session')
    ids=raw.get('request_ids')
    if not isinstance(ids,list) or any(not isinstance(x,str) for x in ids) or len(ids)!=len(set(ids)) or set(ids)!=request_ids:
        raise ValueError('Billing request IDs must exactly match imported requests')
    currency=raw.get('currency')
    if not isinstance(currency,str) or not re.fullmatch('[A-Z]{3}',currency):
        raise ValueError('Invalid billing currency')
    if not isinstance(raw.get('amount'),str) or len(raw['amount'])>100:
        raise ValueError('Billing amount must be a decimal string')
    try:
        amount=Decimal(raw['amount'])
    except InvalidOperation:
        raise ValueError('Invalid billing amount') from None
    if not amount.is_finite() or amount<0 or abs(amount.as_tuple().exponent)>12 or amount>Decimal('1000000000'):
        raise ValueError('Invalid billing amount range')
    reference=_text(raw.get('evidence_reference'),'billing evidence reference')
    return {'amount':format(amount,'f'),'currency':currency,'source':raw['source'],
            'evidence_reference_sha256':hashlib.sha256(reference.encode()).hexdigest(),
            'verification':'caller_supplied; not independently verified',
            'source_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def validate_format_source(manifest: dict, format_name: str) -> None:
    if format_name not in ('deepseek','anthropic'):
        raise ValueError('Unsupported usage format')
    if manifest['evidence_kind']=='provider_export' and manifest['provider']!=format_name:
        raise ValueError('Native provider export must match the selected provider format; use gateway_export for translated usage')


def import_usage(source: Path, manifest_path: Path, format_name: str,
                 billing_path: Path | None = None) -> dict:
    if format_name not in ('deepseek','anthropic'):
        raise ValueError('Unsupported usage format')
    before=source.stat()
    manifest=manifest_fields(load(manifest_path))
    validate_format_source(manifest,format_name)
    requests={}
    duplicates=skipped=0
    for row in _records(source):
        item=normalize(row,format_name,manifest['model'])
        if item is None:
            skipped+=1
            continue
        request_id=item['request_id']
        if request_id in requests:
            if requests[request_id]!=item:
                raise ValueError('Conflicting usage for duplicate request ID')
            duplicates+=1
        else:
            requests[request_id]=item
    if not requests:
        raise ValueError('No finalized usage records')
    if manifest['coverage']=='complete_task' and manifest['expected_request_count']!=len(requests):
        raise ValueError('Request count does not match complete-task declaration')
    totals={key:sum(r['metrics'][key] for r in requests.values())
            if all(r['metrics'][key] is not None for r in requests.values()) else 'unavailable' for key in METRICS}
    cache_ratio=totals['cache_read_tokens']/totals['input_tokens'] if isinstance(totals['cache_read_tokens'],int) and isinstance(totals['input_tokens'],int) and totals['input_tokens']>0 else 'unavailable'
    billed=_billing(billing_path,manifest,set(requests))
    source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    after=source.stat()
    if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
        raise ValueError('Usage source changed during import')
    return {'schema':SCHEMA,'manifest':manifest,'usage_format':format_name,
            'source_sha256':source_hash,
            'provenance':'caller-supplied local export; not independently authenticated',
            'coverage_verification':'caller-declared coverage and request count; not independently observed',
            'unique_requests':len(requests),'duplicate_records_ignored':duplicates,'nonfinal_chunks_ignored':skipped,
            'requests':list(requests.values()),'imported_request_totals':totals,'cache_hit_ratio':cache_ratio,
            'complete_task_totals':totals if manifest['coverage']=='complete_task' else 'unavailable',
            'reported_billing':billed,
            'actual_billed_cost':billed if manifest['evidence_kind']=='provider_export' and isinstance(billed,dict) else 'unavailable',
            'authenticity':'unverified; evidence kind is declared, not certified',
            'is_synthetic':manifest['evidence_kind']=='synthetic'}


def write_bundle(bundle: dict, folder: Path) -> Path:
    folder.mkdir(parents=True,exist_ok=True)
    path=folder/('usage-'+uuid.uuid4().hex+'.json')
    with path.open('x',encoding='utf-8') as stream:
        json.dump(bundle,stream,indent=2,ensure_ascii=True)
        stream.write('\n')
    return path
