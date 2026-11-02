# -*- coding: utf-8 -*-
"""DSH CP1 的离线测试：不联网、不产生费用。

模型调用一律用假的 client 顶替，重点验证三件保证：
  1. 只产出建议，不改原稿；
  2. 模型乱答 / 超时 / 没 key 时安静降级，不影响排版；
  3. 越权动作（比如试图改正文）会被丢掉。
"""
import asyncio
import json
import unittest
from unittest.mock import patch

from core import profile
from core.dsh import runner as dsh_runner
from core.dsh.checkpoints.cp1_preprocess import PreprocessCheckpoint, parse_suggestions


GOOD = json.dumps({
    "issues": [
        {
            "type": "heading_skipped",
            "severity": "high",
            "message": "标题「3.1 国内外比较」为三级但编号形态为二级序号",
            "suggestion": "核对「3.1」应为二级还是三级",
            "target_index": 5,
            "actions": [{"op": "adjust_level", "index": 5, "level": 2,
                         "number": "3", "text": "国内外比较"}],
        },
        {
            "type": "heading_unnumbered",
            "severity": "low",
            "message": "「研究背景」没有编号",
            "suggestion": "补编号",
            "target_index": 2,
            "actions": [],
        },
    ],
    "notes": ["「2.1」的上级标题名称无法从正文推断。"],
    "summary": "存在跳级问题",
    "quality_score": 62,
}, ensure_ascii=False)


class FakeClient:
    def __init__(self, content):
        self.content = content

    async def chat(self, prompt, **kwargs):
        return {'content': self.content,
                'usage': {'total_tokens': 123}}


def make_context():
    return {
        'source': 'source.docx',
        'work_dir': '.',
        'meta': {'title': 'T'},
        'suspicious': True,
        'items': [
            {'kind': 'p', 'pno': 0, 'text': '1 引言', 'heading_level': 1},
            {'kind': 'p', 'pno': 1, 'text': '本文研究背景如下。', 'heading_level': None},
            {'kind': 'p', 'pno': 2, 'text': '研究背景', 'heading_level': 2},
        ],
        'body_start': 0,
    }


class ParseSuggestions(unittest.TestCase):
    def test_reads_plain_json(self):
        parsed = parse_suggestions(GOOD)
        self.assertEqual(len(parsed['issues']), 2)
        # 高严重度排在前
        self.assertEqual(parsed['issues'][0]['severity'], 'high')
        self.assertEqual(parsed['quality_score'], 62)

    def test_strips_markdown_fence(self):
        parsed = parse_suggestions('```json\n%s\n```' % GOOD)
        self.assertEqual(len(parsed['issues']), 2)

    def test_garbage_returns_none(self):
        for bad in ('', '模型觉得自己很棒', '{"issues": ', None):
            self.assertIsNone(parse_suggestions(bad), bad)

    def test_unknown_or_body_editing_actions_are_dropped(self):
        payload = json.dumps({
            "issues": [{
                "type": "x", "severity": "high", "message": "m", "suggestion": "s",
                "actions": [
                    {"op": "adjust_level", "index": 1},
                    {"op": "rewrite_paragraph", "index": 1, "text": "删掉这句"},
                    {"op": "delete_paragraph", "index": 2},
                ],
            }],
        }, ensure_ascii=False)
        parsed = parse_suggestions(payload)
        ops = [a['op'] for a in parsed['issues'][0]['actions']]
        self.assertEqual(ops, ['adjust_level'])


class CheckpointBehaviour(unittest.TestCase):
    def setUp(self):
        self.cfg = profile.load('hkjjxy')

    def _run(self, content):
        cp = PreprocessCheckpoint(self.cfg, FakeClient(content), cost=None)
        return asyncio.run(cp.execute(make_context()))

    def test_returns_suggestions_without_touching_source(self):
        result = self._run(GOOD)
        self.assertTrue(result['success'])
        self.assertEqual(len(result['suggestions']), 2)
        self.assertEqual(result['cost'], 123)
        # 没有 fixed_docx：这个检查点永远不产出改写后的文件
        self.assertNotIn('fixed_docx', result)

    def test_unparseable_output_degrades_quietly(self):
        result = self._run('我觉得这篇文章挺好的')
        self.assertFalse(result['success'])
        self.assertEqual(result['reason'], 'unparseable')
        self.assertTrue(result['warnings'])

    def test_prompt_ships_inside_the_package(self):
        """prompt 必须在 core/dsh/prompts/ 下，否则镜像里读不到。

        Dockerfile 只 COPY core/web/schools，.claude/skills 不进镜像。
        """
        cp = PreprocessCheckpoint(self.cfg, FakeClient(GOOD), cost=None)
        path = cp._prompt_path()
        self.assertTrue(path.is_file(), path)
        text = path.read_text(encoding='utf-8')
        self.assertIn('{structure}', text)
        self.assertIn('{outline}', text)

    def test_should_run_only_when_suspicious(self):
        cp = PreprocessCheckpoint(self.cfg, FakeClient(GOOD), cost=None)
        with patch.object(PreprocessCheckpoint, 'is_enabled', return_value=True):
            ctx = make_context()
            self.assertTrue(cp.should_run(ctx))
            ctx['suspicious'] = False
            self.assertFalse(cp.should_run(ctx))
            self.assertFalse(cp.should_run({'source': None, 'suspicious': True}))


class RunnerDegrades(unittest.TestCase):
    def test_disabled_config_returns_none(self):
        cfg = profile.load('hkjjxy')
        cfg['dsh'] = dict(cfg.get('dsh') or {}, enabled=False)
        with patch.dict('os.environ', {}, clear=True):
            self.assertFalse(dsh_runner.available(cfg))
            self.assertIsNone(dsh_runner.run_preprocess(cfg, make_context()))

    def test_missing_api_key_returns_none(self):
        cfg = profile.load('hkjjxy')
        cfg['dsh'] = dict(cfg.get('dsh') or {}, enabled=True)
        with patch.dict('os.environ', {}, clear=True):
            self.assertFalse(dsh_runner.available(cfg))

    def test_exceptions_never_propagate(self):
        cfg = profile.load('hkjjxy')
        cfg['dsh'] = dict(cfg.get('dsh') or {}, enabled=True)
        with patch.object(dsh_runner, 'available', return_value=True), \
             patch('core.dsh.DSHManager', side_effect=RuntimeError('boom')):
            self.assertIsNone(dsh_runner.run_preprocess(cfg, make_context()))

    def test_messages_are_human_readable(self):
        report = {'issues': [{'message': '标题层级跳级', 'suggestion': '核对层级'}],
                  'notes': ['无法推断上级标题']}
        msgs = dsh_runner.to_messages(report)
        self.assertIn('结构检查：标题层级跳级（核对层级）', msgs)
        self.assertTrue(all(m.startswith('结构检查：') for m in msgs))


if __name__ == '__main__':
    unittest.main()


class ClientOverRealHttp(unittest.TestCase):
    """DeepSeekClient 走真实的 HTTP 往返，对端是本机起的假服务，不联网不花钱。

    镜像里没有 aiohttp，客户端必须只靠标准库就能工作。
    """

    def _serve(self, handler_cls):
        import threading
        from http.server import HTTPServer
        server = HTTPServer(('127.0.0.1', 0), handler_cls)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # addCleanup 后进先出：先 shutdown 停掉 serve_forever，再 close 套接字
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return 'http://127.0.0.1:%d' % server.server_address[1]

    def _client(self, base_url, tmp):
        from pathlib import Path
        from core.dsh.client import DeepSeekClient
        with patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'k',
                                       'DEEPSEEK_BASE_URL': base_url}):
            client = DeepSeekClient({'api': {'timeout': 5, 'reasoning_effort': 'off'}})
        client.cache_dir = Path(tmp)
        return client

    def test_roundtrip_and_cache(self):
        import tempfile
        from http.server import BaseHTTPRequestHandler
        hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                hits.append(self.headers.get('Authorization'))
                body = json.dumps({'choices': [{'message': {'content': 'hi'}}],
                                   'usage': {'total_tokens': 9}}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(self._serve(Handler), tmp)
            first = asyncio.run(client.chat('ping'))
            second = asyncio.run(client.chat('ping'))
        self.assertEqual(first['content'], 'hi')
        self.assertEqual(first['usage']['total_tokens'], 9)
        self.assertEqual(second['content'], 'hi')
        self.assertEqual(hits, ['Bearer k'], '第二次应命中缓存，不再请求')

    def test_http_error_raises_and_runner_degrades(self):
        import tempfile
        from http.server import BaseHTTPRequestHandler

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                # 先把请求体读完再回：真实服务不会在客户端还在发送时就掐连接，
                # 不读的话 Windows 上客户端会收到连接中止，而不是 500。
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(500)
                self.send_header('Content-Length', '4')
                self.end_headers()
                self.wfile.write(b'boom')

            def log_message(self, *a):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(self._serve(Handler), tmp)
            with self.assertRaises(RuntimeError):
                asyncio.run(client.chat('ping'))

    def test_rejected_thinking_params_retries_once_without_them(self):
        import tempfile
        from http.server import BaseHTTPRequestHandler
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append('thinking' in payload)
                if 'thinking' in payload:
                    self.send_response(400)
                    self.send_header('Content-Length', '2')
                    self.end_headers()
                    self.wfile.write(b'no')
                    return
                body = json.dumps({'choices': [{'message': {'content': 'ok'}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            client = self._client(self._serve(Handler), tmp)
            client.reasoning_effort = 'medium'
            result = asyncio.run(client.chat('ping', use_cache=False))
        self.assertEqual(result['content'], 'ok')
        self.assertEqual(seen, [True, False])


class OutlineDoesNotLeakProse(unittest.TestCase):
    """论文是用户未发表的原稿：发给外部模型的只能是结构，不能是正文。"""

    def test_prose_and_table_cells_never_reach_the_prompt(self):
        from core.dsh import outline
        secret = '这是一段不应该被发送给外部模型的未发表论文正文内容，包含具体结论。'
        items = [
            {'kind': 'p', 'pno': 0, 'text': '1 引言', 'heading_level': 1},
            {'kind': 'p', 'pno': 1, 'text': secret, 'heading_level': None},
            {'kind': 'p', 'pno': 2, 'text': '研究背景', 'heading_level': 2},
            {'kind': 'tbl', 'pno': 3, 'rows': [['机密单元格', '123']]},
        ]
        lines = outline.build(items, 0)
        joined = '\n'.join(lines)
        self.assertNotIn(secret[:8], joined)
        self.assertNotIn('机密单元格', joined)
        # 标题文字要给，模型靠它判断层级
        self.assertIn('引言', joined)
        self.assertIn('研究背景', joined)
        self.assertIn('［正文 %d 字］' % len(secret), joined)

    def test_short_unnumbered_line_is_kept_so_model_can_judge_it(self):
        from core.dsh import outline
        items = [{'kind': 'p', 'pno': 0, 'text': '研究意义', 'heading_level': None}]
        self.assertIn('研究意义', '\n'.join(outline.build(items, 0)))

    def test_output_is_capped(self):
        from core.dsh import outline
        items = [{'kind': 'p', 'pno': i, 'text': '标题%d' % i, 'heading_level': 1}
                 for i in range(1000)]
        self.assertLessEqual(len(outline.build(items, 0, max_lines=50)), 51)


class ResolvedLevelsReachTheModel(unittest.TestCase):
    """流水线已经按编号判定的标题，不能因为 Word 大纲级别为空就报「缺失」。

    真实 DeepSeek 实测犯过这个错：把已经识别成四级标题的「（1）…」
    报成 heading_skipped。修法是把这个映射一起给模型。
    """

    def test_pipeline_level_wins_over_empty_word_outline(self):
        from core.dsh import outline
        items = [
            {'kind': 'p', 'pno': 0, 'text': '（1）财务杠杆系数（DFL）', 'heading_level': None},
        ]
        resolved = {'财务杠杆系数（DFL）': 4}
        line = outline.build(items, 0, resolved_levels=resolved)[0]
        self.assertTrue(line.startswith('1 | 4 |'), line)

    def test_level_lookup_works_with_or_without_numbering(self):
        from core.dsh import outline
        resolved = {'财务杠杆系数（DFL）': 4}
        for text in ('（1）财务杠杆系数（DFL）', '（1） 财务杠杆系数（DFL）'):
            items = [{'kind': 'p', 'pno': 0, 'text': text, 'heading_level': None}]
            line = outline.build(items, 0, resolved_levels=resolved)[0]
            self.assertIn('| 4 |', line, text)

    def test_without_map_the_level_falls_back_to_word_outline(self):
        from core.dsh import outline
        items = [{'kind': 'p', 'pno': 0, 'text': '引言', 'heading_level': 1}]
        self.assertIn('| 1 |', outline.build(items, 0)[0])


class EnvOverride(unittest.TestCase):
    """DSH_ENABLED 曾经写在 .env.example 里但代码从不读，是个哑配置。"""

    def test_env_overrides_yaml(self):
        from core import profile
        from core.dsh import runner
        cfg = profile.load('hkjjxy')
        cfg['dsh'] = dict(cfg.get('dsh') or {}, enabled=True)
        with patch.dict('os.environ', {'DSH_ENABLED': 'false'}):
            self.assertFalse(runner.enabled(cfg))
        with patch.dict('os.environ', {'DSH_ENABLED': 'true'}):
            self.assertTrue(runner.enabled(cfg))

    def test_yaml_used_when_env_absent(self):
        from core import profile
        from core.dsh import runner
        cfg = profile.load('hkjjxy')
        cfg['dsh'] = dict(cfg.get('dsh') or {}, enabled=False)
        with patch.dict('os.environ', {}, clear=True):
            self.assertFalse(runner.enabled(cfg))
