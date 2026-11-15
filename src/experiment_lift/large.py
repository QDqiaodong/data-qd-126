"""Streaming audit mode for large snapshots.

The snapshot is parsed incrementally from bounded byte chunks: the reader yields
one JSON value at a time, so the file is never loaded whole and no user set is
held in memory. Assignment/outcome rows go into a private SQLite scratch
database next to the report; only arm counters and the running SHA256 of the
raw source bytes stay in memory. Any validation error raises ValueError before
a report is produced, and the caller removes the scratch database on every
exit path.

User identifiers and variants are stored as UTF-8 BLOBs (surrogate-escaped input
stays representable), so duplicate detection and the assignment/outcome join are
exact across record orders and accept precisely the identifiers the v1 rules do.
"""
import codecs, hashlib, json, os, shutil, sqlite3, tempfile
from fractions import Fraction
from pathlib import Path

from .common import ident, number, require
from .model import summarize

CHUNK = 256 * 1024
MAX_FILE = 1 << 30
MAX_ROWS = 1_000_000
MAX_DB = 2 << 30
PENDING_LIMIT = 8 * 1024 * 1024
BATCH = 2000
COMMIT_ROWS = 50_000
_WS = ' \t\r\n'
_FIELDS = frozenset(('control', 'treatment', 'expected_treatment_share', 'assignments', 'outcomes'))
_ARRAYS = frozenset(('assignments', 'outcomes'))
_FIELD_ERROR = 'expected object fields: control treatment expected_treatment_share assignments outcomes'

_START, _KEYSTART, _KEY, _COLON, _VALUE, _TOPSEP, _ARRSTART, _ELEM, _ARRSEP, _DONE = range(10)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def _invalid(value):
    raise ValueError('nonfinite JSON number: ' + value)


def _scalar(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0)


class _Audit:
    """Scratch-store identity checks for rows arriving in any order."""

    def __init__(self, directory):
        self.path = os.path.join(directory, 'audit.sqlite3')
        self.conn = None
        self.batch_assignments = []
        self.batch_outcomes = []
        self.since_commit = 0
        try:
            self.conn = sqlite3.connect(self.path, isolation_level=None)
            self.conn.execute('PRAGMA journal_mode=OFF')
            self.conn.execute('PRAGMA synchronous=OFF')
            self.conn.execute('PRAGMA temp_store=FILE')
            self.conn.execute('PRAGMA cache_size=-2048')
            self.conn.execute('CREATE TABLE assigned(user BLOB PRIMARY KEY, variant BLOB NOT NULL) WITHOUT ROWID')
            self.conn.execute('CREATE TABLE observed(user BLOB PRIMARY KEY, converted INTEGER NOT NULL) WITHOUT ROWID')
            self.conn.execute('BEGIN')
        except sqlite3.Error as error:
            raise ValueError('large snapshot scratch database: ' + str(error)) from error

    def assignment(self, row):
        require(row, 'user variant')
        user = ident(row['user'])
        self.batch_assignments.append((user.encode('utf-8', 'surrogatepass'),
                                       ident(row['variant']).encode('utf-8', 'surrogatepass')))
        if len(self.batch_assignments) >= BATCH:
            self._flush_assignments()

    def outcome(self, row):
        require(row, 'user converted')
        user = ident(row['user'])
        self.batch_outcomes.append((user.encode('utf-8', 'surrogatepass'), number(row['converted'], 0, 1)))
        if len(self.batch_outcomes) >= BATCH:
            self._flush_outcomes()

    def _run(self, sql, rows, message):
        try:
            self.conn.executemany(sql, rows)
        except sqlite3.IntegrityError as error:
            raise ValueError(message) from error
        except sqlite3.Error as error:
            raise ValueError('large snapshot scratch database: ' + str(error)) from error
        self.since_commit += len(rows)
        if self.since_commit >= COMMIT_ROWS:
            self._commit()

    def _flush_assignments(self):
        if self.batch_assignments:
            rows, self.batch_assignments = self.batch_assignments, []
            self._run('INSERT INTO assigned(user,variant) VALUES(?,?)', rows, 'duplicate assignment or unknown variant')

    def _flush_outcomes(self):
        if self.batch_outcomes:
            rows, self.batch_outcomes = self.batch_outcomes, []
            self._run('INSERT INTO observed(user,converted) VALUES(?,?)', rows, 'duplicate or unassigned outcome')

    def _commit(self):
        try:
            self.conn.execute('COMMIT')
            self.conn.execute('BEGIN')
        except sqlite3.Error as error:
            raise ValueError('large snapshot scratch database: ' + str(error)) from error
        self.since_commit = 0
        if os.stat(self.path).st_size > MAX_DB:
            raise ValueError('large snapshot scratch database exceeds 2 GiB')

    def _close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def finish(self, control, treatment):
        self._flush_assignments()
        self._flush_outcomes()
        try:
            self._commit()
            conn = self.conn
            if conn.execute('SELECT 1 FROM assigned WHERE variant<>? AND variant<>? LIMIT 1',
                            (control.encode('utf-8', 'surrogatepass'),
                             treatment.encode('utf-8', 'surrogatepass'))).fetchone():
                raise ValueError('duplicate assignment or unknown variant')
            if conn.execute('SELECT 1 FROM observed o LEFT JOIN assigned a ON a.user=o.user '
                            'WHERE a.user IS NULL LIMIT 1').fetchone():
                raise ValueError('duplicate or unassigned outcome')
            if conn.execute('SELECT 1 FROM assigned a LEFT JOIN observed o ON o.user=a.user '
                            'WHERE o.user IS NULL LIMIT 1').fetchone():
                raise ValueError('every assigned unit needs an explicit observed 0/1 outcome')
            c = control.encode('utf-8', 'surrogatepass')
            t = treatment.encode('utf-8', 'surrogatepass')
            assigned_control = _scalar(conn, 'SELECT COUNT(*) FROM assigned WHERE variant=?', (c,))
            assigned_treatment = _scalar(conn, 'SELECT COUNT(*) FROM assigned WHERE variant=?', (t,))
            converted_control = _scalar(conn, 'SELECT COUNT(*) FROM assigned a JOIN observed o ON o.user=a.user '
                                               'WHERE a.variant=? AND o.converted=1', (c,))
            converted_treatment = _scalar(conn, 'SELECT COUNT(*) FROM assigned a JOIN observed o ON o.user=a.user '
                                                 'WHERE a.variant=? AND o.converted=1', (t,))
        except ValueError:
            raise
        except sqlite3.Error as error:
            raise ValueError('large snapshot scratch database: ' + str(error)) from error
        return {control: [assigned_control, converted_control],
                treatment: [assigned_treatment, converted_treatment]}


def _scan(path, emit):
    """Walk the raw bytes once, emitting (field, value) for every top-level value.

    Array fields are emitted one element at a time; header fields are emitted as
    whole (small) values. Returns the SHA256 of the complete source bytes.
    """
    utf8 = codecs.getincrementaldecoder('utf-8')('strict')
    decoder = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_invalid)
    digest = hashlib.sha256()
    total = 0
    state = _START
    key = array_key = None
    seen = set()
    rows = {'assignments': 0, 'outcomes': 0}
    buffer = ''
    pos = pending = 0
    with Path(path).open('rb') as stream:
        while True:
            data = stream.read(CHUNK)
            final = not data
            if data:
                total += len(data)
                if total > MAX_FILE:
                    raise ValueError('JSON exceeds 1 GiB')
                digest.update(data)
            buffer += utf8.decode(data, final=final)
            while True:
                while pos < len(buffer) and buffer[pos] in _WS:
                    pos += 1
                if pos == len(buffer):
                    pending = pos
                    break
                pending = pos
                char = buffer[pos]
                if state == _DONE:
                    raise ValueError('invalid JSON: extra data after top-level object')
                if state == _START:
                    if char != '{':
                        raise ValueError(_FIELD_ERROR)
                    pos += 1
                    state = _KEYSTART
                    continue
                if state in (_KEYSTART, _KEY):
                    if char == '}':
                        if state == _KEY:
                            raise ValueError('invalid JSON: trailing comma in object')
                        pos += 1
                        state = _DONE
                        continue
                    if char != '"':
                        raise ValueError('invalid JSON: expected object key')
                    try:
                        key, end = decoder.raw_decode(buffer, pos)
                    except json.JSONDecodeError:
                        if len(buffer) - pending > PENDING_LIMIT:
                            raise ValueError('large snapshot field exceeds expected row size')
                        break
                    if not isinstance(key, str):
                        raise ValueError('invalid JSON: expected object key')
                    if key not in _FIELDS:
                        raise ValueError(_FIELD_ERROR)
                    if key in seen:
                        raise ValueError('duplicate JSON key: ' + key)
                    seen.add(key)
                    pos = end
                    state = _COLON
                    continue
                if state == _COLON:
                    if char != ':':
                        raise ValueError('invalid JSON: expected colon')
                    pos += 1
                    state = _VALUE
                    continue
                if state == _VALUE:
                    if key in _ARRAYS and char != '[':
                        raise ValueError('expected bounded array')
                    if char == '[':
                        if key not in _ARRAYS:
                            if key in ('control', 'treatment'):
                                raise ValueError('expected nonblank identifier, <=128 characters')
                            raise ValueError('expected object fields: numerator denominator')
                        array_key = key
                        pos += 1
                        state = _ARRSTART
                        continue
                    try:
                        value, end = decoder.raw_decode(buffer, pos)
                    except json.JSONDecodeError:
                        if len(buffer) - pending > PENDING_LIMIT:
                            raise ValueError('large snapshot field exceeds expected row size')
                        break
                    pos = end
                    state = _TOPSEP
                    emit(key, value)
                    continue
                if state in (_ARRSTART, _ELEM):
                    if char == ']':
                        if state == _ELEM:
                            raise ValueError('invalid JSON: trailing comma in array')
                        pos += 1
                        state = _TOPSEP
                        continue
                    try:
                        value, end = decoder.raw_decode(buffer, pos)
                    except json.JSONDecodeError:
                        if len(buffer) - pending > PENDING_LIMIT:
                            raise ValueError('large snapshot field exceeds expected row size')
                        break
                    rows[array_key] += 1
                    if rows[array_key] > MAX_ROWS:
                        raise ValueError(f'large snapshot {array_key} exceeds {MAX_ROWS} rows')
                    pos = end
                    state = _ARRSEP
                    emit(array_key, value)
                    continue
                if state == _TOPSEP:
                    if char == ',':
                        pos += 1
                        state = _KEY
                        continue
                    if char == '}':
                        pos += 1
                        state = _DONE
                        continue
                    raise ValueError('invalid JSON: expected comma or object end')
                if state == _ARRSEP:
                    if char == ',':
                        pos += 1
                        state = _ELEM
                        continue
                    if char == ']':
                        pos += 1
                        state = _TOPSEP
                        continue
                    raise ValueError('invalid JSON: expected comma or array end')
            if not data:
                break
            buffer = buffer[pending:]
            pos = pending = 0
    if state != _DONE or pending < len(buffer):
        raise ValueError('invalid JSON: truncated input')
    return digest.hexdigest(), seen


def _read(source, audit):
    records = {}

    def emit(field, value):
        if field == 'assignments':
            audit.assignment(value)
        elif field == 'outcomes':
            audit.outcome(value)
        elif field in ('control', 'treatment'):
            records[field] = ident(value)
        else:
            require(value, 'numerator denominator')
            numerator = number(value['numerator'], 1)
            denominator = number(value['denominator'], 2)
            if numerator >= denominator:
                raise ValueError('expected share must be strictly between zero and one')
            records['expected'] = Fraction(numerator, denominator)

    digest, seen = _scan(source, emit)
    if seen != _FIELDS:
        raise ValueError(_FIELD_ERROR)
    control, treatment = records['control'], records['treatment']
    if control == treatment:
        raise ValueError('control and treatment must differ')
    return digest, control, treatment, records['expected']


def run(source, output):
    """Audit a large snapshot, returning the same report structure as the v1 mode.

    The scratch database lives beside the report so it stays on the same
    filesystem. It is removed before this function returns, on success or error.
    """
    dest = Path(output)
    created_parent = not dest.parent.exists()
    dest.parent.mkdir(parents=True, exist_ok=True)
    scratch = None
    audit = None
    try:
        scratch = tempfile.mkdtemp(prefix='.experiment-lift-', dir=dest.parent)
        audit = _Audit(scratch)
        digest, control, treatment, expected = _read(source, audit)
        counts = audit.finish(control, treatment)
        return {'schema_version': 1, 'source_sha256': digest,
                'result': summarize(control, treatment, expected, counts)}
    finally:
        if audit is not None:
            try:
                audit._close()
            except sqlite3.Error:
                pass
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
        if created_parent and not dest.exists():
            try:
                dest.parent.rmdir()  # leave no empty directory behind on failure
            except OSError:
                pass
