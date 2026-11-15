"""Standalone root helper for bounded cleanup of one owned cgroup."""
import errno
from pathlib import Path
import re
import sys
import time


def wait_empty(path, *, timeout=5, clock=time.monotonic, sleep=time.sleep):
    deadline=clock()+timeout
    while True:
        # cgroup virtual files report size zero even while they contain PIDs.
        events=dict(line.split() for line in (path/'cgroup.events').read_text().splitlines())
        if events.get('populated')=='0': return deadline
        if events.get('populated')!='1': raise ValueError('missing cgroup populated state')
        if clock()>=deadline: raise TimeoutError('owned cgroup remains populated')
        sleep(.05)


def cleanup(value, *, kill):
    root=r'/sys/fs/cgroup/c4g[0-9a-f]{12}'
    pattern=root+r'/child-[0-9a-f]{32}' if kill else root
    if not isinstance(value,str) or re.fullmatch(pattern,value) is None:
        raise ValueError('unsafe cgroup cleanup path')
    path=Path(value)
    if not path.exists(): return
    if kill: (path/'cgroup.kill').write_text('1\n')
    deadline=wait_empty(path)
    while True:
        try:
            path.rmdir()
            return
        except FileNotFoundError:
            return
        except OSError as error:
            if error.errno!=errno.EBUSY or time.monotonic()>=deadline: raise
            time.sleep(.05)


if __name__=='__main__':
    if len(sys.argv)!=3 or sys.argv[2] not in {'kill','remove'}:
        raise ValueError('expected owned cgroup path and kill/remove mode')
    cleanup(sys.argv[1],kill=sys.argv[2]=='kill')
