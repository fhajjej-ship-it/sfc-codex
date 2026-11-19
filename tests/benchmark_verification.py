"""Offline Stage 1 fixture: real checks, temporary Git, fake GitHub/Codex."""
import hashlib
import json
from pathlib import Path
import shlex
import statistics
import sys
import time

from test_verification_reuse import FlowTests, contract
from sfc_lib.factory import Factory
from sfc_lib.common import git


def sample(reuse):
    fixture = FlowTests('test_representative_flow_reuses_checks_keeps_all_independent_lanes')
    fixture.setUp()
    try:
        # Same check, source, lanes and merge protocol in both variants. The
        # deliberate delay is fixture workload, not a real suite speed claim.
        command = shlex.join([sys.executable, '-c',
                              "import time; from pathlib import Path; assert Path('value.txt').exists(); time.sleep(0.2)"])
        fixture.c['verify_commands'] = [command]
        if reuse:
            fixture.c['verification_reuse'] = [contract(command)]
        f = Factory(fixture.root, fixture.c, fixture.gh, fixture.proc)
        tick = time.monotonic()
        f.run(allow_merge=True)
        elapsed = time.monotonic() - tick
        r = f.record(fixture.gh.items[0])
        receipts = [json.loads(p.read_text()) for p in f.run_dir.glob('issue-1/verify-*/receipt.json')]
        executions = [c for v in receipts for c in v['checks'] if not c.get('reuse')]
        return {'reuse_enabled': reuse, 'source_tree': git(fixture.root, 'rev-parse', r['head'] + '^{tree}'),
                'command': command, 'executions': len(executions),
                'reuses': sum(bool(c.get('reuse')) for v in receipts for c in v['checks']),
                'verification_seconds': sum(c['elapsed_seconds'] for c in executions),
                'verification_wall_seconds': sum(v['elapsed_seconds'] for v in receipts),
                'end_to_end_seconds': elapsed, 'lanes': fixture.proc.lanes,
                'merge_writes': len(fixture.gh.attempts), 'phase': r['local_merge']['phase']}
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    samples = [sample(reuse) for _ in range(3) for reuse in (False, True)]
    assert len({s['source_tree'] for s in samples}) == 1
    medians = {str(reuse): {k: statistics.median(s[k] for s in samples if s['reuse_enabled'] == reuse)
                           for k in ('executions', 'verification_seconds', 'end_to_end_seconds')}
               for reuse in (False, True)}
    source = Path(__file__).resolve().parents[1] / 'scripts/sfc_lib/verification.py'
    print(json.dumps({'scope': 'offline controlled fixture; no customer or model latency claim',
                      'implementation_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                      'medians': medians, 'samples': samples}, indent=2))
