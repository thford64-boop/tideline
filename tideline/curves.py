"""Colour curves: control points, the monotone spline drawn through them,
and their form for FFmpeg's ``curves`` filter.

A curve is a list of ``[x, y]`` points in 0..1, sorted by x, from black
(left) to white (right). FFmpeg draws it with PCHIP (``interp=pchip``), a
cubic that never overshoots between points, and ``evaluate`` draws the
same curve, so the editor shows exactly what renders. Plain data, no GTK.
"""

CHANNELS = [("master", "Master"), ("red", "Red"), ("green", "Green"), ("blue", "Blue")]
IDENTITY = [[0.0, 0.0], [1.0, 1.0]]
MAX_POINTS = 16
MIN_GAP = 0.01          # closest two points may sit, horizontally


def default():
    return {key: [list(p) for p in IDENTITY] for key, _ in CHANNELS}


def clean(points):
    """``points`` as a valid curve: clamped to 0..1, sorted, at most one point
    per ``MIN_GAP`` of x, at most ``MAX_POINTS``. Raises ValueError when
    it is not a list of number pairs."""
    if not isinstance(points, (list, tuple)):
        raise ValueError(points)
    out = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise ValueError(p)
        x, y = p
        if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, (int, float)) \
                or not isinstance(y, (int, float)) or x != x or y != y:
            raise ValueError(p)
        out.append([min(1.0, max(0.0, float(x))), min(1.0, max(0.0, float(y)))])
    out.sort(key=lambda p: p[0])
    kept = []
    for p in out:
        if kept and p[0] - kept[-1][0] < MIN_GAP:
            kept[-1] = p                   # the later of two crowded points wins
        else:
            kept.append(p)
    if len(kept) > MAX_POINTS:
        raise ValueError("too many points")
    if len(kept) == 1:
        y = kept[0][1]
        return [[0.0, y], [1.0, y]]
    return kept or [list(p) for p in IDENTITY]


def clean_set(value):
    """A full ``{channel: points}`` value; missing or broken channels become identity."""
    out = default()
    if isinstance(value, dict):
        for key, _ in CHANNELS:
            try:
                out[key] = clean(value.get(key, IDENTITY))
            except ValueError:
                pass
    return out


def is_identity(points):
    pts = clean(points)
    return all(abs(x - y) < 1e-6 for x, y in pts) and len(pts) >= 2 and pts[0][0] <= 1e-6 \
        and pts[-1][0] >= 1 - 1e-6


def to_ffmpeg(points):
    """'x/y x/y …' for the filter's master/red/green/blue options."""
    return " ".join(f"{x:.4g}/{y:.4g}" for x, y in clean(points))


def _slopes(xs, ys):
    """PCHIP tangents (Fritsch–Carlson with the end conditions FFmpeg uses)."""
    n = len(xs)
    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    d = [(ys[i + 1] - ys[i]) / h[i] for i in range(n - 1)]
    if n == 2:
        return [d[0], d[0]]
    m = [0.0] * n
    for i in range(1, n - 1):
        if d[i - 1] * d[i] <= 0:
            m[i] = 0.0
        else:
            w1, w2 = 2 * h[i] + h[i - 1], h[i] + 2 * h[i - 1]
            m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])

    def end(h0, h1, d0, d1):
        s = ((2 * h0 + h1) * d0 - h0 * d1) / (h0 + h1)
        if s * d0 <= 0:
            return 0.0
        if d0 * d1 <= 0 and abs(s) > abs(3 * d0):
            return 3 * d0
        return s

    m[0] = end(h[0], h[1], d[0], d[1])
    m[-1] = end(h[-1], h[-2], d[-1], d[-2])
    return m


def evaluate(points, x):
    """The curve's output at ``x`` (0..1). Flat before the first point and
    after the last, as FFmpeg draws it."""
    pts = clean(points)
    if len(pts) == 1:
        return pts[0][1]
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    m = _slopes(xs, ys)
    i = max(j for j in range(len(xs) - 1) if xs[j] <= x)
    h = xs[i + 1] - xs[i]
    t = (x - xs[i]) / h
    t2, t3 = t * t, t * t * t
    v = ((2 * t3 - 3 * t2 + 1) * ys[i] + (t3 - 2 * t2 + t) * h * m[i]
         + (-2 * t3 + 3 * t2) * ys[i + 1] + (t3 - t2) * h * m[i + 1])
    return min(1.0, max(0.0, v))


def table(points, size=256):
    """The curve sampled at ``size`` evenly spaced inputs."""
    return [evaluate(points, i / (size - 1)) for i in range(size)]


def add_point(points, x, y):
    """A copy with a point at (x, y); returns (points, index of the new point)."""
    pts = [list(p) for p in clean(points)]
    if len(pts) >= MAX_POINTS:
        return pts, -1
    x, y = min(1.0, max(0.0, x)), min(1.0, max(0.0, y))
    if any(abs(p[0] - x) < MIN_GAP for p in pts):
        return pts, -1
    pts.append([x, y])
    pts.sort(key=lambda p: p[0])
    return pts, next(i for i, p in enumerate(pts) if p == [x, y])


def move_point(points, index, x, y):
    """A copy with point ``index`` moved, kept between its neighbours."""
    pts = [list(p) for p in points]
    lo = pts[index - 1][0] + MIN_GAP if index > 0 else 0.0
    hi = pts[index + 1][0] - MIN_GAP if index < len(pts) - 1 else 1.0
    pts[index] = [min(hi, max(lo, x)), min(1.0, max(0.0, y))]
    return pts


def remove_point(points, index):
    """A copy without point ``index``; a curve keeps at least two points."""
    pts = [list(p) for p in points]
    if len(pts) > 2 and 0 <= index < len(pts):
        del pts[index]
    return pts
