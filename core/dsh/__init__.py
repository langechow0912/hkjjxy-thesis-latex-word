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
            self.skills: Dict[str, Dict[str, Any]] = {}
            self._register_checkpoints()
        else:
            self.client = None
            self.cost = None
            self.checkpoints = {}
            self.skills = {}

    def _register_checkpoints(self):
        """扫描 core/dsh/skills/ 注册检查点；单个 skill 坏了只跳过它。"""
        from . import skills

        self.skills: Dict[str, Dict[str, Any]] = {}
        for info in skills.discover():
            if not info['has_checkpoint']:
                continue
            try:
                module = skills.load_module(info['dir'])
                for attr in vars(module).values():
                    if (isinstance(attr, type) and issubclass(attr, Checkpoint)
                            and attr is not Checkpoint
                            and attr.__module__ == module.__name__):
                        instance = attr(self.config, self.client, self.cost)
                        self.checkpoints[instance.name] = instance
                        self.skills[instance.name] = info
                        logger.info("Registered checkpoint %s from skill %s",
                                    instance.name, info['dir'])
            except Exception as e:
                logger.warning("Failed to load skill %s: %s", info['dir'], e)

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

    def list_checkpoints(self) -> List[Dict[str, Any]]:
        """列出所有已注册的检查点及其 skill 元数据"""
        out = []
        for name, cp in self.checkpoints.items():
            info = (getattr(self, 'skills', {}) or {}).get(name, {})
            out.append({
                'name': name,
                'enabled': cp.is_enabled(),
                'description': info.get('description') or cp.__doc__ or '',
                'skill': info.get('dir', ''),
                'version': info.get('version', ''),
            })
        return out


__all__ = ['DSHManager', 'Checkpoint']
