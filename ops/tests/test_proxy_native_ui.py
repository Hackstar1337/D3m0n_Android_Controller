import asyncio
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch
import xml.etree.ElementTree as ET

P=Path(__file__).resolve().parents[1]/'proxy_device.py'
spec=importlib.util.spec_from_file_location('proxy_native_adapter',P)
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)

class NativeUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_only_stdout_reads_actual_file_and_removes_it(self):
        d=m.ProxyDeviceDriver(AsyncMock(),AsyncMock())
        xml='<hierarchy><node package="com.github.metacubex.clash.meta" text="Save"/></hierarchy>'
        d._run_adb=AsyncMock(side_effect=[(0,'UI hierchary dumped to: /data/local/tmp/private.xml'),(0,xml),(0,'')])
        root=await d._ui_tree('emulator-5554')
        self.assertEqual(root.find('node').get('text'),'Save')
        calls=d._run_adb.await_args_list
        path=calls[0].args[-1]
        self.assertTrue(path.startswith('/data/local/tmp/d3m0n-proxy-ui-'))
        self.assertNotEqual(path,'/dev/tty')
        self.assertEqual(calls[1].args,('emulator-5554','shell','cat',path))
        self.assertEqual(calls[2].args,('emulator-5554','shell','rm','-f',path))

    async def test_missing_or_malformed_xml_fails_closed_and_is_cleaned(self):
        for raw in ('UI hierchary dumped to: /dev/tty','<hierarchy><node></hierarchy>'):
            d=m.ProxyDeviceDriver(AsyncMock(),AsyncMock())
            d._run_adb=AsyncMock(side_effect=[(0,'status only'),(0,raw),(0,'')])
            with self.assertRaises(m.SetupRequired):await d._ui_tree('emulator-5554')
            self.assertEqual(d._run_adb.await_args_list[-1].args[2:4],('rm','-f'))

    async def test_dump_failure_cleanup_does_not_mask_the_error(self):
        d=m.ProxyDeviceDriver(AsyncMock(),AsyncMock())
        d._run_adb=AsyncMock(side_effect=[m.SetupRequired('Android command timed out'),(0,'')])
        with self.assertRaises(m.SetupRequired):await d._ui_tree('emulator-5554')
        self.assertEqual(d._run_adb.await_args_list[-1].args[2:4],('rm','-f'))

    async def test_import_waits_for_expected_name_before_any_tap(self):
        d=m.ProxyDeviceDriver(AsyncMock(),AsyncMock()); name='D3m0n-test-profile'
        empty=ET.fromstring('<hierarchy/>')
        ready=ET.fromstring('<hierarchy><node text="'+name+'"/><node package="'+m.PACKAGE+'" text="Save" clickable="true"/></hierarchy>')
        row=ET.fromstring('<hierarchy><node resource-id="'+m.PACKAGE+':id/root_view"><node package="'+m.PACKAGE+'" text="'+name+'"/><node class="android.widget.RadioButton" checked="true"/></node></hierarchy>')
        d._ui_tree=AsyncMock(side_effect=[empty,ready,row,row]);d._tap_node=AsyncMock();d._run_adb=AsyncMock(return_value=(0,''))
        with patch.object(m.asyncio,'sleep',new=AsyncMock()):await d._save_and_select_profile('emulator-5554',name)
        self.assertEqual(d._tap_node.await_count,2)
        self.assertIs(d._tap_node.await_args_list[0].args[1],ready)

    async def test_offscreen_profile_scrolls_list_not_page_title(self):
        d=m.ProxyDeviceDriver(AsyncMock(),AsyncMock());name='D3m0n-new-profile'
        ready=ET.fromstring('<hierarchy><node text="'+name+'"/><node package="'+m.PACKAGE+'" text="Save" clickable="true"/></hierarchy>')
        listing=ET.fromstring('<hierarchy><node package="'+m.PACKAGE+'" text="Profiles" resource-id="'+m.PACKAGE+':id/activity_bar_title_view" clickable="false"/><node package="'+m.PACKAGE+'" resource-id="'+m.PACKAGE+':id/main_list" class="androidx.recyclerview.widget.RecyclerView" bounds="[138,0][1776,1080]"/></hierarchy>')
        row=ET.fromstring('<hierarchy><node resource-id="'+m.PACKAGE+':id/root_view"><node package="'+m.PACKAGE+'" text="'+name+'"/><node class="android.widget.RadioButton" checked="true"/></node></hierarchy>')
        d._ui_tree=AsyncMock(side_effect=[ready,listing,row,row]);d._tap_node=AsyncMock();d._run_adb=AsyncMock(return_value=(0,''))
        with patch.object(m.asyncio,'sleep',new=AsyncMock()):await d._save_and_select_profile('emulator-5554',name)
        commands=[c.args for c in d._run_adb.await_args_list]
        self.assertTrue(any(c[2:4]==('input','swipe') for c in commands))
        self.assertEqual(d._tap_node.await_count,2)
        self.assertNotIn(listing.find('node'),[c.args[-1] for c in d._tap_node.await_args_list])

    async def test_scroll_rejects_unrelated_app_or_screen(self):
        d=m.ProxyDeviceDriver(AsyncMock(),AsyncMock());d._run_adb=AsyncMock()
        unrelated=ET.fromstring('<hierarchy><node package="other.app" text="Profiles"/><node package="other.app" resource-id="other.app:id/main_list" class="RecyclerView" bounds="[0,0][1080,1920]"/></hierarchy>')
        self.assertFalse(await d._scroll_profiles('emulator-5554',unrelated))
        d._run_adb.assert_not_awaited()

    async def test_missing_name_never_taps_or_changes_device(self):
        d=m.ProxyDeviceDriver(AsyncMock(),AsyncMock());d._ui_tree=AsyncMock(return_value=ET.fromstring('<hierarchy/>'));d._tap_node=AsyncMock()
        with patch.object(m.asyncio,'sleep',new=AsyncMock()):
            with self.assertRaises(m.SetupRequired):await d._save_and_select_profile('emulator-5554','D3m0n-test')
        d._tap_node.assert_not_awaited()

if __name__=='__main__':unittest.main()
