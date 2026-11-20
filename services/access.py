"""Local development accounts; no AWS or dotenv access."""
import hashlib
import secrets
import sqlite3
import time
from pathlib import Path
from werkzeug.security import generate_password_hash, check_password_hash

class AccessStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, username TEXT NOT NULL, touched REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (username TEXT PRIMARY KEY, failures INTEGER NOT NULL, started REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS imports (id TEXT PRIMARY KEY, owner TEXT NOT NULL, scope TEXT NOT NULL, created REAL NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL);
            ''')
        self.dummy = generate_password_hash(secrets.token_urlsafe(32))
    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db
    def add_user(self, username, password, role):
        import re
        if not re.fullmatch(r'[A-Za-z0-9_.@-]{3,80}', username): raise ValueError('Username must contain 3–80 letters, digits, _, ., @ or -.')
        if len(password) < 12 or len(password) > 128: raise ValueError('Password must contain 12–128 characters.')
        if role not in ('ADMIN','DATA_ENTRY','ANALYST'): raise ValueError('Invalid role.')
        with self.connect() as db:
            db.execute('INSERT INTO users(username,password_hash,role) VALUES(?,?,?)', (username.lower(),generate_password_hash(password),role))
    def disable(self, username):
        with self.connect() as db:
            db.execute('UPDATE users SET active=0 WHERE username=?',(username.lower(),))
            db.execute('DELETE FROM sessions WHERE username=?',(username.lower(),))
    def login(self, username, password):
        username = username.strip().lower()[:80]
        now = time.time()
        with self.connect() as db:
            attempt = db.execute('SELECT * FROM attempts WHERE username=?',(username,)).fetchone()
            if attempt and now-attempt['started'] < 900 and attempt['failures'] >= 5: return None
            user = db.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
            valid = check_password_hash(user['password_hash'] if user else self.dummy, password[:129])
            if not user or not valid or not user['active']:
                if not attempt or now-attempt['started'] >= 900:
                    db.execute('INSERT OR REPLACE INTO attempts VALUES(?,1,?)',(username,now))
                else: db.execute('UPDATE attempts SET failures=failures+1 WHERE username=?',(username,))
                return None
            db.execute('DELETE FROM attempts WHERE username=?',(username,))
            sid=secrets.token_urlsafe(32)
            db.execute('INSERT INTO sessions VALUES(?,?,?)',(self.digest(sid),username,now))
            return sid
    @staticmethod
    def digest(sid): return hashlib.sha256(sid.encode()).hexdigest()
    def user(self, sid):
        now=time.time()
        with self.connect() as db:
            row=db.execute('SELECT users.username,role,active,touched FROM sessions JOIN users USING(username) WHERE token=?',(self.digest(sid),)).fetchone()
            if not row or not row['active'] or now-row['touched'] >= 1800: return None
            db.execute('UPDATE sessions SET touched=? WHERE token=?',(now,self.digest(sid)))
            return dict(row)
    def logout(self,sid):
        with self.connect() as db: db.execute('DELETE FROM sessions WHERE token=?',(self.digest(sid),))
