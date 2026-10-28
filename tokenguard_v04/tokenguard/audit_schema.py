"""Validate audit observations before arithmetic or identity-based attribution."""
from dataclasses import dataclass, field
import json
import math
from typing import Any


def count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63-1


def number(value: Any) -> bool:
    return count(value) or (isinstance(value, float) and math.isfinite(value) and 0 <= value <= 2**63-1)


def identifier(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 256 and
            not any(ord(char) < 32 or ord(char) == 127 for char in value))


def compatible(child: dict, parent: dict, *, source: bool = False) -> bool:
    """Missing legacy context is unknown, but explicitly supplied context must agree."""
    keys = ('session_id', 'experiment_id', 'artifact_id') + (('source', 'tool_use_id') if source else ())
    return all(key not in child or child[key] == parent.get(key) for key in keys)


def valid(row: dict) -> bool:
    for key in ('compression_id', 'session_id', 'experiment_id', 'artifact_id', 'source', 'event_id', 'recovery_id',
                'tool_use_id', 'tool_name', 'command_type', 'strategy', 'pytest_mode', 'timestamp', 'replacement_status',
                'index_status'):
        if key in row and row[key] is not None and not identifier(row[key]):
            return False
    for key in ('original_chars', 'optimized_chars', 'saved_chars', 'recovery_count', 'returned_chars',
                'scanned_chars', 'index_build_bytes', 'seek_offset_bytes', 'windowed_matches',
                'omitted_context_lines', 'requested_start_char', 'integrity_build_bytes',
                'integrity_checked_bytes'):
        if key in row and not count(row[key]):
            return False
    if 'integrity_status' in row and row['integrity_status'] not in ('built', 'reused', 'uncached'):
        return False
    if 'requested_start_line' in row and (not count(row['requested_start_line']) or row['requested_start_line'] == 0):
        return False
    for key in ('requested_end_line', 'next_start_line'):
        if key in row and row[key] is not None and (not count(row[key]) or row[key] == 0):
            return False
    if 'next_start_char' in row and row['next_start_char'] is not None and not count(row['next_start_char']):
        return False
    for key in ('compression_latency_ms', 'recovery_latency_ms', 'compression_ratio'):
        if key in row and not number(row[key]):
            return False
    for key in ('original_token_estimate', 'optimized_token_estimate'):
        if key in row and (not isinstance(row[key], dict) or not count(row[key].get('estimated_tokens'))):
            return False
    kind = row.get('event')
    if kind == 'compression':
        if (not identifier(row.get('compression_id')) or not count(row.get('original_chars')) or
                not count(row.get('optimized_chars')) or row['optimized_chars'] > row['original_chars']):
            return False
        if 'saved_chars' in row and row['saved_chars'] != row['original_chars']-row['optimized_chars']:
            return False
        if 'compression_ratio' in row:
            original = row['original_chars']
            if not original or not math.isclose(row['compression_ratio'], row['optimized_chars']/original):
                return False
        return True
    if kind == 'recovery':
        return (count(row.get('returned_chars')) and (row.get('compression_id') is not None or
                all(row.get(key) is None for key in ('session_id', 'experiment_id'))))
    if kind == 'replacement_delivery':
        return (identifier(row.get('compression_id')) and
                all(key in row for key in ('session_id', 'experiment_id', 'source')) and
                row.get('status') in ('stdout_write_succeeded', 'stdout_write_failed'))
    return False


@dataclass
class AuditIndex:
    compressions: dict[str, dict] = field(default_factory=dict)
    recoveries: list[dict] = field(default_factory=list)
    deliveries: dict[str, dict] = field(default_factory=dict)
    invalid_rows: int = 0
    duplicate_rows: int = 0
    conflicting_rows: int = 0
    blocked_ids: set[str] = field(default_factory=set)


def _canonical(row: dict) -> str | None:
    try:
        return json.dumps({k: v for k, v in row.items() if k not in ('timestamp', 'event_id')},
                          sort_keys=True, ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return None


def validated(rows: list[dict]) -> AuditIndex:
    """Duplicates collapse; any conflict or invalid occurrence poisons that identity."""
    result = AuditIndex()
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        kind = row.get('event')
        identity = (row.get('compression_id') if kind in ('compression', 'replacement_delivery')
                    else row.get('recovery_id') if row.get('recovery_id') is not None else row.get('event_id'))
        is_valid = valid(row) and _canonical(row) is not None
        if not is_valid:
            result.invalid_rows += 1
        if identifier(identity) and kind in ('compression', 'replacement_delivery', 'recovery'):
            groups.setdefault((kind, identity), []).append(row)
        elif is_valid and kind == 'recovery':
            # Old recovery events have no event ID: identical requests may be
            # separate real reads, so do not infer a deduplication identity.
            result.recoveries.append(row)
    for (kind, identity), grouped in groups.items():
        canonical = {_canonical(row) for row in grouped}
        if len(canonical) > 1:
            result.conflicting_rows += len(grouped)
            if kind == 'compression':
                result.blocked_ids.add(identity)
            continue
        result.duplicate_rows += len(grouped)-1
        row = grouped[0]
        if None in canonical or not all(valid(item) for item in grouped):
            if kind == 'compression':
                result.blocked_ids.add(identity)
            continue
        if kind == 'compression':
            result.compressions[identity] = row
        elif kind == 'replacement_delivery':
            result.deliveries[identity] = row
        else:
            result.recoveries.append(row)
    recoveries = []
    for row in result.recoveries:
        identity = row.get('compression_id')
        if identity is not None:
            owner = result.compressions.get(identity)
            if owner is None or not compatible(row, owner):
                result.invalid_rows += 1
                continue
            # Legacy rows can omit context even though their explicit ID has a
            # unique valid owner. Derive only missing keys in a separate view;
            # explicit null/mismatched values were rejected above.
            derived = {key: owner[key] for key in ('session_id', 'experiment_id', 'artifact_id')
                       if key not in row and key in owner}
            if derived:
                row = {**row, **derived, 'derived_context_fields': sorted(derived)}
        recoveries.append(row)
    result.recoveries = recoveries
    for identity, row in list(result.deliveries.items()):
        owner = result.compressions.get(identity)
        if owner is None or not compatible(row, owner, source=True):
            result.invalid_rows += 1
            del result.deliveries[identity]
    return result
