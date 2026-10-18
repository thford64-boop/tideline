"""Video scopes: waveform, RGB parade, vectorscope and histogram of a frame.

Frames arrive as packed 8-bit RGB (what the monitors show). Everything here
is numpy on a reduced copy of the frame and returns plain arrays: densities
per (level, column) for the waveforms, per (Cr, Cb) bin for the
vectorscope, and counts per level for the histogram. ``to_argb`` turns
densities into a cairo ARGB32 image. Luma and colour difference use the
Rec. 709 coefficients, the HD standard. No GTK.
"""

import numpy as np

KR, KB = 0.2126, 0.0722
KG = 1.0 - KR - KB
LEVELS = 256
MAX_WIDTH = 480          # analysis size: columns are sampled down to this many,
MAX_HEIGHT = 540         # and rows to this many; more rows keep gradients free of gaps

MODES = [("waveform", "Waveform"), ("parade", "RGB Parade"), ("vectorscope", "Vectorscope"),
         ("histogram", "Histogram"), ("all", "All Scopes")]
WAVEFORM_KINDS = [("luma", "Luma"), ("rgb", "RGB Overlay"), ("ycbcr", "YCbCr")]

# Colour bars at 75 %: the vectorscope's target boxes.
BAR_TARGETS = [("R", (0.75, 0.0, 0.0)), ("Mg", (0.75, 0.0, 0.75)), ("B", (0.0, 0.0, 0.75)),
               ("Cy", (0.0, 0.75, 0.75)), ("G", (0.0, 0.75, 0.0)), ("Yl", (0.75, 0.75, 0.0))]
SKIN_ANGLE = 123.0       # degrees from +Cb: the line skin tones of every complexion fall near


def frame_array(pixels, width, height):
    """The frame as an (h, w, 3) uint8 array, sampled down for analysis."""
    img = np.frombuffer(pixels, dtype=np.uint8, count=width * height * 3).reshape(height, width, 3)
    return img[::max(1, int(np.ceil(height / MAX_HEIGHT))), ::max(1, int(np.ceil(width / MAX_WIDTH)))]


def luma(img):
    """Rec. 709 luma, 0..255 floats."""
    f = img.astype(np.float32)
    return f[..., 0] * KR + f[..., 1] * KG + f[..., 2] * KB


def chroma(img):
    """(Cb, Cr) in -0.5..0.5 for every pixel."""
    f = img.astype(np.float32) / 255.0
    y = f[..., 0] * KR + f[..., 1] * KG + f[..., 2] * KB
    return (f[..., 2] - y) / (2 * (1 - KB)), (f[..., 0] - y) / (2 * (1 - KR))


def rgb_to_cbcr(r, g, b):
    y = r * KR + g * KG + b * KB
    return (b - y) / (2 * (1 - KB)), (r - y) / (2 * (1 - KR))


def _column_density(values, width):
    """Counts per (level, column) for an (h, w) array of 0..255 values;
    level 255 is row 0, so white sits at the top as on a hardware scope."""
    h, w = values.shape
    levels = np.clip(np.rint(values), 0, LEVELS - 1).astype(np.int32)
    cols = np.broadcast_to(np.arange(w, dtype=np.int32), (h, w))
    idx = (LEVELS - 1 - levels) * w + cols
    return np.bincount(idx.ravel(), minlength=LEVELS * w).reshape(LEVELS, w).astype(np.float32)


def waveform(img, kind="luma"):
    """Densities for a waveform: ``[(density, (r, g, b))]``, one per trace."""
    w = img.shape[1]
    if kind == "rgb":
        return [(_column_density(img[..., i], w), c)
                for i, c in enumerate(((1.0, 0.3, 0.3), (0.3, 1.0, 0.35), (0.35, 0.5, 1.0)))]
    if kind == "ycbcr":
        cb, cr = chroma(img)
        return [(_column_density(luma(img), w), (0.95, 0.95, 0.95)),
                (_column_density((cb + 0.5) * 255.0, w), (0.35, 0.55, 1.0)),
                (_column_density((cr + 0.5) * 255.0, w), (1.0, 0.4, 0.4))]
    return [(_column_density(luma(img), w), (0.55, 1.0, 0.75))]


def parade(img):
    """Red, green and blue waveforms side by side: three (density, colour)."""
    w = img.shape[1]
    return [(_column_density(img[..., i], w), c)
            for i, c in enumerate(((1.0, 0.3, 0.3), (0.3, 1.0, 0.35), (0.35, 0.5, 1.0)))]


def vectorscope(img, size=256):
    """(density, mean colour) on a ``size`` × ``size`` grid: Cb to the right,
    Cr up. The colour of each bin is the average of the pixels in it."""
    cb, cr = chroma(img)
    x = np.clip(np.rint((cb + 0.5) * (size - 1)), 0, size - 1).astype(np.int32)
    y = np.clip(np.rint((0.5 - cr) * (size - 1)), 0, size - 1).astype(np.int32)
    idx = (y * size + x).ravel()
    n = size * size
    count = np.bincount(idx, minlength=n).astype(np.float32)
    flat = img.reshape(-1, 3).astype(np.float32)
    colour = np.stack([np.bincount(idx, weights=flat[:, i], minlength=n) for i in range(3)], axis=-1)
    colour /= np.maximum(count, 1.0)[:, None]
    return count.reshape(size, size), colour.reshape(size, size, 3)


def histogram(img):
    """Counts per level: {"red", "green", "blue", "luma"} arrays of 256."""
    out = {name: np.bincount(img[..., i].ravel(), minlength=LEVELS).astype(np.float32)
           for i, name in enumerate(("red", "green", "blue"))}
    y = np.clip(np.rint(luma(img)), 0, LEVELS - 1).astype(np.int32)
    out["luma"] = np.bincount(y.ravel(), minlength=LEVELS).astype(np.float32)
    return out


def clipping(img):
    """Fraction of pixels with any channel at 0 (crushed) and at 255 (clipped)."""
    n = max(1, img.shape[0] * img.shape[1])
    return float(np.any(img == 0, axis=-1).sum()) / n, float(np.any(img == 255, axis=-1).sum()) / n


def intensity(density, gain=1.0, full=1.0):
    """0..1 brightness of a trace: logarithmic, so a single stray pixel shows
    faintly while dense areas do not all saturate. A bin holding ``full``
    pixels is fully bright at a gain of 1."""
    return np.clip(gain * np.log1p(density) / np.log1p(max(1.0, full)), 0.0, 1.0)


def spread(amount, weight=0.6):
    """Thicken traces by a level up and down, as a phosphor trace has width;
    a perfectly flat area (a title, colour bars) is then still visible when
    the scope is drawn smaller than 256 levels."""
    out = amount.copy()
    np.maximum(out[1:], amount[:-1] * weight, out=out[1:])
    np.maximum(out[:-1], amount[1:] * weight, out=out[:-1])
    return out


def to_argb(layers, height, width):
    """Additively mix ``[(intensity, (r, g, b))]`` (each ``height`` × ``width``)
    into a cairo ARGB32 buffer (premultiplied, native byte order)."""
    acc = np.zeros((height, width, 3), dtype=np.float32)
    for amount, colour in layers:
        acc += amount[..., None] * np.asarray(colour, dtype=np.float32)
    return argb_from_rgb(acc)


def argb_from_rgb(rgb, alpha=None):
    """(h, w, 3) floats in 0..1 -> uint8 (h, w, 4) for cairo.FORMAT_ARGB32.
    Black is transparent unless ``alpha`` is given."""
    rgb = np.clip(rgb, 0.0, 1.0)
    if alpha is None:
        a = rgb.max(axis=-1)
    else:
        a = np.clip(alpha, 0.0, 1.0)
        rgb = rgb * a[..., None]
    out = np.empty(rgb.shape[:2] + (4,), dtype=np.uint8)
    # cairo stores ARGB32 as one native-endian 32-bit word: B, G, R, A in memory on little endian.
    order = (2, 1, 0) if np.little_endian else (1, 2, 3)
    a_idx = 3 if np.little_endian else 0
    for src, dst in zip(range(3), order):
        out[..., dst] = np.rint(rgb[..., src] * 255.0)   # already premultiplied: colour <= alpha
    out[..., a_idx] = np.rint(a * 255.0)
    return out
