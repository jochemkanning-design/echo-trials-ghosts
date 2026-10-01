"""Deterministic game simulation and bot. No third-party imports.

Physics is expressed in ticks at 60 Hz. The renderer never advances physics.
Navigation edges are actions verified by the same step_body used in the game.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
import heapq
import math
import random
import time
from worlds import NORMAL

FPS = 60
SPEED = NORMAL.speed
GRAVITY = NORMAL.gravity
JUMP = NORMAL.jump
MAX_JUMPS = 5
BW, BH = 22.0, 30.0
MATCH_TICKS = 5 * 60 * FPS
Action = tuple[float, bool]
IDLE: Action = (0, False)


@dataclass(frozen=True)
class Platform:
    x: float
    y: float
    w: float
    h: float = 20.0
    kind: str = "normal"

    @property
    def cx(self):
        return self.x + self.w / 2


@dataclass(frozen=True)
class Coin:
    x: float
    y: float
    platform: int
    value: int = 1


@dataclass
class Body:
    x: float
    y: float
    vy: float = 0.0
    ground: int = -1
    jumps: int = MAX_JUMPS
    facing: int = 1

    @property
    def cx(self):
        return self.x + BW / 2

    @property
    def feet(self):
        return self.y + BH


def overlap(x, y, w, h, px, py, pw, ph):
    return x < px + pw and x + w > px and y < py + ph and y + h > py


class Level:
    """Spaced landings, broad rest stops and small optional gold ledges.

    Distances are between landing surfaces, not platform centres. A broad hub
    can have closer neighbours; other jumps require more deliberate movement.
    All connections are checked by Navigation against actual collision physics.
    """
    STYLES = ("scatter", "rift", "spire")

    def __init__(self, seed=42, style="scatter", *, variant=0):
        self.seed = seed
        self.style = style if style in self.STYLES else "scatter"
        self.layout_variant = variant
        rng = random.Random(seed ^ (variant * 0x9E3779B1))
        self.width, self.height = (2500, 3300) if self.style == "spire" else (3600, 2500)
        self.platforms: list[Platform] = []
        self.coins: list[Coin] = []
        for _ in range(24000):
            if len(self.platforms) >= 72:
                break
            role = rng.choices(["route", "hub", "gold"], [66, 20, 14])[0]
            width = rng.randint(*{"route": (145, 205), "hub": (230, 275), "gold": (88, 110)}[role])
            x = rng.uniform(85, self.width - width - 85)
            y = rng.uniform(190, self.height - 180)
            cx = x + width / 2
            if self.style == "rift":
                # A winding vertical gap, crossed by intermittent stepping stones.
                middle = self.width / 2 + 245 * math.sin(y / 310)
                if abs(cx - middle) < 245 and math.sin(y / 240) < .35:
                    continue
            if self.style == "spire":
                # Alternating clusters pull the climb from one side to the other.
                centre = self.width / 2 + 370 * math.sin(y / 360)
                if abs(cx - centre) > 780 and rng.random() < .85:
                    continue
            # A rest stop provides a forgiving landing and a shorter next hop.
            # The surrounding empty space stays useful, instead of being filled
            # with equally spaced tiny platforms to satisfy a density target.
            if any(math.hypot(max(0, p.x - x - width, x - p.x - p.w), y - p.y)
                   < (150 if role == "hub" or p.w >= 230 else 185)
                   for p in self.platforms):
                continue
            kind = "gold" if role == "gold" else "freeze" if role == "hub" and rng.random() < .4 else "normal"
            self.platforms.append(Platform(round(x), round(y), width, rng.randint(16, 24), kind))
        self.platforms.sort(key=lambda p: (p.y, p.x))
        self.playable = set(range(len(self.platforms)))
        self.route_platforms = {i for i, p in enumerate(self.platforms) if p.kind != "gold"}
        self.populate(self.playable)
        self.reindex()

    def populate(self, playable):
        """Scatter 300 rewards in variable-size pockets on verified platforms."""
        self.playable = set(playable)
        rng = random.Random(self.seed ^ 0x54AF)
        ids = sorted(self.playable)
        amounts = {i: 1 for i in ids}
        capacity = {i: max(1, int((self.platforms[i].w - 28) / 23) + 1) for i in ids}
        while sum(amounts.values()) < 300:
            candidates = [i for i in ids if amounts[i] < capacity[i]]
            if not candidates:
                raise RuntimeError("Not enough connected platform space for 300 coins")
            i = rng.choices(candidates, [self.platforms[j].w * (1.6 if self.platforms[j].kind == "gold" else 1)
                                         for j in candidates])[0]
            amounts[i] += 1
        self.coins = []
        for i in ids:
            p, count = self.platforms[i], amounts[i]
            for k in range(count):
                x = p.cx if count == 1 else p.x + 16 + k * (p.w - 32) / (count - 1)
                self.coins.append(Coin(x, p.y - 13, i, 2 if p.kind == "gold" else 1))
        self.platform_coins = [[] for _ in self.platforms]
        for i, c in enumerate(self.coins):
            self.platform_coins[c.platform].append(i)
        hubs = sorted(self.route_platforms & self.playable, key=lambda i: (-self.platforms[i].w, i))[:10]
        self.box_platforms = hubs
        starts = [i for i in sorted(self.route_platforms & self.playable)
                  if self.platforms[i].w >= 230 and self.platforms[i].kind != "freeze"] or hubs
        self.starts = [max(starts, key=lambda i: self.platforms[i].y + self.platforms[i].x * .32),
                       min(starts, key=lambda i: self.platforms[i].y + self.platforms[i].x * .32)]

    def reindex(self):
        self._spatial: dict[tuple[int, int], list[int]] = {}
        for i, p in enumerate(self.platforms):
            for gx in range(int(p.x // 128), int((p.x + p.w) // 128) + 1):
                for gy in range(int(p.y // 128), int((p.y + p.h) // 128) + 1):
                    self._spatial.setdefault((gx, gy), []).append(i)

    def nearby(self, x, y, w=BW, h=BH):
        ids = set()
        for gx in range(int(x // 128), int((x + w) // 128) + 1):
            for gy in range(int(y // 128), int((y + h) // 128) + 1):
                ids.update(self._spatial.get((gx, gy), ()))
        return sorted(ids)

    def spawn(self, platform):
        p = self.platforms[platform]
        return Body(p.cx - BW / 2, p.y - BH, ground=platform)


def step_body(b: Body, action: Action, level: Level):
    """Solid AABB platforms, instantaneous horizontal control, five jumps.

    Small vertical substeps prevent tunnelling after a long fall.
    The function has no pickups, random events, teleports or AI special cases.
    """
    if hasattr(level, "step_racer"):
        return level.step_racer(b, action)
    physics = getattr(level, "physics", NORMAL)
    direction, jump = action
    vx = max(-1, min(1, direction)) * physics.speed
    if direction:
        b.facing = 1 if direction > 0 else -1
    if jump and b.jumps > 0:
        b.vy = physics.jump
        b.jumps -= 1
    b.vy = min(b.vy + physics.gravity, physics.terminal)
    b.x += vx
    for i in level.nearby(b.x, b.y):
        p = level.platforms[i]
        if overlap(b.x, b.y, BW, BH, p.x, p.y, p.w, p.h):
            if vx > 0:
                b.x = p.x - BW
            elif vx < 0:
                b.x = p.x + p.w
    b.x = max(0.0, min(level.width - BW, b.x))
    b.ground = -1
    substeps = max(1, math.ceil(abs(b.vy) / 8))
    dy = b.vy / substeps
    for _ in range(substeps):
        b.y += dy
        hit = False
        for i in level.nearby(b.x, b.y):
            p = level.platforms[i]
            if overlap(b.x, b.y, BW, BH, p.x, p.y, p.w, p.h):
                if dy > 0:
                    b.y = p.y - BH
                    b.vy = 0
                    b.ground = i
                    b.jumps = MAX_JUMPS
                    hit = True
                    break
                elif dy < 0:
                    b.y = p.y + p.h
                    b.vy = 0
                    hit = True
                    break
        if hit:
            break


def steer(delta, speed=SPEED):
    return 0 if abs(delta) <= speed / 2 + 0.01 else (1 if delta > 0 else -1)


def body_matches(a: Body, b: Body):
    return (abs(a.x - b.x) < 0.1 and abs(a.y - b.y) < 0.1
            and abs(a.vy - b.vy) < 0.1 and a.ground == b.ground and a.jumps == b.jumps)


@dataclass
class Maneuver:
    target: int
    actions: list[Action]
    before: list[Body]
    end: Body


def make_maneuver(start: Body, target: int, level: Level) -> Maneuver | None:
    """Compile and verify a complete jump, including its landing.

    Climb in the corridor between columns; move over the target only once high
    enough. Several flight variants are tried, bounded by the world's flight-time limit.
    No teleportation is used in the generated actions.
    """
    original = level
    physics = getattr(level, "physics", NORMAL)
    p = level.platforms[target]
    src = level.platforms[start.ground] if start.ground >= 0 else None
    if src is not None and start.ground == target:
        return Maneuver(target, [], [], replace(start))
    preferred_side = 1 if p.cx >= start.cx else -1
    policies = [(clearance, side, drop_limit) for drop_limit in (100, 25)
                for clearance, side in ((12.0, preferred_side), (38.0, preferred_side),
                                        (65.0, preferred_side), (38.0, -preferred_side))]
    for clearance, side, drop_limit in policies:
        level = original.fork() if hasattr(original, "fork") else original
        b = replace(start)
        actions, before = [], []
        launched = start.ground < 0
        airborne = launched
        descending = src is not None and p.y > src.y + drop_limit
        going_right = side > 0
        if src is not None:
            if src.x + src.w + BW < p.x:
                gap = (src.x + src.w + p.x) / 2
            elif p.x + p.w + BW < src.x:
                gap = (p.x + p.w + src.x) / 2
            else:
                # Overlapping ledges require going around an actual edge before
                # climbing or dropping. There are no implicit grid corridors.
                gap = max(src.x + src.w, p.x + p.w) + BW if going_right else min(src.x, p.x) - BW
        else:
            gap = p.x - BW if b.cx < p.cx else p.x + p.w + BW
        for _ in range(physics.maneuver_ticks):
            if hasattr(level, "aim_platform"):
                p = level.aim_platform(target, b)
            above = b.feet <= p.y - clearance
            over = p.x + 3 <= b.cx <= p.x + p.w - 3
            # Use an open shaft to gain altitude, then cross onto the platform.
            aim = p.cx if above or (over and b.feet <= p.y + 0.1) else gap
            if src is not None and p.y >= src.y - 25:
                aim = p.cx
            if descending:
                # Leave the current row through its open corridor. Crossing
                # too early would simply land on the adjacent upper platform.
                aim = p.cx if b.y > src.y + 55 else gap
            direction = steer(aim - b.cx, physics.speed)
            want_jump = False
            if not launched:
                # Walk clear of an overlapping upper ledge before spending the
                # first jump. Otherwise the head strike wastes the jump budget.
                under_target = (src is not None and p.y < src.y - 25
                                and b.x + BW > p.x - 5 and b.x < p.x + p.w + 5)
                want_jump = not under_target or b.ground < 0
                launched = want_jump
            elif b.vy >= -0.5 and b.jumps > 0:
                # Renew a jump before falling short of the target. Once over
                # its surface, let gravity produce the required grounded goal.
                need_height = b.feet > p.y - clearance
                need_distance = not over and b.feet > p.y - 65
                want_jump = (need_height or need_distance) and not (over and b.feet <= p.y)
            if descending:
                want_jump = False
            action = (direction, want_jump)
            before.append(replace(b))
            actions.append(action)
            if hasattr(level, "advance_prediction"):
                level.advance_prediction(b)
            step_body(b, action, level)
            if hasattr(level, "trigger"):
                level.trigger(b.ground)
            if b.ground < 0:
                airborne = True
            if airborne and b.ground >= 0:
                if b.ground == target:
                    # Walk to a reproducible endpoint on the same platform.
                    for _ in range(48):
                        p = level.platforms[target]
                        d = steer(p.cx - b.cx, physics.speed)
                        if not d:
                            break
                        before.append(replace(b))
                        actions.append((d, False))
                        if hasattr(level, "advance_prediction"):
                            level.advance_prediction(b)
                        step_body(b, (d, False), level)
                        if b.ground != target:
                            break
                    if b.ground == target:
                        return Maneuver(target, actions, before, replace(b))
                break
            if b.y > level.height + 100 or b.y < -300:
                break
    return None


def strong_components(edges, nodes=None):
    """Small directed graph; iterative searches also support filtered routes."""
    nodes = set(range(len(edges))) if nodes is None else set(nodes)
    reverse = [set() for _ in edges]
    for u in nodes:
        for v in edges[u]:
            if v in nodes:
                reverse[v].add(u)

    def visit(start, adjacency, allowed):
        seen, todo = {start}, [start]
        while todo:
            u = todo.pop()
            for v in adjacency[u]:
                if v in allowed and v not in seen:
                    seen.add(v)
                    todo.append(v)
        return seen

    components, remaining = [], set(nodes)
    while remaining:
        src = min(remaining)
        group = visit(src, edges, remaining) & visit(src, reverse, remaining)
        remaining -= group
        components.append(group)
    return sorted(components, key=lambda c: (-len(c), min(c)))


class Navigation:
    """Physics-verified routes, with an easier connected network underneath.

    Gold detours may use all five jumps. Every main platform must be reachable
    from every other main platform without gold ledges or five-jump maneuvers.
    A bad random candidate is regenerated before a race can start.
    """
    def __init__(self, level: Level, progress=None):
        self.level = level
        t0 = time.perf_counter()
        for attempt in range(8):
            if attempt:
                level.__init__(level.seed, level.style, variant=attempt)
            n = len(level.platforms)
            self.edges: list[dict[int, Maneuver]] = [{} for _ in range(n)]
            for i, p in enumerate(level.platforms):
                candidates = [j for j, q in enumerate(level.platforms) if i != j
                              and abs(q.cx - p.cx) < 660 and -420 < q.y - p.y < 550]
                candidates.sort(key=lambda j: abs(level.platforms[j].cx - p.cx)
                                + abs(level.platforms[j].y - p.y) * 1.25)
                for j in candidates[:20]:
                    m = make_maneuver(level.spawn(i), j, level)
                    if m is not None:
                        self.edges[i][j] = m
                if progress:
                    progress((i + 1) / n)
            main = {i for i, p in enumerate(level.platforms) if p.kind != "gold"}
            self.easy_edges = [{j: m for j, m in edges.items()
                                if j in main and sum(a[1] for a in m.actions) < MAX_JUMPS}
                               if i in main else {} for i, edges in enumerate(self.edges)]
            components = strong_components(self.edges)
            easy_components = strong_components(self.easy_edges, main)
            self.playable = components[0]
            self.route_platforms = easy_components[0] if easy_components else set()
            branching = sum(len(set(self.easy_edges[i]) & self.route_platforms) >= 2
                            for i in self.route_platforms)
            start_hubs = sum(level.platforms[i].w >= 230 and level.platforms[i].kind != "freeze"
                             for i in self.route_platforms)
            # Do not hide rewards on a large component while leaving stray
            # unreachable collision platforms on the map. Accept the whole map.
            self.connected = (len(self.playable) == n and n >= 64
                              and len(self.route_platforms) >= n * .75
                              and branching >= len(self.route_platforms) * .75 and start_hubs >= 2)
            if self.connected:
                break
        else:
            raise RuntimeError(f"Could not build connected routes for {level.style} seed {level.seed}. Try another seed.")
        self.easy_edges = [{j: m for j, m in edges.items() if j in self.route_platforms}
                           if i in self.route_platforms else {} for i, edges in enumerate(self.easy_edges)]
        # A difficult peripheral landing becomes a gold detour. Changing colour
        # and reward does not change geometry or invalidate verified trajectories.
        level.route_platforms = self.route_platforms
        level.platforms = [replace(p, kind="gold") if i not in self.route_platforms else p
                           for i, p in enumerate(level.platforms)]
        level.populate(self.playable)
        self.dist, self.first = [], []
        for src in range(n):
            dist = [float("inf")] * n
            first = [-1] * n
            dist[src] = 0.0
            heap = [(0, src)]
            while heap:
                cost, u = heapq.heappop(heap)
                if cost != dist[u]:
                    continue
                for v, m in self.edges[u].items():
                    new = cost + len(m.actions)
                    if new < dist[v]:
                        dist[v] = new
                        first[v] = v if u == src else first[u]
                        heapq.heappush(heap, (new, v))
            self.dist.append(dist)
            self.first.append(first)
        self.build_seconds = time.perf_counter() - t0

    def anchor(self, b: Body):
        if b.ground >= 0:
            return b.ground
        # Approximation for opponent ETA only. Actual moves require verification.
        candidates = [(abs(p.cx - b.cx) + abs(p.y - b.feet) * 1.3, i)
                      for i, p in enumerate(self.level.platforms)
                      if p.y >= b.feet - 45]
        return min(candidates)[1] if candidates else 0


class Bot:
    """Checkpoint-based execution with short route lookahead.

    No worker replies can go stale: high-level decisions use a small graph.
    Geometry/action compilation is done once per hop. Ownership changes never
    cancel an airborne maneuver. Freeze/respawn invalidate it explicitly.
    """
    def __init__(self, nav: Navigation, difficulty="hard", player=1):
        self.nav, self.level = nav, nav.level
        self.difficulty, self.player = difficulty, player
        self.queue: deque[tuple[Action, Body]] = deque()
        self.target = -1
        self.expected_end = None
        self.cooldown = 0
        self.decisions = 0
        self.repairs = 0
        self.plan_ms: list[float] = []
        self.last_route: list[int] = []

    def invalidate(self):
        self.queue.clear()
        self.expected_end = None
        self.target = -1
        self.cooldown = 0

    def commit(self, m):
        self.queue = deque(zip(m.actions, m.before))
        self.expected_end = m.end

    def _eta(self, b, target, match, player):
        anchor = self.nav.anchor(b)
        offset = abs(b.cx - self.level.platforms[anchor].cx) / SPEED
        return self.nav.dist[anchor][target] + offset + match.freeze[player]

    def choose_target(self, match, b, opponent):
        """Beam search over up to three coin-rich platforms.

        Rewards count individual unowned coins once. Gold and active boost
        values matter. Opponent arrival probability is a heuristic, not a
        claim to predict human intentions exactly.
        """
        t0 = time.perf_counter()
        src = b.ground
        pme, other = self.player, 1 - self.player
        active = [i for i, ids in enumerate(self.level.platform_coins)
                  if any(match.owners[k] < 0 for k in ids)]
        human_eta = {p: self._eta(opponent, p, match, other) for p in active}
        root_offset = abs(b.cx - self.level.platforms[src].cx) / SPEED
        # Beam state: utility, elapsed time, reward, endpoint, visited platforms, route.
        beam = [(0.0, root_offset, 0.0, src, frozenset(), [])]
        best = None
        depth = {"easy": 1, "medium": 2, "hard": 3}[self.difficulty]
        for _ in range(depth):
            expanded = []
            for _, elapsed, reward, current, visited, route in beam:
                nexts = []
                for p in active:
                    if p in visited or not math.isfinite(self.nav.dist[current][p]):
                        continue
                    ids = [k for k in self.level.platform_coins[p] if match.owners[k] < 0]
                    # Include the cost of sweeping the platform, not just arriving.
                    xs = [self.level.coins[k].x for k in ids]
                    sweep = ((max(xs) - min(xs)) + min(abs(min(xs) - self.level.platforms[p].cx),
                              abs(max(xs) - self.level.platforms[p].cx))) / SPEED
                    travel = self.nav.dist[current][p]
                    arrival = elapsed + travel
                    endtime = arrival + sweep + 3
                    if endtime > 720 and route:
                        continue
                    gain = 0.0
                    for k in ids:
                        coin = self.level.coins[k]
                        prob = 1.0
                        if self.difficulty != "easy":
                            adv = max(-30.0, min(30.0, (human_eta[p] - arrival) / 85.0))
                            prob = 0.2 + 0.8 / (1.0 + math.exp(-adv))
                        value = coin.value * (2 if match.double[pme] > arrival else 1)
                        # Coins an opponent could soon take deserve extra urgency.
                        urgency = 0.0
                        if self.difficulty == "hard":
                            urgency = 0.35 * math.exp(-human_eta[p] / 180) * prob
                        gain += value * (prob + urgency)
                    if self.difficulty == "hard":
                        plat = self.level.platforms[p]
                        if plat.kind == "freeze" and p not in match.freeze_used:
                            gain += 0.8
                        if p in self.level.box_platforms and match.box_ready[p] <= match.tick + arrival:
                            gain += 1.2
                    # Discounted gain favours real points soon, with lookahead
                    # accounting for a valuable continuation after the first stop.
                    total = reward + gain * math.exp(-endtime / 400)
                    utility = total - 0.003 * endtime
                    state = (utility, endtime, total, p, visited | {p}, route + [p])
                    nexts.append(state)
                nexts.sort(key=lambda s: s[0], reverse=True)
                expanded.extend(nexts[:6])
            if not expanded:
                break
            expanded.sort(key=lambda s: s[0], reverse=True)
            beam = expanded[:8]
            if best is None or beam[0][0] > best[0]:
                best = beam[0]
        self.last_route = best[5] if best else []
        self.decisions += 1
        self.plan_ms.append((time.perf_counter() - t0) * 1000)
        return self.last_route[0] if self.last_route else -1

    def recover(self, b):
        self.repairs += 1
        candidates = sorted(self.nav.playable,
                            key=lambda i: abs(self.level.platforms[i].cx - b.cx)
                            + abs(self.level.platforms[i].y - b.feet))
        for target in candidates[:5]:
            m = make_maneuver(b, target, self.level)
            if m and m.actions:
                self.commit(m)
                return True
        return False

    def act(self, match):
        b = match.bodies[self.player]
        opponent = match.bodies[1 - self.player]
        if self.queue:
            action, predicted = self.queue[0]
            if body_matches(b, predicted):
                self.queue.popleft()
                return action
            self.invalidate()
        # Complete landing/centering actions even if a target coin was stolen.
        if b.ground < 0:
            if self.cooldown > 0:
                self.cooldown -= 1
                return IDLE
            if self.recover(b):
                return self.queue.popleft()[0]
            self.cooldown = 12
            return IDLE
        if self.cooldown > 0:
            self.cooldown -= 1
            return IDLE
        local = [i for i in self.level.platform_coins[b.ground] if match.owners[i] < 0]
        if local:
            # Actual distance breaks same-platform ties. Walk without risking
            # a new jump; coins are at a height reachable from the platform.
            coin = min(local, key=lambda i: abs(self.level.coins[i].x - b.cx))
            self.target = coin
            return (steer(self.level.coins[coin].x - b.cx), False)
        # Edge compilation assumes a launch near the platform centre. Local
        # sweeps can end at either edge, so walk back before selecting a hop.
        centre_delta = self.level.platforms[b.ground].cx - b.cx
        if abs(centre_delta) > .001:
            # Fractional stick input reaches the precise takeoff position via
            # normal physics. Never snap/teleport the body to a graph node.
            return (max(-1.0, min(1.0, centre_delta / SPEED)), False)
        destination = self.choose_target(match, b, opponent)
        if destination < 0 or destination == b.ground:
            return IDLE
        nxt = self.nav.first[b.ground][destination]
        m = make_maneuver(b, nxt, self.level) if nxt >= 0 else None
        if m is None:
            alternatives = sorted(self.nav.edges[b.ground], key=lambda j:
                                  len(self.nav.edges[b.ground][j].actions) + self.nav.dist[j][destination])
            for other in alternatives[:5]:
                if other == nxt or not math.isfinite(self.nav.dist[other][destination]):
                    continue
                m = make_maneuver(b, other, self.level)
                if m:
                    break
        if m and m.actions:
            self.commit(m)
            if self.difficulty == "easy":
                # Queue runs to completion; then hesitate at the safe landing.
                self.cooldown = 22
            return self.queue.popleft()[0]
        self.cooldown = 8
        self.repairs += 1
        return IDLE


class Match:
    def __init__(self, level, nav, difficulty="hard", seed=None, bot_both=False, solo_bot=False):
        self.level, self.nav, self.difficulty = level, nav, difficulty
        self.rng = random.Random(level.seed if seed is None else seed)
        self.starts = level.starts[:]
        self.bodies = [level.spawn(p) for p in self.starts]
        self.owners = [-1] * len(level.coins)
        self.score = [0, 0]
        self.freeze = [0, 0]
        self.double = [0, 0]
        self.magnet = [0, 0]
        self.freeze_used = set()
        self.box_ready = {p: 0 for p in level.box_platforms}
        self.respawns = [0, 0]
        self.bots: list[Bot | None] = [Bot(nav, difficulty, 0) if bot_both else None, Bot(nav, difficulty, 1)]
        self.tick = 0
        self.over = False
        self.events = []
        self.solo_bot = solo_bot

    @property
    def remaining(self):
        return self.owners.count(-1)

    def set_difficulty(self, value):
        self.difficulty = value
        for b in self.bots:
            if b:
                b.difficulty = value

    def respawn(self, player):
        b = self.bodies[player]
        safe = min(self.nav.playable, key=lambda i:
                   abs(self.level.platforms[i].cx - b.cx) + abs(self.level.platforms[i].y - b.feet))
        self.bodies[player] = self.level.spawn(safe)
        self.respawns[player] += 1
        self.freeze[player] = 0
        if self.bots[player]:
            self.bots[player].invalidate()
        self.events.append(("respawn", player, self.bodies[player].cx, self.bodies[player].y))

    def freeze_player(self, player, duration=90):
        self.freeze[player] = max(self.freeze[player], duration)
        if self.bots[player]:
            self.bots[player].invalidate()

    def step(self, human_action=IDLE):
        if self.over:
            return
        self.events = []
        for p in range(2):
            self.double[p] = max(0, self.double[p] - 1)
            self.magnet[p] = max(0, self.magnet[p] - 1)
            if self.solo_bot and p == 0:
                continue
            if self.freeze[p] > 0:
                self.freeze[p] -= 1
                action = IDLE
            elif self.bots[p]:
                action = self.bots[p].act(self)
            else:
                action = human_action
            step_body(self.bodies[p], action, self.level)
            b = self.bodies[p]
            if b.y > self.level.height + 80 or b.y < -350:
                self.respawn(p)
                b = self.bodies[p]
            if b.ground >= 0:
                plat = self.level.platforms[b.ground]
                allowed = p == 0 or self.difficulty == "hard" or self.bots[0] is not None
                if plat.kind == "freeze" and b.ground not in self.freeze_used and allowed:
                    self.freeze_used.add(b.ground)
                    self.freeze_player(1 - p)
                    self.events.append(("freeze", p, b.cx, b.y))
        # Human wins simultaneous pickup ties, matching the source game.
        for i, c in enumerate(self.level.coins):
            if self.owners[i] >= 0:
                continue
            for p, b in enumerate(self.bodies):
                if self.solo_bot and p == 0:
                    continue
                hit = overlap(b.x, b.y, BW, BH, c.x - 8, c.y - 8, 16, 16)
                if self.magnet[p]:
                    hit |= (b.cx - c.x) ** 2 + (b.y + BH / 2 - c.y) ** 2 < 110 ** 2
                if hit:
                    self.owners[i] = p
                    self.score[p] += c.value * (2 if self.double[p] else 1)
                    self.events.append(("coin", p, c.x, c.y))
                    break
        for plat, ready in self.box_ready.items():
            if ready > self.tick:
                continue
            target = self.level.platforms[plat]
            for p, b in enumerate(self.bodies):
                if self.solo_bot and p == 0:
                    continue
                if abs(b.cx - target.cx) < 31 and abs(b.feet - target.y) < 55:
                    effect = self.rng.choice(["magnet", "double", "bonus"])
                    if effect == "magnet":
                        self.magnet[p] = 300
                    elif effect == "double":
                        self.double[p] = 360
                    else:
                        self.score[p] += 3
                    self.box_ready[plat] = self.tick + 600
                    self.events.append((effect, p, target.cx, target.y - 40))
                    break
        self.tick += 1
        self.over = self.remaining == 0 or self.tick >= MATCH_TICKS


def benchmark(seed=42, ticks=18000, solo=False, style="scatter"):
    level = Level(seed, style)
    nav = Navigation(level)
    m = Match(level, nav, bot_both=not solo, solo_bot=solo)
    # Single-collector coverage test should not lose time to the race clock.
    started = time.perf_counter()
    for _ in range(ticks):
        m.step()
        if m.over:
            break
    planners = [b for b in m.bots if b]
    times = sorted(v for b in planners for v in b.plan_ms)
    return {"seed": seed, "style": style, "platforms": len(level.platforms),
            "playable_platforms": len(nav.playable), "ticks": m.tick, "coins": len(m.owners) - m.remaining,
            "remaining": m.remaining, "score": m.score, "respawns": m.respawns,
            "repairs": [b.repairs for b in planners],
            "graph_edges": sum(map(len, nav.edges)), "graph_connected": nav.connected,
            "graph_build_seconds": round(nav.build_seconds, 3),
            "simulation_seconds": round(time.perf_counter() - started, 3),
            "decision_ms_p95": round(times[int((len(times) - 1) * .95)], 3) if times else 0}
