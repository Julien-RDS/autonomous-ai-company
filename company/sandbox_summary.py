"""Bounded parsing; a reported summary never sets the execution verdict or permissions."""
import json
PREFIX='AAC_RESULT_V1:'
KEYS={'discovered','run','passed','failed','errors','skipped','expected_failures','unexpected_successes'}

def parse_summary(stderr, profile, *, truncated=False):
    if truncated or len(stderr)>131072:
        return None
    records=[line[len(PREFIX):] for line in stderr.splitlines() if line.startswith(PREFIX)]
    if len(records)!=1 or len(records[0])>2048:
        return None
    try:
        value=json.loads(records[0])
    except (ValueError,TypeError):
        return None
    if not isinstance(value,dict): return None
    if profile=='static-html':
        return value if (set(value)=={'kind','structure_valid'} and value.get('kind')=='static'
                         and type(value.get('structure_valid')) is bool and value['structure_valid']) else None
    counts=value.get('counts')
    if set(value)!={'kind','counts'} or profile!='python-unittest' or value.get('kind')!='unit' or not isinstance(counts,dict) or set(counts)!=KEYS:
        return None
    if any(type(v) is not int or not 0<=v<=100000 for v in counts.values()): return None
    if counts['run']>counts['discovered'] or counts['discovered']==0: return None
    if sum(counts[k] for k in KEYS-{'discovered','run'})!=counts['run']: return None
    return {'kind':'unit','counts':counts}
