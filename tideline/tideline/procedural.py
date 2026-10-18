"""Procedural motion: seeded noise and periodic waves.

These drive the Animate effects (shake, oscillate, flicker…). Everything is a
pure function of time, so the same frame always renders the same way and a
generated motion stays attached to the picture like a keyframe does.
"""

import math

WAVES = [("sine", "Sine"), ("triangle", "Triangle"), ("square", "Square"), ("sawtooth", "Sawtooth"),
         ("bounce", "Bounce")]


def _lattice(seed, i):
    """Deterministic pseudo-random value in -1..1 for integer ``i``."""
    h = (i * 374761393 + seed * 668265263) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    h ^= h >> 16
    return h / 2147483647.5 - 1.0


def noise(seed, x):
    """Smooth value noise in -1..1; one new random value per unit of ``x``."""
    i = math.floor(x)
    u = x - i
    u = u * u * (3 - 2 * u)
    a, b = _lattice(seed, i), _lattice(seed, i + 1)
    return a + (b - a) * u


def fractal(seed, x, octaves=1):
    """Noise with finer, weaker detail layered on top (-1..1)."""
    total, weight, norm = 0.0, 1.0, 0.0
    for o in range(max(1, int(octaves))):
        total += noise(seed + o * 7919, x * (1 << o)) * weight
        norm += weight
        weight *= 0.5
    return total / norm


def wave(kind, phase):
    """Periodic wave in -1..1 (``bounce``: 0..1) at ``phase`` cycles."""
    p = phase - math.floor(phase)
    if kind == "triangle":
        return 4 * p - 1 if p < 0.5 else 3 - 4 * p
    if kind == "square":
        return 1.0 if p < 0.5 else -1.0
    if kind == "sawtooth":
        return 2 * p - 1
    if kind == "bounce":
        return abs(math.sin(math.pi * p))
    return math.sin(2 * math.pi * p)


def cover_scale(width, height, dx, dy, degrees):
    """Scale that keeps a frame covering the picture area when it is moved by
    up to (dx, dy) pixels and rotated by up to ``degrees``."""
    a = math.radians(min(90.0, abs(degrees)))
    ratio = max(width / height, height / width)
    return (1 + 2 * max(abs(dx) / width, abs(dy) / height)) * (math.cos(a) + ratio * math.sin(a))
