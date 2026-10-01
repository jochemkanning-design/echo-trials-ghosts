"""Echo Trials public replay service. WSGI + SQLite locally, PostgreSQL hosted.

Only inputs are accepted. Scores, outcomes and ghost frames are reconstructed
by the same unmodified deterministic physics shipped in the browser game.
"""
from contextlib import contextmanager
from hashlib import sha256
from http import HTTPStatus
import base64
import gzip
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'physics'))
from trials_core import TrialLevel, TrialMatch, CHAPTER_FILES
from coral_core import CoralLevel, CoralMatch, CORAL_FILES

PROTOCOL = 1
MAX_BODY = 160_000
COURSES = {}
for level_cls, match_cls, filenames in ((TrialLevel, TrialMatch, CHAPTER_FILES),
                                       (CoralLevel, CoralMatch, CORAL_FILES)):
    for filename in filenames:
        level = level_cls(filename)
        COURSES[level.record_key] = (level_cls, match_cls, filename, level.title)


class RequestError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def identity(token):
    if not re.fullmatch(r'[a-f0-9]{64}', token):
        raise RequestError('A valid browser sharing identity is required.', 401)
    return sha256(token.encode()).hexdigest()


def nickname(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 _-]{1,19}', value):
        raise RequestError('Use a nickname of 2–20 letters, numbers, spaces, _ or -.')
    return value.strip()


def validate_submission(data):
    if not isinstance(data, dict) or data.get('protocol') != PROTOCOL:
        raise RequestError('Unsupported replay protocol; update your game.')
    key = data.get('course')
    if not isinstance(key, str) or key not in COURSES:
        raise RequestError('This exact course version is not supported.', 409)
    actions = data.get('inputs')
    if not isinstance(actions, list) or not 1 <= len(actions) <= 3600:
        raise RequestError('Invalid number of input samples.')
    for action in actions:
        if not isinstance(action, list) or len(action) != 3:
            raise RequestError('Invalid input sample.')
        if type(action[1]) is not bool or any(type(action[i]) not in (int, float)
                or not math.isfinite(action[i]) or not -1 <= action[i] <= 1 for i in (0, 2)):
            raise RequestError('Inputs must stay within the controller range.')
    if type(data.get('score')) is not int or type(data.get('ticks')) is not int:
        raise RequestError('Invalid claimed result.')
    level_cls, match_cls, filename, _ = COURSES[key]
    match = match_cls(level_cls(filename))
    if len(actions) > match.limit_ticks:
        raise RequestError('Run exceeds the course time limit.')
    for action in actions:
        if match.over:
            raise RequestError('Inputs continued after the attempt ended.')
        match.step(action)
    if not match.success or match.markers != 2:
        raise RequestError('The submitted inputs do not finish this course alive.')
    if match.tick != data['ticks'] or match.score[0] != data['score']:
        raise RequestError('The score or time does not match the replayed inputs.')
    return key, match.replay()


class Store:
    def __init__(self, path=None, database_url=None):
        self.path = str(path or ROOT / 'data' / 'ghosts.sqlite3')
        self.url = database_url or ''
        self.lock = threading.RLock()
        if not self.url:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            self.execute(db, '''CREATE TABLE IF NOT EXISTS runs (
                course TEXT NOT NULL, player_id TEXT NOT NULL, nickname TEXT NOT NULL,
                score INTEGER NOT NULL, ticks INTEGER NOT NULL, replay TEXT NOT NULL,
                updated BIGINT NOT NULL, PRIMARY KEY(course, player_id))''')
            self.execute(db, 'CREATE INDEX IF NOT EXISTS ranked_runs ON runs(course, score DESC, ticks ASC)')

    @contextmanager
    def connection(self):
        if self.url:
            import psycopg
            db = psycopg.connect(self.url, connect_timeout=8)
        else:
            db = sqlite3.connect(self.path, timeout=10)
            db.execute('PRAGMA journal_mode=WAL')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def execute(self, db, sql, values=()):
        return db.execute(sql.replace('?', '%s') if self.url else sql, values)

    def save(self, key, player_id, name, run):
        packed = base64.b64encode(gzip.compress(json.dumps(run, separators=(',', ':')).encode())).decode()
        with self.lock, self.connection() as db:
            prior = self.execute(db, 'SELECT score,ticks FROM runs WHERE course=? AND player_id=?', (key, player_id)).fetchone()
            if prior and (run['score'], -run['ticks']) <= (prior[0], -prior[1]):
                return False
            if not prior and self.execute(db, 'SELECT COUNT(*) FROM runs').fetchone()[0] >= int(os.environ.get('MAX_PUBLIC_RUNS', '5000')):
                raise RequestError('The community archive is full; your local run is safe.', 503)
            # The WHERE makes an out-of-order/concurrent request unable to replace a better run.
            result = self.execute(db, '''INSERT INTO runs VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(course,player_id) DO UPDATE SET nickname=excluded.nickname,
                score=excluded.score,ticks=excluded.ticks,replay=excluded.replay,updated=excluded.updated
                WHERE excluded.score>runs.score OR (excluded.score=runs.score AND excluded.ticks<runs.ticks)''',
                (key, player_id, name, run['score'], run['ticks'], packed, int(time.time())))
            return result.rowcount > 0

    def rival(self, key, exclude='', target='best', score=0, ticks=3601):
        with self.connection() as db:
            base = 'SELECT player_id,nickname,score,ticks,replay FROM runs WHERE course=? AND player_id<>?'
            values = [key, exclude]
            if target == 'next':
                base += ' AND (score>? OR (score=? AND ticks<?))'
                values += [score, score, ticks]
                order = ' ORDER BY score ASC,ticks DESC,player_id ASC LIMIT 1'
            else:
                order = ' ORDER BY score DESC,ticks ASC,player_id ASC LIMIT 1'
            row = self.execute(db, base + order, values).fetchone()
            if row is None and target == 'next':
                row = self.execute(db, 'SELECT player_id,nickname,score,ticks,replay FROM runs WHERE course=? AND player_id<>? ORDER BY score DESC,ticks ASC,player_id LIMIT 1', (key, exclude)).fetchone()
            if not row:
                return None
            return {'player_id': row[0], 'nickname': row[1], 'score': row[2], 'ticks': row[3],
                    'run': json.loads(gzip.decompress(base64.b64decode(row[4])))}

    def leaderboard(self, key):
        with self.connection() as db:
            rows = self.execute(db, 'SELECT player_id,nickname,score,ticks FROM runs WHERE course=? ORDER BY score DESC,ticks ASC,player_id LIMIT 10', (key,)).fetchall()
        return [dict(zip(('player_id','nickname','score','ticks'), row)) for row in rows]

    def delete(self, player_id):
        with self.lock, self.connection() as db:
            return self.execute(db, 'DELETE FROM runs WHERE player_id=?', (player_id,)).rowcount


class Service:
    def __init__(self, store):
        self.store = store
        self.buckets = {}
        self.lock = threading.Lock()
        self.simulation_slots = threading.BoundedSemaphore(2)
        self.generation = 0

    def rate_limit(self, key, limit=20, seconds=600):
        now = time.monotonic()
        with self.lock:
            bucket = self.buckets.get(key)
            if bucket is None or now - bucket[0] > seconds:
                if len(self.buckets) > 10000:
                    self.buckets = {k:v for k,v in self.buckets.items() if now-v[0] < 600}
                    if len(self.buckets) > 10000:
                        raise RequestError('Service is busy. Try later.', 429)
                bucket = self.buckets[key] = [now, 0]
            bucket[1] += 1
            if bucket[1] > limit:
                raise RequestError('Too many requests. Try again later; your run is saved locally.', 429)

    def __call__(self, environ, start_response):
        origin = environ.get('HTTP_ORIGIN', '')
        allowed = os.environ.get('ALLOWED_ORIGINS', '*').split(',')
        cors = '*' if '*' in allowed else origin if origin in allowed else ''
        headers = [('Content-Type', 'application/json; charset=utf-8'), ('Cache-Control', 'no-store'),
                   ('X-Content-Type-Options', 'nosniff'), ('Vary', 'Origin, Accept-Encoding')]
        if cors:
            headers += [('Access-Control-Allow-Origin', cors),
                        ('Access-Control-Allow-Methods', 'GET,POST,DELETE,OPTIONS'),
                        ('Access-Control-Allow-Headers', 'Authorization, Content-Type'),
                        ('Access-Control-Max-Age', '600')]
        try:
            if origin and not cors:
                raise RequestError('This game origin is not allowed.', 403)
            if environ['REQUEST_METHOD'] == 'OPTIONS':
                status, body = 204, b''
            else:
                result = self.dispatch(environ)
                status, body = 200, json.dumps(result, separators=(',', ':'), allow_nan=False).encode()
        except RequestError as exc:
            status, body = exc.status, json.dumps({'error': str(exc)}).encode()
            if status == 429:
                headers.append(('Retry-After', '600'))
        except Exception:
            # Never return database URLs, auth tokens or internal tracebacks to clients.
            status, body = 503, b'{"error":"Replay service is temporarily unavailable. Your local saves are safe."}'
        if len(body) > 1024 and 'gzip' in environ.get('HTTP_ACCEPT_ENCODING', ''):
            body = gzip.compress(body)
            headers.append(('Content-Encoding', 'gzip'))
        headers.append(('Content-Length', str(len(body))))
        start_response(f'{status} {HTTPStatus(status).phrase}', headers)
        return [body]

    def dispatch(self, env):
        method, path = env['REQUEST_METHOD'], env.get('PATH_INFO', '')
        if method == 'GET' and path in ('/', '/v1/health'):
            return {'service': 'Echo Trials Ghosts', 'protocol': PROTOCOL, 'courses': len(COURSES),
                    'ranking': 'score_desc_then_time_asc', 'validation': 'server_replayed_inputs'}
        params = parse_qs(env.get('QUERY_STRING', ''))
        key = params.get('course', [''])[0]
        if method == 'GET' and path in ('/v1/ghost', '/v1/leaderboard'):
            if key not in COURSES:
                raise RequestError('Unknown course version.', 409)
            if path == '/v1/leaderboard':
                return {'course': key, 'players': self.store.leaderboard(key)}
            target = params.get('target', ['best'])[0]
            if target not in ('best', 'next'):
                raise RequestError('Unknown rival target.')
            exclude = params.get('exclude', [''])[0]
            if exclude and not re.fullmatch('[a-f0-9]{64}', exclude):
                raise RequestError('Invalid player ID.')
            try:
                score, ticks = int(params.get('score', ['0'])[0]), int(params.get('ticks', ['3601'])[0])
                if not 0 <= score <= 10000 or not 1 <= ticks <= 3601:
                    raise ValueError()
            except ValueError:
                raise RequestError('Invalid personal result.')
            return {'protocol': PROTOCOL, 'course': key, 'target': target,
                    'rival': self.store.rival(key, exclude, target, score, ticks)}
        if method not in ('POST', 'DELETE') or path not in ('/v1/runs', '/v1/me'):
            raise RequestError('Unknown endpoint.', 404)
        token = env.get('HTTP_AUTHORIZATION', '').removeprefix('Bearer ')
        player_id = identity(token)
        self.rate_limit('player:' + player_id)
        # A coarse source limit plus bounded simulation slots prevent unbounded CPU work.
        self.rate_limit('source:' + env.get('REMOTE_ADDR', ''), 240, 600)
        if method == 'DELETE' and path == '/v1/me':
            return {'deleted': self.store.delete(player_id)}
        if method != 'POST' or path != '/v1/runs':
            raise RequestError('Unsupported operation.', 405)
        try:
            length = int(env.get('CONTENT_LENGTH', '0'))
        except ValueError:
            raise RequestError('Invalid request size.')
        if not 0 < length <= MAX_BODY:
            raise RequestError('Submission is too large or empty.', 413)
        if not env.get('CONTENT_TYPE', '').startswith('application/json'):
            raise RequestError('Use application/json.', 415)
        raw = env['wsgi.input'].read(length)
        try:
            data = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, RecursionError):
            raise RequestError('Invalid JSON.')
        if not isinstance(data, dict):
            raise RequestError('Invalid submission.')
        name = nickname(data.get('nickname'))
        if not self.simulation_slots.acquire(blocking=False):
            raise RequestError('Verification is busy. Try again shortly.', 503)
        try:
            key, run = validate_submission(data)
        finally:
            self.simulation_slots.release()
        improved = self.store.save(key, player_id, name, run)
        return {'accepted': True, 'improved': improved, 'player_id': player_id,
                'score': run['score'], 'ticks': run['ticks'], 'course': key}


def create_app():
    database_url = os.environ.get('DATABASE_URL', '')
    if os.environ.get('RENDER') and not database_url:
        raise RuntimeError('Set DATABASE_URL to persistent PostgreSQL. Local SQLite is not durable on Render Free.')
    return Service(Store(os.environ.get('GHOST_DB_PATH'), database_url))


# Gunicorn's app:create_app() calls this factory; importing for tests has no side effects.
if __name__ == '__main__':
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import make_server, WSGIServer
    class LocalServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '8787'))
    print(f'Echo Trials ghost API: http://{host}:{port} (local test server)', flush=True)
    make_server(host, port, create_app(), server_class=LocalServer).serve_forever()
