"""One-life course and tick-exact ghost samples, sharing the original physics.

Ghosts are recorded observations only: they never enter the simulation.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

from campaign import CampaignLevel, Motion, Surface
from core import BW, BH, FPS, Coin, Platform, IDLE, overlap, step_body
from worlds import world_for

ROOT = Path(__file__).resolve().parent


class EchoLevel(CampaignLevel):
    def __init__(self):
        self.spec = json.loads((ROOT / 'levels' / 'echo_trial.json').read_text(encoding='utf-8'))
        s = self.spec
        self.width, self.height = s['width'], s['height']
        self.title = s['title']
        self.number, self.seed, self.style = 1, 42, 'scatter'
        self.world = world_for(1)
        self.physics = self.world.physics
        self.hint = 'Visit flowers 1 and 2, then reach the exit. Coins first; time breaks ties.'
        self.fixed = [Surface(x, y, w, 20, kind) for x, y, w, kind in s['platforms']]
        self.fixed_count = len(self.fixed)
        self.starts = [s['start']]
        self.marker_platforms = s['markers']
        self.exit_platform = s['exit']
        self.safe_route = s['safe_route']
        self.treasure_id = self.fixed_count
        t = s['treasure']
        self.motions = {self.treasure_id: Motion(Platform(t['x'], t['y'], t['width'], 16, 'fall'), t['dock'])}
        self.playable = set(range(self.fixed_count + 1))
        self.route_platforms = set(range(self.fixed_count))
        self.box_platforms = []
        self.limit_ticks = s['seconds'] * FPS
        # A changed course or shared movement rule gets a separate record.
        digest = hashlib.sha256()
        for name in ('levels/echo_trial.json', 'echo_core.py', 'core.py', 'campaign.py', 'worlds.py', 'controls.py'):
            digest.update(name.encode())
            digest.update((ROOT / name).read_bytes())
        self.record_key = 'echo-trial-' + digest.hexdigest()[:20]
        self.reset()

    def reset(self):
        super().reset()
        self.coins = []
        for i, p in enumerate(self.fixed):
            if i in (self.starts[0], self.exit_platform):
                continue
            for k in range(self.spec['coins_per_platform']):
                count = self.spec['coins_per_platform']
                self.coins.append(Coin(p.x + 32 + (p.w - 64) * k / max(1, count - 1), p.y - 13, i))
        # Move the three exit coins onto permanent approach ledges. Keep all
        # other coin positions and the total score pool unchanged.
        for i in self.spec['exit_coin_destinations']:
            p = self.fixed[i]
            self.coins.append(Coin(p.x + 32 + (p.w - 64) / 4, p.y - 13, i))
        t = self.spec['treasure']
        p = self.platforms[self.treasure_id]
        for k in range(t['coins']):
            self.coins.append(Coin(p.x + 12 + (p.w - 24) * k / max(1, t['coins'] - 1),
                                   p.y - 13, self.treasure_id, t['value']))
        self.index_coins()

    def advance(self, bodies=(), *, rewards=True):
        # Treasure stays at its original location even if its branch falls.
        # No rescued rewards, random prizes, or ghost-triggered changes.
        return super().advance(bodies, rewards=False)


def sample(body, score, markers):
    return [round(body.x, 4), round(body.y, 4), round(body.vy, 4),
            body.ground, body.jumps, body.facing, score, markers]


class EchoMatch:
    def __init__(self, level, *, eligible=True):
        self.level = level
        level.reset()
        self.bodies = [level.spawn(level.starts[0])]
        self.owners = [-1] * len(level.coins)
        self.score = [0]
        self.freeze = [0]
        self.tick, self.over, self.success = 0, False, False
        self.reason = ''
        self.markers = 0
        self.events = []
        self.limit_ticks = level.limit_ticks
        self.eligible = eligible
        self.frames = [sample(self.bodies[0], 0, 0)]
        self.gold = 0

    @property
    def remaining(self):
        return self.owners.count(-1)

    def stop(self, reason):
        if not self.over:
            self.over, self.reason = True, reason

    def step(self, action=IDLE):
        if self.over:
            return
        self.events = []
        b = self.bodies[0]
        self.level.advance([b])
        step_body(b, action, self.level)
        self.level.trigger(b.ground)
        self.tick += 1
        if b.feet >= self.level.height or b.y < -350:
            self.stop('fall')
            self.events.append(('death', 0, b.cx, min(b.feet, self.level.height)))
        else:
            for i, c in enumerate(self.level.coins):
                if self.owners[i] < 0 and overlap(b.x, b.y, BW, BH, c.x - 8, c.y - 8, 16, 16):
                    self.owners[i] = 0
                    self.score[0] += c.value
                    self.gold += int(c.value > 1)
                    self.events.append(('gold' if c.value > 1 else 'coin', 0, c.x, c.y))
            if self.markers < len(self.level.marker_platforms):
                p = self.level.fixed[self.level.marker_platforms[self.markers]]
                if overlap(b.x, b.y, BW, BH, p.cx - 26, p.y - 80, 52, 80):
                    self.markers += 1
                    self.events.append(('marker', 0, p.cx, p.y - 55))
            p = self.level.fixed[self.level.exit_platform]
            if self.markers == len(self.level.marker_platforms) and overlap(b.x, b.y, BW, BH, p.cx - 28, p.y - 86, 56, 86):
                self.success = True
                self.stop('finish')
                self.events.append(('finish', 0, p.cx, p.y - 50))
            elif self.tick >= self.limit_ticks:
                self.stop('timeout')
                self.events.append(('timeout', 0, b.cx, b.y))
        self.frames.append(sample(b, self.score[0], self.markers))

    def replay(self):
        return {'ticks': self.tick, 'score': self.score[0], 'success': self.success,
                'reason': self.reason, 'frames': [f[:] for f in self.frames]}


def frame_at(replay, tick):
    """Keep final score available after a ghost has finished or died."""
    return replay['frames'][max(0, min(int(tick), replay['ticks']))] if replay else None


def better_run(candidate, best):
    return candidate['success'] and (not best or
        (candidate['score'], -candidate['ticks']) > (best['score'], -best['ticks']))
