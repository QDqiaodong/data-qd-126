"""End-to-end tests for the SQLite group ledger (init / assign / export).

Every test drives the installed CLI in a subprocess against real files.  The
contract exercised here:

* arms, integer-share treatment probability p (0<p<1) and the seed are fixed by
  ``init``; identifiers are validated by the same rules as the analyzer;
* ``assign`` takes ``{"batch_id": ..., "users": [...]}`` with at most 20000
  users.  Unique users per batch, no repetition across batches; replaying the
  same batch id with the same set is a no-op, a different set is rejected and
  every conflict rolls the whole batch back;
* after each commit the treatment arm holds exactly ``floor(total * p)`` users,
  earlier assignments never move, and new slots go to the users with the
  smallest SHA256 key (ties by user) so allocation is reproducible;
* ``export`` writes the same fields as the analyzer input (minus ``outcomes``)
  in user order, accepts appended outcomes, keeps the output-safety rules and
  survives a million users inside a 128 MiB address space.

Failure of ``export`` after a commit must be recoverable from the ledger alone:
re-running ``export`` reproduces the identical file without regrouping.
"""
import hashlib, json, os, random, subprocess, sys, tempfile, textwrap, unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 128 * 1024 * 1024


def call(*argv, cwd=None):
    return subprocess.run([sys.executable, '-m', 'experiment_lift.cli', *map(str, argv)],
                          capture_output=True, text=True, cwd=str(cwd or ROOT),
                          env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})


def init(path, control='C', treatment='T', numerator=1, denominator=3, seed='seed-1'):
    result = call('init', '--ledger', path, '--control', control, '--treatment', treatment,
                  '--numerator', numerator, '--denominator', denominator, '--seed', seed)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def write_batch(directory, name, batch_id, users):
    path = Path(directory) / name
    path.write_text(json.dumps({'batch_id': batch_id, 'users': users}), encoding='utf-8')
    return path


def assign(ledger, source, output):
    return call('assign', '--ledger', ledger, '--source', source, '--output', output)


def exported(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def variant_of(document, user):
    return next(row['variant'] for row in document['assignments'] if row['user'] == user)


def key(seed, user):
    return hashlib.sha256(b'experiment-lift/assign/v1\0' + seed.encode() + b'\0' + user.encode()).digest()


class Ledger(unittest.TestCase):
    def test_init_freezes_contract_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / 'ledger.db'
            summary = init(ledger, numerator=2, denominator=5, seed='s')
            self.assertEqual(summary['expected_treatment_share'], {'numerator': 2, 'denominator': 5})
            self.assertEqual((summary['control'], summary['treatment'], summary['seed']), ('C', 'T', 's'))
            before = ledger.read_bytes()
            self.assertEqual(call('init', '--ledger', ledger, '--control', 'C', '--treatment', 'T',
                                  '--numerator', 1, '--denominator', 2, '--seed', 'x').returncode, 2)
            self.assertEqual(ledger.read_bytes(), before)

    def test_init_identifier_and_share_validation(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / 'ledger.db'
            cases = [dict(control='C', treatment='C'), dict(control=' C'), dict(treatment=''),
                     dict(seed='bad\x01seed'), dict(control='x' * 129),
                     dict(numerator=0), dict(numerator=3, denominator=3),
                     dict(numerator=2, denominator=1), dict(numerator=1, denominator=1)]
            for index, overrides in enumerate(cases):
                target = Path(td) / f'l{index}.db'
                kwargs = dict(numerator=1, denominator=3, seed='s', control='C', treatment='T')
                kwargs.update(overrides)
                result = call('init', '--ledger', target, '--control', kwargs['control'],
                              '--treatment', kwargs['treatment'], '--numerator', kwargs['numerator'],
                              '--denominator', kwargs['denominator'], '--seed', kwargs['seed'])
                self.assertEqual(result.returncode, 2, overrides)
                self.assertFalse(target.exists(), overrides)
            self.assertFalse(any(p.name.startswith('l') for p in Path(td).iterdir() if p.suffix == '.db'))

    def test_batch_input_validation(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / 'ledger.db'
            init(ledger)
            out = Path(td) / 'out.json'
            bad_bodies = [
                '{}',
                '{"batch_id":"b","users":[]}',
                '{"batch_id":"b","users":["a"],"extra":1}',
                '{"batch_id":" b","users":["a"]}',
                '{"batch_id":"b","users":["a","a"]}',
                '{"batch_id":"b","users":[1]}',
                '{"batch_id":"b","users":["a"],"users":["b"]}',
                '{"batch_id":"b","users":["a"]} trailing',
                '{"batch_id":"b","users":[NaN]}',
                json.dumps({'batch_id': 'b', 'users': [f'u{i}' for i in range(20001)]}),
            ]
            for index, body in enumerate(bad_bodies):
                src = Path(td) / f'bad{index}.json'
                src.write_text(body, encoding='utf-8')
                result = assign(ledger, src, out)
                self.assertEqual(result.returncode, 2, body[:60])
                self.assertFalse(out.exists())
            self.assertEqual(call('export', '--ledger', ledger, '--output', out).returncode, 0)
            self.assertEqual(exported(out)['assignments'], [])

    def test_exact_quota_across_batches_and_sticky_assignments(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / 'ledger.db'
            init(ledger, numerator=2, denominator=5, seed='quota')
            total, previous = 0, {}
            for round_index, size in enumerate([7, 3, 11, 1, 200, 1999]):
                users = [f'r{round_index}-u{i}' for i in range(size)]
                source = write_batch(td, f'b{round_index}.json', f'batch-{round_index}', users)
                out = Path(td) / f'out{round_index}.json'
                result = assign(ledger, source, out)
                self.assertEqual(result.returncode, 0, result.stderr)
                document = exported(out)
                self.assertEqual([row['user'] for row in document['assignments']],
                                 sorted(set(previous) | set(users)))
                treatment = sum(1 for row in document['assignments'] if row['variant'] == 'T')
                previous_treated = sum(1 for variant in previous.values() if variant == 'T')
                total += size
                self.assertEqual(treatment, (total * 2) // 5)
                for user, variant in previous.items():
                    self.assertEqual(variant_of(document, user), variant)
                previous = {row['user']: row['variant'] for row in document['assignments']}
                summary = json.loads(result.stdout)
                self.assertEqual(summary['users'], total)
                self.assertEqual(summary['treated'], treatment)
                self.assertEqual(summary['batch_treated'], treatment - previous_treated)

    def test_allocation_is_seed_and_user_deterministic_and_ignores_order(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / 'ledger.db'
            init(ledger, numerator=1, denominator=2, seed='fixed-seed')
            users = [f'user-{i}' for i in range(40)]
            shuffled = list(users)
            random.Random(7).shuffle(shuffled)
            first = assign(ledger, write_batch(td, 'a.json', 'b1', shuffled), Path(td) / 'a.json.out')
            self.assertEqual(first.returncode, 0, first.stderr)
            document = exported(Path(td) / 'a.json.out')
            expected = sorted(users, key=lambda user: (key('fixed-seed', user), user))[:20]
            self.assertEqual({variant_of(document, user) for user in expected}, {'T'})
            self.assertEqual(sum(1 for row in document['assignments'] if row['variant'] == 'T'), 20)
            self.assertEqual([row['user'] for row in document['assignments']], sorted(users))
            # A different seed changes the treatment set; a different input order does not.
            other = Path(td) / 'other.db'
            init(other, numerator=1, denominator=2, seed='other-seed')
            self.assertEqual(assign(other, write_batch(td, 'b.json', 'b1', users),
                                    Path(td) / 'b.out').returncode, 0)
            other_doc = exported(Path(td) / 'b.out')
            self.assertNotEqual([row['user'] for row in document['assignments'] if row['variant'] == 'T'],
                                [row['user'] for row in other_doc['assignments'] if row['variant'] == 'T'])

    def test_retry_paths_and_conflict_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / 'ledger.db'
            init(ledger, numerator=1, denominator=3, seed='retry')
            base = write_batch(td, 'b1.json', 'b1', ['a', 'b', 'c', 'd'])
            out = Path(td) / 'out.json'
            self.assertEqual(assign(ledger, base, out).returncode, 0)
            first = out.read_bytes()
            replay = assign(ledger, base, out)
            self.assertEqual(replay.returncode, 0, replay.stderr)
            self.assertTrue(json.loads(replay.stdout)['idempotent'])
            self.assertEqual(out.read_bytes(), first)
            # An out-of-order replay of the same user set is the same batch.
            shuffled = ['d', 'a', 'c', 'b']
            reordered = assign(ledger, write_batch(td, 'b1-shuffled.json', 'b1', shuffled), out)
            self.assertEqual(reordered.returncode, 0, reordered.stderr)
            self.assertTrue(json.loads(reordered.stdout)['idempotent'])
            self.assertEqual(out.read_bytes(), first)
            # Same batch id, different set: rejected, ledger unchanged.
            conflict = write_batch(td, 'b2.json', 'b1', ['a', 'b', 'c'])
            self.assertEqual(assign(ledger, conflict, out).returncode, 2)
            self.assertEqual(out.read_bytes(), first)
            # Overlapping users already committed in another batch: whole batch rolls back.
            overlap = write_batch(td, 'b3.json', 'b3', ['x', 'y', 'a'])
            self.assertEqual(assign(ledger, overlap, out).returncode, 2)
            self.assertEqual(out.read_bytes(), first)
            # The rejected batch is not recorded: reusing its id with a clean set works.
            clean = write_batch(td, 'b4.json', 'b3', ['x', 'y', 'z'])
            self.assertEqual(assign(ledger, clean, out).returncode, 0)
            document = exported(out)
            self.assertEqual(sorted(row['user'] for row in document['assignments']),
                             ['a', 'b', 'c', 'd', 'x', 'y', 'z'])
            self.assertEqual(sum(1 for row in document['assignments'] if row['variant'] == 'T'), 7 // 3)
            self.assertEqual(exported(out)['expected_treatment_share'], {'numerator': 1, 'denominator': 3})

    def test_assign_commit_survives_failed_export_and_replays_idempotently(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / 'ledger.db'
            init(ledger, numerator=1, denominator=3, seed='commit-export')
            users = [f'c{i:02d}' for i in range(30)]
            source = write_batch(td, 'b.json', 'commit-1', users)
            out = Path(td) / 'out.json'
            out.mkdir()  # publication cannot succeed; serialisation still runs
            failed = assign(ledger, source, out)
            self.assertEqual(failed.returncode, 2)
            out.rmdir()
            # The batch is already committed: replay is a no-op that exports it.
            retry = assign(ledger, source, out)
            self.assertEqual(retry.returncode, 0, retry.stderr)
            summary = json.loads(retry.stdout)
            self.assertTrue(summary['idempotent'])
            self.assertEqual(summary['assigned'], 0)
            self.assertEqual(summary['users'], 30)
            self.assertEqual(summary['treated'], 10)
            document = exported(out)
            self.assertEqual(len(document['assignments']), 30)
            self.assertEqual(sum(1 for row in document['assignments'] if row['variant'] == 'T'), 10)
            # A fresh export of the same ledger is byte-identical (no regroup).
            second = Path(td) / 'second.json'
            self.assertEqual(call('export', '--ledger', ledger, '--output', second).returncode, 0)
            self.assertEqual(second.read_bytes(), out.read_bytes())
            # The failed first attempt left no publication temp files behind.
            self.assertEqual([p.name for p in Path(td).iterdir() if p.name.startswith('.experiment-ledger-')], [])

    def test_export_recovery_after_failed_publication(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / 'ledger.db'
            init(ledger, numerator=1, denominator=4, seed='recover')
            users = [f'u{i:03d}' for i in range(50)]
            out = Path(td) / 'out.json'
            result = assign(ledger, write_batch(td, 'b.json', 'b', users), out)
            self.assertEqual(result.returncode, 0, result.stderr)
            good = out.read_bytes()
            # Simulate "commit succeeded, export failed": make the destination a directory
            # so publication is impossible, then retry export only.
            out.unlink()
            out.mkdir()
            (out / 'keep').write_text('x')
            retry = call('export', '--ledger', ledger, '--output', out)
            self.assertEqual(retry.returncode, 2)
            self.assertEqual((out / 'keep').read_text(), 'x')
            # Removing the obstruction lets the read-only export reproduce byte-identical output.
            (out / 'keep').unlink()
            out.rmdir()
            again = call('export', '--ledger', ledger, '--output', out)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(out.read_bytes(), good)
            # Replaying assign with the same batch must also not regroup or double-count.
            self.assertEqual(assign(ledger, Path(td) / 'b.json', out).returncode, 0)
            self.assertEqual(out.read_bytes(), good)
            # Leftover temp files from the failed publication are cleaned up.
            self.assertEqual([p.name for p in Path(td).iterdir() if p.name.startswith('.experiment-ledger-')], [])

    def test_lock_conflict_exits_2_without_writing(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / 'ledger.db'
            init(ledger, numerator=1, denominator=2, seed='lock')
            out = Path(td) / 'out.json'
            self.assertEqual(assign(ledger, write_batch(td, 'b1.json', 'b1', ['a', 'b']), out).returncode, 0)
            first = out.read_bytes()
            holder = textwrap.dedent(f'''
                import sqlite3, time
                conn = sqlite3.connect({str(ledger)!r}, isolation_level=None)
                conn.execute('BEGIN EXCLUSIVE')
                print('held', flush=True)
                time.sleep(30)
            ''')
            child = subprocess.Popen([sys.executable, '-c', holder], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'held')
                wrapper = textwrap.dedent(f'''
                    import sys
                    sys.path.insert(0, {str(ROOT / 'src')!r})
                    from experiment_lift import ledger
                    ledger.BUSY_TIMEOUT_MS = 200
                    from experiment_lift.cli import main
                    raise SystemExit(main())
                ''')
                source = write_batch(td, 'b2.json', 'b2', ['c', 'd'])
                result = subprocess.run([sys.executable, '-I', '-c', wrapper, 'assign', '--ledger', str(ledger),
                                         '--source', str(source), '--output', str(out)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn('locked', result.stderr)
                self.assertEqual(out.read_bytes(), first)
            finally:
                child.kill()
                child.wait()
                child.stdout.close()
            # Once the lock goes away the batch is still unknown and can be committed.
            self.assertEqual(assign(ledger, Path(td) / 'b2.json', out).returncode, 0)
            self.assertEqual(len(exported(out)['assignments']), 4)

    def test_unicode_and_escaped_identifiers_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / 'ledger.db'
            init(ledger, control='对照', treatment='处理', numerator=1, denominator=2, seed='种子')
            users = ['café', '中文', 'emoji-\U0001f600', 'a b', '\U0001f600']
            body = ('{"batch_id":"\\u6279\\u6b21","users":["caf\\u00e9","\\u4e2d\\u6587",'
                    '"emoji-\\ud83d\\ude00","a b","\\ud83d\\ude00"]}')
            src = Path(td) / 'b.json'
            src.write_text(body, encoding='utf-8')
            out = Path(td) / 'out.json'
            result = assign(ledger, src, out)
            self.assertEqual(result.returncode, 0, result.stderr)
            document = json.loads(out.read_text(encoding='ascii'))
            self.assertEqual([row['user'] for row in document['assignments']],
                             sorted(users, key=lambda user: user.encode('utf-8')))
            self.assertEqual(document['control'], '对照')
            self.assertEqual(document['treatment'], '处理')
            self.assertEqual(document['expected_treatment_share'], {'numerator': 1, 'denominator': 2})
            # The exported document is accepted by the original analyzer unchanged.
            document['outcomes'] = [{'user': row['user'], 'converted': 1}
                                    for row in document['assignments']]
            analysis = Path(td) / 'analysis.json'
            analysis.write_text(json.dumps(document), encoding='utf-8')
            report = call('--source', analysis, '--output', Path(td) / 'report.json')
            self.assertEqual(report.returncode, 0, report.stderr)

    def test_export_output_safety(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / 'ledger.db'
            init(ledger)
            out = Path(td) / 'out.json'
            self.assertEqual(assign(ledger, write_batch(td, 'b.json', 'b', ['a', 'b']), out).returncode, 0)
            before = out.read_bytes()
            link = Path(td) / 'link.json'
            link.symlink_to(out)
            self.assertEqual(call('export', '--ledger', ledger, '--output', link).returncode, 2)
            self.assertEqual(out.read_bytes(), before)
            self.assertEqual(call('export', '--ledger', ledger, '--output', ledger).returncode, 2)
            self.assertEqual(out.read_bytes(), before)
            source = write_batch(td, 'same.json', 'third', ['c'])
            self.assertEqual(assign(ledger, source, source).returncode, 2)
            link = Path(td) / 'link2.json'
            link.symlink_to(out)
            self.assertEqual(assign(ledger, source, link).returncode, 2)
            # The rejected assign never consumed its batch id or users.
            self.assertEqual(assign(ledger, source, out).returncode, 0)
            self.assertEqual(len(exported(out)['assignments']), 3)

    def test_export_is_analysis_input_after_appending_outcomes(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / 'ledger.db'
            init(ledger, numerator=1, denominator=2, seed='analysis')
            out = Path(td) / 'out.json'
            self.assertEqual(assign(ledger, write_batch(td, 'b.json', 'b', [f'u{i}' for i in range(8)]),
                                    out).returncode, 0)
            document = exported(out)
            self.assertEqual(sorted(document), ['assignments', 'control', 'expected_treatment_share', 'treatment'])
            document['outcomes'] = [{'user': row['user'], 'converted': index % 2}
                                    for index, row in enumerate(document['assignments'])]
            analysis = Path(td) / 'analysis.json'
            analysis.write_text(json.dumps(document), encoding='utf-8')
            report = call('--source', analysis, '--output', Path(td) / 'report.json')
            self.assertEqual(report.returncode, 0, report.stderr)
            result = exported(Path(td) / 'report.json')['result']
            self.assertEqual(result['arms']['C']['assigned'] + result['arms']['T']['assigned'], 8)

    def test_export_beyond_default_limits_uses_large_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / 'ledger.db'
            init(ledger, numerator=3, denominator=7, seed='large-analysis')
            out = Path(td) / 'out.json'
            total = 25_000
            for batch in range(2):
                src = write_batch(td, f'b{batch}.json', f'b{batch}',
                                  [f'u{i}' for i in range(batch * 12_500, (batch + 1) * 12_500)])
                self.assertEqual(assign(ledger, src, out).returncode, 0)
            document = exported(out)
            self.assertEqual(len(document['assignments']), total)
            self.assertEqual(sum(1 for row in document['assignments'] if row['variant'] == 'T'),
                             total * 3 // 7)
            document['outcomes'] = [{'user': row['user'], 'converted': 1 if index % 3 == 0 else 0}
                                    for index, row in enumerate(document['assignments'])]
            analysis = Path(td) / 'analysis.json'
            analysis.write_text(json.dumps(document), encoding='utf-8')
            self.assertEqual(call('--source', analysis, '--output', Path(td) / 'report.json').returncode, 2)
            large = call('--source', analysis, '--output', Path(td) / 'large.json', '--large-snapshot')
            self.assertEqual(large.returncode, 0, large.stderr)
            result = exported(Path(td) / 'large.json')['result']
            self.assertEqual(result['arms']['C']['assigned'], total - total * 3 // 7)
            self.assertEqual(result['arms']['T']['assigned'], total * 3 // 7)

    def test_concurrent_writers_preserve_quota_and_identity(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / 'ledger.db'
            init(ledger, numerator=1, denominator=3, seed='race')
            batches = []
            for index in range(6):
                users = [f'w{index}-{i}' for i in range(37)]
                batches.append((write_batch(td, f'w{index}.json', f'w{index}', users),
                                Path(td) / f'w{index}.out.json'))
            with ThreadPoolExecutor(max_workers=len(batches)) as pool:
                results = list(pool.map(lambda pair: assign(ledger, pair[0], pair[1]), batches))
            for result in results:
                self.assertIn(result.returncode, (0, 2), result.stderr)
                if result.returncode == 2:
                    self.assertIn('locked', result.stderr)
                else:
                    self.assertNotIn('locked', result.stderr)
            wins = [pair for pair, result in zip(batches, results) if result.returncode == 0]
            self.assertTrue(wins)
            final = Path(td) / 'final.json'
            self.assertEqual(call('export', '--ledger', ledger, '--output', final).returncode, 0)
            document = exported(final)
            total = len(document['assignments'])
            self.assertEqual(total, 37 * len(wins))
            self.assertEqual(sum(1 for row in document['assignments'] if row['variant'] == 'T'), total // 3)
            self.assertEqual(len({row['user'] for row in document['assignments']}), total)
            # The successful writers' exports agree with the final ledger counts.
            final_map = {row['user']: row['variant'] for row in document['assignments']}
            for source, output in wins:
                view = exported(output)
                self.assertEqual(sum(1 for row in view['assignments'] if row['variant'] == 'T'),
                                 len(view['assignments']) // 3)
                for row in view['assignments']:
                    self.assertEqual(final_map[row['user']], row['variant'])
            # A subsequent conflicting batch with repeated users is rejected entirely.
            clash = write_batch(td, 'clash.json', 'clash', ['w0-0', 'fresh'])
            self.assertEqual(assign(ledger, clash, Path(td) / 'clash.out').returncode, 2)
            self.assertEqual(len(exported(final)['assignments']), total)

    def test_concurrent_writers_never_duplicate_users(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / 'ledger.db'
            init(ledger, numerator=1, denominator=2, seed='race-shared')
            batches = []
            for index in range(8):
                # Every batch claims the same shared user; only one can commit it.
                users = ['shared', f's{index}-a', f's{index}-b']
                batches.append((write_batch(td, f's{index}.json', f's{index}', users),
                                Path(td) / f's{index}.out.json'))
            with ThreadPoolExecutor(max_workers=len(batches)) as pool:
                results = list(pool.map(lambda pair: assign(ledger, pair[0], pair[1]), batches))
            for result in results:
                self.assertIn(result.returncode, (0, 2), result.stderr)
                if result.returncode == 2:
                    # Either the lock timed out or a competing batch already took the user.
                    self.assertTrue('locked' in result.stderr or 'already assigned' in result.stderr,
                                    result.stderr)
                else:
                    self.assertNotIn('locked', result.stderr)
            winners = sum(1 for result in results if result.returncode == 0)
            self.assertEqual(winners, 1)
            final = Path(td) / 'final.json'
            self.assertEqual(call('export', '--ledger', ledger, '--output', final).returncode, 0)
            document = exported(final)
            self.assertEqual(len(document['assignments']), 3)
            self.assertEqual(sum(1 for row in document['assignments'] if row['user'] == 'shared'), 1)
            self.assertEqual(sum(1 for row in document['assignments'] if row['variant'] == 'T'), 1)

    def test_large_batches_build_a_million_user_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td) / 'ledger.db'
            init(ledger, numerator=1, denominator=2, seed='million')
            total = 1_000_000
            size = 20_000
            step = 7919
            out = Path(td) / 'out.json'
            for batch in range(total // size):
                src = Path(td) / 'batch.json'
                with src.open('w', encoding='utf-8') as stream:
                    stream.write('{"batch_id":"m%d","users":[' % batch)
                    stream.write(','.join('"u%d"' % ((i * step) % total)
                                          for i in range(batch * size, (batch + 1) * size)))
                    stream.write(']}')
                result = assign(ledger, src, out)
                self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual((summary['users'], summary['treated']), (total, total // 2))
            first = out.read_bytes()
            self.assertTrue(first.startswith(b'{"assignments":[{"user":"u0",'))
            wrapper = textwrap.dedent(f'''
                import resource, sys
                resource.setrlimit(resource.RLIMIT_AS, ({LIMIT}, {LIMIT}))
                sys.path.insert(0, {str(ROOT / 'src')!r})
                from experiment_lift.cli import main
                raise SystemExit(main())
            ''')
            exported_again = subprocess.run(
                [sys.executable, '-I', '-c', wrapper, 'export', '--ledger', str(ledger), '--output', str(out)],
                capture_output=True, text=True)
            self.assertEqual(exported_again.returncode, 0, exported_again.stderr)
            self.assertEqual(out.read_bytes(), first)
            # Spot-check ordering and the exact quota without loading the file whole.
            document = json.loads(first)
            self.assertEqual(len(document['assignments']), total)
            self.assertEqual(sum(1 for row in document['assignments'] if row['variant'] == 'T'), total // 2)
            self.assertEqual([row['user'] for row in document['assignments']][:3], ['u0', 'u1', 'u10'])


if __name__ == '__main__':
    unittest.main()
