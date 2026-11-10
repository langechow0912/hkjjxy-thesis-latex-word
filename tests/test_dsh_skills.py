# -*- coding: utf-8 -*-
"""DSH skill 的发现与加载：新增功能 = 新增目录，坏 skill 不能拖垮其他。"""
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from core import profile
from core.dsh import DSHManager, skills


def _write(root, name, manifest=True, code=None):
    d = Path(root) / name
    d.mkdir()
    if manifest:
        (d / 'SKILL.md').write_text(
            '---\nname: %s\ndescription: 测试 skill\nversion: 0.0.1\n---\n正文' % name, encoding='utf-8')
    if code is not None:
        (d / 'checkpoint.py').write_text(code, encoding='utf-8')


class SkillDiscovery(unittest.TestCase):

    def test_builtin_skills_are_found_with_metadata(self):
        by_dir = {s['dir']: s for s in skills.discover()}
        self.assertIn('dsh-preprocess', by_dir)
        self.assertEqual(by_dir['dsh-preprocess']['checkpoint'], 'cp1_preprocess')
        self.assertTrue(by_dir['dsh-preprocess']['has_checkpoint'])
        self.assertTrue(by_dir['dsh-preprocess']['description'])

    def test_directory_without_manifest_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, 'dsh-ok', code='x = 1')
            _write(tmp, 'dsh-nomanifest', manifest=False, code='x = 1')
            self.assertEqual([s['dir'] for s in skills.discover(tmp)], ['dsh-ok'])

    def test_frontmatter_without_header_is_empty(self):
        self.assertEqual(skills.parse_frontmatter('没有头部'), {})


class ManagerLoading(unittest.TestCase):

    def cfg(self):
        cfg = profile.load('hkjjxy')
        cfg['dsh']['enabled'] = True
        cfg['dsh']['api'] = {'api_key': 'test-key'}
        return cfg

    def test_manager_registers_skills_and_reports_metadata(self):
        manager = DSHManager(self.cfg())
        listing = {c['name']: c for c in manager.list_checkpoints()}
        self.assertIn('cp1_preprocess', listing)
        self.assertEqual(listing['cp1_preprocess']['skill'], 'dsh-preprocess')
        self.assertTrue(listing['cp1_preprocess']['description'])

    def test_broken_skill_does_not_block_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, 'dsh-broken', code='raise RuntimeError("boom")')
            real = skills.discover()
            broken = skills.discover(tmp)
            with unittest.mock.patch.object(skills, 'discover', return_value=broken + real):
                orig = skills.load_module

                def load(name, root=None):
                    return orig(name, tmp if name == 'dsh-broken' else None)
                with unittest.mock.patch.object(skills, 'load_module', side_effect=load):
                    manager = DSHManager(self.cfg())
        self.assertIn('cp1_preprocess', manager.checkpoints)
        self.assertNotIn('dsh-broken', manager.skills)


if __name__ == '__main__':
    unittest.main()
