"""
DeepSeek Harness Integration
智能文档处理与质量检查框架

检查点说明：
- cp1_preprocess: 文档预处理（标题层级、格式规范化）
- cp2_structure: 结构验证（章节逻辑、参考文献）
- cp3_integrity: 内容保全（对比检测、丢失内容）
- cp4_quality: 最终质检（目录准确性、格式合规）

使用方式：
    from core.dsh import DSHManager

    dsh = DSHManager(cfg)
    result = await dsh.run_checkpoint('cp1_preprocess', {
        'source': '/path/to/source.docx',
        'work_dir': '/tmp/work',
        'meta': {...}
    })
"""

from typing import Dict, Any, Optional, List
import asyncio
import logging
from pathlib import Path

from .client import DeepSeekClient
from .cost_control import CostController
from .checkpoints import Checkpoint

logger = logging.getLogger(__name__)


class DSHManager:
    """DeepSeek Harness 管理器"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.dsh_config = config.get('dsh', {})
        self.enabled = self.dsh_config.get('enabled', False)

        if self.enabled:
            self.client = DeepSeekClient(self.dsh_config)
            self.cost = CostController(self.dsh_config)
            self.checkpoints: Dict[str, Checkpoint] = {}
            self._register_checkpoints()
        else:
            self.client = None
            self.cost = None
            self.checkpoints = {}

    def _register_checkpoints(self):
        """注册检查点（从 skill 加载）"""
        # 内置检查点（未来移到 skill）
        from .checkpoints.cp1_preprocess import PreprocessCheckpoint
        from .checkpoints.cp4_quality import QualityCheckpoint

        for cp_class in [PreprocessCheckpoint, QualityCheckpoint]:
            try:
                instance = cp_class(self.config, self.client, self.cost)
                self.checkpoints[instance.name] = instance
                logger.info(f"Registered checkpoint: {instance.name}")
            except Exception as e:
                logger.warning(f"Failed to register {cp_class.__name__}: {e}")

        # TODO: 从 .claude/skills/dsh-*/checkpoint.py 加载自定义检查点
        self._load_skill_checkpoints()

    def _load_skill_checkpoints(self):
        """从 skill 目录加载检查点"""
        skills_dir = Path('.claude/skills')
        if not skills_dir.exists():
            return

        for skill_dir in skills_dir.iterdir():
            if not skill_dir.is_dir() or not skill_dir.name.startswith('dsh-'):
                continue

            checkpoint_file = skill_dir / 'checkpoint.py'
            if not checkpoint_file.exists():
                continue

            try:
                # 动态加载 checkpoint.py
                import importlib.util
                spec = importlib.util.spec_from_file_location(
                    f"skill_{skill_dir.name}", checkpoint_file
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                # 查找 Checkpoint 子类
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (isinstance(attr, type) and
                        issubclass(attr, Checkpoint) and
                        attr != Checkpoint):
                        instance = attr(self.config, self.client, self.cost)
                        self.checkpoints[instance.name] = instance
                        logger.info(f"Loaded skill checkpoint: {instance.name} from {skill_dir.name}")
            except Exception as e:
                logger.warning(f"Failed to load checkpoint from {skill_dir.name}: {e}")

    async def run_checkpoint(
        self,
        name: str,
        context: Dict[str, Any],
        job_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        运行指定检查点

        Args:
            name: 检查点名称（cp1_preprocess, cp2_structure 等）
            context: 检查点上下文（source, work_dir, meta 等）
            job_id: 任务 ID（用于成本控制）

        Returns:
            {
                'success': bool,
                'actions': [{'type': 'fix'|'warn', 'description': str, 'data': any}],
                'warnings': [str],
                'fixed_docx': str,  # 如果有修复文件
                'quality_score': int,  # cp4 返回
                'issues': [str],  # cp4 返回
                'cost': float  # API 调用成本（tokens）
            }
        """
        if not self.enabled:
            return None

        cp = self.checkpoints.get(name)
        if not cp or not cp.is_enabled():
            logger.debug(f"Checkpoint {name} not enabled or not found")
            return None

        if not cp.should_run(context):
            logger.debug(f"Checkpoint {name} skipped (should_run=False)")
            return None

        # 检查配额
        if not self.cost.can_run(name, job_id):
            logger.warning(f"Checkpoint {name} quota exceeded")
            return {
                'success': False,
                'reason': 'quota_exceeded',
                'actions': [],
                'warnings': ['DSH 配额已用完，已跳过智能检查']
            }

        try:
            result = await cp.execute(context)
            self.cost.record(name, job_id, result.get('cost', 0))
            return result
        except asyncio.TimeoutError:
            logger.warning(f"Checkpoint {name} timeout")
            return {'success': False, 'reason': 'timeout', 'actions': []}
        except Exception as e:
            logger.error(f"Checkpoint {name} failed: {e}", exc_info=True)
            fallback = self.dsh_config.get('fallback', {}).get('on_error', 'warn')
            if fallback == 'error':
                raise
            return {'success': False, 'error': str(e), 'actions': []}

    def list_checkpoints(self) -> List[Dict[str, str]]:
        """列出所有已注册的检查点"""
        return [
            {
                'name': name,
                'enabled': cp.is_enabled(),
                'description': cp.__doc__ or ''
            }
            for name, cp in self.checkpoints.items()
        ]


__all__ = ['DSHManager', 'Checkpoint']
