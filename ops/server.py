"""Serve the upstream production dashboard and API from one local origin."""
from pathlib import Path
from urllib.parse import urlsplit
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles
from gitd.app import app
from ops.proxy_privacy import install_proxy_privacy
install_proxy_privacy(app)
app.title = "D3m0n in the droid"
from starlette.responses import JSONResponse
from ops.platform_config import get_allowed_origins, get_public_origin


def _configured_public_host() -> str | None:
    origin = get_public_origin()
    return urlsplit(origin).hostname if origin else None


class D3m0nDomainProtection:
    """Reject browser cross-origin control requests on the HTTPS domain."""
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']=='http':
            headers=dict(scope.get('headers',[]))
            host=headers.get(b'host',b'').decode().split(':')[0]
            if host == _configured_public_host() and scope['method'] not in {'GET','HEAD','OPTIONS'}:
                origin=headers.get(b'origin',b'').decode()
                fetch_site=headers.get(b'sec-fetch-site',b'').decode()
                if (origin and origin not in get_allowed_origins()) or fetch_site=='cross-site':
                    await JSONResponse({'ok':False,'error':'Cross-origin device control denied'},status_code=403)(scope,receive,send)
                    return
        await self.app(scope,receive,send)
app.add_middleware(D3m0nDomainProtection)
class FleetUploadBodyLimit:
    """Bound multipart bodies before Starlette spools their file content."""
    def __init__(self, app): self.app = app
    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http' or scope.get('method') != 'POST' or scope.get('path') != '/api/fleet/files':
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        origin = headers.get(b'origin', b'').decode()
        if (origin and origin not in get_allowed_origins()) or headers.get(b'sec-fetch-site') == b'cross-site':
            return await JSONResponse({'detail':'Cross-origin upload denied'}, status_code=403)(scope, receive, send)
        limit = 257 * 1024 * 1024
        try: length = int(headers.get(b'content-length', b'0'))
        except ValueError: length = limit + 1
        if length < 0 or length > limit:
            return await JSONResponse({'detail':'Maximum file size is 256 MiB'}, status_code=413)(scope, receive, send)
        seen = 0
        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message.get('type') == 'http.request':
                seen += len(message.get('body', b''))
                if seen > limit:
                    from starlette.formparsers import MultiPartException
                    raise MultiPartException('Maximum file size is 256 MiB')
            return message
        return await self.app(scope, limited_receive, send)
app.add_middleware(FleetUploadBodyLimit)


class DashboardFiles(StaticFiles):
    async def get_response(self,path,scope):
        try:
            return await super().get_response(path,scope)
        except HTTPException as error:
            if error.status_code != 404 or path.startswith('api/'):
                raise
            return await super().get_response('index.html',scope)

# Prepend a concrete Starlette root route. New FastAPI releases retain
# included routers as nested objects, so deleting their path cannot override
# the upstream root redirect. All existing API/docs routes remain unchanged.
from starlette.responses import FileResponse
from starlette.routing import Route
DASHBOARD=Path(__file__).resolve().parents[1]/'frontend'/'dist'
async def dashboard_index(request):
    return FileResponse(DASHBOARD/'index.html')
app.router.routes.insert(0,Route('/',dashboard_index,methods=['GET','HEAD']))
from ops.android_h264 import router as android_h264_router
app.include_router(android_h264_router)
# Instant input over the scrcpy control socket plus SYNC ALL fan-out. These
# routes are placed ahead of the upstream /api/phone/tap|swipe|key|input and
# delegate to them whenever the fast path does not apply.
from ops.fast_control import install_fast_control
install_fast_control(app)
from ops.fleet_control import router as fleet_router
app.include_router(fleet_router)
# Persistent phone numbers (1, 2, 3, ...) shared by every browser.
from ops.fleet_numbers import router as fleet_numbers_router
app.include_router(fleet_numbers_router)
# Confirmed phone reboot with USB-assisted wireless ADB recovery status.
from ops.fleet_reboot import router as fleet_reboot_router
app.include_router(fleet_reboot_router)
from ops.fleet_files import router as fleet_files_router
app.include_router(fleet_files_router)
from ops.proxy_manager import router as proxy_manager_router
app.include_router(proxy_manager_router)
# Secure, encrypted provider configuration.  Keep this registration before the
# dashboard gate so the gate remains the outermost protection for these routes.
from gitd.routers.agent_chat import ai_settings_router
app.include_router(ai_settings_router)
from ops.ai_settings import install_ai_settings
install_ai_settings(app)
# Owner-only Mobilerun fleet control plane.  Keep this registration before the
# dashboard gate; install_dashboard_auth remains the outermost middleware.
from ops.ai_fleet_api import install_ai_fleet
install_ai_fleet(app)
# LAN discovery is additive and optional: an older source export can still boot
# without the module, while a reconciled export registers its authenticated API.
try:
    from ops.lan_discovery_api import router as lan_discovery_router
except ImportError:
    lan_discovery_router = None
if lan_discovery_router is not None:
    app.include_router(lan_discovery_router)
# Dashboard-wide password unlock: registered last so the gate is the outermost
# middleware and covers every HTTP and WebSocket route, including the mount.
from ops.dashboard_auth import install_dashboard_auth
install_dashboard_auth(app, DASHBOARD)
app.mount('/',DashboardFiles(directory=DASHBOARD,html=True),name='dashboard')
