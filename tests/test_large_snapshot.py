"""End-to-end tests for the --large-snapshot streaming audit mode.

These write real files and run the installed CLI in a subprocess. The million
user/outcome case runs under a 128 MiB address-space limit; every failure path
also asserts that no scratch database or report temp file is left behind.
"""
import json, os, random, subprocess, sys, tempfile, textwrap, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'fixtures' / 'input.json'
LIMIT = 128 * 1024 * 1024


def call(source, output, large=True):
    argv = [sys.executable, '-m', 'experiment_lift.cli', '--source', str(source), '--output', str(output)]
    if large:
        argv.append('--large-snapshot')
    return subprocess.run(argv, capture_output=True, text=True, cwd=str(ROOT),
                          env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})


def snapshot(control='C', treatment='T', numerator=1, denominator=2, assignments=(), outcomes=()):
    return {'control': control, 'treatment': treatment,
            'expected_treatment_share': {'numerator': numerator, 'denominator': denominator},
            'assignments': list(assignments), 'outcomes': list(outcomes)}


def leftovers(directory):
    return sorted(p.name for p in Path(directory).iterdir() if p.name.startswith('.experiment-lift-'))


class LargeSnapshot(unittest.TestCase):
    def run_ok(self, td, body, name='in.json'):
        src = Path(td) / name
        if isinstance(body, bytes):
            src.write_bytes(body)
        elif isinstance(body, str):
            src.write_text(body, encoding='utf-8')
        else:
            src.write_text(json.dumps(body), encoding='utf-8')
        out = Path(td) / 'out.json'
        result = call(src, out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(leftovers(td), [])
        return json.loads(out.read_text())

    def run_bad(self, td, body, message=None):
        src = Path(td) / 'bad.json'
        src.write_bytes(body if isinstance(body, bytes) else json.dumps(body).encode('utf-8'))
        out = Path(td) / 'out.json'
        out.write_text('prior')
        result = call(src, out)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(out.read_text(), 'prior')
        self.assertEqual(leftovers(td), [])
        if message is not None:
            self.assertIn(message, result.stderr)
        return result

    def test_matches_legacy_report_field_by_field(self):
        with tempfile.TemporaryDirectory() as td:
            source = SRC.read_bytes()
            for body in (source, json.dumps(snapshot(
                    assignments=[{'user': 'x', 'variant': 'C'}, {'user': 'y', 'variant': 'C'},
                                 {'user': 'z', 'variant': 'T'}, {'user': 'w', 'variant': 'T'}],
                    outcomes=[{'user': 'x', 'converted': 1}, {'user': 'y', 'converted': 1},
                              {'user': 'z', 'converted': 1}, {'user': 'w', 'converted': 0}])).encode()):
                (Path(td) / 'in.json').write_bytes(body)
                legacy = call(Path(td) / 'in.json', Path(td) / 'legacy.json', large=False)
                self.assertEqual(legacy.returncode, 0, legacy.stderr)
                modern = call(Path(td) / 'in.json', Path(td) / 'modern.json', large=True)
                self.assertEqual(modern.returncode, 0, modern.stderr)
                old = json.loads((Path(td) / 'legacy.json').read_text())
                new = json.loads((Path(td) / 'modern.json').read_text())
                self.assertEqual(old, new)
                self.assertEqual((Path(td) / 'legacy.json').read_bytes(),
                                 (Path(td) / 'modern.json').read_bytes())
                self.assertEqual(leftovers(td), [])

    def test_field_order_and_result_before_group_table(self):
        with tempfile.TemporaryDirectory() as td:
            body = ('{"outcomes":[{"converted":1,"user":"b"},{"user":"a","converted":0}],'
                    '"expected_treatment_share":{"denominator":2,"numerator":1},'
                    '"assignments":[{"variant":"T","user":"b"},{"user":"a","variant":"C"}],'
                    '"treatment":"T","control":"C"}')
            report = self.run_ok(td, body)
            self.assertEqual(report['result']['arms']['C']['rate'], '0')
            self.assertEqual(report['result']['arms']['T']['rate'], '1')

    def test_raw_byte_sha256_covers_escapes_and_whitespace(self):
        with tempfile.TemporaryDirectory() as td:
            body = ('{ "control" : "C",\n "treatment":"T",'
                    '"expected_treatment_share":{"numerator":1,"denominator":2},'
                    '"assignments":[{"user":"caf\\u00e9-\\ud83d\\ude00","variant":"C"},'
                    '{"user":"\\u4e2d\\u6587","variant":"T"}],'
                    '"outcomes":[{"user":"caf\\u00e9-\\ud83d\\ude00","converted":1},'
                    '{"user":"\\u4e2d\\u6587","converted":0}] }').encode('utf-8')
            import hashlib
            report = self.run_ok(td, body)
            self.assertEqual(report['source_sha256'], hashlib.sha256(body).hexdigest())

    def test_invalid_snapshots_leave_no_scratch_state(self):
        base = snapshot(assignments=[{'user': 'a', 'variant': 'C'}, {'user': 'b', 'variant': 'T'}],
                        outcomes=[{'user': 'a', 'converted': 1}, {'user': 'b', 'converted': 0}])
        with tempfile.TemporaryDirectory() as td:
            self.run_bad(td, b'{"control":"C","control":"T"}', 'duplicate JSON key')
            self.run_bad(td, {**base, 'extra': 1}, 'expected object fields')
            self.run_bad(td, {**base, 'assignments': base['assignments'] + [base['assignments'][0]]},
                         'duplicate assignment')
            self.run_bad(td, {**base, 'outcomes': [{'user': 'a', 'converted': 1}]},
                         'every assigned unit needs an explicit observed 0/1 outcome')
            self.run_bad(td, {**base, 'assignments': [{'user': 'a', 'variant': 'X'},
                                                      {'user': 'b', 'variant': 'T'}]},
                         'duplicate assignment or unknown variant')
            self.run_bad(td, {**base, 'outcomes': base['outcomes'] + [{'user': 'a', 'converted': 0}]},
                         'duplicate or unassigned outcome')
            self.run_bad(td, {**base, 'outcomes': [{'user': 'missing', 'converted': 1}]},
                         'duplicate or unassigned outcome')
            self.run_bad(td, {**base, 'outcomes': [{'user': 'a', 'converted': True},
                                                   {'user': 'b', 'converted': 0}]},
                         'integer outside allowed bounds')
            raw = json.dumps(base).encode()
            self.run_bad(td, raw[:-2], 'invalid JSON')
            self.run_bad(td, raw + b'null', 'invalid JSON')
            self.run_bad(td, raw.replace(b'"converted": 1', b'"converted": NaN', 1), 'nonfinite JSON number')
            streak = {**base, 'outcomes': [{'user': 'a', 'converted': 1}] * 3000 + [{'user': 'b', 'converted': 0}]}
            self.run_bad(td, streak, 'duplicate or unassigned outcome')
            self.run_bad(td, b'\xff\xfe{}', 'utf-8')
            self.run_bad(td, {**base, 'outcomes': {}}, 'expected bounded array')
            self.run_bad(td, snapshot())  # both arms empty
            empty = snapshot()
            empty['assignments'] = [{'user': 'a', 'variant': 'C'}]
            empty['outcomes'] = [{'user': 'a', 'converted': 1}]
            self.run_bad(td, empty, 'both arms require observed assignments')

    def test_far_apart_duplicate_users_straddling_chunks(self):
        with tempfile.TemporaryDirectory() as td:
            pairs = [(f'u{i}', 'C' if i % 2 else 'T') for i in range(30000)]
            outcomes = [{'user': user, 'converted': 1 if i % 3 == 0 else 0} for i, (user, _) in enumerate(pairs)]
            body = snapshot(assignments=[{'user': u, 'variant': v} for u, v in pairs], outcomes=outcomes)
            encoded = json.dumps(body).encode('utf-8')
            self.assertGreater(len(encoded), 600_000)  # spans multiple 256 KiB reads
            self.run_ok(td, encoded)
            modulo = {**body, 'assignments': body['assignments'] + [{'user': 'u0', 'variant': 'T'}]}
            self.run_bad(td, modulo, 'duplicate assignment')
            modulo = {**body, 'outcomes': body['outcomes'] + [outcomes[0]]}
            self.run_bad(td, modulo, 'duplicate or unassigned outcome')

    def test_utf8_straddles_read_boundary(self):
        from experiment_lift import large
        with tempfile.TemporaryDirectory() as td:
            head = ('{"control":"C","treatment":"T",'
                    '"expected_treatment_share":{"numerator":1,"denominator":2},"assignments":[').encode()
            emoji = b'\xf0\x9f\x98\x80'  # four bytes crossing the first read boundary
            pad = b' ' * (large.CHUNK - 1 - len(head) - len(b'{"user":"caf'))
            rows = (b'{"user":"caf' + emoji + b'-x","variant":"C"},{"user":"b","variant":"T"}')
            outcomes = (b'{"user":"caf\\ud83d\\ude00-x","converted":1},{"user":"b","converted":0}')
            raw = head + pad + rows + b'],"outcomes":[' + outcomes + b']}'
            self.assertEqual(raw[large.CHUNK - 1:large.CHUNK + 3], emoji)
            report = self.run_ok(td, raw)
            self.assertEqual(report['result']['arms']['C']['converted'], 1)
            import hashlib
            self.assertEqual(report['source_sha256'], hashlib.sha256(raw).hexdigest())

    def test_surrogate_escaped_identifiers_are_not_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            body = ('{"control":"C","treatment":"T","expected_treatment_share":{"numerator":1,"denominator":2},'
                    '"assignments":[{"user":"\\ud800x","variant":"C"},{"user":"b","variant":"T"}],'
                    '"outcomes":[{"user":"\\ud800x","converted":1},{"user":"b","converted":0}]}')
            report = self.run_ok(td, body)
            self.assertEqual(report['result']['arms']['C']['converted'], 1)

    def test_declared_limits_are_enforced_without_scratch_leftovers(self):
        from experiment_lift import large
        body = snapshot(assignments=[{'user': 'a', 'variant': 'C'}, {'user': 'b', 'variant': 'T'}],
                        outcomes=[{'user': 'a', 'converted': 1}, {'user': 'b', 'converted': 0}])
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / 'in.json'
            src.write_text(json.dumps(body))
            out = Path(td) / 'out.json'
            original_rows, original_file = large.MAX_ROWS, large.MAX_FILE
            try:
                large.MAX_ROWS = 1
                with self.assertRaises(ValueError):
                    large.run(src, out)
                large.MAX_ROWS = original_rows
                large.MAX_FILE = src.stat().st_size - 1
                with self.assertRaisesRegex(ValueError, '1 GiB'):
                    large.run(src, out)
            finally:
                large.MAX_ROWS, large.MAX_FILE = original_rows, original_file
            self.assertFalse(out.exists())
            self.assertEqual(leftovers(td), [])

    def test_million_rows_under_128_mib(self):
        with tempfile.TemporaryDirectory() as td:
            total = 1_000_000
            step = 7919
            path = Path(td) / 'big.json'
            with path.open('w', encoding='utf-8') as stream:
                stream.write('{"outcomes":[')
                stream.write(','.join('{"user":"u%d","converted":%d}' % ((i * step) % total, 1 if (i * step) % total % 3 == 0 else 0)
                                      for i in range(total)))
                stream.write('],"treatment":"T","control":"C","assignments":[')
                stream.write(','.join('{"variant":"%s","user":"u%d"}' % ('C' if ((i * step) % total) % 2 == 0 else 'T', (i * step) % total)
                                      for i in range(total)))
                stream.write('],"expected_treatment_share":{"numerator":1,"denominator":2}}')
            out = Path(td) / 'report.json'
            wrapper = textwrap.dedent(f'''
                import resource, sys
                resource.setrlimit(resource.RLIMIT_AS, ({LIMIT}, {LIMIT}))
                sys.path.insert(0, {str(ROOT / 'src')!r})
                from experiment_lift.cli import main
                raise SystemExit(main())
            ''')
            result = subprocess.run([sys.executable, '-I', '-c', wrapper, '--source', str(path),
                                     '--output', str(out), '--large-snapshot'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(out.read_text())
            self.assertEqual(report['result']['arms']['C']['assigned'], 500_000)
            self.assertEqual(report['result']['arms']['T']['assigned'], 500_000)
            self.assertEqual(report['result']['arms']['C']['converted'],
                             report['result']['arms']['T']['converted'])
            self.assertEqual(leftovers(td), [])


if __name__ == '__main__':
    unittest.main()
