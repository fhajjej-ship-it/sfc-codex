"""Exercise the public release downloader with local fake downloads."""
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import install_release


class InstallReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'releases'
        self.version = '1.2.3'

    def payload(self, extra=None):
        buffer = BytesIO()
        with ZipFile(buffer, 'w') as archive:
            archive.writestr('sfc/scripts/install_codex_plugin.py', '# Fixture installer\n')
            if extra:
                archive.writestr(extra, 'unexpected')
        return buffer.getvalue()

    def checksum(self, data):
        return f'{sha256(data).hexdigest()}  sfc-{self.version}.zip\n'.encode()

    def test_keeps_verified_source_and_refuses_replacement(self):
        data = self.payload()
        with patch.object(install_release, 'download', side_effect=[self.checksum(data), data]) as download:
            root = install_release.prepare(self.version, self.root)
            with self.assertRaisesRegex(ValueError, 'already has an installation folder'):
                install_release.prepare(self.version, self.root)
        self.assertEqual(root, self.root / self.version / 'sfc')
        self.assertTrue((root / 'scripts/install_codex_plugin.py').is_file())
        self.assertTrue(download.call_args_list[0].args[0].endswith('/v1.2.3/sfc-1.2.3.zip.sha256'))
        self.assertEqual(download.call_count, 2)

    def test_rejects_tampered_data_before_creating_installation(self):
        data = self.payload()
        with patch.object(install_release, 'download', side_effect=[self.checksum(data), data + b'extra']):
            with self.assertRaisesRegex(ValueError, 'Checksum mismatch'):
                install_release.prepare(self.version, self.root)
        self.assertFalse(self.root.exists())

    def test_rejects_paths_outside_the_package(self):
        data = self.payload('sfc/../unexpected')
        with patch.object(install_release, 'download', side_effect=[self.checksum(data), data]):
            with self.assertRaisesRegex(ValueError, 'Unexpected file'):
                install_release.prepare(self.version, self.root)
        self.assertFalse((self.root / self.version).exists())


if __name__ == '__main__':
    unittest.main()
