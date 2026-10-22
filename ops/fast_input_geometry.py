"""Fast, validated mapping of stream coordinates to Android input coordinates.

The module deliberately does not invoke adb.  ``fetch_size`` is supplied by the
caller so this helper remains usable by tests and by transports other than adb.
"""

from __future__ import annotations

from collections import OrderedDict
import math
import numbers
import re
import threading
import time
from typing import Callable


_CACHE_TTL_SECONDS = 5.0
_MAX_CACHED_DEVICES = 128

# The global lock protects only cache metadata. Fixed-size query locks deduplicate
# same-device misses without blocking the whole fleet behind one slow phone.
_size_cache: OrderedDict[str, tuple[float, tuple[int, int]]] = OrderedDict()
_cache_lock = threading.RLock()
_query_locks = tuple(threading.RLock() for _ in range(64))

_SIZE_LINE = re.compile(
    r"^\s*(?P<label>Override size|Physical size)\s*:\s*"
    r"(?P<w>\d+)\s*[xX]\s*(?P<h>\d+)\s*$",
    re.IGNORECASE,
)
_KNOWN_LABEL = re.compile(r"^\s*(Override size|Physical size)\s*:", re.IGNORECASE)


# Kept private so a malformed recognized label cannot silently fall through to
# the other size.  Override is preferred only after it has been parsed and
# validated successfully.
def _parse_wm_size(raw: str) -> tuple[int, int]:
    if not isinstance(raw, str):
        raise ValueError("wm size query returned a non-text result")

    parsed: dict[str, tuple[int, int]] = {}
    for line in raw.splitlines():
        if not _KNOWN_LABEL.match(line):
            continue
        match = _SIZE_LINE.match(line)
        if match is None:
            raise ValueError(f"malformed wm size line: {line!r}")
        label = match.group("label").lower()
        width = int(match.group("w"))
        height = int(match.group("h"))
        if width <= 0 or height <= 0:
            raise ValueError(f"invalid wm dimensions: {width}x{height}")
        parsed[label] = (width, height)

    if "override size" in parsed:
        return parsed["override size"]
    if "physical size" in parsed:
        return parsed["physical size"]
    raise ValueError("wm size query did not provide Physical size or Override size")


def _valid_stream_dimension(value: object, name: str) -> int:
    # bool is an int subclass, but is not a meaningful image dimension.
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    if value <= 0 or value > 8192:
        raise ValueError(f"{name} must be in the range 1..8192")
    return value


def _validated_points(
    points: list[tuple[int, int]], stream_w: int, stream_h: int
) -> list[tuple[numbers.Real, numbers.Real]]:
    if not isinstance(points, list):
        raise TypeError("points must be a list of coordinate pairs")

    checked: list[tuple[numbers.Real, numbers.Real]] = []
    for point in points:
        if not isinstance(point, (tuple, list)) or len(point) != 2:
            raise TypeError("each point must contain exactly two numeric coordinates")
        x, y = point
        for value, axis, limit in ((x, "x", stream_w), (y, "y", stream_h)):
            if isinstance(value, bool) or not isinstance(value, numbers.Real):
                raise TypeError(f"point {axis} coordinate must be numeric")
            if not math.isfinite(value):
                raise ValueError(f"point {axis} coordinate must be finite")
            if value < 0 or value > limit:
                raise ValueError(
                    f"point {axis} coordinate {value!r} is outside the stream image"
                )
        checked.append((x, y))
    return checked


def _native_size(device: str, fetch_size: Callable[[], str]) -> tuple[int, int]:
    if not isinstance(device, str):
        raise TypeError("device must be a string")
    if not callable(fetch_size):
        raise TypeError("fetch_size must be callable")

    with _query_locks[hash(device) % len(_query_locks)]:
        with _cache_lock:
            now = time.monotonic()
            cached = _size_cache.get(device)
            if cached is not None:
                fetched_at, size = cached
                if now - fetched_at < _CACHE_TTL_SECONDS:
                    _size_cache.move_to_end(device)
                    return size
                del _size_cache[device]

        size = _parse_wm_size(fetch_size())
        with _cache_lock:
            _size_cache[device] = (time.monotonic(), size)
            _size_cache.move_to_end(device)
            while len(_size_cache) > _MAX_CACHED_DEVICES:
                _size_cache.popitem(last=False)
        return size


def mapped_points(
    device: str,
    points: list[tuple[int, int]],
    stream_w: int,
    stream_h: int,
    fetch_size: Callable[[], str],
) -> list[tuple[int, int]]:
    """Map stream points to the current Android input coordinate space.

    Stream points use the inclusive image boundary convention: ``x == stream_w``
    and ``y == stream_h`` are accepted and clamp to the final native pixel.
    Points outside the image, non-finite values, malformed sizes, and invalid
    dimensions raise instead of producing potentially dangerous coordinates.
    """

    width = _valid_stream_dimension(stream_w, "stream_w")
    height = _valid_stream_dimension(stream_h, "stream_h")
    checked_points = _validated_points(points, width, height)
    native_w, native_h = _native_size(device, fetch_size)

    stream_landscape = width > height
    stream_portrait = height > width
    native_landscape = native_w > native_h
    native_portrait = native_h > native_w
    if (stream_landscape and native_portrait) or (
        stream_portrait and native_landscape
    ):
        native_w, native_h = native_h, native_w

    mapped: list[tuple[int, int]] = []
    for x, y in checked_points:
        # Inputs are non-negative after validation, so int() is a floor.  The
        # explicit min handles the inclusive stream boundary exactly.
        native_x = min(native_w - 1, int(x * native_w / width))
        native_y = min(native_h - 1, int(y * native_h / height))
        mapped.append((native_x, native_y))
    return mapped


def clear_size_cache(device: str | None = None) -> None:
    """Clear all cached sizes, or just one device's entry."""

    with _cache_lock:
        if device is None:
            _size_cache.clear()
        else:
            _size_cache.pop(device, None)


def cache_info() -> dict[str, object]:
    """Return a read-only snapshot useful for diagnostics and benchmarks."""

    with _cache_lock:
        return {
            "size": len(_size_cache),
            "max_size": _MAX_CACHED_DEVICES,
            "ttl_seconds": _CACHE_TTL_SECONDS,
            "devices": tuple(_size_cache),
        }


__all__ = ["mapped_points", "clear_size_cache", "cache_info"]
