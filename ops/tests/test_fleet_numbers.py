"""Tests for persistent fleet phone numbers (ops.fleet_numbers)."""
from __future__ import annotations

import os
import tempfile
import unittest

import httpx
from fastapi import FastAPI

import ops.fleet_control as fleet_control
import ops.fleet_numbers as fleet_numbers


def ip(n: int) -> str:
    return f"192.168.50.{n}:5555"


class FleetNumberTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._old_state = os.environ.get("D3M0N_FLEET_STATE")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["D3M0N_FLEET_STATE"] = self._tmp.name

    async def asyncTearDown(self) -> None:
        if self._old_state is None:
            os.environ.pop("D3M0N_FLEET_STATE", None)
        else:
            os.environ["D3M0N_FLEET_STATE"] = self._old_state
        self._tmp.cleanup()

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        app = FastAPI()
        app.include_router(fleet_control.router)
        app.include_router(fleet_numbers.router)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)

    async def test_first_batch_is_numbered_in_natural_ip_order(self) -> None:
        # Deliberately shuffled, with .100 that a text sort would place before .37.
        serials = [ip(57), ip(100), ip(37), ip(9), ip(45), "R58M123ABC"]
        response = await self.request("POST", "/api/fleet/numbers/assign", json={"devices": serials})
        self.assertEqual(response.status_code, 200)
        numbers = response.json()["numbers"]
        self.assertEqual(numbers, {ip(9): 1, ip(37): 2, ip(45): 3, ip(57): 4, ip(100): 5, "R58M123ABC": 6})
        listed = await self.request("GET", "/api/fleet/numbers")
        self.assertEqual(listed.json()["numbers"], numbers)

    async def test_numbers_are_stable_when_phones_go_offline_and_new_ones_fill_gaps(self) -> None:
        await self.request("POST", "/api/fleet/numbers/assign", json={"devices": [ip(n) for n in range(37, 42)]})
        # .39 offline: assigning the remaining phones changes nothing.
        again = await self.request("POST", "/api/fleet/numbers/assign", json={"devices": [ip(37), ip(38), ip(40), ip(41)]})
        self.assertEqual(again.json()["assigned"], [])
        self.assertEqual(again.json()["numbers"][ip(39)], 3)
        # A new phone gets the next number, never an existing one.
        added = await self.request("POST", "/api/fleet/numbers/assign", json={"devices": [ip(37), ip(60)]})
        self.assertEqual(added.json()["assigned"], [ip(60)])
        self.assertEqual(added.json()["numbers"][ip(60)], 6)
        self.assertEqual(len(set(added.json()["numbers"].values())), 6)

    async def test_set_number_swaps_with_current_holder(self) -> None:
        await self.request("POST", "/api/fleet/numbers/assign", json={"devices": [ip(37), ip(38), ip(39)]})
        response = await self.request("PUT", f"/api/fleet/numbers/{ip(39)}", json={"number": 1})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["numbers"], {ip(39): 1, ip(38): 2, ip(37): 3})
        self.assertEqual(body["swapped"], {"device": ip(37), "number": 3})
        # Setting a free number moves only that phone.
        free = await self.request("PUT", f"/api/fleet/numbers/{ip(38)}", json={"number": 25})
        self.assertEqual(free.json()["numbers"][ip(38)], 25)
        self.assertIsNone(free.json()["swapped"])
        # An unnumbered phone taking a used number pushes the holder to a free slot.
        newcomer = await self.request("PUT", f"/api/fleet/numbers/{ip(50)}", json={"number": 1})
        numbers = newcomer.json()["numbers"]
        self.assertEqual(numbers[ip(50)], 1)
        self.assertEqual(numbers[ip(39)], 2)
        self.assertEqual(len(set(numbers.values())), len(numbers))

    async def test_reset_renumbers_given_phones_from_one(self) -> None:
        await self.request("POST", "/api/fleet/numbers/assign", json={"devices": [ip(37), ip(38), ip(39)]})
        await self.request("PUT", f"/api/fleet/numbers/{ip(37)}", json={"number": 40})
        response = await self.request("POST", "/api/fleet/numbers/reset", json={"devices": [ip(39), ip(37)]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["numbers"], {ip(37): 1, ip(39): 2})
        empty = await self.request("POST", "/api/fleet/numbers/reset", json={"devices": []})
        self.assertEqual(empty.status_code, 422)

    async def test_validation_and_cross_origin_writes_are_rejected(self) -> None:
        bad_serial = await self.request("POST", "/api/fleet/numbers/assign", json={"devices": ["bad serial!"]})
        self.assertEqual(bad_serial.status_code, 422)
        bad_number = await self.request("PUT", f"/api/fleet/numbers/{ip(37)}", json={"number": 0})
        self.assertEqual(bad_number.status_code, 422)
        too_big = await self.request("PUT", f"/api/fleet/numbers/{ip(37)}", json={"number": 10000})
        self.assertEqual(too_big.status_code, 422)
        extra = await self.request("POST", "/api/fleet/numbers/assign", json={"devices": [], "x": 1})
        self.assertEqual(extra.status_code, 422)
        cross = await self.request("POST", "/api/fleet/numbers/assign", json={"devices": [ip(37)]}, headers={"Origin": "https://evil.example"})
        self.assertEqual(cross.status_code, 403)
        listed = await self.request("GET", "/api/fleet/numbers")
        self.assertEqual(listed.json()["numbers"], {})

    async def test_existing_fleet_groups_are_untouched(self) -> None:
        created = await self.request("POST", "/api/fleet/groups", json={"name": "Row A", "devices": [ip(37)]})
        self.assertEqual(created.status_code, 200)
        await self.request("POST", "/api/fleet/numbers/assign", json={"devices": [ip(37)]})
        groups = await self.request("GET", "/api/fleet/groups")
        self.assertEqual([g["name"] for g in groups.json()["groups"]], ["Row A"])


if __name__ == "__main__":
    unittest.main()
