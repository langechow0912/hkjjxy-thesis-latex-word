import io
import json
import os
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

from fastapi.testclient import TestClient
from core import alipay, service_billing as billing, tabword
from web import app as web


class ServiceBillingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'ALIPAY_ORDER_DB': os.path.join(self.temp.name, 'orders.db'),
                                         'TABWORD_ENABLE_CALLS': 'true', 'TABWORD_API_KEY': 'offline-only'})
        self.env.start(); self.addCleanup(self.env.stop)
        cfg = {'enabled': True, 'amount_cents': 2999, 'mode': 'sandbox',
               'app_id': 'test-app', 'seller_id': 'seller', 'public_path': None}
        for p in (patch.object(alipay, 'config', return_value=cfg),
                  patch.object(alipay, '_signed_params', return_value={'offline': 'test'}),
                  patch.object(web, 'AFFILIATE_ADMIN_ON', True),
                  patch.object(web, 'AFFILIATE_ADMIN_USER', 'admin'),
                  patch.object(web, 'AFFILIATE_ADMIN_PASS', 'secret')):
            p.start(); self.addCleanup(p.stop)
        self.client = TestClient(web.app)
        self.auth = ('admin', 'secret')

    def purchase(self, operation='detect_text', values=None, file=None):
        values = values or {'text': '论文正文', 'task_platform': 'test'}
        quote = billing.quote(operation, values, file)
        order = billing.create_payment(quote['quote_id'])
        return quote, order, values

    def paid(self, order):
        notification = {'out_trade_no': order['out_trade_no'], 'app_id': 'test-app', 'seller_id': 'seller',
                        'total_amount': f"{order['amount_cents'] / 100:.2f}", 'trade_status': 'TRADE_SUCCESS',
                        'trade_no': 'offline-trade'}
        with patch.object(alipay, 'verify', return_value=True):
            self.assertTrue(alipay.apply_notification(notification))

    def dispatch(self, operation, values, order_id='', file=None):
        return self.client.post('/api/tabword/' + operation, data={
            'submit': 'true', 'params': json.dumps(values), 'payment_order': order_id},
            files={'file': file} if file else None)

    def test_defaults_and_persistent_admin_price_changes(self):
        self.assertEqual(billing.prices(), billing.DEFAULTS)
        self.assertEqual(self.client.get('/api/admin/service-prices').status_code, 401)
        self.assertEqual(self.client.put('/api/admin/service-prices', json=billing.DEFAULTS).status_code, 401)
        updated = {'format_export_cents': 1999, 'detect_text_cents': 199, 'detect_file_cents': 599, 'rewrite_per_1000_cents': 200}
        response = self.client.put('/api/admin/service-prices', json=updated, auth=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(billing.prices(), updated)
        self.assertEqual(self.client.get('/api/tabword').json()['prices'], updated)
        for bad in ({**updated, 'detect_text_cents': 0}, {**updated, 'detect_text_cents': True},
                    {**updated, 'detect_file_cents': 1.2}, {'detect_text_cents': 99}):
            self.assertEqual(self.client.put('/api/admin/service-prices', json=bad, auth=self.auth).status_code, 422)
        self.assertEqual(billing.prices(), updated)

    def test_character_billing_counts_whitespace_and_rounds_up_to_cent(self):
        for count, cents in ((1, 1), (10, 1), (11, 2), (999, 100), (1000, 100), (1001, 101)):
            q = billing.quote('paraphrase_stream', {'text': '字' * count + ' \n\t', 'preset': 'a'})
            self.assertEqual((q['characters'], q['amount_cents']), (count, cents))
        with self.assertRaises(tabword.TabwordError):
            billing.quote('paraphrase', {'text': '   ', 'preset': 'a'})

    def test_file_detection_flat_price_and_docx_rewrite_counts_server_side(self):
        q = billing.quote('detect_file', {'task_platform': 'test'}, ('x.txt', b'paper'))
        self.assertEqual(q['amount_cents'], 399)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as doc:
            doc.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>' + '字' * 1001 + '</w:t></w:r></w:p></w:body></w:document>')
        q = billing.quote('document_async', {'preset_zh': 'a'}, ('x.docx', buffer.getvalue()))
        self.assertEqual((q['characters'], q['amount_cents']), (1001, 101))
        for name, content in (('x.doc', b'fake'), ('x.docx', b'fake')):
            with self.assertRaises(tabword.TabwordError):
                billing.quote('document_async', {}, (name, content))

    def test_quote_snapshot_survives_price_change(self):
        quote, order, _ = self.purchase()
        billing.save_prices({**billing.DEFAULTS, 'detect_text_cents': 999})
        again = billing.create_payment(quote['quote_id'])
        self.assertEqual(order['out_trade_no'], again['out_trade_no'])
        self.assertEqual(again['amount_cents'], 99)
        self.assertEqual(billing.quote('detect_text', {'text': '新文', 'task_platform': 'test'})['amount_cents'], 999)

    def test_no_upstream_until_paid_and_content_bound_single_use(self):
        quote, order, values = self.purchase()
        with patch.object(tabword, 'call', return_value={'score': 12}) as upstream:
            self.assertEqual(self.dispatch('detect_text', values).status_code, 402)
            self.assertEqual(self.dispatch('detect_text', values, order['out_trade_no']).status_code, 402)
            upstream.assert_not_called()
            self.paid(order)
            for changed in ({**values, 'text': '另一篇'}, {**values, 'task_platform': 'other'}):
                self.assertEqual(self.dispatch('detect_text', changed, order['out_trade_no']).status_code, 403)
            upstream.assert_not_called()
            self.assertEqual(self.dispatch('detect_text', values, order['out_trade_no']).status_code, 200)
            self.assertEqual(self.dispatch('detect_text', values, order['out_trade_no']).status_code, 409)
            upstream.assert_called_once()
        url = f"/api/tabword/billing/{quote['quote_id']}/{order['out_trade_no']}/result"
        self.assertEqual(self.client.get(url).json(), {'score': 12})
        self.assertEqual(self.client.get(url).headers['cache-control'], 'no-store')
        self.assertEqual(self.client.get(url.replace(order['out_trade_no'], 'nonexistent')).status_code, 402)

    def test_file_content_cannot_be_replaced_after_payment(self):
        file = ('x.txt', b'original')
        _, order, values = self.purchase('detect_file', {'task_platform': 'test'}, file)
        self.paid(order)
        with patch.object(tabword, 'call') as upstream:
            self.assertEqual(self.dispatch('detect_file', values, order['out_trade_no'], ('x.txt', b'other')).status_code, 403)
            upstream.assert_not_called()

    def test_async_task_is_bound_to_paid_order(self):
        quote, order, values = self.purchase('paraphrase_async', {'text': '正文', 'preset': 'a'})
        self.paid(order)
        with patch.object(tabword, 'call', return_value={'data': {'task_id': 'task-A'}}) as upstream:
            self.assertEqual(self.dispatch('paraphrase_async', values, order['out_trade_no']).status_code, 200)
            self.assertEqual(self.dispatch('paraphrase_task', {'task_id': 'task-B'}, order['out_trade_no']).status_code, 403)
            self.assertEqual(upstream.call_count, 1)
            self.assertEqual(self.dispatch('paraphrase_task', {'task_id': 'task-A'}, order['out_trade_no']).status_code, 200)
            self.assertEqual(upstream.call_count, 2)
        state = self.client.get(f"/api/tabword/billing/{quote['quote_id']}/{order['out_trade_no']}").json()
        self.assertEqual(state['task_id'], 'task-A')

    def test_stream_result_can_be_restored_without_upstream_replay(self):
        quote, order, values = self.purchase('paraphrase_stream', {'text': '正文', 'preset': 'a'})
        self.paid(order)
        payload = b'event: done\ndata: {"rewritten":"done"}\n\n'
        with patch.object(tabword, 'stream_call', return_value=iter([payload])) as upstream:
            response = self.dispatch('paraphrase_stream', values, order['out_trade_no'])
            self.assertEqual(response.status_code, 200)
            self.assertIn('done', response.text)
            recovered = self.client.get(f"/api/tabword/billing/{quote['quote_id']}/{order['out_trade_no']}/result")
            self.assertEqual(recovered.content, payload)
            self.assertEqual(self.dispatch('paraphrase_stream', values, order['out_trade_no']).status_code, 409)
            upstream.assert_called_once()

    def test_failure_is_not_automatically_retryable_and_visible_to_admin(self):
        quote, order, values = self.purchase()
        self.paid(order)
        with patch.object(tabword, 'call', side_effect=tabword.TabwordError('offline failure')) as upstream:
            self.assertEqual(self.dispatch('detect_text', values, order['out_trade_no']).status_code, 502)
            self.assertEqual(self.dispatch('detect_text', values, order['out_trade_no']).status_code, 409)
            upstream.assert_called_once()
        self.assertEqual(billing.get_quote(quote['quote_id'])['state'], 'failed')
        self.assertEqual(self.client.get('/api/admin/service-orders').status_code, 401)
        rows = self.client.get('/api/admin/service-orders', auth=self.auth).json()['orders']
        self.assertEqual((rows[0]['state'], rows[0]['payment_status']), ('failed', 'TRADE_SUCCESS'))
        self.assertNotIn('正文', json.dumps(rows, ensure_ascii=False))

    def test_incomplete_stream_is_marked_for_manual_review(self):
        quote, order, values = self.purchase('paraphrase_stream', {'text': '正文', 'preset': 'a'})
        self.paid(order)
        with patch.object(tabword, 'stream_call', return_value=iter([b'event: progress\ndata: {}\n\n'])):
            self.assertEqual(self.dispatch('paraphrase_stream', values, order['out_trade_no']).status_code, 200)
        self.assertEqual(billing.get_quote(quote['quote_id'])['state'], 'failed')
        self.assertEqual(self.dispatch('paraphrase_stream', values, order['out_trade_no']).status_code, 409)

    def test_simultaneous_submissions_only_one_claim_succeeds(self):
        _, order, values = self.purchase()
        self.paid(order)
        def claim(_):
            try:
                billing.authorize('detect_text', values, None, order['out_trade_no'])
                return 200
            except tabword.TabwordError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(claim, range(2))), [200, 409])

    def test_http_quote_checkout_and_notification_amount_validation(self):
        with patch.object(tabword, 'call') as upstream:
            response = self.client.post('/api/tabword/billing/quote', data={
                'operation': 'detect_text', 'params': json.dumps({'text': '测试正文', 'task_platform': 'test'})})
            self.assertEqual(response.status_code, 200)
            quote = response.json()
            response = self.client.post(f"/api/tabword/billing/{quote['quote_id']}/payment")
            self.assertEqual(response.status_code, 200)
            payment = response.json()
            self.assertEqual(payment['amount_cents'], 99)
            redirect = self.client.get(payment['checkout_url'], follow_redirects=False)
            self.assertEqual(redirect.status_code, 303)
            self.assertTrue(redirect.headers['location'].startswith('https://openapi-sandbox.'))
            with patch.object(alipay, 'verify', return_value=True):
                self.assertFalse(alipay.apply_notification({'out_trade_no': payment['order_id'],
                    'app_id': 'test-app', 'seller_id': 'seller', 'total_amount': '0.01', 'trade_status': 'TRADE_SUCCESS'}))
            upstream.assert_not_called()


if __name__ == '__main__':
    unittest.main()
