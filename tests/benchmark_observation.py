"""Offline benchmark: temp repos/fake GitHub; python3 tests/benchmark_observation.py.

The 2ms request delay is simulated, not a customer latency measurement.
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from sfc_lib.common import git
from sfc_lib.factory import Factory
from sfc_lib.watch import Watch
from test_pickup import PickupGitHub, ticket


def main():
    rows = []
    for size in (1, 25, 100, 250):
        with tempfile.TemporaryDirectory(prefix='sfc-observation-benchmark-') as folder:
            root = Path(folder)
            git(root, 'init', '-b', 'main')
            gh = PickupGitHub(root, [ticket(n, status='Done' if n > 1 else 'Backlog',
                                          state='CLOSED' if n > 1 else 'OPEN') for n in range(1, size + 1)])
            factory = Factory(root, gh.c, gh)
            factory.watcher = Watch(factory)
            factory.include_backlog = True
            factory.state['cards'] = {str(n): {'status': 'Done', 'activity': {'state': 'Completed'}}
                                      for n in range(2, size + 1)}
            gh.metadata()
            gh.reads.clear()
            original = gh.graphql
            def delayed(query, variables):
                time.sleep(.002)
                return original(query, variables)
            gh.graphql = delayed
            started = time.perf_counter()
            cards = factory.queue_cards()
            elapsed = time.perf_counter() - started
            assert not gh.writes and not gh.activity_writes
            rows.append({'board_issues': size, 'graphql_calls': len(gh.reads), 'seconds': elapsed,
                         'active_issues': [c['number'] for c in cards if c['state'] == 'OPEN'], 'writes': 0})
    sources = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
        'scripts/sfc_lib/factory.py', 'scripts/sfc_lib/pickup.py', 'tests/test_pickup.py')}
    print(json.dumps({'transport': 'fake, 2ms added per GraphQL request', 'sources_sha256': sources,
                      'rows': rows}, indent=2))


if __name__ == '__main__':
    main()
