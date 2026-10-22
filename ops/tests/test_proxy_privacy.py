import asyncio,logging,unittest
from ops.proxy_privacy import ProxyAccessLogFilter,ProxyResponsePrivacy

class ProxyPrivacyTests(unittest.TestCase):
    def test_bootstrap_path_redacted(self):
        record=logging.LogRecord('uvicorn.access',20,'',0,'%s - "%s %s HTTP/%s" %s',('127.0.0.1','GET','/api/proxy/bootstrap/private-value?x=1','1.1',200),None)
        self.assertTrue(ProxyAccessLogFilter().filter(record))
        self.assertNotIn('private-value',record.getMessage())
        self.assertIn('[redacted]',record.getMessage())
    def test_other_log_unchanged(self):
        args=('127.0.0.1','GET','/api/health','1.1',200)
        record=logging.LogRecord('uvicorn.access',20,'',0,'%s %s %s %s %s',args,None)
        ProxyAccessLogFilter().filter(record)
        self.assertEqual(record.args,args)
    def test_no_store_for_proxy_routes_only(self):
        async def exercise(path):
            messages=[]
            async def app(scope,receive,send):
                await send({'type':'http.response.start','status':200,'headers':[(b'cache-control',b'public,max-age=600')]})
                await send({'type':'http.response.body','body':b'{}'})
            async def send(message):messages.append(message)
            await ProxyResponsePrivacy(app)({'type':'http','path':path},None,send)
            return dict(messages[0]['headers'])
        self.assertIn(b'no-store',asyncio.run(exercise('/api/proxy/devices'))[b'cache-control'])
        self.assertEqual(asyncio.run(exercise('/api/health'))[b'cache-control'],b'public,max-age=600')

if __name__=='__main__':unittest.main()
