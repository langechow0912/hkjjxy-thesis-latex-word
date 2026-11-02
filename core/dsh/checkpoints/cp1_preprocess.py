"""CP1: 文档预处理检查点（占位实现）"""

from typing import Dict, Any
from . import Checkpoint


class PreprocessCheckpoint(Checkpoint):
    """
    文档预处理检查点

    功能：
    - 标题层级修复（自动补全缺失的上级标题）
    - 图片完整性检查
    - 格式规范化建议

    TODO: Phase 2 完整实现，当前仅返回占位结果
    """

    @property
    def name(self) -> str:
        return 'cp1_preprocess'

    def should_run(self, context: Dict[str, Any]) -> bool:
        """总是运行（因为是第一个检查点）"""
        return context.get('source') is not None

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行预处理

        Phase 1: 返回占位结果
        Phase 2: 调用 DeepSeek API 分析文档并生成修复建议
        """
        # Phase 1: 占位实现
        return {
            'success': True,
            'actions': [
                {
                    'type': 'info',
                    'description': 'DSH 预处理已启用（Phase 1 占位）',
                    'data': None
                }
            ],
            'warnings': [],
            'cost': 0
        }

        # Phase 2 实现预览（注释掉）：
        # result = await self.client.analyze_document(
        #     context['source'],
        #     'preprocess',
        #     meta=context.get('meta', {})
        # )
        #
        # if not result['success']:
        #     return result
        #
        # actions = []
        # for fix in result['data'].get('fixes', []):
        #     if fix['type'] == 'heading_hierarchy':
        #         actions.append({
        #             'type': 'fix',
        #             'description': f"建议在「{fix['after']}」之前添加{fix['level']}级标题「{fix['title']}」",
        #             'data': fix
        #         })
        #
        # return {
        #     'success': True,
        #     'actions': actions,
        #     'warnings': result['data'].get('warnings', []),
        #     'cost': result['cost']
        # }
