"""Five authored rounds, dynamic surfaces and a fair contact score pool.

All times are 60 Hz ticks. The same step_racer and platform clock run in play
and in maneuver prediction. Fixed routes reserve the entire moving corridor.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import copy
import heapq
import math
import random
import time

from core import (BH, BW, FPS, MAX_JUMPS, IDLE, Body,
                  Platform, Coin, Level, Bot, overlap, step_body,
                  make_maneuver, body_matches, strong_components)

ROUND_TICKS = 180 * FPS
from worlds import WORLDS, world_for, level_setup

TITLES = tuple(w.name for w in WORLDS)
HINTS = tuple(w.hint for w in WORLDS)


@dataclass
class Racer(Body):
    knock_x: float = 0.0


@dataclass(frozen=True)
class Surface(Platform):
    dx: float = 0.0
    dy: float = 0.0
    active: bool = True


@dataclass(frozen=True)
class Motion:
    base: Platform
    dock: int
    end_x: float = 0.0
    end_y: float = 0.0
    period: int = 336
    phase: int = 0

    def pose(self, tick):
        if self.base.kind == "fall":
            return Surface(self.base.x, self.base.y, self.base.w, self.base.h, "fall")
        # Short endpoint pauses and eased reversals, with constant cycle length.
        q = (tick + self.phase) % self.period
        half, pause = self.period / 2, 24
        reverse = q >= half
        u = min(1.0, max(0.0, ((q % half) - pause) / (half - pause)))
        u = (1 - math.cos(math.pi * u)) / 2
        if reverse:
            u = 1 - u
        return Surface(self.base.x + self.end_x * u, self.base.y + self.end_y * u,
                       self.base.w, self.base.h, self.base.kind)

    @property
    def envelope(self):
        p = self.base
        return Platform(min(p.x, p.x + self.end_x), min(p.y, p.y + self.end_y),
                        p.w + abs(self.end_x), p.h + abs(self.end_y), "reserved")


def authored_points(number):
    """Menu overview of the same authored setup used in the live level."""
    setup = level_setup(number)
    return [(p.x + p.w / 2, p.y) for p in setup.platforms], (setup.width, setup.height)


class CampaignLevel:
    is_campaign = True
    reindex = Level.reindex

    def __init__(self, number=1, seed=42):
        if number not in range(1, 6):
            raise ValueError("Campaign level must be 1 through 5")
        self.number, self.seed = number, seed
        self.world = world_for(number)
        self.physics = self.world.physics
        self.title, self.hint = TITLES[number - 1], HINTS[number - 1]
        self.style = ("scatter", "rift", "spire", "rift", "spire")[number - 1]
        setup = level_setup(number)
        self.width, self.height = setup.width, setup.height
        self.fixed = [Surface(p.x, p.y, p.w, p.h, p.kind) for p in setup.platforms]
        self.fixed_count = len(self.fixed)
        self.starts = [min(range(self.fixed_count), key=lambda i: self.fixed[i].cx),
                       max(range(self.fixed_count), key=lambda i: self.fixed[i].cx)]
        for i in self.starts:
            p = self.fixed[i]
            self.fixed[i] = replace(p, x=p.cx - 90, w=180, kind="hub")
        self.motions: dict[int, Motion] = {}
        self._place_branches(setup.moving, setup.falling)
        self.playable = set(range(self.fixed_count + len(self.motions)))
        self.route_platforms = set(range(self.fixed_count))
        # Mystery boosts stay in classic mode. The campaign has one readable,
        # shared prize instead of an unlimited refill of randomly awarded points.
        self.box_platforms = []
        self.reset()

    def _place_branches(self, moving, falling, safe_edges=None, progress=None):
        # Place branches in the interior of the loops, away from their permanent
        # perimeter. Candidate corridors are rejected if they touch fixed decks.
        candidates = []
        for dock, p in enumerate(self.fixed):
            for side in (-1, 1):
                for dy in (-155, -215, 155):
                    for vertical in (False, True):
                        x = p.cx + side * 205 - 68
                        y = p.y + dy
                        base = Platform(x, y, 136, 16, "lift" if vertical else "shuttle")
                        motion = Motion(base, dock, 0 if vertical else side * 160,
                                        -160 if vertical else 0, phase=(dock * 37) % 336)
                        e = motion.envelope
                        if e.x < 75 or e.x + e.w > self.width - 75 or e.y < 100 or e.y + e.h > self.height - 110:
                            continue
                        if any(overlap(e.x - 38, e.y - BH - 12, e.w + 76, e.h + BH + 24,
                                       q.x, q.y, q.w, q.h) for q in self.fixed):
                            continue
                        # Prefer interior branches, spread evenly across the map.
                        centre = (self.width / 4 if p.cx < self.width / 2 else self.width * 3 / 4)
                        cost = abs(e.x + e.w / 2 - centre) + abs(dy) * .15
                        candidates.append((cost, dock, motion))
        used_docks = set()
        branch_paths = []
        for k in range(moving + falling):
            want_fall = k >= moving
            want_vertical = not want_fall and self.number >= 3 and k % 2 == 1
            options = []
            for cost, dock, m in candidates:
                if dock in used_docks or (m.base.kind == "lift") != want_vertical:
                    continue
                if want_fall:
                    m = Motion(replace(m.base, w=120, kind="fall"), dock)
                e = m.envelope
                if any(overlap(e.x - 65, e.y - 50, e.w + 130, e.h + 100,
                               q.envelope.x, q.envelope.y, q.envelope.w, q.envelope.h)
                       for q in self.motions.values()):
                    continue
                options.append((cost, dock, m))
            if not options:
                raise RuntimeError(f"No clear corridor for branch {k} in level {self.number}")
            selected = None
            for _, dock, m in sorted(options, key=lambda row: (row[0], row[1])):
                if safe_edges is not None:
                    e = m.envelope
                    if any(overlap(b.x - 2, b.y - 2, BW + 4, BH + 4, e.x, e.y, e.w, e.h)
                           for hop in branch_paths for b in hop.before + [hop.end]):
                        continue
                    retained = [{v: hop for v, hop in row.items()
                                 if not any(overlap(b.x - 2, b.y - 2, BW + 4, BH + 4, e.x, e.y, e.w, e.h)
                                            for b in hop.before + [hop.end])}
                                for row in safe_edges]
                    if len(strong_components(retained, range(self.fixed_count))) != 1:
                        continue
                    candidate_id = self.fixed_count + len(self.motions)
                    self.motions[candidate_id] = m
                    self.reset()
                    ways = set()
                    new_paths = []
                    for phase in range(0, m.period, 42):
                        future = self.fork()
                        for _ in range(phase):
                            future.advance((), rewards=False)
                        for src, dst in ((dock, candidate_id), (candidate_id, dock)):
                            if (src, dst) not in ways:
                                hop = make_maneuver(future.spawn(src), dst, future)
                                if hop:
                                    ways.add((src, dst))
                                    new_paths.append(hop)
                        if len(ways) == 2:
                            break
                    del self.motions[candidate_id]
                    if len(ways) != 2:
                        continue
                selected = dock, m
                if safe_edges is not None:
                    safe_edges = retained
                    branch_paths.extend(new_paths)
                break
            if selected is None:
                raise RuntimeError(f"Level {self.number}: cannot preserve fixed routes for branch {k}")
            dock, m = selected
            self.motions[self.fixed_count + len(self.motions)] = m
            used_docks.add(dock)
            if progress:
                progress((k + 1) / (moving + falling))
        return safe_edges

    def reset(self):
        self.tick = 0
        self.fall_at: dict[int, int] = {}
        self.relocated: set[int] = set()
        self.platforms = self.fixed[:] + [m.pose(0) for m in self.motions.values()]
        self.previous = self.platforms[:]
        self.coins = []
        for i, p in enumerate(self.platforms):
            count = 5 if p.kind == "hub" else 4 if self.number == 1 else 3
            if i >= self.fixed_count:
                count = 3
            for k in range(count):
                self.coins.append(Coin(p.x + 18 + (p.w - 36) * k / (count - 1), p.y - 13, i,
                                       2 if p.kind in ("fall", "lift") else 1))
        self.coin_offsets = [(c.x - self.platforms[c.platform].x, c.y - self.platforms[c.platform].y)
                             for c in self.coins]
        self.index_coins()
        # The spatial hash contains fixed surfaces only; moving surfaces are few.
        current = self.platforms
        self.platforms = self.fixed
        self.reindex()
        self.platforms = current

    def index_coins(self):
        self.platform_coins = [[] for _ in self.platforms]
        for i, c in enumerate(self.coins):
            self.platform_coins[c.platform].append(i)

    def spawn(self, platform):
        p = self.platforms[platform]
        return Racer(p.cx - BW / 2, p.y - BH, ground=platform)

    def nearby(self, x, y, w=BW, h=BH):
        ids = set()
        for gx in range(int(x // 128), int((x + w) // 128) + 1):
            for gy in range(int(y // 128), int((y + h) // 128) + 1):
                ids.update(self._spatial.get((gx, gy), ()))
        ids.update(i for i in self.motions if self.platforms[i].active)
        return sorted(ids)

    def fork(self):
        other = copy.copy(self)
        other.platforms = self.platforms[:]
        other.previous = self.previous[:]
        other.fall_at = self.fall_at.copy()
        # Predicting movement does not predict coin ownership.
        return other

    def aim_platform(self, target, body):
        p = self.platforms[target]
        if target in self.motions and p.kind == "shuttle":
            eta = min(45, max(6, round(abs(p.cx - body.cx) / self.physics.speed)))
            return self.motions[target].pose(self.tick + eta)
        return p

    def trigger(self, platform):
        if platform in self.motions and self.platforms[platform].kind == "fall":
            self.fall_at.setdefault(platform, self.tick + 60)

    def advance_prediction(self, body):
        self.advance([body], rewards=False)

    def advance(self, bodies=(), *, rewards=True):
        self.tick += 1
        self.previous = self.platforms[:]
        departed = []
        for i, m in self.motions.items():
            p = m.pose(self.tick)
            fall = self.fall_at.get(i)
            if fall is not None and self.tick >= fall:
                elapsed = self.tick - fall
                if elapsed >= 300 and not any(overlap(b.x, b.y, BW, BH, p.x - 2, p.y - BH, p.w + 4, p.h + BH)
                                              for b in bodies):
                    del self.fall_at[i]
                else:
                    if self.previous[i].active:
                        departed.append(i)
                    p = replace(p, y=p.y + min(self.height + 200, .23 * elapsed * elapsed), active=False)
            old = self.previous[i]
            self.platforms[i] = replace(p, dx=p.x - old.x, dy=p.y - old.y)
        if rewards:
            for i in departed:
                if i in self.relocated:
                    continue
                self.relocated.add(i)
                dock = self.motions[i].dock
                p = self.platforms[dock]
                ids = self.platform_coins[i][:]
                for k, ci in enumerate(ids):
                    self.coins[ci] = replace(self.coins[ci], x=p.x + 24 + k * (p.w - 48) / max(1, len(ids) - 1),
                                             y=p.y - 13, platform=dock)
                self.index_coins()
            for ci, c in enumerate(self.coins):
                if c.platform in self.motions:
                    p = self.platforms[c.platform]
                    dx, dy = self.coin_offsets[ci]
                    self.coins[ci] = replace(c, x=p.x + dx, y=p.y + dy)
        return departed

    def step_racer(self, b, action):
        direction, jump = action
        previous_feet = b.feet
        old_ground = b.ground
        # Carry by the actual displacement before player motion, including the
        # takeoff tick. Dynamic surfaces are one-way; they cannot crush a rider.
        if old_ground in self.motions:
            p = self.platforms[old_ground]
            if p.active and self.previous[old_ground].active:
                b.x += p.dx
                b.y += p.dy
        vx = max(-1, min(1, direction)) * self.physics.speed + b.knock_x
        b.knock_x *= .78
        if abs(b.knock_x) < .025:
            b.knock_x = 0
        if direction:
            b.facing = 1 if direction > 0 else -1
        if jump and b.jumps > 0:
            b.vy, b.jumps = self.physics.jump, b.jumps - 1
        b.vy = min(self.physics.terminal, b.vy + self.physics.gravity)
        b.x += vx
        for i in self.nearby(b.x, b.y):
            p = self.platforms[i]
            if i < self.fixed_count and overlap(b.x, b.y, BW, BH, p.x, p.y, p.w, p.h):
                if vx > 0:
                    b.x = p.x - BW
                elif vx < 0:
                    b.x = p.x + p.w
        b.x = min(self.width - BW, max(0, b.x))
        b.ground = -1
        steps = max(1, math.ceil(abs(b.vy) / 8))
        dy = b.vy / steps
        for s in range(steps):
            old_feet = b.feet
            b.y += dy
            for i in self.nearby(b.x, b.y):
                p = self.platforms[i]
                if i < self.fixed_count:
                    if not overlap(b.x, b.y, BW, BH, p.x, p.y, p.w, p.h):
                        continue
                    if dy < 0:
                        b.y, b.vy = p.y + p.h, 0
                        return
                    if dy <= 0:
                        continue
                else:
                    was_rider = old_ground == i and self.previous[i].active
                    if not (b.x < p.x + p.w and b.x + BW > p.x):
                        continue
                    # Relative crossing includes a lift moving upward into feet.
                    before_top = self.previous[i].y
                    if was_rider:
                        crossing = old_feet <= p.y + .01 and b.feet >= p.y and dy >= 0
                    else:
                        crossing = (previous_feet <= before_top + .01 and b.feet >= p.y
                                    and b.vy >= p.dy and not jump)
                    if not crossing:
                        continue
                b.y, b.vy, b.ground, b.jumps = p.y - BH, 0, i, MAX_JUMPS
                return


class FixedWorld(Level):
    """Static verification world. Reserved corridors are solid obstacles."""
    def __init__(self, level, reserved=True):
        self.width, self.height = level.width, level.height
        self.physics = level.physics
        self.platforms = level.fixed[:] + ([m.envelope for m in level.motions.values()] if reserved else [])
        self.reindex()


class CampaignNavigation:
    def __init__(self, level, progress=None):
        started = time.perf_counter()
        self.level = level
        n, fixed = len(level.platforms), level.fixed_count
        self.edges = [{} for _ in range(n)]
        self.easy_edges = [{} for _ in range(n)]
        self.jump_budget = level_setup(level.number).jump_budget
        world = FixedWorld(level, reserved=False)
        for i, p in enumerate(level.fixed):
            candidates = sorted((j for j, q in enumerate(level.fixed) if j != i and
                                 abs(q.cx - p.cx) < 670 and -390 < q.y - p.y < 550),
                                key=lambda j: abs(level.fixed[j].cx - p.cx) + abs(level.fixed[j].y - p.y))
            for j in candidates[:14]:
                m = make_maneuver(world.spawn(i), j, world)
                if m and sum(a[1] for a in m.actions) <= self.jump_budget:
                    self.edges[i][j] = m
                    self.easy_edges[i][j] = m
            if progress:
                progress(.45 * (i + 1) / fixed)
        components = strong_components(self.easy_edges, range(fixed))
        if len(components) != 1:
            raise RuntimeError(f"Level {level.number}: permanent routes disconnected: {[len(c) for c in components]}")
        # Reserve real flight corridors before accepting any moving surface.
        moving = sum(m.base.kind != "fall" for m in level.motions.values())
        falling = len(level.motions) - moving
        level.motions = {}
        self.easy_edges = level._place_branches(moving, falling, self.easy_edges,
                                               (lambda p: progress(.45 + .35 * p)) if progress else None)
        self.edges = [row.copy() for row in self.easy_edges]
        level.reset()
        self.windows = {}
        self.exits = {}
        # A known departure window in both directions, using the actual moving
        # surface. The live bot recompiles for its actual position and phase.
        for k, (i, motion) in enumerate(level.motions.items()):
            found = {}
            for phase in range(0, motion.period, 12):
                future = level.fork()
                for _ in range(phase):
                    future.advance((), rewards=False)
                for src, dst in ((motion.dock, i), (i, motion.dock)):
                    if (src, dst) not in found:
                        m = make_maneuver(future.spawn(src), dst, future)
                        if m and m.actions:
                            found[src, dst] = (phase, m)
                if len(found) == 2:
                    break
            if len(found) != 2:
                raise RuntimeError(f"Level {level.number}: branch {i} lacks a timed boarding/return route")
            for (src, dst), (phase, m) in found.items():
                self.edges[src][dst] = m
                self.windows[src, dst] = phase
            # Link the branch onward to another fixed landing, so it can be a
            # useful shortcut instead of always forcing a trip back to its dock.
            endpoint = motion.pose(motion.period // 2 - motion.phase)
            neighbours = sorted((j for j in range(fixed) if j != motion.dock), key=lambda j:
                                abs(level.fixed[j].cx - endpoint.cx) + abs(level.fixed[j].y - endpoint.y))[:6]
            for dst in neighbours:
                linked = False
                for phase in range(0, motion.period, 42):
                    future = level.fork()
                    for _ in range(phase):
                        future.advance((), rewards=False)
                    m = make_maneuver(future.spawn(i), dst, future)
                    if m and m.actions:
                        self.edges[i][dst] = m
                        self.windows[i, dst] = phase
                        self.exits[i] = dst
                        # A second boarding direction is useful when available.
                        reverse = make_maneuver(future.spawn(dst), i, future)
                        if reverse and reverse.actions:
                            self.edges[dst][i] = reverse
                            self.windows[dst, i] = phase
                        linked = True
                        break
                if linked:
                    break
            if i not in self.exits:
                raise RuntimeError(f"Level {level.number}: branch {i} has no onward fixed landing")
            if progress:
                progress(.8 + .2 * (k + 1) / len(level.motions))
        self.dist, self.first = [], []
        for src in range(n):
            dist, first = [float("inf")] * n, [-1] * n
            dist[src] = 0
            heap = [(0, src)]
            while heap:
                cost, u = heapq.heappop(heap)
                if dist[u] != cost:
                    continue
                for v, m in self.edges[u].items():
                    new = cost + len(m.actions) + (24 if v >= fixed else 0)
                    if new < dist[v]:
                        dist[v], first[v] = new, v if u == src else first[u]
                        heapq.heappush(heap, (new, v))
            self.dist.append(dist)
            self.first.append(first)
        self.playable = level.playable
        self.route_platforms = level.route_platforms
        self.connected = all(math.isfinite(d) for row in self.dist for d in row)
        self.build_seconds = time.perf_counter() - started
        if progress:
            progress(1)

    def anchor(self, b):
        if b.ground >= 0:
            return b.ground
        return min(range(self.level.fixed_count), key=lambda i:
                   abs(self.level.fixed[i].cx - b.cx) + abs(self.level.fixed[i].y - b.feet))

    def next_hop(self, src, target):
        # A absent/reforming crumble is never required while the fixed bypass
        # is open. Predicted shuttle movement remains eligible for timed hops.
        heap, seen = [(0, src, -1)], set()
        while heap:
            cost, u, first = heapq.heappop(heap)
            if u == target:
                return first
            if u in seen:
                continue
            seen.add(u)
            for v, m in self.edges[u].items():
                p = self.level.platforms[v]
                fall = self.level.fall_at.get(v)
                if not p.active or (fall is not None and fall - self.level.tick < 45):
                    continue
                heapq.heappush(heap, (cost + len(m.actions) + (24 if v in self.level.motions else 0),
                                     v, v if u == src else first))
        return -1


@dataclass
class Token:
    x: float
    y: float
    platform: int
    ready: int
    value: int = 1


class CampaignBot(Bot):
    def __init__(self, nav, difficulty="hard", player=1):
        super().__init__(nav, difficulty, player)
        self.waits = 0
        self.dynamic_landings = set()

    def choose_target(self, match, b, opponent):
        started = time.perf_counter()
        src, other = b.ground, self.nav.anchor(opponent)
        rewards = {}
        for i, c in enumerate(self.level.coins):
            if match.owners[i] < 0 and self.level.platforms[c.platform].active:
                rewards[c.platform] = rewards.get(c.platform, 0) + c.value
        for c in match.tokens:
            rewards[c.platform] = rewards.get(c.platform, 0) + c.value
        if match.bonus:
            rewards[match.bonus.platform] = rewards.get(match.bonus.platform, 0) + 5
        candidates = []
        for i, value in rewards.items():
            if i == src:
                continue
            eta = self.nav.dist[src][i]
            if not math.isfinite(eta):
                continue
            # Compare rival ETA without assuming that the rival will choose it.
            contest = .7 if self.nav.dist[other][i] + 30 < eta else 1.0
            future = sorted((v / (70 + self.nav.dist[i][j]) for j, v in rewards.items() if j != i), reverse=True)
            horizon = {"easy": 0, "medium": 1, "hard": 2}[self.difficulty]
            utility = value * contest / (45 + eta) + .35 * sum(future[:horizon])
            candidates.append((utility, -eta, -i))
        self.plan_ms.append((time.perf_counter() - started) * 1000)
        self.decisions += 1
        return -max(candidates)[2] if candidates else -1

    def act(self, match):
        b = match.bodies[self.player]
        if self.queue:
            action, expected = self.queue[0]
            if body_matches(b, expected) and abs(b.knock_x - getattr(expected, "knock_x", 0)) < .05:
                self.queue.popleft()
                return action
            self.invalidate()
            self.repairs += 1
        if self.cooldown:
            self.cooldown -= 1
            return IDLE
        if b.ground < 0:
            candidates = sorted(range(self.level.fixed_count), key=lambda i:
                                abs(self.level.fixed[i].cx - b.cx) + abs(self.level.fixed[i].y - b.feet))[:7]
            for i in candidates:
                m = make_maneuver(b, i, self.level)
                if m and m.actions:
                    self.commit(m)
                    return self.queue.popleft()[0]
            self.cooldown = 10
            return IDLE
        if b.ground >= self.level.fixed_count:
            self.dynamic_landings.add(b.ground)
        fall = self.level.fall_at.get(b.ground)
        urgent = fall is not None and fall - self.level.tick < 35
        local = [self.level.coins[i] for i in self.level.platform_coins[b.ground] if match.owners[i] < 0]
        local += [t for t in match.tokens if t.platform == b.ground]
        if match.bonus and match.bonus.platform == b.ground:
            local.append(match.bonus)
        if local and not urgent:
            c = min(local, key=lambda c: abs(c.x - b.cx))
            return (max(-1, min(1, (c.x - b.cx) / self.level.physics.speed)), False)
        delta = self.level.platforms[b.ground].cx - b.cx
        if abs(delta) > .01 and not urgent:
            return (max(-1, min(1, delta / self.level.physics.speed)), False)
        if b.ground in self.level.motions:
            dest = self.choose_target(match, b, match.bodies[1 - self.player])
            nxt = self.nav.next_hop(b.ground, dest) if dest >= 0 else self.level.motions[b.ground].dock
        else:
            dest = self.choose_target(match, b, match.bodies[1 - self.player])
            self.last_route = [b.ground, dest]
            if dest < 0:
                return IDLE
            nxt = self.nav.next_hop(b.ground, dest)
        m = make_maneuver(b, nxt, self.level) if nxt >= 0 else None
        if m and m.actions:
            self.commit(m)
            if self.difficulty == "easy":
                self.cooldown = 16
            return self.queue.popleft()[0]
        # Fixed deck or riding surface remains a safe place to await the next
        # departure phase. A crack warning instead asks for a real fixed escape.
        if urgent:
            for dst in sorted(range(self.level.fixed_count), key=lambda j:
                              abs(self.level.fixed[j].cx - b.cx) + abs(self.level.fixed[j].y - b.feet))[:6]:
                m = make_maneuver(b, dst, self.level)
                if m and m.actions:
                    self.commit(m)
                    return self.queue.popleft()[0]
        self.waits += 1
        self.cooldown = 11
        return IDLE


def contact_kind(before, after):
    """Swept relative AABB: return stomp attacker, or -1 for a side contact."""
    a, b = before
    aa, bb = after
    dx, dy = a.x - b.x, a.y - b.y
    vx, vy = (aa.x - a.x) - (bb.x - b.x), (aa.y - a.y) - (bb.y - b.y)
    enter, leave = 0.0, 1.0
    vertical_entry = -float("inf")
    for pos, vel, extent in ((dx, vx, BW), (dy, vy, BH)):
        if abs(vel) < 1e-9:
            if not -extent < pos < extent:
                return None
        else:
            low, high = sorted(((-extent - pos) / vel, (extent - pos) / vel))
            if extent == BH:
                vertical_entry = low
            enter, leave = max(enter, low), min(leave, high)
            if enter > leave:
                return None
    if a.feet <= b.y + 1 and vy > 0 and vertical_entry >= enter - 1e-8:
        return 0
    if b.feet <= a.y + 1 and vy < 0 and vertical_entry >= enter - 1e-8:
        return 1
    return -1


class CampaignMatch:
    limit_ticks = ROUND_TICKS

    def __init__(self, level, nav, difficulty="hard", bot_both=False, solo_bot=False):
        level.reset()
        self.level, self.nav, self.difficulty = level, nav, difficulty
        self.starts = level.starts[:]
        self.bodies = [level.spawn(i) for i in self.starts]
        self.owners, self.score = [-1] * len(level.coins), [0, 0]
        self.freeze, self.double, self.magnet = [0, 0], [0, 0], [0, 0]
        self.stun, self.protection = [0, 0], [75, 75]
        self.freeze_used, self.box_ready = set(), {}
        self.respawns = [0, 0]
        self.bots = [CampaignBot(nav, difficulty, 0) if bot_both else None, CampaignBot(nav, difficulty, 1)]
        self.tick, self.over, self.events = 0, False, []
        self.tokens = []
        self.bonus = None
        self.bonus_until = 0
        self.solo_bot = solo_bot
        self.contacts = 0

    @property
    def remaining(self):
        return self.owners.count(-1)

    def set_difficulty(self, value):
        self.difficulty = value
        for bot in self.bots:
            if bot:
                bot.difficulty = value

    def safe_anchor(self, b):
        return min(range(self.level.fixed_count), key=lambda i:
                   abs(self.level.fixed[i].cx - b.cx) + abs(self.level.fixed[i].y - b.feet))

    def drop(self, player, count):
        count = min(count, self.score[player])
        self.score[player] -= count
        b = self.bodies[player]
        dock = self.safe_anchor(b)
        p = self.level.fixed[dock]
        for k in range(count):
            self.tokens.append(Token(p.cx + (k - (count - 1) / 2) * 24, p.y - 13, dock, self.tick + 24))

    def respawn(self, player):
        self.drop(player, 3)
        dock = self.safe_anchor(self.bodies[player])
        self.bodies[player] = self.level.spawn(dock)
        self.protection[player], self.stun[player] = 75, 0
        self.respawns[player] += 1
        if self.bots[player]:
            self.bots[player].invalidate()
        self.events.append(("respawn", player, self.bodies[player].cx, self.bodies[player].y))

    def resolve_contact(self, before):
        if self.solo_bot or any(self.protection):
            return
        kind = contact_kind(before, self.bodies)
        if kind is None:
            return
        self.contacts += 1
        left = 0 if before[0].cx <= before[1].cx else 1
        if kind == -1:
            for p in (0, 1):
                self.drop(p, 1)
                self.stun[p] = 8
                self.bodies[p].knock_x = -4 if p == left else 4
            self.events.append(("bump", 0, self.bodies[0].cx, self.bodies[0].y))
        else:
            defender = 1 - kind
            self.drop(defender, 3)
            self.bodies[kind].vy, self.bodies[kind].ground = self.level.physics.jump * .85, -1
            self.stun[defender] = 8
            self.bodies[defender].knock_x = -4 if defender == left else 4
            self.events.append(("stomp", kind, self.bodies[defender].cx, self.bodies[defender].y))
        for p in (0, 1):
            self.protection[p] = 75
            self.bodies[p].jumps = max(1, self.bodies[p].jumps)
            if self.bots[p]:
                self.bots[p].invalidate()

    def collector(self, c):
        # Alternate simultaneous ties each tick, rather than granting either
        # racer a permanent pickup priority.
        for p in ((0, 1) if self.tick % 2 == 0 else (1, 0)):
            if self.solo_bot and p == 0:
                continue
            b = self.bodies[p]
            if overlap(b.x, b.y, BW, BH, c.x - 8, c.y - 8, 16, 16):
                return p
        return None

    def step(self, human_action=IDLE):
        if self.over:
            return
        self.events = []
        before = [replace(b) for b in self.bodies]
        actions = []
        for p in (0, 1):
            self.protection[p] = max(0, self.protection[p] - 1)
            self.double[p] = max(0, self.double[p] - 1)
            if self.stun[p]:
                self.stun[p] -= 1
                actions.append(IDLE)
            else:
                actions.append(self.bots[p].act(self) if self.bots[p] else human_action)
        departed = self.level.advance(self.bodies)
        for i in departed:
            p = self.level.fixed[self.level.motions[i].dock]
            self.events.append(("rescue", -1, p.cx, p.y - 20))
        for p in (0, 1):
            if self.solo_bot and p == 0:
                continue
            step_body(self.bodies[p], actions[p], self.level)
            self.level.trigger(self.bodies[p].ground)
        self.resolve_contact(before)
        for p in (0, 1):
            if self.solo_bot and p == 0:
                continue
            b = self.bodies[p]
            if b.feet >= self.level.height or b.y < -350:
                self.respawn(p)
        for i, c in enumerate(self.level.coins):
            if self.owners[i] >= 0:
                continue
            p = self.collector(c)
            if p is not None:
                self.owners[i] = p
                self.score[p] += c.value * (2 if self.double[p] else 1)
                self.events.append(("coin", p, c.x, c.y))
        for c in self.tokens[:]:
            p = self.collector(c) if self.tick >= c.ready else None
            if p is not None:
                self.score[p] += c.value  # Transfers are never multiplied.
                self.tokens.remove(c)
                self.events.append(("coin", p, c.x, c.y))
        if self.bonus and self.tick >= self.bonus_until:
            self.bonus = None
        if self.bonus:
            p = self.collector(self.bonus)
            if p is not None:
                self.score[p] += 5
                self.events.append(("prize", p, self.bonus.x, self.bonus.y))
                self.bonus = None
        self.tick += 1
        if self.level.number >= 2 and self.tick % 1500 == 0 and self.remaining:
            anchors = [self.nav.anchor(b) for b in self.bodies]
            # Choose a central fixed contest with balanced estimated arrival.
            candidates = sorted(range(self.level.fixed_count), key=lambda i:
                                abs(self.level.fixed[i].cx - self.level.width / 2))[:12]
            dock = min(candidates, key=lambda i: abs(self.nav.dist[anchors[0]][i] - self.nav.dist[anchors[1]][i])
                       + .3 * max(self.nav.dist[a][i] for a in anchors))
            p = self.level.fixed[dock]
            self.bonus = Token(p.cx, p.y - 18, dock, self.tick, 5)
            self.bonus_until = self.tick + 900
            self.events.append(("beacon", -1, p.cx, p.y - 35))
        self.over = self.tick >= self.limit_ticks or (self.remaining == 0 and not self.tokens)


class Campaign:
    """Round results are keyed, so a replay cannot award league points twice."""
    def __init__(self):
        self.results = {}

    def record(self, number, score):
        self.results[number] = tuple(score)

    def replay(self, number):
        self.results.pop(number, None)

    @property
    def league(self):
        pts = [0, 0]
        for a, b in self.results.values():
            pts[0] += 2 if a > b else 1 if a == b else 0
            pts[1] += 2 if b > a else 1 if a == b else 0
        return pts

    @property
    def totals(self):
        return [sum(s[p] for s in self.results.values()) for p in (0, 1)]

    @property
    def winner(self):
        a, b = [(self.league[p], self.totals[p]) for p in (0, 1)]
        return 0 if a > b else 1 if b > a else -1


def benchmark_campaign(number=1, seed=42, ticks=ROUND_TICKS, solo=False):
    level = CampaignLevel(number, seed)
    nav = CampaignNavigation(level)
    match = CampaignMatch(level, nav, bot_both=not solo, solo_bot=solo)
    started = time.perf_counter()
    for _ in range(ticks):
        match.step()
        if match.over:
            break
    bots = [b for b in match.bots if b]
    times = sorted(t for b in bots for t in b.plan_ms)
    return {"level": number, "title": level.title, "fixed": level.fixed_count,
            "moving": sum(m.base.kind != "fall" for m in level.motions.values()),
            "falling": sum(m.base.kind == "fall" for m in level.motions.values()),
            "coins": len(match.owners), "remaining": match.remaining, "loose": len(match.tokens),
            "ticks": match.tick, "score": match.score, "respawns": match.respawns,
            "contacts": match.contacts, "dynamic_visited": sorted(set().union(*(b.dynamic_landings for b in bots))),
            "waits": [b.waits for b in bots], "repairs": [b.repairs for b in bots],
            "connected": nav.connected, "build_seconds": round(nav.build_seconds, 2),
            "simulation_seconds": round(time.perf_counter() - started, 2),
            "decision_ms_p95": round(times[int(.95 * (len(times) - 1))], 3) if times else 0}
