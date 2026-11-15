"""SQLite group ledger with sticky, exact-quota randomisation.

The ledger keeps the arms, treatment share ``p`` and seed fixed at ``init``.
Every committed batch adds users to the ledger; treatment slots are handed to
the new users only, so already committed assignments never move.  After each
commit the treatment arm holds exactly ``floor(N * p)`` of the ``N`` users in
the ledger.  Within a batch, slots go to the users with the smallest

    SHA256(b"experiment-lift/assign/v1\0" + seed + b"\0" + user)

keys (ties broken by the UTF-8 encoded user), which makes the allocation a pure
function of the seed and the user identifier.

Writers take SQLite's write lock before reading counters, so quota arithmetic
and identity checks are serialised.  A batch is one transaction: any conflict
rolls the whole batch back.  Export is a separate, read-only snapshot and is
written through a temporary file followed by an atomic rename.
"""
from pathlib import Path
import hashlib, json, os, sqlite3, tempfile

from .common import ident, number, require

SCHEMA_VERSION = 1
MAX_BATCH = 20_000
MAX_SOURCE = 32 * 1024 * 1024
EXPORT_BATCH = 4096
BUSY_TIMEOUT_MS = 10_000
DOMAIN = b'experiment-lift/assign/v1\0'
_FIELDS = 'batch_id users'


def _blob(value):
    """Identifiers are stored as exact UTF-8 bytes (surrogates included)."""
    return value.encode('utf-8', 'surrogatepass')


def _text(value):
    return value.decode('utf-8', 'surrogatepass')


def assignment_key(seed, user):
    """Return the deterministic treatment-ordering key for ``seed``/``user``.

    Both parts are identifiers, so neither can contain NUL (control characters
    are rejected by ``common.ident``).  The fixed domain prefix and the NUL
    separators therefore make the encoding unambiguous.
    """
    if not isinstance(seed, str):
        raise ValueError('expected nonblank identifier, <=128 characters')
    return hashlib.sha256(DOMAIN + _blob(ident(seed)) + b'\0' + _blob(ident(user))).digest()


def _key_for(seed_blob, user_blob):
    return hashlib.sha256(DOMAIN + seed_blob + b'\0' + user_blob).digest()


def _connect(path):
    ledger = Path(path)
    if not ledger.is_file():
        raise ValueError('ledger does not exist')
    try:
        conn = sqlite3.connect(str(ledger), isolation_level=None)
        conn.execute(f'PRAGMA busy_timeout={BUSY_TIMEOUT_MS}')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA temp_store=FILE')
        conn.execute('PRAGMA cache_size=-4096')
        conn.execute('PRAGMA mmap_size=0')
        return conn
    except sqlite3.Error as error:
        raise ValueError('cannot open ledger: ' + str(error)) from error


def _meta(conn):
    try:
        row = conn.execute('SELECT schema_version,control,treatment,numerator,denominator,seed '
                           'FROM meta WHERE id=1').fetchone()
    except sqlite3.OperationalError as error:
        if 'locked' in str(error) or 'busy' in str(error):
            raise ValueError('ledger is locked: ' + str(error)) from error
        raise ValueError('not an experiment ledger: ' + str(error)) from error
    except sqlite3.Error as error:
        raise ValueError('not an experiment ledger: ' + str(error)) from error
    if row is None or row[0] != SCHEMA_VERSION:
        raise ValueError('not a supported experiment ledger')
    return {'control': _text(row[1]), 'treatment': _text(row[2]),
            'numerator': int(row[3]), 'denominator': int(row[4]), 'seed': _text(row[5])}


def _publish_new(temp, ledger):
    """Move a finished temporary database to a fresh path without clobbering."""
    try:
        os.link(temp, ledger)
        return
    except FileExistsError as error:
        raise ValueError('ledger already exists') from error
    except OSError:
        pass
    # Filesystems without hard links: fall back to an exclusive create+copy.
    try:
        with open(temp, 'rb') as source, open(ledger, 'xb') as dest:
            try:
                while True:
                    block = source.read(1 << 20)
                    if not block:
                        break
                    dest.write(block)
            except BaseException:
                dest.close()
                try:
                    os.unlink(ledger)
                except OSError:
                    pass
                raise
    except FileExistsError as error:
        raise ValueError('ledger already exists') from error


def init_ledger(path, control, treatment, numerator, denominator, seed):
    """Create a new ledger and freeze arms, share and seed.

    The database is built under a private name in the same directory and only
    then hard-linked to ``path``, so an existing ledger is never overwritten and
    a failed init leaves no usable ledger behind.
    """
    control = ident(control)
    treatment = ident(treatment)
    seed = ident(seed)
    if control == treatment:
        raise ValueError('control and treatment must differ')
    numerator = number(numerator, 1)
    denominator = number(denominator, 2)
    if numerator >= denominator:
        raise ValueError('expected share must be strictly between zero and one')
    ledger = Path(path)
    if ledger.exists() or ledger.is_symlink():
        raise ValueError('ledger already exists')
    ledger.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix='.experiment-ledger-init-', suffix='.db',
                                    dir=ledger.parent)
    os.close(handle)
    conn = None
    try:
        conn = sqlite3.connect(temp, isolation_level=None)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('CREATE TABLE meta(id INTEGER PRIMARY KEY CHECK(id=1), '
                     'schema_version INTEGER NOT NULL, control BLOB NOT NULL, treatment BLOB NOT NULL, '
                     'numerator INTEGER NOT NULL, denominator INTEGER NOT NULL, seed BLOB NOT NULL)')
        conn.execute('CREATE TABLE batches(batch_id BLOB PRIMARY KEY, user_count INTEGER NOT NULL '
                     'CHECK(user_count>0), treatment_count INTEGER NOT NULL '
                     'CHECK(treatment_count>=0 AND treatment_count<=user_count)) WITHOUT ROWID')
        conn.execute('CREATE TABLE users(user BLOB PRIMARY KEY, batch_id BLOB NOT NULL '
                     'REFERENCES batches(batch_id), variant BLOB NOT NULL) WITHOUT ROWID')
        conn.execute('CREATE INDEX users_batch ON users(batch_id)')
        conn.execute('INSERT INTO meta(id,schema_version,control,treatment,numerator,denominator,seed) '
                     'VALUES(1,?,?,?,?,?,?)',
                     (SCHEMA_VERSION, _blob(control), _blob(treatment), numerator, denominator, _blob(seed)))
        conn.execute('COMMIT')
        # Fold the WAL into the main file: the ledger is published by hard link,
        # so no side file may still hold committed pages.
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        if conn.execute('SELECT schema_version FROM meta WHERE id=1').fetchone() != (SCHEMA_VERSION,):
            raise ValueError('ledger initialization did not commit')
        conn.close()
        conn = None
        _publish_new(temp, ledger)
    except sqlite3.Error as error:
        raise ValueError('cannot initialize ledger: ' + str(error)) from error
    finally:
        if conn is not None:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            conn.close()
        for candidate in (temp, temp + '-wal', temp + '-shm'):
            try:
                os.unlink(candidate)
            except OSError:
                pass
    return {'schema_version': SCHEMA_VERSION, 'control': control, 'treatment': treatment,
            'expected_treatment_share': {'numerator': numerator, 'denominator': denominator},
            'seed': seed}


def read_batch(path):
    """Read the strict ``{"batch_id": ..., "users": [...]}`` assign input."""
    source = Path(path)
    with source.open('rb') as stream:
        raw = stream.read(MAX_SOURCE + 1)
    if len(raw) > MAX_SOURCE:
        raise ValueError('batch JSON exceeds 32 MB')

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result

    def invalid(value):
        raise ValueError('nonfinite JSON number: ' + value)

    try:
        data = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)
    except UnicodeDecodeError as error:
        raise ValueError('batch JSON is not valid UTF-8') from error
    require(data, _FIELDS)
    batch_id = ident(data['batch_id'])
    users = data['users']
    if not isinstance(users, list) or not 1 <= len(users) <= MAX_BATCH:
        raise ValueError(f'users must be an array of 1 to {MAX_BATCH} identifiers')
    return batch_id, users


def assign_batch(path, batch_id, users):
    """Commit one batch and return a summary; retries are idempotent.

    The caller is responsible for exporting afterwards.  If the same batch id
    is replayed with the same user set, no row is written; a different set is
    rejected.  Any user conflict or quota violation rolls the entire batch back.
    """
    batch_id = ident(batch_id)
    if not isinstance(users, (list, tuple)):
        raise ValueError('expected an array of users')
    if not 1 <= len(users) <= MAX_BATCH:
        raise ValueError(f'users must be an array of 1 to {MAX_BATCH} identifiers')
    user_blobs = [_blob(ident(user)) for user in users]
    if len(set(user_blobs)) != len(user_blobs):
        raise ValueError('duplicate user in batch')
    batch_blob = _blob(batch_id)
    users_sorted = sorted(user_blobs)

    conn = _connect(path)
    try:
        meta = _meta(conn)
        control = _blob(meta['control'])
        treatment = _blob(meta['treatment'])
        try:
            conn.execute('BEGIN IMMEDIATE')
        except sqlite3.Error as error:
            raise ValueError('ledger is locked: ' + str(error)) from error
        try:
            row = conn.execute('SELECT user_count,treatment_count FROM batches WHERE batch_id=?',
                               (batch_blob,)).fetchone()
            if row is not None:
                existing = [value for (value,) in
                            conn.execute('SELECT user FROM users WHERE batch_id=? ORDER BY user',
                                         (batch_blob,))]
                if int(row[0]) != len(users_sorted) or existing != users_sorted:
                    raise ValueError('batch id already used with a different user set')
                total, treated = conn.execute(
                    'SELECT COALESCE(SUM(user_count),0),COALESCE(SUM(treatment_count),0) '
                    'FROM batches').fetchone()
                conn.execute('COMMIT')
                return {'batch_id': batch_id, 'assigned': 0, 'idempotent': True,
                        'batch_users': len(users_sorted), 'batch_treated': int(row[1]),
                        'users': int(total), 'treated': int(treated),
                        'control': meta['control'], 'treatment': meta['treatment']}
            total, treated = conn.execute(
                'SELECT COALESCE(SUM(user_count),0),COALESCE(SUM(treatment_count),0) FROM batches').fetchone()
            total, treated = int(total), int(treated)
            target = (total + len(users_sorted)) * meta['numerator'] // meta['denominator']
            slots = target - treated
            if not 0 <= slots <= len(users_sorted):
                raise ValueError('ledger quota invariant is violated')
            seed_blob = _blob(meta['seed'])
            ranked = sorted(users_sorted, key=lambda user: (_key_for(seed_blob, user), user))
            chosen = set(ranked[:slots])
            try:
                conn.execute('INSERT INTO batches(batch_id,user_count,treatment_count) VALUES(?,?,?)',
                             (batch_blob, len(users_sorted), slots))
                conn.executemany('INSERT INTO users(user,batch_id,variant) VALUES(?,?,?)',
                                 [(user, batch_blob, treatment if user in chosen else control)
                                  for user in user_blobs])
            except sqlite3.IntegrityError as error:
                if 'users.user' in str(error):
                    raise ValueError('user already assigned in an earlier batch') from error
                raise ValueError('ledger write failed: ' + str(error)) from error
            checked_total, checked_treated = conn.execute(
                'SELECT COALESCE(SUM(user_count),0),COALESCE(SUM(treatment_count),0) FROM batches').fetchone()
            if int(checked_total) != total + len(users_sorted) or int(checked_treated) != target:
                raise ValueError('ledger quota invariant is violated')
            conn.execute('COMMIT')
            return {'batch_id': batch_id, 'assigned': len(users_sorted), 'idempotent': False,
                    'batch_users': len(users_sorted), 'batch_treated': slots,
                    'users': total + len(users_sorted), 'treated': target,
                    'control': meta['control'], 'treatment': meta['treatment']}
        except ValueError:
            raise
        except sqlite3.Error as error:
            raise ValueError('ledger write failed: ' + str(error)) from error
        except BaseException:
            raise
        finally:
            if conn.in_transaction:
                try:
                    conn.execute('ROLLBACK')
                except sqlite3.Error:
                    pass
    finally:
        conn.close()


def _write_export(conn, dest, meta):
    dest = Path(dest)
    if dest.is_symlink():
        raise ValueError('output cannot be a symlink')
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    counted = {meta['control']: 0, meta['treatment']: 0}
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='ascii', dir=dest.parent, delete=False,
                                         prefix='.experiment-ledger-', suffix='.tmp') as stream:
            temp = stream.name
            stream.write('{"assignments":[')
            first = True
            chunk = []
            for user, variant in conn.execute('SELECT user,variant FROM users ORDER BY user'):
                user_text = _text(user)
                variant_text = _text(variant)
                chunk.append(('' if first else ',') + '{"user":' +
                             json.dumps(user_text, ensure_ascii=True) + ',"variant":' +
                             json.dumps(variant_text, ensure_ascii=True) + '}')
                first = False
                counted[variant_text] += 1
                if len(chunk) >= EXPORT_BATCH:
                    stream.write(''.join(chunk))
                    chunk.clear()
            stream.write(''.join(chunk))
            stream.write('],"control":')
            stream.write(json.dumps(meta['control'], ensure_ascii=True, allow_nan=False))
            stream.write(',"expected_treatment_share":{"denominator":')
            stream.write(str(meta['denominator']))
            stream.write(',"numerator":')
            stream.write(str(meta['numerator']))
            stream.write('},"treatment":')
            stream.write(json.dumps(meta['treatment'], ensure_ascii=True, allow_nan=False))
            stream.write('}\n')
        os.replace(temp, dest)
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)
    return {'assignments': sum(counted.values()),
            'control_assigned': counted[meta['control']],
            'treatment_assigned': counted[meta['treatment']]}


def export_ledger(path, output):
    """Stream a consistent ledger snapshot as analysis-ready assignments.

    The result has exactly the v1 input fields other than ``outcomes``, so an
    outcomes array can be appended and passed to the original analyzer.  Rows
    are emitted in UTF-8 byte order of the user identifier.  No user collection
    is held in memory and JSON is written incrementally.
    """
    if Path(output).resolve() == Path(path).resolve():
        raise ValueError('input and output must differ')
    conn = _connect(path)
    try:
        try:
            conn.execute('BEGIN')
        except sqlite3.Error as error:
            raise ValueError('cannot read ledger: ' + str(error)) from error
        try:
            try:
                meta = _meta(conn)
                summary = _write_export(conn, output, meta)
            except OSError:
                raise
            except sqlite3.Error as error:
                raise ValueError('ledger read failed: ' + str(error)) from error
            conn.execute('COMMIT')
            return summary
        finally:
            if conn.in_transaction:
                try:
                    conn.execute('ROLLBACK')
                except sqlite3.Error:
                    pass
    finally:
        conn.close()
