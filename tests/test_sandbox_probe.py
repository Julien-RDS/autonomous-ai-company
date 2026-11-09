"""Sonde de limites : agencements cgroup v1, v2 (namespace privé) et v2 (namespace hôte).

Sans Docker : le script fixe de la sonde est exécuté contre un /sys et /proc simulés.
"""
import contextlib
import io
import json

import pytest

from company import sandbox


def run_probe(files):
    def fake_open(path, *args, **kwargs):
        if path not in files:
            raise FileNotFoundError(path)
        return io.StringIO(files[path])
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(sandbox.PROBE_SCRIPT, {"open": fake_open, "__name__": "probe"})
    return json.loads(out.getvalue())


V1 = {"/proc/self/cgroup": "4:memory:/docker/abc\n8:pids:/docker/abc\n1:cpu,cpuacct:/docker/abc\n",
      "/sys/fs/cgroup/memory/memory.limit_in_bytes": "268435456",
      "/sys/fs/cgroup/memory/memory.memsw.limit_in_bytes": "268435456",
      "/sys/fs/cgroup/pids/pids.max": "64",
      "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us": "100000",
      "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us": "100000"}
V2_PRIVATE = {"/proc/self/cgroup": "0::/\n", "/sys/fs/cgroup/memory.max": "268435456",
              "/sys/fs/cgroup/memory.swap.max": "0", "/sys/fs/cgroup/pids.max": "64",
              "/sys/fs/cgroup/cpu.max": "100000 100000"}
NESTED = "/sys/fs/cgroup/system.slice/docker-abc.scope"
V2_HOST = {"/proc/self/cgroup": "0::/system.slice/docker-abc.scope\n",
           # Root files belong to the HOST and must not be mistaken for the container's limits.
           "/sys/fs/cgroup/memory.max": "max", "/sys/fs/cgroup/pids.max": "max",
           "/sys/fs/cgroup/cpu.max": "max 100000", "/sys/fs/cgroup/memory.swap.max": "max",
           f"{NESTED}/memory.max": "268435456", f"{NESTED}/memory.swap.max": "0",
           f"{NESTED}/pids.max": "64", f"{NESTED}/cpu.max": "100000 100000"}


@pytest.mark.parametrize("files", [V1, V2_PRIVATE, V2_HOST], ids=["v1", "v2-private", "v2-host"])
def test_probe_reads_each_cgroup_layout(files):
    problems = sandbox.limits_mismatch(run_probe(files), sandbox.DEFAULT_LIMITS)
    # The real uid of the test process may be root; only the limits matter here.
    assert set(problems) <= {"utilisateur non root"}


def test_probe_refuses_unlimited_v2_and_reports_observed_values():
    observed = run_probe({"/proc/self/cgroup": "0::/\n", "/sys/fs/cgroup/memory.max": "max",
                          "/sys/fs/cgroup/memory.swap.max": "max", "/sys/fs/cgroup/pids.max": "max",
                          "/sys/fs/cgroup/cpu.max": "max 100000"})
    problems = sandbox.limits_mismatch(observed, sandbox.DEFAULT_LIMITS)
    assert {"mémoire", "swap", "processus", "CPU"} <= set(problems)
    assert "memory=max" in sandbox.describe_observed(observed)


def test_probe_without_cgroup_files_refuses():
    observed = run_probe({})
    assert {"mémoire", "swap", "processus", "CPU"} <= set(sandbox.limits_mismatch(observed, sandbox.DEFAULT_LIMITS))


def test_failed_create_does_not_remove_an_existing_probe(tmp_path, monkeypatch):
    import subprocess
    removed=[]
    monkeypatch.setattr(sandbox,'_docker',lambda *a,**k: subprocess.CompletedProcess(a,1,b'',b'name already exists'))
    monkeypatch.setattr(sandbox,'_remove',lambda *a:removed.append(a))
    problems=sandbox.probe_enforcement('docker',tmp_path,'sha256:'+'a'*64,sandbox.DEFAULT_LIMITS,'b'*32)
    assert problems==['conteneur de vérification non créé']
    assert removed==[]


def test_concurrent_secure_base_creation(tmp_path, monkeypatch):
    import os
    import threading
    from concurrent.futures import ThreadPoolExecutor
    base=tmp_path/'shared'
    monkeypatch.setenv('SANDBOX_WORKDIR',str(base))
    original=os.lstat
    barrier=threading.Barrier(2)
    local=threading.local()
    def lstat(path,*args,**kwargs):
        if str(path)==str(base) and not getattr(local,'seen',False):
            local.seen=True
            try:
                return original(path,*args,**kwargs)
            except FileNotFoundError:
                barrier.wait(timeout=5)
                raise
        return original(path,*args,**kwargs)
    monkeypatch.setattr(os,'lstat',lstat)
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda _:sandbox.secure_base(),range(2)))
    assert results==[base,base]
    assert base.stat().st_mode & 0o777 == 0o700
