"""The five worlds' shared presentation and movement settings.

Tick units at 60 Hz. Physics is consumed by live racers AND route prediction.
All art paths are relative to assets; no network access is needed to play.
"""
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path


@dataclass(frozen=True)
class Physics:
    speed: float = 5.0
    gravity: float = .5
    jump: float = -10.0
    terminal: float = 20.0
    maneuver_ticks: int = 210


NORMAL = Physics()
WATER = Physics(speed=4.4, gravity=.32, jump=-8.0, terminal=8.0, maneuver_ticks=300)


@dataclass(frozen=True)
class World:
    number: int
    key: str
    name: str
    subtitle: str
    hint: str
    physics: Physics = NORMAL
    boundary: str = "lava"

    @property
    def background(self):
        return "forest.png" if self.number == 1 else f"worlds/{self.key}/background.png"

    @property
    def atlas(self):
        return "atlas.json" if self.number == 1 else f"worlds/{self.key}/atlas.json"


WORLDS = (
    World(1, "woodland", "Woodland", "First Sparks",
          "Two loops. Small landings. Bump gently; stomp to steal the lead."),
    World(2, "coral", "Coral Depths", "Crossing Currents",
          "Low gravity! Tap five swim pulses; land to refill. Ride the shell shuttles.", WATER, "abyss"),
    World(3, "crystal", "Crystal Heights", "Liftworks",
          "Ride the crystal lifts; jump off toward a fixed landing.", boundary="void"),
    World(4, "ruins", "Sunken Ruins", "Cracking Bridges",
          "Ancient ledges crack for one second. Keep an escape jump.", boundary="sand"),
    World(5, "ember", "Ember Caverns", "Lava Relay",
          "Link the shortcuts, then rest on a fixed deck. Race for the prize!"),
)


def world_for(number=1):
    if number not in range(1, 6):
        raise ValueError("World must be 1 through 5")
    return WORLDS[number - 1]


@dataclass(frozen=True)
class Deck:
    x: float
    y: float
    w: float
    h: float
    kind: str


@dataclass(frozen=True)
class LevelSetup:
    width: int
    height: int
    moving: int
    falling: int
    jump_budget: int
    platforms: tuple[Deck, ...]


@lru_cache(maxsize=5)
def level_setup(number):
    """Load immutable, per-world authored landings once, including menu maps.

    Dynamic branches are placed around these decks and flight-validated at
    launch. A changed setup cannot bypass that safety validation.
    """
    world = world_for(number)
    path = Path(__file__).with_name("levels") / f"{number:02}_{world.key}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data["format"] != 1 or data["world"] != world.key:
        raise ValueError(f"Wrong level setup for {world.name}")
    decks = tuple(Deck(**entry) for entry in data["platforms"])
    if len(decks) < 2 or not 1 <= data["jump_budget"] <= 4:
        raise ValueError(f"Invalid platform setup for {world.name}")
    if data["moving"] < 0 or data["falling"] < 0:
        raise ValueError("Platform counts must be nonnegative")
    for p in decks:
        if not (p.w >= 60 and p.h > 0 and 0 <= p.x < p.x + p.w <= data["width"]
                and 60 <= p.y < data["height"] - 60 and p.kind in ("normal", "hub")):
            raise ValueError(f"Invalid fixed landing in {world.name}")
    return LevelSetup(data["width"], data["height"], data["moving"], data["falling"],
                      data["jump_budget"], decks)
