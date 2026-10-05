"""SQLite settings and write-ahead move journal. Never stores API secrets."""
from contextlib import contextmanager
import json
import os
import sqlite3
from pathlib import Path


def data_dir():
    base = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local' / 'share'))
    path = base / 'SafeFileOrganizer'
    path.mkdir(parents=True, exist_ok=True)
    return path


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path else data_dir() / 'organizer.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS choices (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS excluded (key TEXT PRIMARY KEY, path TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS moves (
              id INTEGER PRIMARY KEY AUTOINCREMENT, job INTEGER NOT NULL,
              src TEXT NOT NULL, dst TEXT NOT NULL, before TEXT NOT NULL,
              after TEXT, state TEXT NOT NULL, error TEXT DEFAULT '', restored TEXT);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=None, table='settings'):
        assert table in ('settings', 'cache', 'choices')
        with self.connect() as db:
            row = db.execute(f'SELECT value FROM {table} WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value, table='settings'):
        assert table in ('settings', 'cache', 'choices')
        with self.connect() as db:
            db.execute(f'INSERT OR REPLACE INTO {table} VALUES (?, ?)', (key, json.dumps(value, ensure_ascii=False)))

    def exclude(self, key, path):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO excluded VALUES (?,?)', (key, str(path)))

    def exclusions(self):
        with self.connect() as db:
            return dict(db.execute('SELECT key,path FROM excluded').fetchall())

    def clear_exclusions(self):
        with self.connect() as db:
            db.execute('DELETE FROM excluded')

    def new_job(self):
        with self.connect() as db:
            return db.execute('INSERT INTO jobs DEFAULT VALUES').lastrowid

    def journal(self, job, src, dst, before):
        with self.connect() as db:
            return db.execute('INSERT INTO moves(job,src,dst,before,state) VALUES (?,?,?,?,?)',
                (job, str(src), str(dst), json.dumps(before), 'pending')).lastrowid

    def update_move(self, mid, state, after=None, error='', restored=None):
        with self.connect() as db:
            db.execute('UPDATE moves SET state=?,after=COALESCE(?,after),error=?,restored=COALESCE(?,restored) WHERE id=?',
                (state, json.dumps(after) if after is not None else None, error, str(restored) if restored else None, mid))

    def records(self, job=None):
        with self.connect() as db:
            if job is None:
                return [dict(r) for r in db.execute('SELECT * FROM moves ORDER BY id DESC')]
            return [dict(r) for r in db.execute('SELECT * FROM moves WHERE job=? ORDER BY id DESC', (job,))]

    def last_job(self):
        with self.connect() as db:
            row = db.execute("SELECT MAX(job) FROM moves WHERE state IN ('done','pending','undo_pending','uncertain')").fetchone()
            return row[0]
