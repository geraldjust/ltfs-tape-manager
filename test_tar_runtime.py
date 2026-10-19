import hashlib
import tempfile
import unittest
from pathlib import Path
from tar_planning import plan_archives
from tar_runtime import create_archive, extract_members, load_manifest

class TarTests(unittest.TestCase):
    def test_planner_siblings_and_limit(self):
        files=[('a/one.txt',12),('a/two.txt',0),('b/three.txt',9),('a/large.bin',16*1024*1024)]
        direct, archives=plan_archives(files)
        self.assertEqual([x[0] for x in direct],['a/large.bin'])
        self.assertEqual(len(archives),2)
        self.assertEqual([x[0] for x in archives[0]['members']],['a/one.txt','a/two.txt'])
        self.assertTrue(all(a['estimated_bytes']<=32*1024**3 for a in archives))
    def test_stream_roundtrip_selected(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);src=root/'src';src.mkdir()
            files=[]
            for name,data in [('alpha.txt',b'hello'*1000),('beta.bin',b'\\x00\\xff'*6000),('empty',b'')]:
                p=src/name;p.write_bytes(data)
                files.append((name,p,len(data),p.stat().st_mtime_ns))
            tape=root/'tape';tape.mkdir()
            archive=tape/'small-files-001.tar';manifest=tape/'small-files-001.tar.manifest.json'
            created=create_archive(archive,manifest,files)
            self.assertEqual(len(created['members']),3)
            self.assertEqual(load_manifest(archive)['archive_sha256'],hashlib.sha256(archive.read_bytes()).hexdigest())
            restore=root/'restore';restore.mkdir()
            count=extract_members(archive,restore,['beta.bin'])
            self.assertEqual(count,1)
            self.assertEqual((restore/'beta.bin').read_bytes(),(src/'beta.bin').read_bytes())
            self.assertFalse((restore/'alpha.txt').exists())
            with self.assertRaises(ValueError):
                extract_members(archive,restore,['../escape'])
    def test_corrupt_archive_detected(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);p=root/'source';p.write_bytes(b'hello world')
            archive=root/'a.tar';manifest=root/'a.tar.manifest.json'
            create_archive(archive,manifest,[('source',p,11,p.stat().st_mtime_ns)])
            with open(archive,'r+b') as f:
                f.seek(520);f.write(b'X')
            out=root/'restore';out.mkdir()
            with self.assertRaises(ValueError):
                extract_members(archive,out,['source'])

if __name__=='__main__':
    unittest.main()
