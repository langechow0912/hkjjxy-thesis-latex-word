"""DSH 调用成本控制"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class CostController:
    """
    DSH 成本控制器

    功能：
    - 日调用配额限制
    - 单任务调用次数限制
    - 成本统计与报告
    """

    def __init__(self, config: Dict[str, Any]):
        cost_config = config.get('cost_control', {})
        self.daily_quota = cost_config.get('daily_quota', 1000)
        self.per_job_limit = cost_config.get('per_job_limit', 5)

        self.state_file = Path('.dsh_cost.json')
        self._load_state()

    def _load_state(self):
        """加载状态"""
        if self.state_file.exists():
            try:
                self.state = json.loads(self.state_file.read_text(encoding='utf-8'))
            except Exception as e:
                logger.warning(f"Failed to load cost state: {e}")
                self.state = self._init_state()
        else:
            self.state = self._init_state()

    def _init_state(self) -> Dict[str, Any]:
        """初始化状态"""
        return {
            'daily': {},  # {date: count}
            'jobs': {},   # {job_id: count}
            'checkpoints': {},  # {checkpoint: total_count}
            'tokens': {'total': 0, 'by_checkpoint': {}}
        }

    def _save_state(self):
        """保存状态"""
        try:
            self.state_file.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=2),
                encoding='utf-8'
            )
        except Exception as e:
            logger.warning(f"Failed to save cost state: {e}")

    def _today(self) -> str:
        """获取今天日期"""
        return datetime.now().strftime('%Y-%m-%d')

    def can_run(self, checkpoint: str, job_id: Optional[str] = None) -> bool:
        """
        检查是否允许运行

        Args:
            checkpoint: 检查点名称
            job_id: 任务 ID

        Returns:
            是否允许运行
        """
        today = self._today()

        # 检查日配额
        daily_count = self.state['daily'].get(today, 0)
        if daily_count >= self.daily_quota:
            logger.warning(f"Daily quota exceeded: {daily_count}/{self.daily_quota}")
            return False

        # 检查单任务限制
        if job_id:
            job_count = self.state['jobs'].get(job_id, 0)
            if job_count >= self.per_job_limit:
                logger.warning(f"Job {job_id} quota exceeded: {job_count}/{self.per_job_limit}")
                return False

        return True

    def record(self, checkpoint: str, job_id: Optional[str] = None, tokens: int = 0):
        """
        记录一次调用

        Args:
            checkpoint: 检查点名称
            job_id: 任务 ID
            tokens: 消耗的 tokens 数量
        """
        today = self._today()

        # 更新日计数
        self.state['daily'][today] = self.state['daily'].get(today, 0) + 1

        # 更新任务计数
        if job_id:
            self.state['jobs'][job_id] = self.state['jobs'].get(job_id, 0) + 1

        # 更新检查点计数
        self.state['checkpoints'][checkpoint] = self.state['checkpoints'].get(checkpoint, 0) + 1

        # 更新 token 统计
        self.state['tokens']['total'] += tokens
        cp_tokens = self.state['tokens']['by_checkpoint']
        cp_tokens[checkpoint] = cp_tokens.get(checkpoint, 0) + tokens

        self._save_state()

        logger.info(f"DSH call recorded: {checkpoint}, tokens={tokens}, job={job_id}")

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        today = self._today()
        return {
            'today': {
                'date': today,
                'calls': self.state['daily'].get(today, 0),
                'quota': self.daily_quota,
                'remaining': max(0, self.daily_quota - self.state['daily'].get(today, 0))
            },
            'total': {
                'calls': sum(self.state['checkpoints'].values()),
                'tokens': self.state['tokens']['total']
            },
            'by_checkpoint': self.state['checkpoints']
        }

    def cleanup_old_data(self, days: int = 7):
        """清理旧数据"""
        from datetime import timedelta

        cutoff = datetime.now() - timedelta(days=days)
        cutoff_str = cutoff.strftime('%Y-%m-%d')

        # 清理旧的日计数
        old_dates = [d for d in self.state['daily'].keys() if d < cutoff_str]
        for date in old_dates:
            del self.state['daily'][date]

        # 清理旧的任务计数（保留最近 1000 个）
        if len(self.state['jobs']) > 1000:
            # 简单策略：清空所有任务计数
            self.state['jobs'] = {}

        self._save_state()
        logger.info(f"Cleaned up DSH cost data older than {days} days")
