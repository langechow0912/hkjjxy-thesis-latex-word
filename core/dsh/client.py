"""DeepSeek API 客户端封装"""

import os
import json
import asyncio
import logging
from typing import Dict, Any, List, Optional
from pathlib import Path
import hashlib

logger = logging.getLogger(__name__)


class DeepSeekClient:
    """
    DeepSeek API 封装

    支持：
    - 异步调用
    - 结果缓存（基于 prompt hash）
    - 超时控制
    - 成本统计
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.api_config = config.get('api', {})
        self.api_key = os.getenv('DEEPSEEK_API_KEY', '')
        self.base_url = self.api_config.get('base_url', 'https://api.deepseek.com')
        self.model = self.api_config.get('model', 'deepseek-chat')
        self.timeout = self.api_config.get('timeout', 30)

        # 缓存配置
        cache_config = config.get('cost_control', {})
        self.cache_ttl = cache_config.get('cache_ttl', 3600)
        self.cache_dir = Path('.dsh_cache')
        self.cache_dir.mkdir(exist_ok=True)

        if not self.api_key:
            logger.warning("DEEPSEEK_API_KEY not set, DSH will be disabled")

    def _cache_key(self, prompt: str, **kwargs) -> str:
        """生成缓存键"""
        content = json.dumps({'prompt': prompt, 'model': self.model, **kwargs}, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def _get_cache(self, key: str) -> Optional[Dict[str, Any]]:
        """读取缓存"""
        cache_file = self.cache_dir / f"{key}.json"
        if not cache_file.exists():
            return None

        try:
            import time
            data = json.loads(cache_file.read_text(encoding='utf-8'))
            if time.time() - data.get('timestamp', 0) > self.cache_ttl:
                cache_file.unlink()
                return None
            return data.get('result')
        except Exception as e:
            logger.warning(f"Failed to read cache {key}: {e}")
            return None

    def _set_cache(self, key: str, result: Dict[str, Any]):
        """写入缓存"""
        import time
        cache_file = self.cache_dir / f"{key}.json"
        try:
            cache_file.write_text(json.dumps({
                'timestamp': time.time(),
                'result': result
            }, ensure_ascii=False), encoding='utf-8')
        except Exception as e:
            logger.warning(f"Failed to write cache {key}: {e}")

    async def chat(
        self,
        prompt: str,
        system: Optional[str] = None,
        temperature: float = 0.7,
        use_cache: bool = True,
        **kwargs
    ) -> Dict[str, Any]:
        """
        调用 DeepSeek Chat API

        Returns:
            {
                'content': str,  # 返回内容
                'usage': {'prompt_tokens': int, 'completion_tokens': int, 'total_tokens': int}
            }
        """
        if not self.api_key:
            raise RuntimeError("DEEPSEEK_API_KEY not set")

        # 检查缓存
        if use_cache:
            cache_key = self._cache_key(prompt, system=system, temperature=temperature, **kwargs)
            cached = self._get_cache(cache_key)
            if cached:
                logger.debug(f"Cache hit: {cache_key}")
                return cached

        # 构建请求
        messages = []
        if system:
            messages.append({'role': 'system', 'content': system})
        messages.append({'role': 'user', 'content': prompt})

        payload = {
            'model': self.model,
            'messages': messages,
            'temperature': temperature,
            **kwargs
        }

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        'Authorization': f'Bearer {self.api_key}',
                        'Content-Type': 'application/json'
                    },
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=self.timeout)
                ) as response:
                    if response.status != 200:
                        text = await response.text()
                        raise RuntimeError(f"DeepSeek API error {response.status}: {text}")

                    data = await response.json()
                    result = {
                        'content': data['choices'][0]['message']['content'],
                        'usage': data.get('usage', {})
                    }

                    # 写入缓存
                    if use_cache:
                        self._set_cache(cache_key, result)

                    return result

        except asyncio.TimeoutError:
            raise RuntimeError(f"DeepSeek API timeout after {self.timeout}s")
        except Exception as e:
            logger.error(f"DeepSeek API call failed: {e}")
            raise

    async def analyze_document(
        self,
        docx_path: str,
        analysis_type: str,
        **kwargs
    ) -> Dict[str, Any]:
        """
        分析 DOCX 文档（通用接口）

        Args:
            docx_path: DOCX 文件路径
            analysis_type: 分析类型（heading_fix, structure_check 等）
            **kwargs: 额外参数

        Returns:
            分析结果（JSON 格式）
        """
        from docx import Document

        # 提取文档内容（简化版，只提取文本和标题样式）
        doc = Document(docx_path)
        content_parts = []

        for para in doc.paragraphs[:100]:  # 限制前 100 段，避免 token 过多
            style = para.style.name if para.style else 'Normal'
            text = para.text.strip()
            if text:
                content_parts.append(f"[{style}] {text}")

        document_summary = '\n'.join(content_parts)

        # 加载对应的 prompt 模板
        prompt_file = Path(__file__).parent / 'prompts' / f'{analysis_type}.txt'
        if prompt_file.exists():
            prompt_template = prompt_file.read_text(encoding='utf-8')
            prompt = prompt_template.format(
                document=document_summary,
                **kwargs
            )
        else:
            prompt = f"Analyze this document for {analysis_type}:\n\n{document_summary}"

        result = await self.chat(prompt, temperature=0.3)

        # 尝试解析 JSON 响应
        try:
            content = result['content'].strip()
            # 提取 JSON（如果包裹在 markdown 代码块中）
            if content.startswith('```'):
                lines = content.split('\n')
                content = '\n'.join(lines[1:-1]) if len(lines) > 2 else content

            parsed = json.loads(content)
            return {
                'success': True,
                'data': parsed,
                'cost': result['usage'].get('total_tokens', 0)
            }
        except json.JSONDecodeError:
            # 如果不是 JSON，返回原始文本
            return {
                'success': True,
                'data': {'raw': result['content']},
                'cost': result['usage'].get('total_tokens', 0)
            }
