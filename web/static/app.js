'use strict';
const $ = id => document.getElementById(id);
const fields = ['title','authorName','titleEn','authorNameEn','tutorName','tutorNameEn','subjectName','subjectNameEn','subTitle','subTitleEn'];
let selectedRule = null, searchTimer = null, searchVersion = 0, composing = false;
let selectedFile = null, busy = false, searching = false, job = null, timer = null;
let paymentJob = null, paymentOrder = null, paymentTimer = null;
function message(text, kind = '') { $('msg').textContent = text; $('msg').className = 'msg ' + kind; }
function controls() {
  for (const id of [...fields, 'query','drop']) $(id).disabled = busy;
  for (const button of $('schoolResults').querySelectorAll('button')) button.disabled = busy;
  $('schoolResults').setAttribute('aria-busy', String(searching));
  $('go').disabled = busy || searching || !selectedFile || !selectedRule?.id || !$('title').value.trim() || !$('authorName').value.trim();
  $('bar').hidden = !busy;
  $('result').setAttribute('aria-busy', String(busy));
}
function clearResult() {
  clearTimeout(timer); job = null; $('files').replaceChildren(); $('summary').replaceChildren();
  clearTimeout(paymentTimer); paymentJob = null; paymentOrder = null; $('paymentPanel').hidden = true;
  sessionStorage.removeItem('thesisPaymentOrder');
  $('summary').hidden = true; $('resume').hidden = true;
  $('state').textContent = selectedFile ? '待排版' : '待上传'; message('');
}
async function request(url, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 120000);
  try {
    const response = await fetch(url, {...options, signal:controller.signal});
    if (!response.ok) {
      const errors = {401:'排版服务拒绝了当前鉴权请求，请联系维护人员核对接口配置。',403:'排版服务未授权，请检查接口权限。',413:'文件过大，请压缩至 20 MB 以内。',429:'请求过于频繁，请稍后再试。',503:'排版服务暂不可用，请检查配置或稍后重试。'};
      const detail = await response.json().catch(() => ({}));
      throw new Error(errors[response.status] || (typeof detail.detail === 'string' ? detail.detail : '请求未成功，请检查填写内容或稍后重试。'));
    }
    return await response.json();
  } finally { clearTimeout(timeout); }
}
function renderSchools(items) {
  $('schoolResults').replaceChildren();
  for (const rule of items) {
    const label = rule.label || [rule.name, rule.targetSchoolName && !rule.name?.includes(rule.targetSchoolName) ? rule.targetSchoolName : null].filter(Boolean).join(' · ');
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'school-option'; button.textContent = label;
    button.setAttribute('aria-pressed', String(selectedRule?.id === rule.id));
    button.onclick = () => {
      if (busy) return;
      selectedRule = {id:rule.id, label}; clearResult();
      for (const item of $('schoolResults').children) item.setAttribute('aria-pressed', String(item === button));
      $('ruleInfo').textContent = '已选择：' + label; routeHint(); controls();
    };
    $('schoolResults').append(button);
  }
}
function queueSearch() {
  if (busy) return;
  clearTimeout(searchTimer); ++searchVersion; selectedRule = null; searching = false;
  $('schoolResults').replaceChildren(); clearResult(); routeHint();
  const q = $('query').value.trim();
  $('ruleInfo').textContent = q ? '正在匹配院校…' : '输入院校名称或关键词，如：师范。';
  controls();
  if (q && !composing) searchTimer = setTimeout(searchRules, 400);
}
async function searchRules() {
  clearTimeout(searchTimer);
  if (busy || composing) return;
  const q = $('query').value.trim(), version = ++searchVersion;
  if (!q) { queueSearch(); return; }
  searching = true; selectedRule = null; clearResult(); $('schoolResults').replaceChildren(); routeHint(); controls();
  $('ruleInfo').textContent = '正在匹配院校…';
  try {
    const data = await request('/api/format/rules?q=' + encodeURIComponent(q) + '&size=20');
    if (version !== searchVersion) return;
    const items = Array.isArray(data.items) ? data.items : [];
    if (items.length === 1) selectedRule = {id:items[0].id, label:items[0].name};
    renderSchools(items);
    $('ruleInfo').textContent = items.length === 1 ? '已自动选择匹配院校。' : items.length ? `找到 ${data.total ?? items.length} 个结果，请点击选择。` : '没有匹配的院校，请调整关键词。';
    routeHint();
  } catch (e) {
    if (version === searchVersion) $('ruleInfo').textContent = e.message + ' 请重新输入或按回车重试。';
  } finally { if (version === searchVersion) { searching = false; controls(); } }
}
function selectFile(file) {
  if (busy) return;
  clearResult(); selectedFile = null; $('filename').hidden = true; $('file').value = '';
  if (!/\.(docx|doc)$/i.test(file.name)) message('请上传 .docx / .doc 格式的 Word 论文。', 'error');
  else if (!file.size || file.size > 20 * 1024 * 1024) message('文件不能为空，且不能超过 20 MB。', 'error');
  else {
    selectedFile = file; $('filename').hidden = false; $('filename').textContent = file.name;
    if (!$('title').value.trim()) $('title').value = file.name.replace(/\.(docx|doc)$/i, '');
    $('state').textContent = '待排版'; message('稿件已选择，请核对题目、作者和排版规范。');
  }
  controls();
}
async function submit() {
  if ($('go').disabled) return;
  busy = true; clearResult(); controls(); $('state').textContent = '提交中';
  const data = new FormData(); data.append('file', selectedFile); data.append('ruleId', selectedRule.id);
  for (const id of fields) data.append(id, $(id).value.trim());
  message('正在上传稿件并提交排版…');
  try {
    const result = await request('/api/format', {method:'POST',body:data});
    if (!result.job) throw new Error('未收到任务编号，请确认服务状态。');
    job = result.job; sessionStorage.setItem('thesisJob', job);
    sessionStorage.setItem('thesisDraft', JSON.stringify({query:$('query').value, ruleId:selectedRule.id,
      ruleLabel:selectedRule.label,
      fields:Object.fromEntries(fields.map(id => [id, $(id).value]))}));
    poll();
  } catch (e) {
    // A lost response may still have created a task. Avoid automatic resubmission.
    busy = false; controls(); $('state').textContent = '提交未确认';
    message(e.message + ' 若提交时网络中断，请先确认任务是否已创建，避免重复提交。', 'error');
  }
}
async function setupPayment(id) {
  clearTimeout(paymentTimer); paymentJob = id; paymentOrder = null;
  $('paymentPanel').hidden = false; $('paymentCheckout').hidden = true;
  $('paidDownload').hidden = true; $('createPayment').hidden = true;
  $('couponControls').hidden = true; $('couponInfo').textContent = ''; $('couponCode').value = '';
  $('couponCode').disabled = false; $('applyCoupon').disabled = false;
  try {
    const config = await request('/api/alipay');
    if (paymentJob !== id) return;
    if (!config.enabled) {
      $('paymentInfo').textContent = '支付宝支付尚未配置完成，暂不能下载 Word。';
      return;
    }
    $('paymentInfo').textContent = `支付宝付款 ¥${(config.amount_cents / 100).toFixed(2)} 后导出成品`;
    $('couponControls').hidden = false;
    $('createPayment').hidden = false;
    const saved = sessionStorage.getItem('thesisPaymentOrder');
    if (saved) {
      try {
        const order = JSON.parse(saved);
        if (order.job === id && order.orderId) {
          paymentOrder = order.orderId;
          $('paymentCheckout').href = `/api/format/jobs/${encodeURIComponent(id)}/payment/${encodeURIComponent(paymentOrder)}/checkout`;
          $('paymentCheckout').hidden = false; $('createPayment').hidden = true;
          $('couponCode').value = order.couponCode || '';
          $('couponCode').disabled = true; $('applyCoupon').disabled = true;
          checkPayment(id, paymentOrder);
        }
      } catch { sessionStorage.removeItem('thesisPaymentOrder'); }
    }
  } catch (error) {
    if (paymentJob === id) $('paymentInfo').textContent = error.message;
  }
}
async function createPayment() {
  if (!paymentJob) return;
  const id = paymentJob, button = $('createPayment'); button.disabled = true;
  $('paymentInfo').textContent = '正在创建支付宝订单…';
  try {
    const result = await request(`/api/format/jobs/${encodeURIComponent(id)}/payment`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({coupon_code:$('couponCode').value.trim()}),
    });
    if (paymentJob !== id) return;
    paymentOrder = result.order_id;
    sessionStorage.setItem('thesisPaymentOrder', JSON.stringify({job:id,orderId:paymentOrder,couponCode:result.coupon_code}));
    button.hidden = true;
    $('paymentCheckout').href = result.checkout_url;
    $('paymentCheckout').hidden = result.status === 'TRADE_SUCCESS';
    $('couponCode').disabled = true; $('applyCoupon').disabled = true;
    const priceText = result.discount_cents
      ? `原价 ¥${(result.list_price_cents / 100).toFixed(2)}，优惠 -¥${(result.discount_cents / 100).toFixed(2)}，应付 ¥${(result.amount_cents / 100).toFixed(2)}`
      : `应付 ¥${(result.amount_cents / 100).toFixed(2)}`;
    $('paymentInfo').textContent = `点击“前往支付宝付款”打开收银台，付款后回到本页下载。${priceText}`;
    if (result.coupon_code) $('couponInfo').textContent = `已使用优惠码 ${result.coupon_code}`;
    checkPayment(id, paymentOrder);
  } catch (error) { if (paymentJob === id) $('paymentInfo').textContent = error.message; }
  finally { button.disabled = false; }
}
async function checkPayment(id, order) {
  if (paymentJob !== id || paymentOrder !== order) return;
  try {
    const state = await request(`/api/format/jobs/${encodeURIComponent(id)}/payment/${encodeURIComponent(order)}`);
    if (paymentJob !== id || paymentOrder !== order) return;
    if (state.paid) {
      clearTimeout(paymentTimer); $('paymentInfo').textContent = '支付成功，可以下载排版成品。';
      $('paymentCheckout').hidden = true;
      $('paidDownload').hidden = false; return;
    }
    if (state.expired) {
      $('paymentCheckout').hidden = true; $('createPayment').hidden = false;
      $('couponCode').disabled = false; $('applyCoupon').disabled = false;
      $('paymentInfo').textContent = '订单已过期或支付方式已更新，请重新创建支付订单。';
      sessionStorage.removeItem('thesisPaymentOrder'); paymentOrder = null; return;
    }
  } catch (error) {
    if (paymentJob !== id || paymentOrder !== order) return;
    $('paymentInfo').textContent = error.message;
  }
  paymentTimer = setTimeout(() => checkPayment(id, order), 2500);
}
async function downloadPaid() {
  if (!paymentJob || !paymentOrder) return;
  const button = $('paidDownload'); button.disabled = true;
  try {
    const url = `/api/format/jobs/${encodeURIComponent(paymentJob)}/download?fmt=1&payment_order=${encodeURIComponent(paymentOrder)}`;
    const response = await fetch(url);
    if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(data.detail || '下载失败，请稍后重试。'); }
    const blob = await response.blob(), objectUrl = URL.createObjectURL(blob);
    const link = document.createElement('a'); link.href = objectUrl; link.download = '排版论文.docx'; link.click();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
  } catch (error) { $('paymentInfo').textContent = error.message; }
  finally { button.disabled = false; }
}
async function applyCoupon() {
  if (!paymentJob || !$('couponCode').value.trim()) {
    $('couponInfo').textContent = '请先输入优惠码。'; return;
  }
  const button = $('applyCoupon'); button.disabled = true;
  try {
    const quote = await request(`/api/format/jobs/${encodeURIComponent(paymentJob)}/coupon`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({code:$('couponCode').value.trim()}),
    });
    $('couponInfo').textContent = `${quote.coupon_code} 已应用：优惠 ¥${(quote.discount_cents / 100).toFixed(2)}，应付 ¥${(quote.amount_cents / 100).toFixed(2)}。`;
  } catch (error) { $('couponInfo').textContent = error.message; }
  finally { button.disabled = false; }
}
async function poll() {
  clearTimeout(timer);
  try {
    const data = await request('/api/format/jobs/' + encodeURIComponent(job));
    if (data.state === 'running') {
      $('state').textContent = '排版中'; message(data.statusLabel || data.stage || '正在处理稿件，请稍候…');
      timer = setTimeout(poll, 2000); return;
    }
    if (!['done','error'].includes(data.state)) throw new Error('任务状态异常。');
    busy = false; controls(); $('resume').hidden = true; sessionStorage.removeItem('thesisJob');
    if (data.state === 'error') { $('state').textContent = '排版失败'; message(data.stage || '稿件排版失败，请检查 Word 原稿或稍后重试。', 'error'); return; }
    $('state').textContent = '已完成'; message('排版完成，请先预览并核对建议，再导出 Word。', 'ok');
    $('files').replaceChildren();
    if (data.preview) {
      const link = document.createElement('a'); link.href = `/api/format/jobs/${encodeURIComponent(job)}/preview`;
      link.target = '_blank'; link.rel = 'noopener'; link.textContent = '预览 PDF'; $('files').append(link);
    }
    if (data.freePdfDownload) {
      const link = document.createElement('a'); link.href = `/api/format/jobs/${encodeURIComponent(job)}/download?fmt=2`;
      link.textContent = '下载 PDF'; $('files').append(link);
    }
    const button = document.createElement('button'); button.className = 'secondary'; button.textContent = '下载 Word（需支付）';
    const completedJob = job; button.onclick = () => {
      $('paymentPanel').hidden = false;
      setupPayment(completedJob);
      $('paymentPanel').scrollIntoView({behavior:'smooth', block:'center'});
    };
    $('files').append(button);
    const suggestions = Array.isArray(data.suggestions) ? data.suggestions : [];
    $('summary').replaceChildren();
    for (const suggestion of suggestions) {
      const item = document.createElement('p');
      item.textContent = typeof suggestion === 'string' ? suggestion : suggestion.message || suggestion.content || suggestion.description || JSON.stringify(suggestion);
      $('summary').append(item);
    }
    $('summary').hidden = !suggestions.length;
  } catch (e) {
    $('bar').hidden = true; $('result').setAttribute('aria-busy','false'); $('state').textContent = '进度待确认'; $('resume').hidden = false;
    message(e.message + ' 点击继续查询进度，避免重复提交。', 'error');
  }
}
$('query').addEventListener('input', queueSearch);
$('query').addEventListener('compositionstart', () => { composing = true; queueSearch(); });
$('query').addEventListener('compositionend', () => { composing = false; queueSearch(); });
$('query').onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); searchRules(); } };
function routeHint() {
  $('routeHint').textContent = selectedRule?.id
    ? '该规范由大以论文 API 提供，稿件将提交给服务方处理；导出计费前会单独确认。'
    : '请先选择院校规范。';
}
for (const id of fields) $(id).oninput = () => { if (!busy) clearResult(); controls(); };
$('drop').onclick = () => $('file').click();
$('file').onchange = e => { if (e.target.files[0]) selectFile(e.target.files[0]); };
for (const event of ['dragenter','dragover']) $('drop').addEventListener(event, e => { e.preventDefault(); if (!busy) $('drop').classList.add('over'); });
for (const event of ['dragleave','drop']) $('drop').addEventListener(event, e => { e.preventDefault(); $('drop').classList.remove('over'); });
$('drop').addEventListener('drop', e => { if (e.dataTransfer.files[0]) selectFile(e.dataTransfer.files[0]); });
$('go').onclick = submit;
$('createPayment').onclick = createPayment;
$('paidDownload').onclick = downloadPaid;
$('applyCoupon').onclick = applyCoupon;
$('couponCode').addEventListener('input', () => { $('couponInfo').textContent = ''; });
$('resume').onclick = () => { $('resume').hidden = true; controls(); poll(); };
job = sessionStorage.getItem('thesisJob');
if (job) {
  let draft = null;
  try { draft = JSON.parse(sessionStorage.getItem('thesisDraft')); } catch (_) { /* Ignore stale browser data. */ }
  selectedRule = {id:draft?.ruleId || 'resumed', label:draft?.ruleLabel || '已提交的院校'};
  renderSchools([selectedRule]);
  if (draft) {
    $('query').value = draft.query;
    for (const id of fields) $(id).value = draft.fields?.[id] || '';
    routeHint();
  } else { $('routeHint').textContent = '正在查询已提交的排版任务。'; }
  busy = true; controls(); poll();
} else { controls(); searchRules(); }

// Tabword requests are sent upstream only after an explicit user action.
let twConfig = null, twBusy = false;
let twMode = 'reduce_ai', twInput = 'text', twLanguage = 'zh', twPlatform = '知网_zh';
const twPresetCache = new Map();
const twModeDefaults = {
  reduce_duplicate: {
    service: 'paraphrase_stream',
    zh: [['zh_jc_1_0325','降重测试模式0325-1'],['zh_jc_2_0325','降重测试模式0325-2'],['zh_jc_3_0325','降重测试模式0325-3'],['zh_jc_4_0325','降重测试模式0325-4'],['jc_muti_language_1','超级改写（扩写降重）'],['jc_muti_language_zh','降重模式260206_zh']],
    en: [['5.09en','文档示例模型 · 5.09en']]
  },
  reduce_ai: {
    service: 'paraphrase_stream',
    zh: [['aigcmove_tp_s_merge_1','降 AI 混合模式'],['aigcmove_tp_s_micro_1','降 AI 微调模式 1'],['aigcmove_tp_s_micro_2','降 AI 微调模式 2'],['aigcmove_tp_s_micro_3','降 AI 微调模式 3'],['aigcmove_tp_s_micro_4','降 AI 微调模式 4'],['aigcmove_tp_s_micro_5','降 AI 微调模式 5'],['paperpass0618','PaperPass 专用'],['wanfang_0731','万方专用'],['gzd_1020','格子达专用'],['aigcmove_tp_s_1','AI 移除 260112-1 · 小幅改写'],['aigcmove_tp_s_2','AI 移除 260112-2 · 小幅改写'],['aigcmove_tp_s_3','AI 移除 260112-3 · 中幅改写'],['aigcmove_tp_s_4','AI 移除 260112-4 · 中幅改写'],['aigcmove_tp_s_5','AI 移除 260112-5 · 大幅改写'],['turnitin_s_1','Turnitin 260917'],['jc_remove_djgzl_p_8','AI 移除 5.09 · 口语化'],['aimove_matrix_s_1','AI 移除 7.22'],['aimove_weipu_english','维普英文专用'],['aimove_weipu_0513','维普专用 0513'],['aimove_weipu_0520','格子达学校版专用'],['daya','大雅专用']],
    en: [['5.09en','文档默认预设 · 5.09en']]
  },
  both: {
    service: 'paraphrase_stream',
    zh: [['aigcmove_weipu_sj','维普双降专用'],['aigcmove_wanfang_sj','万方双降专用'],['aigcmove_daya_sj','大雅双降专用'],['aigcmove_zhuque_sj','朱雀双降专用'],['aigcmove_paperpass_sj','PaperPass 双降专用'],['aigcmove_gzd_sj','格子达双降'],['aigcmove_sj_s_1','双降 260112-1'],['aigcmove_sj_s_2','双降 260112-2'],['aigcmove_sj_s_3','双降 260112-3']],
    en: [['5.09en','文档默认预设 · 5.09en']]
  },
  detect: {service:'detect_text', zh:[], en:[]}
};
function twSpec() { return twModeDefaults[twMode] || twModeDefaults.reduce_ai; }
function twServiceForState() { return twMode === 'detect' ? (twInput === 'file' ? 'detect_file' : 'detect_text') : (twInput === 'file' ? 'document_async' : 'paraphrase_stream'); }
function twSetOutputEmpty() {
  $('twOutput').innerHTML = '<div class="tw-empty"><span>▤</span><strong>当前暂无处理结果</strong><small>准备内容后，结果会显示在这里</small></div>';
  $('twStats').textContent = '';
  $('twLinks').replaceChildren();
  $('twOriginal').textContent = ''; $('twOriginal').hidden = true;
  $('twCompare').setAttribute('aria-pressed','false');
  document.querySelector('.tw-result-pane').classList.remove('is-comparing');
  $('twCompare').disabled = true;
}
function twPopulatePresets() {
  const select = $('twPreset');
  const list = twPresetCache.get(twMode)?.[twLanguage] || twSpec()[twLanguage] || [];
  const old = select.value;
  select.replaceChildren(...list.map(([value,label]) => new Option(label,value)));
  if (list.some(item => item[0] === old)) select.value = old;
  else if (list[0]) select.value = list[0][0];
  document.querySelector('.tw-model-group').hidden = twMode === 'detect';
}
function twRenderModeCards() {
  for (const card of document.querySelectorAll('[data-tw-mode]')) {
    const active = card.dataset.twMode === twMode;
    card.classList.toggle('selected', active);
    card.setAttribute('aria-pressed', String(active));
  }
}
function twRenderPlatformLanguage() {
  const suffix = twLanguage === 'en' ? '_en' : '_zh';
  const fallback = twLanguage === 'en' ? '知网_en' : '知网_zh';
  const buttons = [...document.querySelectorAll('[data-platform]')];
  for (const button of buttons) {
    const code = button.dataset.platform || '';
    button.dataset.platform = code.replace(/_(zh|en)$/i, '') + suffix;
  }
  if (!buttons.some(button => button.dataset.platform === twPlatform)) twPlatform = fallback;
  $('twPlatform').value = twPlatform;
  for (const button of buttons) {
    const active = button.dataset.platform === twPlatform;
    button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active));
  }
}
function twSetMode(mode) {
  twMode = twModeDefaults[mode] ? mode : 'reduce_ai';
  $('twMode').value = twMode;
  $('twService').value = twServiceForState();
  twRenderModeCards(); twPopulatePresets(); twChange();
}
function twSetInput(input) {
  twInput = input === 'file' ? 'file' : 'text';
  $('twService').value = twServiceForState();
  twChange();
}
function twChange() {
  const service = $('twService').value || twServiceForState();
  const file = service === 'document_async' || service === 'detect_file';
  const detect = service === 'detect_text' || service === 'detect_file';
  twInput = file ? 'file' : 'text';
  if (detect) twMode = 'detect';
  $('twTextWrap').hidden = file;
  $('twFileWrap').hidden = !file;
  $('twEnglishWrap').hidden = !file || detect;
  $('twDetectionWrap').hidden = !detect;
  $('twDetectionMeta').hidden = !detect || !file;
  $('twFormatWrap').hidden = true;
  $('twFile').accept = detect ? '.doc,.docx,.txt' : '.docx';
  $('twPlatform').value = twPlatform;
  document.querySelector('.tw-platform-group .tw-option-label').textContent = '检测平台';
  document.querySelector('.tw-platform-group').hidden = !detect;
  document.querySelector('.tw-model-group').hidden = detect;
  for (const button of document.querySelectorAll('[data-tw-input]')) {
    const active = button.dataset.twInput === twInput;
    button.classList.toggle('active', active); button.setAttribute('aria-selected', String(active));
  }
  $('twPrepare').innerHTML = '计价并创建付款订单 <span aria-hidden="true">↗</span>';
  $('twSubmit').hidden = true;
  $('twEstimate').hidden = true;
  $('twTaskWrap').hidden = !$('twTask').value;
  twRenderModeCards(); twRenderPlatformLanguage();
  if (!twBusy) twSetOutputEmpty();
}
function twPayload() {
  const service = $('twService').value;
  if (service === 'document_async') return {preset_zh:$('twPreset').value.trim(),preset_en:$('twEnglish').value.trim(),generate_diff:$('twDiff').checked};
  if (service === 'detect_text') {
    const data = {task_platform:$('twPlatform').value.trim(),detection_type:'ai_rate',language:twLanguage === 'en' ? '英文' : '中文'};
    data.text = $('twText').value;
    if ($('twDetectionMode').value) data.mode = $('twDetectionMode').value;
    return data;
  }
  if (service === 'detect_file') {
    const data = {task_platform:$('twPlatform').value.trim()};
    if ($('twDetectionMode').value) data.mode = $('twDetectionMode').value;
    for (const [id,key] of [['twDetectTitle','title'],['twDetectAuthor','author'],['twBrandTitle','brand_title'],['twBrandSubtitle','brand_subtitle']]) if ($(id).value.trim()) data[key] = $(id).value.trim();
    return data;
  }
  return {preset:$('twPreset').value.trim(),text:$('twText').value};
}
function twRender(data, eventName = '') {
  if (!data || typeof data !== 'object') { $('twOutput').textContent = String(data ?? ''); return; }
  if (data.submitted === false) { $('twOutput').textContent = `${data.message}\n${data.development ? '服务商标记：开发中。' : '请求参数已准备。'}`; return; }
  if (eventName === 'progress' && data.rewritten) {
    if ($('twOutput').querySelector('.tw-empty')) $('twOutput').textContent = '';
    $('twOutput').textContent += data.rewritten + '\n';
    if (data.original) {
      $('twOriginal').textContent += `${data.original}\n`;
      $('twCompare').disabled = false;
    }
  }
  const payload = data.data && typeof data.data === 'object' ? data.data : data.result && typeof data.result === 'object' ? data.result : data;
  const detectionSummary = payload.score != null ? `AI 率检测完成：${payload.score}%${payload.level ? `（${payload.level}）` : ''}` : '';
  const text = payload.paraphrased_text || payload.rewritten || payload.result_text || detectionSummary || payload.stage || payload.status || (!$('twOutput').textContent ? payload.message : null);
  if (payload.message) $('twMessage').textContent = payload.message;
  if (text && eventName !== 'progress') $('twOutput').textContent = text;
  if (payload.paraphrased_text && twMode !== 'detect') {
    $('twOriginal').textContent = `原文\n${$('twText').value}`;
    $('twCompare').disabled = false;
  }
  if (payload.task_id || payload.jobId) { $('twTask').value = payload.task_id || payload.jobId; $('twTaskWrap').hidden = false; }
  const stats = [];
  for (const [key,label] of [['score','AI 率'],['level','风险等级'],['total_chars','检测字符数'],['ai_rate','AI 率'],['original_ai_rate','原始 AI 率'],['rewritten_ai_rate','改写后 AI 率'],['reduction','降低'],['character_count','字符数'],['total_time','耗时']]) if (payload[key] != null) stats.push(`${label}：${payload[key]}${key === 'score' ? '%' : ''}`);
  if (stats.length) $('twStats').textContent = stats.join('　·　');
  const links = {...payload, ...(payload.report_files || {})};
  for (const key of ['result_url','paraphrased_url','diff_url','docxUrl','previewUrl','downloadUrl','workbenchUrl','html_detail_url','pdf_summary_url']) {
    if (!links[key]) continue;
    try { const url = new URL(links[key]); if (!['https:','http:'].includes(url.protocol)) continue;
      const link = document.createElement('a'); link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
      link.textContent = key === 'diff_url' ? '下载标红对比稿' : key === 'previewUrl' || key === 'workbenchUrl' ? '查看报告 / 对照' : key === 'html_detail_url' ? '查看 HTML 详细报告' : key === 'pdf_summary_url' ? '下载 PDF 检测报告' : '下载处理结果'; $('twLinks').append(link);
    } catch {}
  }
  if (!payload.paraphrased_text && !(eventName === 'progress' && payload.original)) $('twCompare').disabled = true;
}
async function twRequest(operation, values, submit, includeFile = true, paidOrder = '', savedFile = undefined) {
  const body = new FormData(); body.append('params', JSON.stringify(values)); body.append('submit', String(submit));
  if (paidOrder) body.append('payment_order', paidOrder);
  const file = savedFile === undefined ? (!$('twFileWrap').hidden ? $('twFile').files[0] : null) : savedFile;
  if (includeFile && file) body.append('file', file);
  const response = await fetch('/api/tabword/' + operation, {method:'POST', body});
  await twConsume(response);
}
async function twConsume(response) {
  if (!response.ok) { const error = await response.json().catch(() => ({})); throw new Error(error.detail || '请求失败；请勿重复提交。'); }
  if (!(response.headers.get('content-type') || '').includes('text/event-stream')) { twRender(await response.json()); return; }
  const reader = response.body.getReader(), decoder = new TextDecoder(); let pending = '';
  try {
    while (true) {
      const {value, done} = await reader.read(); pending += decoder.decode(value || new Uint8Array(), {stream:!done}); pending = pending.replace(/\r\n/g, '\n');
      let boundary;
      while ((boundary = pending.indexOf('\n\n')) >= 0) {
        const packet = pending.slice(0, boundary); pending = pending.slice(boundary + 2);
        const event = packet.split('\n').find(line => line.startsWith('event:'))?.slice(6).trim();
        const raw = packet.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n');
        if (!raw) continue;
        const data = JSON.parse(raw); if (event === 'error') throw new Error(data.message || '服务商处理失败；请勿自动重试。'); twRender(data, event);
      }
      if (done) break;
    }
  } finally { await reader.cancel().catch(() => {}); }
}
async function twAction(submit, polling = false) {
  if (submit && !polling) {
    await twBuy();
    return;
  }
  if (twBusy) return; twBusy = true;
  for (const id of ['twPrepare','twSubmit','twPoll','twEstimate']) $(id).disabled = true;
  $('twMessage').textContent = polling ? '正在查询任务…' : '正在请求服务，请勿重复提交…';
  if (!polling) twSetOutputEmpty();
  try {
    const service = $('twService').value;
    const operation = polling ? (twPending?.operation === 'document_async' ? 'document_task' : 'paraphrase_task') : service;
    const values = polling ? {task_id:$('twTask').value} : twPayload();
    await twRequest(operation, values, submit, !polling, twPending?.order_id || '');
  } catch (error) { $('twMessage').textContent = error.message; }
  finally { twBusy = false; for (const id of ['twPrepare','twSubmit','twPoll','twEstimate']) $(id).disabled = false; }
}
let twPending = null, twPurchase = null;
try { twPending = JSON.parse(sessionStorage.getItem('twPayment') || 'null'); } catch {}
if (twPending) {
  $('twBilling').hidden = false;
  $('twBillInfo').textContent = '有待处理的服务订单。页面刷新后请恢复原文、文件和选项，再确认付款状态。';
  if (twPending.checkout_url) { $('twCheckout').href = twPending.checkout_url; $('twCheckout').hidden = false; }
}
async function twBuy() {
  if (twBusy) return;
  if (twPending) { $('twBilling').hidden = false; $('twMessage').textContent = '请先处理已有订单，或点击“另建新任务”。'; return; }
  twBusy = true; $('twPrepare').disabled = true;
  try {
    const operation = $('twService').value, values = twPayload();
    const file = !$('twFileWrap').hidden ? $('twFile').files[0] || null : null;
    const body = new FormData(); body.append('operation', operation); body.append('params', JSON.stringify(values));
    if (file) body.append('file', file);
    const response = await fetch('/api/tabword/billing/quote', {method:'POST', body});
    const quote = await response.json(); if (!response.ok) throw new Error(quote.detail || '计价失败');
    const payment = await request(`/api/tabword/billing/${quote.quote_id}/payment`, {method:'POST'});
    twPurchase = {operation, values, file};
    twPending = {...payment, quote_id:quote.quote_id, operation};
    sessionStorage.setItem('twPayment', JSON.stringify(twPending));
    $('twBilling').hidden = false;
    $('twBillInfo').textContent = `${operation.startsWith('detect') ? '检测' : '改写'}${quote.characters ? ` · ${quote.characters} 字符` : ''}，应付 ¥${(quote.amount_cents / 100).toFixed(2)}。订单号：${payment.order_id}。付款后点击“我已付款，开始处理”。`;
    $('twCheckout').href = payment.checkout_url; $('twCheckout').hidden = false;
    $('twMessage').textContent = '订单已创建，尚未调用处理服务。';
  } catch (error) { $('twMessage').textContent = error.message; }
  finally { twBusy = false; $('twPrepare').disabled = false; }
}
async function twRunPaid() {
  if (twBusy || !twPending) return;
  twBusy = true; $('twPaidRun').disabled = true;
  try {
    const state = await request(`/api/tabword/billing/${twPending.quote_id}/${twPending.order_id}`);
    if (state.status !== 'TRADE_SUCCESS') throw new Error('尚未收到支付宝付款确认，请稍后重试。');
    $('twCheckout').hidden = true;
    if (state.service_state === 'complete') {
      twSetOutputEmpty();
      await twConsume(await fetch(`/api/tabword/billing/${twPending.quote_id}/${twPending.order_id}/result`));
      $('twMessage').textContent = '已恢复已付款订单的结果，未重复调用处理服务。';
      return;
    }
    if (state.task_id) { $('twTask').value = state.task_id; $('twTaskWrap').hidden = false; $('twMessage').textContent = '已提交，请查询任务结果。'; return; }
    if (state.service_state !== 'ready') throw new Error('该订单已经提交处理，不能重复调用。如未收到结果，请联系管理员核查。');
    const purchase = twPurchase || {operation:$('twService').value, values:twPayload(), file:!$('twFileWrap').hidden ? $('twFile').files[0] || null : null};
    if (purchase.operation !== twPending.operation) throw new Error('请恢复购买时的处理类型和选项。');
    $('twMessage').textContent = '付款已确认，正在处理，请勿关闭页面或重复提交…';
    twSetOutputEmpty();
    await twRequest(purchase.operation, purchase.values, true, true, twPending.order_id, purchase.file);
    $('twBillInfo').textContent = `订单 ${twPending.order_id} 已提交处理；新内容请另建新任务。`;
  } catch (error) { $('twMessage').textContent = error.message; }
  finally { twBusy = false; $('twPaidRun').disabled = false; }
}
$('twPaidRun').addEventListener('click', twRunPaid);
$('twNewOrder').addEventListener('click', () => {
  if (twBusy) return;
  if (!confirm('另建新任务需重新付款。请确认已有订单已处理完毕；未完成的订单请保留订单号以便核查。')) return;
  twPending = null; twPurchase = null; sessionStorage.removeItem('twPayment');
  $('twBilling').hidden = true; $('twMessage').textContent = '可以创建新任务。';
});
async function twEstimate() {
  if (twBusy || $('twFile').files.length === 0) { $('twMessage').textContent = '请先选择 Word 文档。'; return; }
  twBusy = true; $('twEstimate').disabled = true; $('twMessage').textContent = '正在本地准备费用估算请求…';
  try { await twRequest('estimate_cost', {preset_zh:$('twPreset').value.trim()}, false, true); $('twMessage').textContent = '费用估算请求已准备 · 未提交、未计费'; }
  catch (error) { $('twMessage').textContent = error.message; }
  finally { twBusy = false; $('twEstimate').disabled = false; }
}
$('twService').addEventListener('change', twChange);
$('twPrepare').addEventListener('click', () => twAction(true));
$('twSubmit').addEventListener('click', () => { $('twMessage').textContent = '付费任务提交暂未开放。'; });
$('twPoll').addEventListener('click', () => twAction(true, true));
$('twEstimate').addEventListener('click', twEstimate);
$('twText').addEventListener('input', () => { $('twCounter').textContent = `${$('twText').value.length} / 50000 字符`; });
$('twFile').addEventListener('change', () => { const file = $('twFile').files[0]; $('twFileName').hidden = !file; if (file) $('twFileName').textContent = file.name; });
for (const button of document.querySelectorAll('[data-tw-mode]')) button.addEventListener('click', () => twSetMode(button.dataset.twMode));
for (const button of document.querySelectorAll('[data-tw-input]')) button.addEventListener('click', () => twSetInput(button.dataset.twInput));
for (const button of document.querySelectorAll('[data-tw-language]')) button.addEventListener('click', () => {
  twLanguage = button.dataset.twLanguage;
  for (const item of document.querySelectorAll('[data-tw-language]')) { const active = item === button; item.classList.toggle('active', active); item.setAttribute('aria-pressed', String(active)); }
  twPopulatePresets(); twRenderPlatformLanguage();
});
for (const button of document.querySelectorAll('[data-platform]')) button.addEventListener('click', () => {
  twPlatform = button.dataset.platform; $('twPlatform').value = twPlatform;
  for (const item of document.querySelectorAll('[data-platform]')) { const active = item === button; item.classList.toggle('active', active); item.setAttribute('aria-pressed', String(active)); }
});
$('twModels').addEventListener('click', async () => {
  if (!twConfig?.catalog_reads_enabled) { $('twModelInfo').textContent = '尚未配置 Tabword API Key，无法读取官方预设。'; return; }
  try {
    const mode = twMode === 'both' ? 'both' : twMode === 'reduce_duplicate' ? 'reduce_duplicate' : 'reduce_ai';
    const response = await fetch('/api/tabword/catalog/presets?mode=' + mode); const data = await response.json(); if (!response.ok) throw new Error(data.detail || '模型列表读取失败');
    const list = Array.isArray(data.presets) ? data.presets.filter(p => p && p.code) : [];
    if (!list.length) throw new Error('服务商未返回模型列表。');
    twPresetCache.set(twMode, {zh:list.map(p => [p.code,p.name || p.code]),en:list.map(p => [p.code,p.name || p.code])});
    twPopulatePresets(); $('twModelInfo').textContent = `已同步 ${list.length} 个官方模型。`;
  } catch (error) { $('twModelInfo').textContent = error.message; }
});
async function twRefreshPlatforms() {
  if (!twConfig?.catalog_reads_enabled) {
    $('twMessage').textContent = '尚未配置 Tabword API Key，无法读取官方检测平台。';
    return;
  }
  const button = $('twPlatformRefresh'); button.disabled = true; $('twMessage').textContent = '正在读取官方检测平台…';
  try {
    const response = await fetch('/api/tabword/catalog/detection_options?language=' + twLanguage); const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '检测平台读取失败');
    const list = (data?.data?.platforms || []).filter(item => item?.task_platform).map(item => {
      const name = item.task_platform.replace(/_(zh|en)$/i,'');
      return {code:item.task_platform, name};
    });
    const unique = [...new Map(list.map(item => [item.code,item])).values()];
    if (!unique.length) throw new Error('服务商未返回可用平台');
    const wrap = $('twPlatforms'); wrap.replaceChildren(...unique.map(item => {
      const code = item.code || item.name, button = document.createElement('button'); button.type='button'; button.dataset.platform=code; button.textContent=item.name || code; button.setAttribute('aria-pressed','false'); return button;
    }));
    for (const item of wrap.querySelectorAll('button')) item.addEventListener('click', () => { twPlatform=item.dataset.platform; $('twPlatform').value=twPlatform; for (const peer of wrap.querySelectorAll('button')) { const active=peer===item; peer.classList.toggle('active',active); peer.setAttribute('aria-pressed',String(active)); } });
    twPlatform = unique[0].code; $('twPlatform').value=twPlatform;
    for (const item of wrap.querySelectorAll('button')) { const active=item.dataset.platform===twPlatform; item.classList.toggle('active',active); item.setAttribute('aria-pressed',String(active)); }
    $('twMessage').textContent = `已同步 ${unique.length} 个官方检测平台。`;
  } catch (error) { $('twMessage').textContent = error.message; }
  finally { button.disabled = false; }
}
$('twPlatformRefresh').addEventListener('click', twRefreshPlatforms);
$('twCompare').addEventListener('click', () => {
  const visible = $('twOriginal').hidden;
  $('twOriginal').hidden = !visible;
  $('twCompare').classList.toggle('active', visible);
  $('twCompare').setAttribute('aria-pressed', String(visible));
  document.querySelector('.tw-result-pane').classList.toggle('is-comparing', visible);
});
fetch('/api/tabword').then(response => { if (!response.ok) throw new Error(); return response.json(); }).then(data => {
  twConfig = data; twChange();
  const p = data.prices;
  $('twPriceInfo').textContent = `文本检测 ¥${(p.detect_text_cents/100).toFixed(2)}/次 · 文件检测 ¥${(p.detect_file_cents/100).toFixed(2)}/次 · 改写 ¥${(p.rewrite_per_1000_cents/100).toFixed(2)}/千字（非空白字符按比例计费，向上取整到分）。文档改写请用 .docx。`;
}).catch(() => { $('twMessage').textContent = '服务配置加载失败。'; });
function showWorkspace(view) {
  const format = view === 'format', detect = view === 'detect';
  $('formatPanel').hidden = !format; $('onlinePanel').hidden = format;
  const title = format ? '论文排版' : detect ? 'AIGC 检测' : '降重 / 降 AI';
  $('pageTitle').textContent = title; $('pageCrumb').textContent = title;
  $('pageLede').textContent = format ? '匹配院校规范，从初稿到成稿。' : detect ? '选择检测平台，查看论文的 AI 率报告。' : '按需选择降重或降 AI 模型，保留原意，整理表达。';
  for (const button of document.querySelectorAll('[data-view]')) { const active = button.dataset.view === view; button.classList.toggle('active', active); if (active) button.setAttribute('aria-current','page'); else button.removeAttribute('aria-current'); }
  if (!format) { if (detect) twSetMode('detect'); else if (twMode === 'detect') twSetMode('reduce_ai'); else twChange(); }
}
for (const button of document.querySelectorAll('[data-view]')) button.addEventListener('click', () => showWorkspace(button.dataset.view));
for (const button of document.querySelectorAll('[data-school]')) button.addEventListener('click', () => {
  if (busy) return; $('query').value = button.dataset.school; queueSearch(); searchRules();
});
for (const link of document.querySelectorAll('.flow-step')) link.addEventListener('click', () => {
  for (const item of document.querySelectorAll('.flow-step')) item.classList.toggle('active', item === link);
});
twSetMode('reduce_ai');
showWorkspace('format');

