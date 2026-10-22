import importlib.util,sys,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
P=Path(__file__).resolve().parents[1]/'proxy_device.py'
spec=importlib.util.spec_from_file_location('proxy_vpn_adapter',P)
m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
NET='NetworkAgentInfo{ ni{[type: VPN[], state: CONNECTED/CONNECTED]} network{606} }'
SERVICE='* ServiceRecord{a u0 '+m.PACKAGE+'/com.github.kr328.clash.service.TunService}\nintent={act=android.net.VpnService cmp='+m.PACKAGE+'/com.github.kr328.clash.service.TunService}'

class VpnOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_android9_connected_managed_service_without_owner_field(self):
        d=m.ProxyDeviceDriver(AsyncMock(),AsyncMock());d._run_adb=AsyncMock(side_effect=[(0,'null'),(0,NET),(0,'tun0 UNKNOWN 172.19.0.1/30'),(0,SERVICE)])
        self.assertEqual(await d._vpn_state('emulator-5554'),('null',False,True))

    async def test_unknown_active_vpn_is_other_and_never_claimed_managed(self):
        d=m.ProxyDeviceDriver(AsyncMock(),AsyncMock());d._run_adb=AsyncMock(side_effect=[(0,'null'),(0,NET),(0,'tun0 UNKNOWN 172.19.0.1/30'),(0,'')])
        self.assertEqual(await d._vpn_state('emulator-5554'),('null',True,False))

    async def test_another_always_on_owner_cannot_be_overridden_by_managed_service(self):
        d=m.ProxyDeviceDriver(AsyncMock(),AsyncMock());d._run_adb=AsyncMock(side_effect=[(0,'other.vpn'),(0,NET),(0,'tun0 UNKNOWN 172.19.0.1/30'),(0,SERVICE)])
        self.assertEqual(await d._vpn_state('emulator-5554'),('other.vpn',True,False))

    async def test_service_or_proxy_without_tunnel_is_not_success(self):
        d=m.ProxyDeviceDriver(AsyncMock(),AsyncMock());d._run_adb=AsyncMock(side_effect=[(0,'null'),(0,NET),(0,'wlan0 UP 192.168.50.10/24')])
        self.assertEqual(await d._vpn_state('emulator-5554'),('null',False,False))

    async def test_stale_service_without_connected_vpn_is_not_success(self):
        d=m.ProxyDeviceDriver(AsyncMock(),AsyncMock());d._run_adb=AsyncMock(side_effect=[(0,'null'),(0,'type: VPN state: DISCONNECTED'),(0,'tun0 UNKNOWN 172.19.0.1/30'),(0,SERVICE)])
        self.assertEqual(await d._vpn_state('emulator-5554'),('null',True,False))

    async def test_explicit_other_owner_is_never_success(self):
        d=m.ProxyDeviceDriver(AsyncMock(),AsyncMock());d._run_adb=AsyncMock(side_effect=[(0,'null'),(0,'mPackage=other.vpn\n'+NET),(0,'tun0 UNKNOWN 172.19.0.1/30')])
        self.assertEqual(await d._vpn_state('emulator-5554'),('null',True,False))

    async def test_explicit_managed_owner_remains_supported(self):
        d=m.ProxyDeviceDriver(AsyncMock(),AsyncMock());d._run_adb=AsyncMock(side_effect=[(0,'null'),(0,'mPackage='+m.PACKAGE+'\n'+NET),(0,'tun0 UNKNOWN 172.19.0.1/30')])
        self.assertEqual(await d._vpn_state('emulator-5554'),('null',False,True))

if __name__=='__main__':unittest.main()
