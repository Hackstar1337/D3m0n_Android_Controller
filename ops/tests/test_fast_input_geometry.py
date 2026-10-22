"""Offline tests for the deployed ops.fast_input_geometry helper.

The helper under test is imported from this source checkout;
all Android size responses below are local callbacks and no adb/device is used.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch


DEPLOYED_ROOT = Path(str(Path(__file__).resolve().parents[2]))
if str(DEPLOYED_ROOT) not in sys.path:
    sys.path.insert(0, str(DEPLOYED_ROOT))

import ops.fast_input_geometry as geometry  # noqa: E402


class FastInputGeometryTests(unittest.TestCase):
    def setUp(self) -> None:
        geometry.clear_size_cache()

    def tearDown(self) -> None:
        geometry.clear_size_cache()

    @staticmethod
    def size_output(width: int = 100, height: int = 50) -> str:
        return f"Physical size: {width}x{height}\n"

    def test_cache_miss_then_hit(self) -> None:
        calls = 0

        def fetch() -> str:
            nonlocal calls
            calls += 1
            return self.size_output()

        first = geometry.mapped_points("device-a", [(0, 0), (10, 5)], 10, 5, fetch)
        second = geometry.mapped_points("device-a", [(5, 2)], 10, 5, fetch)

        self.assertEqual(first, [(0, 0), (99, 49)])
        self.assertEqual(second, [(50, 20)])
        self.assertEqual(calls, 1)

    def test_ttl_expiry_uses_patchable_monotonic_clock(self) -> None:
        now = [100.0]
        calls = 0

        def fetch() -> str:
            nonlocal calls
            calls += 1
            return self.size_output()

        with patch.object(geometry.time, "monotonic", side_effect=lambda: now[0]):
            geometry.mapped_points("device-a", [(1, 1)], 10, 5, fetch)
            now[0] = 104.999
            geometry.mapped_points("device-a", [(1, 1)], 10, 5, fetch)
            now[0] = 105.0
            geometry.mapped_points("device-a", [(1, 1)], 10, 5, fetch)

        self.assertEqual(calls, 2)

    def test_device_isolation_and_targeted_clear(self) -> None:
        calls: list[str] = []

        def fetch_a() -> str:
            calls.append("a")
            return self.size_output(100, 50)

        def fetch_b() -> str:
            calls.append("b")
            return self.size_output(200, 100)

        geometry.mapped_points("a", [(1, 1)], 10, 5, fetch_a)
        geometry.mapped_points("b", [(1, 1)], 10, 5, fetch_b)
        geometry.mapped_points("a", [(1, 1)], 10, 5, fetch_a)
        geometry.clear_size_cache("a")
        geometry.mapped_points("a", [(1, 1)], 10, 5, fetch_a)
        geometry.mapped_points("b", [(1, 1)], 10, 5, fetch_b)

        self.assertEqual(calls, ["a", "b", "a"])

    def test_override_size_is_preferred(self) -> None:
        def fetch() -> str:
            return "Physical size: 2000x1000\nOverride size: 1000x500\n"

        result = geometry.mapped_points("override", [(100, 50)], 200, 100, fetch)
        self.assertEqual(result, [(500, 250)])

    def test_portrait_stream_swaps_landscape_native_size(self) -> None:
        def fetch() -> str:
            return "Physical size: 200x100\n"

        result = geometry.mapped_points("portrait", [(50, 100)], 100, 200, fetch)
        self.assertEqual(result, [(50, 100)])

    def test_landscape_stream_swaps_portrait_native_size(self) -> None:
        def fetch() -> str:
            return "Physical size: 100x200\n"

        result = geometry.mapped_points("landscape", [(200, 50)], 200, 100, fetch)
        self.assertEqual(result, [(199, 50)])

    def test_inclusive_boundary_clamps_to_last_pixel(self) -> None:
        result = geometry.mapped_points(
            "bounds", [(0, 0), (10, 5)], 10, 5, lambda: self.size_output(100, 50)
        )
        self.assertEqual(result, [(0, 0), (99, 49)])

    def test_invalid_stream_and_points_are_rejected(self) -> None:
        fetch = lambda: self.size_output()
        invalid_streams = [(0, 5), (5, 0), (8193, 5), (5, 8193), (True, 5)]
        for stream_w, stream_h in invalid_streams:
            with self.subTest(stream_w=stream_w, stream_h=stream_h):
                with self.assertRaises((TypeError, ValueError)):
                    geometry.mapped_points("invalid", [(1, 1)], stream_w, stream_h, fetch)

        for point in [(-1, 1), (11, 1), (1, -1), (1, 6), (float("nan"), 1), (1, float("inf"))]:
            with self.subTest(point=point):
                with self.assertRaises((TypeError, ValueError)):
                    geometry.mapped_points("invalid-point", [point], 10, 5, fetch)

        with self.assertRaises((TypeError, ValueError)):
            geometry.mapped_points("invalid-point", [("1", 1)], 10, 5, fetch)

    def test_malformed_or_invalid_query_is_not_cached(self) -> None:
        responses = iter(["Physical size: malformed\n", "Physical size: 100x50\n"])
        calls = 0

        def fetch() -> str:
            nonlocal calls
            calls += 1
            return next(responses)

        with self.assertRaises(ValueError):
            geometry.mapped_points("bad-query", [(1, 1)], 10, 5, fetch)
        self.assertEqual(geometry.cache_info()["size"], 0)
        self.assertEqual(
            geometry.mapped_points("bad-query", [(1, 1)], 10, 5, fetch), [(10, 10)]
        )
        self.assertEqual(calls, 2)

        geometry.clear_size_cache()
        with self.assertRaises(ValueError):
            geometry.mapped_points(
                "zero-query", [(1, 1)], 10, 5, lambda: "Override size: 0x50\n"
            )
        self.assertEqual(geometry.cache_info()["size"], 0)

    def test_bounded_cache_evicts_oldest_devices(self) -> None:
        def fetch() -> str:
            return self.size_output()

        for index in range(129):
            geometry.mapped_points(f"device-{index}", [(1, 1)], 10, 5, fetch)

        info = geometry.cache_info()
        self.assertEqual(info["size"], 128)
        self.assertNotIn("device-0", info["devices"])
        self.assertIn("device-128", info["devices"])

    def test_concurrent_same_device_misses_are_deduplicated(self) -> None:
        calls = 0
        calls_lock = threading.Lock()

        def fetch() -> str:
            nonlocal calls
            with calls_lock:
                calls += 1
            # Ensure other workers arrive while the first query owns the lock.
            time.sleep(0.03)
            return self.size_output()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda _: geometry.mapped_points(
                        "same-device", [(5, 2)], 10, 5, fetch
                    ),
                    range(8),
                )
            )

        self.assertEqual(calls, 1)
        self.assertEqual(results, [[(50, 20)]] * 8)


if __name__ == "__main__":
    unittest.main()
