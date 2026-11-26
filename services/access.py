"""Local development accounts (demo mode) and import previews; no AWS or dotenv access.

In AWS mode users, sessions and audit events live in MySQL (services/mysql_store.py).
Both stores issue the same kind of JWT and expose the same methods, with the same rules:
  - only ADMIN and SUPERADMIN can sign in (services/roles.py);
  - only a SUPERADMIN creates accounts, changes other passwords or enables/disables ADMIN accounts;
  - nobody can disable, demote or change the password of a SUPERADMIN from the panel;
  - the system never ends up without an active SUPERADMIN;
  - roles only change from the command line (manage_users.py), never from the web.
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
from services import roles
from services.jwt_tokens import TokenError, TokenSigner

ROLES = roles.STAFF + roles.HEREDADOS     # valid in the table; only roles.STAFF can sign in
CONTROL = re.compile(r'[\x00-\x1f\x7f]')


def clean_display_name(value):
    name = (value or '').strip()
    if not 1 <= len(name) <= 120 or CONTROL.search(name):
        raise ValueError('The display name must contain 1 to 120 characters.')
    return name


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
            # Safe migration of older local databases: add columns, never drop data or touch hashes.
            columns = {row['name'] for row in db.execute('PRAGMA table_info(users)')}
            if 'display_name' not in columns: db.execute('ALTER TABLE users ADD COLUMN display_name TEXT')
            if 'created' not in columns: db.execute('ALTER TABLE users ADD COLUMN created REAL')
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

    # ------------------------------------------------------------------ rules shared with MySQL
    @staticmethod
    def other_active_supers(db, username):
        return db.execute("SELECT COUNT(*) FROM users WHERE role=? AND active=1 AND username<>?",
                          (roles.SUPERADMIN, username)).fetchone()[0]

    @staticmethod
    def check_manager(actor, target_username):
        """From the web (actor present) only a SUPERADMIN manages accounts other than its own."""
        if actor is not None and actor.get('username', actor.get('user_id')) != target_username \
                and not roles.es_super(actor):
            raise ValueError('Only the super administrator can manage administrator accounts.')

    # ------------------------------------------------------------------ accounts
    def add_user(self, username, password, role, display_name=None, actor=None):
        if not re.fullmatch(r'[A-Za-z0-9_.@-]{3,80}', username): raise ValueError('Username must contain 3–80 letters, digits, _, ., @ or -.')
        if len(password) < 12 or len(password) > 128: raise ValueError('Password must contain 12–128 characters.')
        if role not in ROLES: raise ValueError('Invalid role.')
        if actor is not None and role != roles.ADMIN:
            raise ValueError('Accounts created in the panel are always administrators.')
        self.check_manager(actor, None)
        name = clean_display_name(display_name) if display_name else username.lower()
        with self.connect() as db:
            if db.execute('SELECT 1 FROM users WHERE username=?', (username.lower(),)).fetchone():
                raise ValueError('A user with that name already exists.')
            db.execute('INSERT INTO users(username,password_hash,role,display_name,created) VALUES(?,?,?,?,?)',
                       (username.lower(), generate_password_hash(password), role, name, time.time()))
        self.audit(actor, 'USER_CREATE', 'USER', username.lower(), 'SUCCESS', after={'username': username.lower(), 'role': role})

    def set_active(self, username, active, actor=None):
        username = username.lower()
        self.check_manager(actor, None)
        with self.connect() as db:
            row = db.execute('SELECT role, active FROM users WHERE username=?', (username,)).fetchone()
            if not row: raise ValueError('User not found.')
            if actor is not None and actor.get('username') == username:
                raise ValueError('You cannot change your own account status.')
            if row['role'] == roles.SUPERADMIN:
                if actor is not None:
                    raise ValueError('A super administrator account cannot be disabled or enabled from the panel.')
                if not active and row['active'] and not self.other_active_supers(db, username):
                    raise ValueError('The system must keep at least one active super administrator.')
            db.execute('UPDATE users SET active=? WHERE username=?', (1 if active else 0, username))
            db.execute('DELETE FROM sessions WHERE username=?', (username,))
        self.audit(actor, 'USER_ENABLE' if active else 'USER_DISABLE', 'USER', username, 'SUCCESS')

    def disable(self, username, actor=None): self.set_active(username, False, actor)
    def enable(self, username, actor=None): self.set_active(username, True, actor)

    def set_password(self, username, password, actor=None):
        if len(password) < 12 or len(password) > 128: raise ValueError('Password must contain 12–128 characters.')
        username = username.lower()
        self.check_manager(actor, username)
        with self.connect() as db:
            row = db.execute('SELECT role FROM users WHERE username=?', (username,)).fetchone()
            if not row: raise ValueError('User not found.')
            if row['role'] == roles.SUPERADMIN and actor is not None and actor.get('username') != username:
                raise ValueError("A super administrator's password can only be changed by that same account.")
            db.execute('UPDATE users SET password_hash=? WHERE username=?', (generate_password_hash(password), username))
            db.execute('DELETE FROM sessions WHERE username=?', (username,))
        self.audit(actor, 'PASSWORD_RESET', 'USER', username, 'SUCCESS')

    def set_role(self, username, role, actor=None):
        """Command line only (manage_users.py). The web and the API never change roles."""
        username = username.lower()
        if role not in roles.STAFF: raise ValueError('Role must be ADMIN or SUPERADMIN.')
        with self.connect() as db:
            row = db.execute('SELECT role, active FROM users WHERE username=?', (username,)).fetchone()
            if not row: raise ValueError('User not found.')
            if row['role'] == role: return False
            if row['role'] == roles.SUPERADMIN and row['active'] and not self.other_active_supers(db, username):
                raise ValueError('The system must keep at least one active super administrator.')
            db.execute('UPDATE users SET role=? WHERE username=?', (role, username))
            db.execute('DELETE FROM sessions WHERE username=?', (username,))
        self.audit(actor, 'ROLE_CHANGE', 'USER', username, 'SUCCESS', before={'role': row['role']}, after={'role': role})
        return True

    def save_admin(self, username, display_name, password, actor=None, request_id=None, mode=None):
        """Demo twin of the MySQL procedure sp_guardar_admin: create an admin or change its password.
        mode='create' rejects an existing account; mode='password' requires it; None does either."""
        username = (username or '').strip().lower()
        with self.connect() as db:
            row = db.execute('SELECT role FROM users WHERE username=?', (username,)).fetchone()
        if mode == 'create' and row: raise ValueError('An account with that username already exists.')
        if mode == 'password' and not row: raise ValueError('That account does not exist.')
        if not row:
            self.add_user(username, password, roles.ADMIN, display_name or None, actor=actor)
            return 'CREADO'
        if row['role'] not in roles.STAFF: raise ValueError('That account is not an administrator.')
        self.set_password(username, password, actor)
        if display_name and display_name.strip():
            with self.connect() as db:
                db.execute('UPDATE users SET display_name=? WHERE username=?', (clean_display_name(display_name), username))
        return 'CONTRASENA'

    def change_own_password(self, user, current, new):
        with self.connect() as db:
            row = db.execute('SELECT password_hash FROM users WHERE username=?', (user['username'],)).fetchone()
        if not row or not check_password_hash(row['password_hash'], (current or '')[:129]):
            raise ValueError('Your current password is not correct.')
        return self.save_admin(user['username'], None, new, user)

    def profile(self, user):
        with self.connect() as db:
            row = db.execute('SELECT username, display_name, role, created FROM users WHERE username=?',
                             (user['username'],)).fetchone()
        if not row: return None
        created = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(row['created'])) if row['created'] else ''
        return dict(username=row['username'], display_name=row['display_name'] or row['username'],
                    role=row['role'], created_at_utc=created)

    def update_own_profile(self, user, display_name):
        name = clean_display_name(display_name)
        with self.connect() as db:
            before = db.execute('SELECT display_name FROM users WHERE username=?', (user['username'],)).fetchone()
            if not before: raise ValueError('User not found.')
            db.execute('UPDATE users SET display_name=? WHERE username=?', (name, user['username']))
        self.audit(user, 'PROFILE_UPDATE', 'USER', user['username'], 'SUCCESS',
                   before={'display_name': before['display_name']}, after={'display_name': name})

    # ------------------------------------------------------------------ audit log
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

    # ------------------------------------------------------------------ sessions
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
                if user and valid and user['active'] and user['role'] in roles.STAFF:
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
            row = db.execute('SELECT users.username,role,active,touched,display_name FROM sessions JOIN users USING(username) WHERE token=?', (self.digest(sid),)).fetchone()
            if not row or not row['active'] or row['role'] not in roles.STAFF or now-row['touched'] >= 1800: return None
            db.execute('UPDATE sessions SET touched=? WHERE token=?', (now, self.digest(sid)))
            return dict(username=row['username'], role=row['role'], active=row['active'], touched=row['touched'],
                        user_id=row['username'], display_name=row['display_name'] or row['username'],
                        session_id=self.digest(sid))

    def logout(self, token):
        sid = self.session_id(token, check_expiry=False)
        if not sid: return
        with self.connect() as db:
            row = db.execute('SELECT username FROM sessions WHERE token=?', (self.digest(sid),)).fetchone()
            db.execute('DELETE FROM sessions WHERE token=?', (self.digest(sid),))
        if row: self.audit({'user_id': row['username']}, 'LOGOUT', 'USER', row['username'], 'SUCCESS')

    # ------------------------------------------------------------------ lists
    def list_users(self):
        marks = ','.join('?' * len(roles.STAFF))
        with self.connect() as db:
            rows = db.execute(f'SELECT * FROM users WHERE role IN ({marks}) ORDER BY username', roles.STAFF).fetchall()
        return [dict(username=r['username'], display_name=r['display_name'] or r['username'], role=r['role'],
                     active=bool(r['active']),
                     created_at_utc=time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(r['created'])) if r['created'] else '')
                for r in rows]

    def count_admins(self):
        """Active accounts that can sign in (ADMIN + SUPERADMIN)."""
        marks = ','.join('?' * len(roles.STAFF))
        with self.connect() as db:
            return db.execute(f'SELECT COUNT(*) FROM users WHERE role IN ({marks}) AND active=1', roles.STAFF).fetchone()[0]

    def count_superadmins(self):
        with self.connect() as db:
            return db.execute('SELECT COUNT(*) FROM users WHERE role=? AND active=1', (roles.SUPERADMIN,)).fetchone()[0]

    def audit(self, actor, action, entity_type, entity_key, outcome, before=None, after=None, reason=None, request_id=None):
        with self.connect() as db:
            db.execute('INSERT INTO audit_events VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (str(uuid.uuid4()), time.time(), actor['user_id'] if actor else None, action, entity_type, str(entity_key),
                        outcome, reason, request_id, None if before is None else dumps(before), None if after is None else dumps(after)))

    def audit_log(self, limit=100, actor=None):
        """All events, or only the events of `actor` (what an ADMIN may see)."""
        with self.connect() as db:
            if actor is not None:
                rows = db.execute('SELECT * FROM audit_events WHERE actor=? ORDER BY occurred DESC LIMIT ?',
                                  (actor['user_id'], int(limit))).fetchall()
            else:
                rows = db.execute('SELECT * FROM audit_events ORDER BY occurred DESC LIMIT ?', (int(limit),)).fetchall()
        return [dict(occurred_at_utc=time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(r['occurred'])), actor=r['actor'] or '—',
                     action=r['action'], entity_type=r['entity_type'], entity_key=r['entity_key'], outcome=r['outcome'],
                     reason=r['reason'], request_id=r['request_id']) for r in rows]

    def catalog(self, fresh=False):
        return None   # demo mode: free-text IDs; the catalog lives in MySQL
