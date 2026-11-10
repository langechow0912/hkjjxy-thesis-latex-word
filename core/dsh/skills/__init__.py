# -*- coding: utf-8 -*-
"""DSH skill 发现与加载。

一个 skill = `core/dsh/skills/<name>/` 目录：
    SKILL.md        frontmatter（name / description / version / checkpoint）
    checkpoint.py   里面放 Checkpoint 子类
    prompts/        该 skill 用到的 prompt 模板

新增功能就是新增一个目录，不用改调度代码。目录名带连字符，不能当包 import，
所以一律按文件路径加载。
"""
import importlib.util
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parent
_FRONT = re.compile(r'\A---\s*\n(.*?)\n---\s*(?:\n|\Z)', re.S)


def parse_frontmatter(text):
    """极简 frontmatter 解析：只认 `key: value` 单行，够用就不引 yaml。"""
    m = _FRONT.match(text or '')
    if not m:
        return {}
    meta = {}
    for line in m.group(1).splitlines():
        if ':' in line and not line.lstrip().startswith('#'):
            key, _, value = line.partition(':')
            meta[key.strip()] = value.strip()
    return meta


def discover(root=None):
    """列出 skill 目录及元数据（不加载代码）。没有 SKILL.md 的目录会被忽略。"""
    root = Path(root) if root else SKILLS_DIR
    found = []
    for path in sorted(root.iterdir()):
        manifest = path / 'SKILL.md'
        if not path.is_dir() or path.name.startswith(('_', '.')) or not manifest.is_file():
            continue
        try:
            meta = parse_frontmatter(manifest.read_text(encoding='utf-8'))
        except OSError as exc:
            logger.warning("Cannot read %s: %s", manifest, exc)
            continue
        found.append({
            'dir': path.name,
            'path': path,
            'name': meta.get('name') or path.name,
            'description': meta.get('description', ''),
            'version': meta.get('version', ''),
            'checkpoint': meta.get('checkpoint', ''),
            'has_checkpoint': (path / 'checkpoint.py').is_file(),
        })
    return found


def load_module(skill_dir, root=None):
    """按路径加载某个 skill 的 checkpoint.py，返回模块。"""
    root = Path(root) if root else SKILLS_DIR
    file = root / skill_dir / 'checkpoint.py'
    spec = importlib.util.spec_from_file_location(
        'dsh_skill_' + re.sub(r'\W', '_', skill_dir), file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
