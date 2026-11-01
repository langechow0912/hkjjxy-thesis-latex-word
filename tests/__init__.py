# -*- coding: utf-8 -*-
"""离线测试：不联网、不需要 PAT、不产生计费。"""
import atexit
import shutil
import tempfile
from pathlib import Path

from core import accounts

# 账号数据写临时库，绝不碰 .secrets 里的真实数据；有的用例会清空环境变量，所以直接替换函数。
_ACCOUNT_DIR = tempfile.mkdtemp(prefix='thesis-accounts-test-')
atexit.register(shutil.rmtree, _ACCOUNT_DIR, True)
accounts._db_path = lambda: Path(_ACCOUNT_DIR) / 'accounts.sqlite3'

TEST_USER = {'id': 1, 'username': 'tester'}


def signed_in():
    return TEST_USER


def sign_in_by_default():
    """上传接口要求登录；旧用例不关心账号，统一当作已登录的 TEST_USER"""
    from web import app as web
    from web.account_routes import current_user
    web.app.dependency_overrides[current_user] = signed_in


sign_in_by_default()
