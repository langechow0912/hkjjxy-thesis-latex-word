"""DSH 缓存目录：生产容器根文件系统只读，建不出缓存目录不能让 AI 对话整个失败。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.dsh.client import DeepSeekClient


class CacheDir(unittest.TestCase):
    def test_default_is_under_temp_dir_not_cwd(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {'DSH_CACHE_DIR': ''}), \
                patch('tempfile.gettempdir', return_value=tmp):
            client = DeepSeekClient({})
            self.assertEqual(client.cache_dir, Path(tmp) / 'dsh_cache')
            self.assertTrue(client.cache_dir.is_dir())

    def test_env_overrides_location(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {'DSH_CACHE_DIR': str(Path(tmp) / 'x' / 'y')}):
            self.assertTrue(DeepSeekClient({}).cache_dir.is_dir())

    def test_read_only_filesystem_disables_cache_instead_of_raising(self):
        # 报警里的原始错误：OSError: [Errno 30] Read-only file system: '.dsh_cache'
        with patch.object(Path, 'mkdir', side_effect=OSError(30, 'Read-only file system')):
            client = DeepSeekClient({})
        self.assertIsNone(client.cache_dir)
        client._set_cache('k', {'a': 1})                 # 不抛
        self.assertIsNone(client._get_cache('k'))


if __name__ == '__main__':
    unittest.main()
