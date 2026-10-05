import os
import tempfile
import unittest
from pathlib import Path
from organizer.storage import Store
from organizer.secrets import save_key, load_key
from organizer.filesystem import native, move_no_replace

@unittest.skipUnless(os.name == 'nt', 'Windows-only integration')
class WindowsTests(unittest.TestCase):
    def test_dpapi_roundtrip_not_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'test.db')
            save_key(store,'test-secret-not-real')
            self.assertEqual(load_key(store),'test-secret-not-real')
            self.assertNotIn('test-secret-not-real',store.path.read_bytes().decode('latin1'))
    def test_long_unicode_path_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            for i in range(7): folder=folder/('한글경로'+str(i)+'_'+'x'*35)
            Path(native(folder)).mkdir(parents=True)
            src=folder/'한글파일.txt'; dst=folder/'이동파일.txt'
            Path(native(src)).write_text('sample',encoding='utf-8')
            move_no_replace(src,dst)
            self.assertEqual(Path(native(dst)).read_text(encoding='utf-8'),'sample')
            self.assertFalse(Path(native(src)).exists())
if __name__=='__main__': unittest.main()
