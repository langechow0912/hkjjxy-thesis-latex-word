"""DSH 检查点基类"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..client import DeepSeekClient
    from ..cost_control import CostController


class Checkpoint(ABC):
    """
    DSH 检查点基类

    每个检查点负责一个特定的文档检查任务，例如：
    - 标题层级修复
    - 结构完整性验证
    - 内容保全检查
    - 最终质量评分

    通过继承此类并实现抽象方法，可以添加新的检查点。
    建议将自定义检查点放在 .claude/skills/dsh-xxx/checkpoint.py 中。
    """

    def __init__(
        self,
        config: Dict[str, Any],
        client: 'DeepSeekClient',
        cost: 'CostController'
    ):
        self.config = config
        self.client = client
        self.cost = cost
        self.dsh_config = config.get('dsh', {})

    @property
    @abstractmethod
    def name(self) -> str:
        """
        检查点名称（例如 cp1_preprocess, cp2_structure）

        命名规范：
        - 内置检查点：cp1_xxx, cp2_xxx...
        - 自定义检查点：cpx_skill_name
        """
        pass

    @abstractmethod
    def should_run(self, context: Dict[str, Any]) -> bool:
        """
        判断是否需要在当前上下文运行此检查点

        Args:
            context: 检查点上下文，包含：
                - source: str, DOCX 文件路径
                - work_dir: str, 工作目录
                - meta: Dict, 元数据（标题、作者等）
                - struct: Dict, 文档结构（cp2+ 有此字段）
                - pdf: str, PDF 文件路径（cp4 有此字段）

        Returns:
            是否运行此检查点

        示例：
            # 仅当文档有标题层级问题时运行
            return context.get('has_heading_issues', False)
        """
        pass

    @abstractmethod
    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行检查点

        Args:
            context: 同 should_run()

        Returns:
            {
                'success': bool,  # 是否成功执行
                'actions': [      # 执行的操作列表
                    {
                        'type': 'fix' | 'warn' | 'info',
                        'description': '操作描述',
                        'data': Any  # 额外数据
                    }
                ],
                'warnings': [str],  # 警告信息
                'fixed_docx': str,  # (可选) 修复后的 DOCX 路径
                'quality_score': int,  # (可选, cp4) 质量评分 0-100
                'issues': [str],  # (可选, cp4) 发现的问题列表
                'cost': int  # API 调用成本（tokens）
            }

        抛出异常时，DSHManager 会根据 fallback.on_error 配置决定如何处理。
        """
        pass

    def is_enabled(self) -> bool:
        """检查此检查点是否启用"""
        if not self.dsh_config.get('enabled', False):
            return False

        enabled_checkpoints = self.dsh_config.get('checkpoints', [])
        return self.name in enabled_checkpoints


__all__ = ['Checkpoint']
