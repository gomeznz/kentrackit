"""python3 tests/test_geo.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from radio_avl import geo

SQUARE = {"shape": "polygon", "geometry": {"points": [[-46.41, 168.35], [-46.41, 168.36], [-46.42, 168.36], [-46.42, 168.35]]}}
CIRCLE = {"shape": "circle", "geometry": {"center": [-46.41232, 168.35238], "radius": 150}}


def test_haversine():
    d = geo.haversine(-46.41, 168.35, -46.42, 168.35)
    assert 1100 < d < 1125            # 0.01 deg latitude ~ 1.11 km


def test_polygon_inside_and_buffer():
    assert geo.inside(SQUARE, -46.415, 168.355)
    assert not geo.inside(SQUARE, -46.405, 168.355)          # ~550 m north
    just_out = -46.4098                                      # ~22 m north of the edge
    assert not geo.inside(SQUARE, just_out, 168.355)
    assert geo.inside(SQUARE, just_out, 168.355, buffer_m=25)


def test_circle():
    assert geo.inside(CIRCLE, -46.4130, 168.3524)
    assert not geo.inside(CIRCLE, -46.4150, 168.3524)
    assert geo.inside(CIRCLE, -46.4140, 168.3524, buffer_m=100)


def test_valid_geometry():
    assert geo.valid_geometry("circle", {"center": [-46, 168], "radius": 2})is False
    assert geo.valid_geometry("polygon", {"points": [[0, 0], [1, 1]]}) is False
    assert geo.valid_geometry("circle", {"center": ["-46.4", "168.3"], "radius": "200"})["radius"] == 200


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
