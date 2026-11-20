"""Local development accounts (demo mode) and import previews; no AWS or dotenv access.

In AWS mode users, sessions and audit events live in MySQL (services/mysql_store.py).
Both stores issue the same kind of JWT and expose the same methods.
"""
import hashlib
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from werkzeug.security import generate_password_hash, check_password_hash
from contract import dumps
from services.jwt_tokens import TokenError, TokenSigner

ROLES = ('ADMIN', 'DATA_ENTRY', 'ANALYST')


class AccessStore:
    kind = 'local'

    def __init__(self, path, signer=None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.signer = signer or TokenSigner()
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, username TEXT NOT NULL, touched REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (username TEXT PRIMARY KEY, failures INTEGER NOT NULL, started REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS imports (id TEXT PRIMARY KEY, owner TEXT NOT NULL, scope TEXT NOT NULL, created REAL NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit_events (event_id TEXT PRIMARY KEY, occurred REAL NOT NULL, actor TEXT, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_key TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT, request_id TEXT, before_data TEXT, after_data TEXT);
            CREATE TABLE IF NOT EXISTS audit_events_history (event_id TEXT PRIMARY KEY, occurred REAL NOT NULL, actor TEXT, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_key TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT, request_id TEXT, before_data TEXT, after_data TEXT, archived REAL NOT NULL);
            ''')
        self.dummy = generate_password_hash(secrets.token_urlsafe(32))

    @contextmanager
    def connect(self):
        """Commit (or roll back) and always close: Windows cannot delete an open SQLite file."""
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add_user(self, username, password, role, display_name=None, actor=None):
        if not re.fullmatch(r'[A-Za-z0-9_.@-]{3,80}', username): raise ValueError('Username must contain 3–80 letters, digits, _, ., @ or -.')
        if len(password) < 12 or len(password) > 128: raise ValueError('Password must contain 12–128 characters.')
        if role not in ROLES: raise ValueError('Invalid role.')
        with self.connect() as db:
            if db.execute('SELECT 1 FROM users WHERE username=?', (username.lower(),)).fetchone():
                raise ValueError('A user with that name already exists.')
            db.execute('INSERT INTO users(username,password_hash,role) VALUES(?,?,?)', (username.lower(), generate_password_hash(password), role))
        self.audit(actor, 'USER_CREATE', 'USER', username.lower(), 'SUCCESS', after={'username': username.lower(), 'role': role})

    def set_active(self, username, active, actor=None):
        with self.connect() as db:
            if not db.execute('UPDATE users SET active=? WHERE username=?', (1 if active else 0, username.lower())).rowcount:
                raise ValueError('User not found.')
            db.execute('DELETE FROM sessions WHERE username=?', (username.lower(),))
        self.audit(actor, 'USER_ENABLE' if active else 'USER_DISABLE', 'USER', username.lower(), 'SUCCESS')

    def disable(self, username, actor=None): self.set_active(username, False, actor)
    def enable(self, username, actor=None): self.set_active(username, True, actor)

    def set_password(self, username, password, actor=None):
        if len(password) < 12 or len(password) > 128: raise ValueError('Password must contain 12–128 characters.')
        with self.connect() as db:
            if not db.execute('UPDATE users SET password_hash=? WHERE username=?', (generate_password_hash(password), username.lower())).rowcount:
                raise ValueError('User not found.')
            db.execute('DELETE FROM sessions WHERE username=?', (username.lower(),))
        self.audit(actor, 'PASSWORD_RESET', 'USER', username.lower(), 'SUCCESS')

    def save_admin(self, username, display_name, password, actor=None, request_id=None):
        """Demo twin of the MySQL procedure sp_guardar_admin: create an admin or change its password."""
        username = (username or '').strip().lower()
        with self.connect() as db:
            row = db.execute('SELECT role FROM users WHERE username=?', (username,)).fetchone()
        if not row:
            self.add_user(username, password, 'ADMIN', actor=actor)
            return 'CREADO'
        if row['role'] != 'ADMIN': raise ValueError('That account is not an administrator.')
        self.set_password(username, password, actor)
        return 'CONTRASENA'

    def change_own_password(self, user, current, new):
        with self.connect() as db:
            row = db.execute('SELECT password_hash FROM users WHERE username=?', (user['username'],)).fetchone()
        if not row or not check_password_hash(row['password_hash'], (current or '')[:129]):
            raise ValueError('Your current password is not correct.')
        return self.save_admin(user['username'], None, new, user)

    def rotate_audit(self, days, actor=None, request_id=None):
        """Demo twin of sp_rotar_auditoria."""
        days = int(days)
        if not 0 <= days <= 3650: raise ValueError('Days must be between 0 and 3650.')
        now = time.time(); cutoff = now - days * 86400
        with self.connect() as db:
            moved = db.execute('INSERT INTO audit_events_history SELECT *, ? FROM audit_events WHERE occurred < ?', (now, cutoff)).rowcount
            db.execute('DELETE FROM audit_events WHERE occurred < ? AND event_id IN (SELECT event_id FROM audit_events_history)', (cutoff,))
            sessions = db.execute('DELETE FROM sessions WHERE touched < ?', (now - 1800,)).rowcount
        self.audit(actor, 'AUDIT_ROTATE', 'AUDIT', f'dias={days}', 'SUCCESS', after={'movidos_al_historico': moved, 'sesiones_eliminadas': sessions})
        return moved, sessions

    def history_count(self):
        with self.connect() as db:
            return db.execute('SELECT COUNT(*) FROM audit_events_history').fetchone()[0]

    def login(self, username, password):
        username = username.strip().lower()[:80]
        now = time.time()
        with self.connect() as db:
            attempt = db.execute('SELECT * FROM attempts WHERE username=?', (username,)).fetchone()
            if attempt and now-attempt['started'] < 900 and attempt['failures'] >= 5:
                outcome = 'BLOCKED'
            else:
                user = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
                valid = check_password_hash(user['password_hash'] if user else self.dummy, password[:129])
                if user and valid and user['active'] and user['role'] == 'ADMIN':
                    db.execute('DELETE FROM attempts WHERE username=?', (username,))
                    sid = secrets.token_urlsafe(32)
                    db.execute('INSERT INTO sessions VALUES(?,?,?)', (self.digest(sid), username, now))
                    outcome = 'SUCCESS'
                else:
                    if not attempt or now-attempt['started'] >= 900:
                        db.execute('INSERT OR REPLACE INTO attempts VALUES(?,1,?)', (username, now))
                    else: db.execute('UPDATE attempts SET failures=failures+1 WHERE username=?', (username,))
                    outcome = 'FAILURE'
        self.audit({'user_id': username} if outcome == 'SUCCESS' else None, 'LOGIN', 'USER', username, outcome)
        if outcome != 'SUCCESS': return None
        return self.signer.issue({'sub': username, 'sid': sid, 'ver': 1, 'role': user['role'], 'email': username})

    @staticmethod
    def digest(sid): return hashlib.sha256(sid.encode()).hexdigest()

    def session_id(self, token, check_expiry=True):
        try: return self.signer.verify(token, check_expiry)['sid']
        except (TokenError, KeyError, TypeError): return ''

    def user(self, token):
        sid = self.session_id(token)
        if not sid: return None
        now = time.time()
        with self.connect() as db:
            row = db.execute('SELECT users.username,role,active,touched FROM sessions JOIN users USING(username) WHERE token=?', (self.digest(sid),)).fetchone()
            if not row or not row['active'] or row['role'] != 'ADMIN' or now-row['touched'] >= 1800: return None
            db.execute('UPDATE sessions SET touched=? WHERE token=?', (now, self.digest(sid)))
            return dict(row, user_id=row['username'], display_name=row['username'], session_id=self.digest(sid))

    def logout(self, token):
        sid = self.session_id(token, check_expiry=False)
        if not sid: return
        with self.connect() as db:
            row = db.execute('SELECT username FROM sessions WHERE token=?', (self.digest(sid),)).fetchone()
            db.execute('DELETE FROM sessions WHERE token=?', (self.digest(sid),))
        if row: self.audit({'user_id': row['username']}, 'LOGOUT', 'USER', row['username'], 'SUCCESS')

    def list_users(self):
        with self.connect() as db:
            return [dict(username=r['username'], display_name=r['username'], role=r['role'], active=bool(r['active']), created_at_utc='')
                    for r in db.execute("SELECT * FROM users WHERE role='ADMIN' ORDER BY username")]

    def count_admins(self):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM users WHERE role='ADMIN' AND active=1").fetchone()[0]

    def audit(self, actor, action, entity_type, entity_key, outcome, before=None, after=None, reason=None, request_id=None):
        with self.connect() as db:
            db.execute('INSERT INTO audit_events VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (str(uuid.uuid4()), time.time(), actor['user_id'] if actor else None, action, entity_type, str(entity_key),
                        outcome, reason, request_id, None if before is None else dumps(before), None if after is None else dumps(after)))

    def audit_log(self, limit=100):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM audit_events ORDER BY occurred DESC LIMIT ?', (int(limit),)).fetchall()
        return [dict(occurred_at_utc=time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(r['occurred'])), actor=r['actor'] or '—',
                     action=r['action'], entity_type=r['entity_type'], entity_key=r['entity_key'], outcome=r['outcome'],
                     reason=r['reason'], request_id=r['request_id']) for r in rows]

    def catalog(self, fresh=False):
        return None   # demo mode: free-text IDs; the catalog lives in MySQL
