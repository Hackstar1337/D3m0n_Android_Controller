import os,socket,threading,io,importlib.util,pathlib,unittest
P=pathlib.Path(__file__).parent/'client.py';spec=importlib.util.spec_from_file_location('fixedclient',P);client=importlib.util.module_from_spec(spec);spec.loader.exec_module(client)
@unittest.skipIf(os.name=='nt','Unix selector/pipe gate runs on actual Ubuntu')
class Interactive(unittest.TestCase):
 def test_short_frame_before_eof(self):
  r,w=os.pipe();stdin=os.fdopen(r,'rb');left,right=socket.socketpair();output=io.BytesIO();thread=threading.Thread(target=client.relay,args=(left,stdin,output),daemon=True);thread.start()
  try:
   os.write(w,b'{"short":true}\n');right.settimeout(1);self.assertEqual(right.recv(100),b'{"short":true}\n');self.assertTrue(thread.is_alive());right.sendall(b'{"response":true}\n');right.shutdown(socket.SHUT_WR);thread.join(1);self.assertFalse(thread.is_alive());self.assertEqual(output.getvalue(),b'{"response":true}\n')
  finally:os.close(w);stdin.close();left.close();right.close()
if __name__=='__main__':unittest.main(verbosity=2)
