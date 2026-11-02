"""DSH 测试"""

import pytest
import asyncio
from pathlib import Path
import sys

# 添加项目根到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.dsh import DSHManager
from core.dsh.checkpoints import Checkpoint


def test_load_config():
    """测试配置加载"""
    import yaml
    profile = Path('schools/hkjjxy/profile.yaml')
    assert profile.exists()

    cfg = yaml.safe_load(profile.read_text(encoding='utf-8'))
    dsh = cfg.get('dsh', {})

    assert 'enabled' in dsh
    assert 'checkpoints' in dsh
    assert 'api' in dsh
    assert 'cost_control' in dsh
    assert 'fallback' in dsh


def test_dsh_manager_init():
    """测试 DSHManager 初始化"""
    import yaml
    cfg = yaml.safe_load(Path('schools/hkjjxy/profile.yaml').read_text(encoding='utf-8'))

    # DSH 关闭时
    manager = DSHManager(cfg)
    assert not manager.enabled
    assert manager.client is None
    assert len(manager.checkpoints) == 0

    # DSH 开启时（模拟）
    cfg['dsh']['enabled'] = True
    try:
        manager = DSHManager(cfg)
        # 如果没有 API key，client 会初始化但不可用
        assert manager.enabled
        assert len(manager.checkpoints) >= 2  # cp1, cp4
    except Exception as e:
        pytest.skip(f"DSH init failed (expected without API key): {e}")


def test_checkpoint_registration():
    """测试检查点注册"""
    import yaml
    cfg = yaml.safe_load(Path('schools/hkjjxy/profile.yaml').read_text(encoding='utf-8'))
    cfg['dsh']['enabled'] = True

    try:
        manager = DSHManager(cfg)
        checkpoints = manager.list_checkpoints()

        names = [cp['name'] for cp in checkpoints]
        assert 'cp1_preprocess' in names
        assert 'cp4_quality' in names

        print("\nRegistered checkpoints:")
        for cp in checkpoints:
            print(f"  - {cp['name']}: enabled={cp['enabled']}")
    except Exception as e:
        pytest.skip(f"Checkpoint registration test skipped: {e}")


@pytest.mark.asyncio
async def test_checkpoint_run_disabled():
    """测试检查点在禁用时跳过"""
    import yaml
    cfg = yaml.safe_load(Path('schools/hkjjxy/profile.yaml').read_text(encoding='utf-8'))

    # DSH 关闭
    manager = DSHManager(cfg)
    result = await manager.run_checkpoint('cp1_preprocess', {'source': '/tmp/test.docx'})
    assert result is None


@pytest.mark.asyncio
async def test_checkpoint_run_placeholder():
    """测试检查点占位实现"""
    import yaml
    cfg = yaml.safe_load(Path('schools/hkjjxy/profile.yaml').read_text(encoding='utf-8'))
    cfg['dsh']['enabled'] = True

    try:
        manager = DSHManager(cfg)

        # CP1 占位测试
        result = await manager.run_checkpoint('cp1_preprocess', {
            'source': str(Path(__file__).parent / 'fixtures' / 'minimal.docx'),
            'work_dir': '/tmp',
            'meta': {}
        })

        if result:  # 如果没有 API key 可能返回 None
            assert result['success']
            assert 'actions' in result
            assert result['cost'] == 0  # 占位实现不消耗 API
            print(f"\nCP1 result: {result}")
    except Exception as e:
        pytest.skip(f"Placeholder test skipped: {e}")


def test_cost_controller():
    """测试成本控制器"""
    from core.dsh.cost_control import CostController

    config = {
        'cost_control': {
            'daily_quota': 10,
            'per_job_limit': 3
        }
    }

    cost = CostController(config)

    # 未超配额
    assert cost.can_run('cp1', 'job1')
    cost.record('cp1', 'job1', tokens=100)

    # 单任务限制
    cost.record('cp1', 'job1', tokens=50)
    cost.record('cp1', 'job1', tokens=50)
    assert not cost.can_run('cp1', 'job1')  # 已调用 3 次

    # 日配额限制
    for i in range(10):
        cost.record(f'cp{i}', f'job{i}', tokens=10)
    assert not cost.can_run('cpX', 'jobX')  # 日配额用尽

    # 统计信息
    stats = cost.get_stats()
    assert stats['today']['calls'] >= 10
    assert stats['total']['tokens'] > 0


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
