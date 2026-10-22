"""Privacy controls for the separately authenticated proxy-management feature."""
import logging

class ProxyAccessLogFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            args = list(record.args)
            if isinstance(args[2], str) and args[2].split('?',1)[0].startswith('/api/proxy/bootstrap/'):
                args[2] = '/api/proxy/bootstrap/[redacted]'
                record.args = tuple(args)
        return True

class ProxyResponsePrivacy:
    def __init__(self, app):
        self.app = app
    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http' or not scope.get('path','').startswith('/api/proxy'):
            return await self.app(scope, receive, send)
        async def private_send(message):
            if message.get('type') == 'http.response.start':
                message = dict(message)
                headers = [(k,v) for k,v in message.get('headers',[]) if k.lower() not in (b'cache-control',b'pragma',b'referrer-policy')]
                headers.extend([(b'cache-control',b'no-store, private'),(b'pragma',b'no-cache'),(b'referrer-policy',b'no-referrer')])
                message['headers'] = headers
            await send(message)
        return await self.app(scope, receive, private_send)

def install_proxy_privacy(app):
    logger = logging.getLogger('uvicorn.access')
    if not any(isinstance(f,ProxyAccessLogFilter) for f in logger.filters):
        logger.addFilter(ProxyAccessLogFilter())
    app.add_middleware(ProxyResponsePrivacy)
