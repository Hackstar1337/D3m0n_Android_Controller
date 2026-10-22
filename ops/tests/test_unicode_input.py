import asyncio
import struct
import unittest
from unittest import mock

from ops import unicode_input as ui


class FakeProcess:
    def __init__(self, output=b""):
        self.returncode = None
        self.stdout = None
        self._output = output

    async def communicate(self):
        self.returncode = 0
        return self._output, b""

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9


class FakeReader:
    def __init__(self, clipboard="", initial=True):
        self.buf = bytearray(b"\x00" + b"device".ljust(64, b"\x00") if initial else b"")
        self.clipboard = clipboard
        self.events = []
        self.closed = False
        self.fail_paste = False

    def append(self, data):
        self.buf.extend(data)

    async def readexactly(self, count):
        while len(self.buf) < count:
            await asyncio.sleep(0)
        data = bytes(self.buf[:count])
        del self.buf[:count]
        return data

    def response_for(self, payload):
        if payload[:1] == b"\x08":
            raw = self.clipboard.encode("utf-8")
            self.append(b"\x00" + struct.pack(">I", len(raw)) + raw)
            self.events.append("get")
            return
        if payload[:1] == b"\x09":
            sequence = payload[1:9]
            paste = bool(payload[9])
            self.events.append("paste" if paste else "set")
            if paste and self.fail_paste:
                self.fail_paste = False
                return
            length = struct.unpack(">I", payload[10:14])[0]
            self.clipboard = payload[14:14 + length].decode("utf-8")
            self.append(b"\x01" + sequence)


class FakeWriter:
    def __init__(self, reader):
        self.reader = reader
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(bytes(data))
        self.reader.response_for(bytes(data))

    async def drain(self):
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


class UnicodeInputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ui._DEVICE_LOCKS.clear()
        self.readers = []
        self.writers = []
        self.adb_calls = []
        self.processes = []

        async def fake_adb(serial, *args, timeout=ui.ADB_TIMEOUT):
            self.adb_calls.append((serial, args))
            if args[:1] == ("forward",) and args[1:2] == ("tcp:0",):
                return str(42000 + len(self.readers))
            return ""

        async def fake_create(*args, **kwargs):
            proc = FakeProcess()
            self.processes.append((args, proc))
            return proc

        async def fake_open(host, port):
            reader = FakeReader("原始剪贴板")
            writer = FakeWriter(reader)
            self.readers.append(reader)
            self.writers.append(writer)
            return reader, writer

        self.patches = [
            mock.patch.object(ui, "adb", fake_adb),
            mock.patch.object(ui.asyncio, "create_subprocess_exec", fake_create),
            mock.patch.object(ui.asyncio, "open_connection", fake_open),
            mock.patch.object(ui, "MESSAGE_TIMEOUT", 0.02),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()

    def test_unicode_payload_validation_and_utf8_bound(self):
        raw = ui._encode_text("中文 नमस्ते 👩🏽‍💻")
        self.assertIn("👩🏽".encode(), raw)
        self.assertEqual(len(ui._encode_text("😀" * 4096)), 16384)
        with self.assertRaises(ValueError):
            ui._encode_text("😀" * 4097)
        with self.assertRaises(ValueError):
            ui._encode_text("bad\x00text")
        with self.assertRaises(ValueError):
            ui._encode_text("\ud800")

    def test_command_uses_control_only_and_is_under_android_cap(self):
        command = ui._server_command("serial", "12345678")
        android_command = " ".join(command[5:])
        self.assertLess(len(android_command.encode()), 255)
        self.assertIn("video=false", command)
        self.assertIn("audio=false", command)
        self.assertIn("control=true", command)
        self.assertIn("clipboard_autosync=false", command)
        self.assertNotIn("root", android_command)

    async def test_paste_ack_sequence_and_restore(self):
        await ui.paste_text("phone-a", "中文 नमस्ते 😀")
        writer = self.writers[0]
        self.assertEqual(writer.writes[0], b"\x08\x00")
        self.assertEqual(writer.writes[1][0], 9)
        self.assertEqual(writer.writes[1][9], 0)
        self.assertEqual(writer.writes[2], b"\x08\x00")
        self.assertEqual(writer.writes[3][0], 9)
        self.assertEqual(writer.writes[3][9], 0)
        self.assertEqual(self.readers[0].events, ["get", "set", "get", "set"])
        self.assertTrue(any(args == ("shell", "input", "keyevent", "KEYCODE_PASTE") for _, args in self.adb_calls))
        self.assertTrue(any(args[:2] == ("forward", "--remove") for _, args in self.adb_calls))
        self.assertTrue(writer.closed)

    async def test_empty_clipboard_is_valid_and_unavailable_fails_before_set(self):
        self.readers.clear()
        self.writers.clear()

        async def fake_open_empty(host, port):
            reader = FakeReader("")
            writer = FakeWriter(reader)
            self.readers.append(reader)
            self.writers.append(writer)
            return reader, writer

        with mock.patch.object(ui.asyncio, "open_connection", fake_open_empty):
            await ui.paste_text("phone-empty", "新文本")
        self.assertEqual(self.readers[0].events, ["get", "set", "get", "set"])

    async def test_failure_attempts_restore_without_exposing_clipboard(self):
        base_adb = ui.adb
        async def fail_paste(serial, *args, **kwargs):
            if args == ("shell", "input", "keyevent", "KEYCODE_PASTE"):
                raise ui.UnicodeInputError("Input injection unavailable")
            return await base_adb(serial, *args, **kwargs)
        with mock.patch.object(ui, "adb", fail_paste):
            with self.assertRaises(ui.UnicodeInputError) as caught:
                await ui.paste_text("phone-fail", "秘密")
        self.assertNotIn("秘密", str(caught.exception))
        self.assertEqual(self.readers[0].events, ["get", "set", "get", "set"])

    async def test_cancellation_attempts_restore(self):
        real_sleep = asyncio.sleep
        entered_wait = asyncio.Event()

        async def controlled_sleep(delay):
            if delay > 1:
                entered_wait.set()
                await real_sleep(3600)
            await real_sleep(delay)

        with mock.patch.object(ui, "PASTE_SETTLE_SECONDS", 30.0), mock.patch.object(ui.asyncio, "sleep", controlled_sleep):
            task = asyncio.create_task(ui.paste_text("phone-cancel", "取消测试"))
            while not self.writers:
                await real_sleep(0)
            while len(self.writers[0].writes) < 2:
                await real_sleep(0)
            await asyncio.wait_for(entered_wait.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertGreaterEqual(len(self.writers[0].writes), 3)
        self.assertEqual(self.writers[0].writes[-1][9], 0)
        self.assertTrue(self.writers[0].closed)

    async def test_roundtrip_returns_bool_without_paste(self):
        self.assertTrue(await ui.clipboard_roundtrip_test("phone-test", "हिन्दी 😀"))
        self.assertEqual(self.readers[0].events, ["get", "set", "get", "set", "get"])
        self.assertTrue(all(not (len(w) > 9 and w[9]) for w in self.writers[0].writes[1:]))

    async def test_verified_absent_android9_clipboard_restores_absence(self):
        base_adb = ui.adb
        async def binder_adb(serial, *args, **kwargs):
            result = await base_adb(serial, *args, **kwargs)
            if args[:5] == ("shell", "service", "call", "clipboard", "2"):
                return "Result: Parcel(00000000    '....')"
            return result
        with mock.patch.object(ui, "adb", binder_adb), mock.patch.object(ui, "_android9_clipboard_presence", new=mock.AsyncMock(side_effect=[False, True, False])):
            await ui.paste_text("phone-absent", "नमस्ते 😀")
        self.assertEqual(self.readers[0].events, ["set", "get"])
        self.assertTrue(any(args[:5] == ("shell", "service", "call", "clipboard", "2") for _, args in self.adb_calls))

    async def test_clipboard_presence_is_version_and_parcel_checked(self):
        replies = iter(["28", "Result: Parcel(00000000 00000000 '........')", "28", "Result: Parcel(00000000 00000001 '........')", "29"])
        async def fake_adb(*args, **kwargs): return next(replies)
        with mock.patch.object(ui, "adb", fake_adb):
            self.assertIs(await ui._android9_clipboard_presence("phone"), False)
            self.assertIs(await ui._android9_clipboard_presence("phone"), True)
            self.assertIsNone(await ui._android9_clipboard_presence("phone"))
        self.assertEqual(len(ui._restorable_bytes("x" * 8192)), 8192)
        with self.assertRaises(ui.UnicodeInputError):
            ui._restorable_bytes("x" * (ui.MAX_RESTORE_BYTES + 1))

    async def test_startup_retries_handshake_before_any_clipboard_commands(self):
        calls = 0
        async def delayed_open(host, port):
            nonlocal calls
            calls += 1
            reader = FakeReader("original", initial=calls > 1)
            writer = FakeWriter(reader)
            self.readers.append(reader)
            self.writers.append(writer)
            return reader, writer
        with mock.patch.object(ui.asyncio, "open_connection", delayed_open):
            session = ui._ControlSession("starting-phone")
            await session.open()
            await session.close()
        self.assertEqual(calls, 2)
        self.assertTrue(self.writers[0].closed)
        self.assertFalse(any(writer.writes for writer in self.writers))

    async def test_device_sessions_have_distinct_scids_and_cleanup(self):
        first = ui._ControlSession("phone-one")
        second = ui._ControlSession("phone-two")
        await first.open()
        await second.open()
        self.assertNotEqual(first.scid, second.scid)
        await first.close()
        await second.close()
        removals = [args for _, args in self.adb_calls if args[:2] == ("forward", "--remove")]
        self.assertEqual(len(removals), 2)


if __name__ == "__main__":
    unittest.main()
