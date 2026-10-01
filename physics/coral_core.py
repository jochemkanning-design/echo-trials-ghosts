"""Coral-only additions; Woodland simulation and archive keys stay unchanged."""
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path

from core import BW, BH
from trials_core import TrialLevel, TrialMatch, polygons_overlap

CORAL_FILES = tuple(f'coral_{i:02}.json' for i in range(1, 6))


@dataclass(frozen=True)
class Jellybell:
    """Rest, warn, sting on a repeatable clock. Visible shapes are colliders."""
    x: float
    y: float
    size: float = 40
    travel_x: float = 0
    travel_y: float = 35
    period: int = 300
    phase: int = 0

    def state(self, tick):
        q = (tick + self.phase) % self.period / self.period
        return 'rest' if q < .48 else 'warn' if q < .65 else 'sting'

    def pose(self, tick):
        angle = math.tau * (tick + self.phase) / self.period
        return self.x + self.travel_x * math.sin(angle), self.y + self.travel_y * math.sin(angle), 0

    def silhouette(self, tick):
        x, y, _ = self.pose(tick)
        r = self.size
        # Curved dome and waving strands, split into convex pieces so the
        # organic outline is still precisely usable by swept contact checks.
        dome = [(math.cos(math.pi+k*math.pi/16),math.sin(math.pi+k*math.pi/16))
                for k in range(17)] + [(.7,.18),(-.7,.18)]
        shapes = [[(x+u*r,y+v*r) for u,v in dome]]
        for n in (-1,0,1):
            nodes=[]
            for k in range(9):
                depth=.12+k*.195
                xx=x+n*r*.55+math.sin(tick*.04+n+depth*3)*r*.15*depth
                nodes.append((xx,y+depth*r))
            for k,(a,b) in enumerate(zip(nodes,nodes[1:])):
                half=r*(.07-k*.004)
                shapes.append([(a[0]-half,a[1]),(a[0]+half,a[1]),
                               (b[0]+half,b[1]),(b[0]-half,b[1])])
        return shapes

    def blades(self, tick):
        return self.silhouette(tick) if self.state(tick) == 'sting' else []

    def hits(self, before, after, tick):
        a, b = self.pose(tick-1), self.pose(tick)
        distance = math.hypot(after[0]-before[0],after[1]-before[1])+math.hypot(b[0]-a[0],b[1]-a[1])+self.size*.03
        steps = max(1,math.ceil(distance/3))
        for i in range(steps+1):
            u = i/steps
            clock = tick-1+u
            if self.state(clock) != 'sting':
                continue
            x,y = (before[k]+(after[k]-before[k])*u for k in (0,1))
            cx,cy,_ = self.pose(clock)
            if x+BW < cx-self.size*1.2 or x > cx+self.size*1.2 or y+BH < cy-self.size*1.2 or y > cy+self.size*1.8:
                continue
            body=[(x,y),(x+BW,y),(x+BW,y+BH),(x,y+BH)]
            if any(polygons_overlap(body,shape) for shape in self.blades(clock)):
                return True
        return False


class CoralLevel(TrialLevel):
    def __init__(self, filename='coral_01.json'):
        super().__init__(filename)
        if self.number != 2:
            raise ValueError('CoralLevel requires a Coral course')
        self.axes = [Jellybell(**item) for item in self.spec.get('jellies', [])]
        for jelly in self.axes:
            if jelly.size < 10 or jelly.period < 120:
                raise ValueError('Jellybells need a readable size and warning time')
        digest=hashlib.sha256(self.record_key.encode())
        digest.update(Path(__file__).read_bytes())
        self.record_key=self.level_id+'-'+digest.hexdigest()[:20]


class CoralMatch(TrialMatch):
    def step(self, action=(0,False,0)):
        super().step(action)
        if self.death_cause == 'blade':
            self.death_cause = 'jellyfish'
        elif self.death_cause == 'brambles':
            self.death_cause = 'urchins'
