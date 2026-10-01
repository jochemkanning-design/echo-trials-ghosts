"""Input forgiveness and camera framing; neither changes movement physics."""
from __future__ import annotations

from dataclasses import dataclass
import math

from core import BH, BW


@dataclass
class JumpBuffer:
    """Remember one press for six 60 Hz ticks when no jump is available.

    Available air jumps still happen immediately. One press produces one jump;
    holding a button does not spend all five. Pauses/freezes clear old input.
    """
    remaining: int = 0

    def press(self):
        self.remaining = 6

    def clear(self):
        self.remaining = 0

    def step(self, jumps_available, blocked=False):
        if blocked:
            self.clear()
        if self.remaining and jumps_available > 0:
            self.clear()
            return True
        self.remaining = max(0, self.remaining - 1)
        return False


def clamp(value, low, high):
    return max(low, min(high, value))


@dataclass
class FollowCamera:
    x: float = 0.0
    y: float = 0.0
    look_y: float = 0.0
    initialized: bool = False

    def update(self, body, world_size, view_size, dt, *, snap=False, top_landing=0.0):
        width, height = world_size
        vw, vh = view_size
        snap = snap or not self.initialized
        look = (0 if body.ground >= 0 else -115 if body.vy < -2
                else 100 if body.vy > 5 else self.look_y)
        self.look_y = look if snap else self.look_y + (look - self.look_y) * (1 - math.exp(-4 * dt))
        # Above the map, retain the highest landing in view when actor + ledge
        # fit together. Extend upward only as needed to keep the actor visible.
        # Also show a short fall below the bottom before the normal respawn.
        min_y = min(0.0, top_landing + 24 - vh, body.y - 24)
        max_y = max(min_y, height + 200 - vh)
        max_x = max(0.0, width - vw)
        tx = clamp(body.cx + body.facing * 80 - vw / 2, 0, max_x)
        ty = clamp(body.y + self.look_y - vh / 2, min_y, max_y)
        blend = 1 if snap else 1 - math.exp(-7 * dt)
        self.x += (tx - self.x) * blend
        self.y += (ty - self.y) * blend
        # Smoothing must never strand the followed actor outside the viewport.
        # This also handles a sudden zoom change or a long frame.
        mx, my = min(80, vw * .18), min(64, vh * .18)
        self.x = clamp(clamp(self.x, body.x + BW + mx - vw, body.x - mx), 0, max_x)
        self.y = clamp(clamp(self.y, body.y + BH + my - vh, body.y - my), min_y, max_y)
        self.initialized = True
        return self.x, self.y
