"""Public CI diagnostics: node names only, never traceback locals or environment."""
import json
import re
import sys
from pathlib import Path


def node_name(nodeid):
    value=re.sub(r'\[[^\]]*\]','[case]',nodeid)
    return re.sub(r'[^a-zA-Z0-9_./:\[\]-]','_',value)[:240]


def pytest_runtest_logreport(report):
    if report.failed:
        print(f'::error title=pytest failure::{node_name(report.nodeid)} ({report.when})',flush=True)


def pytest_collectreport(report):
    if report.failed:
        print(f'::error title=pytest collection::{node_name(report.nodeid)}',flush=True)


def probe_notice(path):
    for line in Path(path).read_text(errors='replace').splitlines():
        prefix='Valeurs observées dans le conteneur : '
        if not line.startswith(prefix): continue
        values=json.loads(line[len(prefix):])
        safe={}
        for key in ('memory','swap_v2','memsw_v1','pids','cpu_v2','quota_v1','period_v1','uid'):
            value=values.get(key)
            if value is None or type(value) is int or (isinstance(value,str) and re.fullmatch(r'[0-9max ]{1,60}',value)):
                safe[key]=value
        print('::notice title=Sandbox kernel limits::'+json.dumps(safe,sort_keys=True),flush=True)

if __name__=='__main__':
    probe_notice(sys.argv[1])
