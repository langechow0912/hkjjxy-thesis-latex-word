# -*- coding: utf-8 -*-
"""改写模型以官方目录为准：下线的预设在报价时就被拦下，不让用户付款后才失败。全部离线。"""
import json
from unittest.mock import patch

from core import service_billing as billing, tabword
from tests.test_points import PointsBase, make_user

OFFICIAL = {'presets': [{'code': 'aigcmove_tp_s_1', 'name': '降 AI'}, {'code': 'zh_jc_1_0325', 'name': '降重'}]}


class PresetCheck(PointsBase):
    def setUp(self):
        super().setUp()
        for patcher in (patch.dict('os.environ', {'TABWORD_VALIDATE_PRESETS': 'true'}),
                        patch.dict(billing._PRESETS, {'at': 0, 'codes': None})):
            patcher.start(); self.addCleanup(patcher.stop)
        self.as_user(make_user(500))

    def quote(self, preset, operation='paraphrase_stream', **extra):
        values = {'text': '论文正文' * 30, 'preset': preset, **extra}
        return self.client.post('/api/tabword/billing/quote', data={
            'operation': operation, 'params': json.dumps(values, ensure_ascii=False)})

    def test_retired_preset_is_refused_before_any_payment(self):
        with patch.object(tabword, 'read_catalog', return_value=OFFICIAL):
            res = self.quote('aimove_weipu_0520')
        self.assertEqual(res.status_code, 422)
        self.assertIn('已下线或不存在', res.json()['detail'])

    def test_current_preset_is_accepted(self):
        with patch.object(tabword, 'read_catalog', return_value=OFFICIAL):
            self.assertEqual(self.quote('aigcmove_tp_s_1').status_code, 200)
            self.assertEqual(self.quote('zh_jc_1_0325').status_code, 200)

    def test_unreadable_catalog_never_blocks_a_purchase(self):
        with patch.object(tabword, 'read_catalog', side_effect=tabword.TabwordError('down', 502)):
            self.assertEqual(self.quote('anything').status_code, 200)

    def test_empty_catalog_never_blocks_a_purchase(self):
        with patch.object(tabword, 'read_catalog', return_value={'presets': []}):
            self.assertEqual(self.quote('anything').status_code, 200)

    def test_catalog_is_cached_between_quotes(self):
        with patch.object(tabword, 'read_catalog', return_value=OFFICIAL) as catalog:
            self.quote('aigcmove_tp_s_1'); self.quote('aigcmove_tp_s_1'); self.quote('zh_jc_1_0325')
        catalog.assert_called_once()

    def test_detection_has_no_preset_to_check(self):
        with patch.object(tabword, 'read_catalog', return_value=OFFICIAL) as catalog:
            res = self.client.post('/api/tabword/billing/quote', data={
                'operation': 'detect_text', 'params': json.dumps({'text': '正文' * 40, 'task_platform': '格子达_zh'}, ensure_ascii=False)})
        self.assertEqual(res.status_code, 200)
        catalog.assert_not_called()

    def test_validation_can_be_switched_off(self):
        with patch.dict('os.environ', {'TABWORD_VALIDATE_PRESETS': 'false'}), \
             patch.object(tabword, 'read_catalog', return_value=OFFICIAL) as catalog:
            self.assertEqual(self.quote('whatever').status_code, 200)
        catalog.assert_not_called()


if __name__ == '__main__':
    import unittest
    unittest.main()
