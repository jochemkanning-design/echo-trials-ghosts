"""Authored Echo sublevels: pure 60 Hz physics, climbing and swept hazards.

The existing games are unchanged. This module owns only the new chapter.
All hazard geometry used for drawing also defines contact in the simulation.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path

from campaign import CampaignLevel, Motion, Racer, Surface
from core import BW, BH, FPS, MAX_JUMPS, Coin, Platform, overlap
from echo_core import EchoMatch, sample
from worlds import world_for

ROOT = Path(__file__).resolve().parent
CHAPTER_FILES = tuple(f'woodland_{i:02}.json' for i in range(1, 6))


@dataclass
class Climber(Racer):
    wall: int = -1
    wall_side: int = 0
    detach_ticks: int = 0


def polygons_overlap(a, b):
    """Separating axes for two convex polygons, including rectangle corners."""
    for polygon in (a, b):
        for p, q in zip(polygon, polygon[1:] + polygon[:1]):
            nx, ny = -(q[1] - p[1]), q[0] - p[0]
            aa = [x * nx + y * ny for x, y in a]
            bb = [x * nx + y * ny for x, y in b]
            if max(aa) <= min(bb) or max(bb) <= min(aa):
                return False
    return True


@dataclass(frozen=True)
class Pendulum:
    x: float
    y: float
    length: float = 185
    amplitude: float = .95
    period: int = 300
    phase: int = 0
    size: float = 28

    def pose(self, tick):
        angle = self.amplitude * math.sin(math.tau * (tick + self.phase) / self.period)
        return self.x + self.length * math.sin(angle), self.y + self.length * math.cos(angle), angle

    def blades(self, tick):
        x, y, a = self.pose(tick)
        # Each bit is convex; their union is an axe silhouette, not a box.
        shape = [(.12, -.38), (.7, -1), (1.15, -.66), (1.35, 0),
                 (1.15, .66), (.7, 1), (.12, .38)]
        return [[(x + self.size * (side*u * math.cos(a) + v * math.sin(a)),
                  y + self.size * (-side*u * math.sin(a) + v * math.cos(a))) for u, v in shape]
                for side in (-1,1)]

    def hits(self, before, after, tick):
        start, end = self.pose(tick - 1), self.pose(tick)
        travel = math.hypot(after[0] - before[0], after[1] - before[1])
        travel += math.hypot(end[0] - start[0], end[1] - start[1])
        for i in range(max(1, math.ceil(travel / 3)) + 1):
            steps = max(1, math.ceil(travel / 3))
            t = i / steps
            x = before[0] + (after[0] - before[0]) * t
            y = before[1] + (after[1] - before[1]) * t
            cx, cy, _ = self.pose(tick - 1 + t)
            radius = self.size * 1.4
            if x + BW < cx - radius or x > cx + radius or y + BH < cy - radius or y > cy + radius:
                continue
            body = [(x, y), (x + BW, y), (x + BW, y + BH), (x, y + BH)]
            if any(polygons_overlap(body, blade) for blade in self.blades(tick - 1 + t)):
                return True
        return False


class TrialLevel(CampaignLevel):
    """Reusable level schema; Woodland supplies the first five authored courses."""
    def __init__(self, filename='woodland_01.json'):
        path = ROOT / 'levels' / 'trials' / filename
        self.spec = s = json.loads(path.read_text(encoding='utf-8'))
        self.number, self.seed, self.style = s['world'], 42, 'scatter'
        self.world = world_for(self.number)
        self.physics = self.world.physics
        self.width, self.height = s['width'], s['height']
        self.title, self.hint = s['title'], s['hint']
        self.level_id = s['id']
        self.practice_only = s.get('practice', False)
        self.fixed = [Surface(*p) for p in s['platforms']]
        self.deck_count = len(self.fixed)
        self.wall_ids = []
        for wall in s.get('walls', []):
            self.wall_ids.append(len(self.fixed))
            self.fixed.append(Surface(*wall, 'wall'))
        self.fixed_count = len(self.fixed)
        self.starts = [s['start']]
        self.marker_platforms = s['markers']
        self.exit_platform = s['exit']
        self.safe_route = s['route']
        self.motions = {}
        for item in s.get('moving', []):
            self.motions[len(self.fixed) + len(self.motions)] = Motion(
                Platform(*item['deck']), item['dock'], *item.get('travel', [0, 0]),
                item.get('period', 300), item.get('phase', 0))
        self.axes = [Pendulum(**item) for item in s.get('axes', [])]
        self.treasure_id = s['gold'][0]
        self.playable = set(range(len(self.fixed) + len(self.motions))) - set(self.wall_ids)
        self.route_platforms = set(range(self.deck_count))
        self.box_platforms = []
        self.limit_ticks = s['seconds'] * FPS
        if not 15 <= s['seconds'] <= 60 or len(self.marker_platforms) != 2:
            raise ValueError('Each course needs two flowers and a supported time limit')
        if any(i not in range(self.deck_count) for i in [*self.marker_platforms, self.exit_platform, *self.starts]):
            raise ValueError('Start, flowers and exit must be on permanent decks')
        for p in self.fixed:
            if p.w < 20 or p.h <= 0 or p.x < 0 or p.y < 0 or p.x + p.w > self.width or p.y + p.h > self.height:
                raise ValueError('Platform outside the playable world')
        if any(i not in self.playable or i == self.exit_platform for i in s['gold']):
            raise ValueError('Gold must have a valid platform away from the exit')
        digest = hashlib.sha256(path.read_bytes())
        for name in ('trials_core.py', 'core.py', 'campaign.py', 'worlds.py', 'controls.py'):
            digest.update((ROOT / name).read_bytes())
        self.record_key = self.level_id + '-' + digest.hexdigest()[:20]
        self.reset()

    def reset(self):
        super().reset()
        self.coins = []
        for i, p in enumerate(self.platforms):
            if i in self.wall_ids or i == self.exit_platform or i in self.starts:
                continue
            gold = i in self.spec['gold']
            count = 5 if gold else 3
            for k in range(count):
                self.coins.append(Coin(p.x + 18 + (p.w - 36) * k / (count - 1), p.y - 13, i, 2 if gold else 1))
        self.coin_offsets = [(c.x - self.platforms[c.platform].x, c.y - self.platforms[c.platform].y) for c in self.coins]
        self.index_coins()

    def spawn(self, platform):
        p = self.platforms[platform]
        return Climber(p.cx - BW / 2, p.y - BH, ground=platform)

    def fork(self):
        other = super().fork()
        other.coins = self.coins[:]
        return other

    def advance(self, bodies=(), *, rewards=True):
        departed = super().advance(bodies, rewards=False)
        # Ferries carry their coins. Crumble coins disappear with their branch
        # and return uncollected, retaining their original ownership indices.
        for ci, coin in enumerate(self.coins):
            if coin.platform in self.motions:
                p = self.platforms[coin.platform]
                dx, dy = self.coin_offsets[ci]
                self.coins[ci] = replace(coin, x=p.x + dx, y=p.y + dy)
        return departed

    def _climb(self, b, vertical):
        wall = self.fixed[b.wall]
        old_y = b.y
        b.y += vertical * 3.2
        b.vy = vertical * 3.2
        b.ground = -1
        for i in self.nearby(b.x, b.y):
            p = self.platforms[i]
            if i == b.wall or not p.active or not overlap(b.x, b.y, BW, BH, p.x, p.y, p.w, p.h):
                continue
            if vertical < 0 and i < self.fixed_count:
                b.y, b.vy = p.y + p.h, 0
            elif vertical > 0 and old_y + BH <= p.y + .01:
                b.y, b.vy, b.ground, b.jumps = p.y - BH, 0, i, MAX_JUMPS
                b.wall = -1
                return
        if b.feet <= wall.y and vertical < 0:
            # Mantle onto the top. It is a real landing and refills jumps.
            candidate_x = wall.x + 2 if b.wall_side == 1 else wall.x + wall.w - BW - 2
            candidate_y = wall.y - BH
            blocked = any(overlap(candidate_x, candidate_y, BW, BH, p.x, p.y, p.w, p.h)
                          for i, p in enumerate(self.fixed) if i != b.wall)
            if not blocked:
                b.x, b.y = candidate_x, candidate_y
                b.ground, b.jumps, b.vy, b.wall = b.wall, MAX_JUMPS, 0, -1
            else:
                b.y, b.vy = old_y, 0
        elif b.y >= wall.y + wall.h:
            b.wall, b.detach_ticks = -1, 8

    def step_racer(self, b, action):
        direction, jump = action[:2]
        vertical = action[2] if len(action) > 2 else 0
        b.detach_ticks = max(0, b.detach_ticks - 1)
        if b.wall >= 0:
            away = direction * b.wall_side < -.25
            if jump and b.jumps:
                b.knock_x = -b.wall_side * 4
                b.vy, b.jumps = self.physics.jump, b.jumps - 1
                b.wall, b.detach_ticks = -1, 16
                super().step_racer(b, (direction or -b.wall_side, False))
                return
            if away:
                b.wall, b.detach_ticks = -1, 12
            else:
                self._climb(b, vertical)
                return
        super().step_racer(b, (direction, jump))
        if b.detach_ticks or jump:
            return
        for ident in self.wall_ids:
            wall = self.fixed[ident]
            if not (b.y + BH > wall.y + 2 and b.y < wall.y + wall.h - 2):
                continue
            side = 1 if abs(b.x + BW - wall.x) <= 6 else -1 if abs(b.x - wall.x - wall.w) <= 6 else 0
            if side and ((direction * side > .25 and b.ground < 0) or (vertical != 0 and direction * side >= 0)):
                b.wall, b.wall_side, b.ground, b.vy = ident, side, -1, 0
                b.x = wall.x - BW if side == 1 else wall.x + wall.w
                self._climb(b, vertical)
                break


class TrialMatch(EchoMatch):
    def __init__(self, level, *, eligible=True):
        super().__init__(level, eligible=eligible and not level.practice_only)
        self.death_cause = ''
        self.grips = [False]
        self.climb_ticks = 0

    def step(self, action=(0, False, 0)):
        if self.over:
            return
        self.events = []
        level, b = self.level, self.bodies[0]
        before = b.x, b.y
        level.advance([b])
        level.step_racer(b, action)
        level.trigger(b.ground)
        self.tick += 1
        self.climb_ticks += int(b.wall >= 0 and len(action) > 2 and bool(action[2]))
        if b.feet >= level.height or b.y < -350:
            self.death_cause = 'brambles'
        elif any(axe.hits(before, (b.x, b.y), self.tick) for axe in level.axes):
            self.death_cause = 'blade'
        if self.death_cause:
            self.stop('fall')  # Compatible archive outcome; cause adds context.
            self.events.append(('death', 0, b.cx, min(b.feet, level.height)))
        else:
            for i, c in enumerate(level.coins):
                if self.owners[i] < 0 and level.platforms[c.platform].active and overlap(b.x, b.y, BW, BH, c.x - 8, c.y - 8, 16, 16):
                    self.owners[i] = 0
                    self.score[0] += c.value
                    self.gold += int(c.value > 1)
                    self.events.append(('gold' if c.value > 1 else 'coin', 0, c.x, c.y))
            if self.markers < 2:
                p = level.fixed[level.marker_platforms[self.markers]]
                if overlap(b.x, b.y, BW, BH, p.cx - 26, p.y - 80, 52, 80):
                    self.markers += 1
                    self.events.append(('marker', 0, p.cx, p.y - 55))
            p = level.fixed[level.exit_platform]
            if self.markers == 2 and overlap(b.x, b.y, BW, BH, p.cx - 28, p.y - 86, 56, 86):
                self.success = True
                self.stop('finish')
                self.events.append(('finish', 0, p.cx, p.y - 50))
            elif self.tick >= self.limit_ticks:
                self.stop('timeout')
                self.events.append(('timeout', 0, b.cx, b.y))
        self.frames.append(sample(b, self.score[0], self.markers))
        self.grips.append(b.wall >= 0)

    def replay(self):
        return {**super().replay(), 'cause': self.death_cause, 'grips': self.grips[:], 'level_id': self.level.level_id}
