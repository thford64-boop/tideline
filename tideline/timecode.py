"""Frame count <-> SMPTE timecode conversion, including drop-frame."""

import re


def is_drop(fps_num, fps_den):
    return fps_den == 1001 and fps_num in (30000, 60000)


def timebase(fps_num, fps_den=1):
    return max(1, round(fps_num / fps_den))


def frames_to_tc(frame, fps_num, fps_den=1, drop=None):
    """Format a frame count as HH:MM:SS:FF (HH;MM;SS;FF for drop-frame rates)."""
    if drop is None:
        drop = is_drop(fps_num, fps_den)
    base = timebase(fps_num, fps_den)
    sign = "-" if frame < 0 else ""
    frame = abs(int(frame))
    if drop:
        d = 2 * (base // 30)
        per_min = base * 60 - d
        per_10 = base * 600 - 9 * d
        tens, rem = divmod(frame, per_10)
        frame += d * 9 * tens
        if rem > d:
            frame += d * ((rem - d) // per_min)
    ff = frame % base
    ss = (frame // base) % 60
    mm = (frame // (base * 60)) % 60
    hh = frame // (base * 3600)
    sep = ";" if drop else ":"
    return f"{sign}{hh:02d}{sep}{mm:02d}{sep}{ss:02d}{sep}{ff:02d}"


def tc_to_frames(text, fps_num, fps_den=1, drop=None):
    """Parse timecode text. Returns (frames, relative) or None if unparsable.

    Accepts "00:00:10:00", "10:00", bare digits read right-aligned in pairs
    ("1015" is 10 seconds 15 frames), and a leading +/- for relative moves.
    """
    if drop is None:
        drop = is_drop(fps_num, fps_den)
    text = text.strip()
    if not text:
        return None
    relative = text[0] in "+-"
    sign = -1 if text[0] == "-" else 1
    if relative:
        text = text[1:].strip()
    if re.fullmatch(r"\d+", text):
        text = text.zfill(8)[-8:] if len(text) <= 8 else text
        parts = [text[max(0, i - 2):i] for i in range(len(text), 0, -2)][::-1]
    else:
        parts = re.split(r"[:;.,]", text)
    if not parts or len(parts) > 4 or not all(re.fullmatch(r"\d*", p) for p in parts):
        return None
    nums = [int(p) if p else 0 for p in parts]
    nums = [0] * (4 - len(nums)) + nums
    hh, mm, ss, ff = nums
    base = timebase(fps_num, fps_den)
    frames = ((hh * 60 + mm) * 60 + ss) * base + ff
    if drop:
        d = 2 * (base // 30)
        minutes = hh * 60 + mm
        frames -= d * (minutes - minutes // 10)
    return sign * frames, relative


def frames_to_seconds(frame, fps_num, fps_den=1):
    return frame * fps_den / fps_num


def seconds_to_frames(seconds, fps_num, fps_den=1):
    return int(round(seconds * fps_num / fps_den))


def format_duration(seconds):
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
