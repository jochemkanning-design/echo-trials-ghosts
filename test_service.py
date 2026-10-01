import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

from app import Service, Store, COURSES, validate_submission, RequestError, identity

FIXTURES = Path(__file__).with_name('test_routes.json')


class CommunityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.routes = json.loads(FIXTURES.read_text())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'ghosts.sqlite3'
        self.app = Service(Store(self.path))

    def tearDown(self):
        self.temp.cleanup()

    def call(self, method, path, body=None, token='a'*64, query='', extra=None):
        raw = json.dumps(body).encode() if body is not None else b''
        env = {'REQUEST_METHOD': method, 'PATH_INFO': path, 'QUERY_STRING': query,
            'wsgi.input': io.BytesIO(raw), 'CONTENT_LENGTH': str(len(raw)),
            'CONTENT_TYPE': 'application/json', 'HTTP_AUTHORIZATION': 'Bearer '+token,
            'HTTP_ORIGIN': 'https://html-classic.itch.zone', 'REMOTE_ADDR': '127.0.0.1'}
        if extra:
            env.update(extra)
        response = {}
        def start(status, headers):
            response.update(status=int(status[:3]), headers=dict(headers))
        data = b''.join(self.app(env, start))
        return response, json.loads(data) if data else None

    def publish(self, route=0, token='a'*64, name='Runner A'):
        payload = dict(self.routes[route], nickname=name)
        response, data = self.call('POST', '/v1/runs', payload, token)
        self.assertEqual(response['status'], 200, data)
        return data

    def test_all_ten_courses_reconstruct_real_successful_runs(self):
        self.assertEqual(len(COURSES), 10)
        for route in self.routes:
            key, replay = validate_submission(route)
            self.assertEqual(replay['score'], route['score'])
            self.assertEqual(replay['ticks'], route['ticks'])
            self.assertTrue(replay['success'])
            self.assertEqual(len(replay['frames']), replay['ticks']+1)

    def test_two_players_share_persist_exclude_self_and_delete_only_own(self):
        a = self.publish()
        b = self.publish(token='b'*64, name='Runner B')
        key = self.routes[0]['course']
        self.app = Service(Store(self.path))  # durable across a process/restart boundary
        response, data = self.call('GET', '/v1/ghost', query='course='+key+'&exclude='+a['player_id'])
        self.assertEqual(response['headers']['Access-Control-Allow-Origin'], '*')
        self.assertEqual(data['rival']['nickname'], 'Runner B')
        self.assertTrue(data['rival']['run']['success'])
        self.assertNotIn('inputs', data['rival']['run'])
        self.assertNotIn('token', data['rival'])
        _, deleted = self.call('DELETE', '/v1/me')
        self.assertEqual(deleted['deleted'], 1)
        _, board = self.call('GET', '/v1/leaderboard', query='course='+key)
        self.assertEqual([p['nickname'] for p in board['players']], ['Runner B'])

    def test_score_time_order_and_next_rival(self):
        route = self.routes[0]
        self.publish()
        slower = copy.deepcopy(route)
        slower['inputs'] = [[0,False,0]]*40 + slower['inputs']
        slower['ticks'] += 40
        slower['nickname'] = 'Slower B'
        response, b = self.call('POST','/v1/runs',slower,'b'*64)
        self.assertEqual(response['status'],200,b)
        _, data = self.call('GET','/v1/ghost',query=f"course={route['course']}&target=next&score={route['score']}&ticks={route['ticks']+80}")
        self.assertEqual(data['rival']['nickname'],'Slower B')
        _, data = self.call('GET','/v1/ghost',query='course='+route['course'])
        self.assertEqual(data['rival']['nickname'],'Runner A')
        # A delayed inferior submission from A must not overwrite A's prior record.
        response, data = self.call('POST','/v1/runs',slower)
        self.assertFalse(data['improved'])
        self.assertEqual(len(self.app.store.leaderboard(route['course'])),2)

    def test_forged_results_dead_runs_wrong_versions_and_invalid_inputs_rejected(self):
        for change in ({'score':9999}, {'ticks':1}, {'course':'old-version'},
                       {'inputs':[[0,False,0]]}, {'inputs':[[2,True,0]]},
                       {'inputs':[[True,True,0]]}, {'inputs':[[0,1,0]]},
                       {'inputs':[[float('nan'),False,0]]}):
            with self.subTest(change=change):
                payload = dict(self.routes[0],nickname='Runner',**change)
                response, data = self.call('POST','/v1/runs',payload)
                self.assertIn(response['status'],(400,409),data)
        self.assertEqual(self.app.store.leaderboard(self.routes[0]['course']),[])

    def test_missing_identity_and_oversized_uploads_rejected(self):
        response,_=self.call('POST','/v1/runs',{},token='bad')
        self.assertEqual(response['status'],401)
        response,_=self.call('POST','/v1/runs',{},extra={'CONTENT_LENGTH':'99999999'})
        self.assertEqual(response['status'],413)
        response,_=self.call('OPTIONS','/v1/runs')
        self.assertEqual(response['status'],204)

    def test_rate_limited_without_losing_saved_runs(self):
        self.publish()
        for _ in range(21):
            response,_=self.call('POST','/v1/runs',{})
        self.assertEqual(response['status'],429)
        self.assertEqual(len(self.app.store.leaderboard(self.routes[0]['course'])),1)


if __name__ == '__main__':
    unittest.main()
