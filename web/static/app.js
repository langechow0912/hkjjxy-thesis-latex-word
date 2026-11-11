'use strict';
const $ = id => document.getElementById(id);
const fields = ['title','authorName','titleEn','authorNameEn','tutorName','tutorNameEn','subjectName','subjectNameEn','subTitle','subTitleEn'];
let selectedRule = null, searchTimer = null, searchVersion = 0, composing = false;
let selectedFile = null, busy = false, searching = false, job = null, timer = null;
let paymentJob = null, paymentOrder = null, paymentTimer = null, finished = false, autoTitle = '';
// Declared up here: pricing code that runs while the page loads reads these.
let currentUser = null, afterLogin = null, pointsInfo = null, loginTab = 'email';
// 识别结果预览的状态：controls() 在页面加载时就会读它，所以必须声明在文件最前面。
const aiState = {id:null, base:new Map(), rows:[], edits:new Map(), suggestions:new Map(), filter:'issues', similar:null,
  info:null, notes:[], seq:0, busy:false, confirmed:false, truncated:false};
const yuan = cents => `¥${(cents / 100).toFixed(2)}`;
// 价格同时用人民币和积分显示：¥0.99（10 积分）
const dualPrice = cents => `${yuan(cents)}（${fmtPoints(pointsFor(cents))} 积分）`;
// 积分最多一位小数：10 显示 10，9.9 显示 9.9
const fmtPoints = value => Number.isInteger(value) ? String(value) : (Math.round(value * 10) / 10).toFixed(1);
function message(text, kind = '') { $('msg').textContent = text; $('msg').className = 'msg ' + kind; }
function formReady() {
  return !busy && !searching && !!selectedRule?.id && !!$('title').value.trim() && !!$('authorName').value.trim();
}
function controls() {
  for (const id of [...fields, 'query','drop']) $(id).disabled = busy;
  for (const button of $('schoolResults').querySelectorAll('button')) button.disabled = busy;
  $('schoolResults').setAttribute('aria-busy', String(searching));
  $('go').disabled = !formReady() || !selectedFile;
  const docx = !!selectedFile && /\.docx$/i.test(selectedFile.name);
  $('checkStructure').disabled = busy || searching || !docx || aiState.busy;
  $('checkStructure').title = !selectedFile ? '先选择 .docx 论文' : docx ? '' : '识别结果预览暂只支持 .docx，.doc 请直接点「开始排版」';
  $('bar').hidden = !busy;
  $('result').setAttribute('aria-busy', String(busy));
}
// A finished (possibly paid) result stays on screen until the user submits a new
// paper; editing fields, searching or choosing a file must not discard the order.
function clearResult(force = false) {
  if (finished && !force) return;
  finished = false;
  clearTimeout(timer); job = null; $('files').replaceChildren(); $('summary').replaceChildren();
  clearTimeout(paymentTimer); paymentJob = null; paymentOrder = null; $('paymentPanel').hidden = true;
  sessionStorage.removeItem('thesisPaymentOrder'); sessionStorage.removeItem('thesisJob');
  $('summary').hidden = true; $('resume').hidden = true;
  $('state').textContent = selectedFile ? '待排版' : '待上传'; message('');
}
async function request(url, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 120000);
  try {
    let response;
    try { response = await fetch(url, {...options, signal:controller.signal}); }
    catch (error) {
      throw new Error(error.name === 'AbortError' ? '请求超时（超过 2 分钟），请检查网络后重试。' : '网络连接失败，请检查网络后重试。');
    }
    if (response.status === 401 && !url.startsWith('/api/account/')) {
      // Login expired or never happened: ask for it instead of showing a raw error.
      currentUser = null; renderAccount(); openLogin();
    }
    if (!response.ok) {
      const errors = {401:'请先登录。',403:'排版服务未授权，请检查接口权限。',413:'文件过大，请压缩至 20 MB 以内。',429:'请求过于频繁，请稍后再试。',503:'排版服务暂不可用，请检查配置或稍后重试。'};
      const detail = await response.json().catch(() => ({}));
      const error = new Error(typeof detail.detail === 'string' && detail.detail ? detail.detail : errors[response.status] || '请求未成功，请检查填写内容或稍后重试。');
      error.status = response.status;
      throw error;
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
  aiClose();
  clearResult(); selectedFile = null; $('filename').hidden = true; $('file').value = '';
  if (!/\.(docx|doc)$/i.test(file.name)) message('请上传 .docx / .doc 格式的 Word 论文。', 'error');
  else if (!file.size || file.size > 20 * 1024 * 1024) message('文件不能为空，且不能超过 20 MB。', 'error');
  else {
    selectedFile = file; $('filename').hidden = false; $('filename').textContent = file.name;
    // Follow the file name unless the user typed their own title.
    const title = $('title').value.trim();
    if (!title || title === autoTitle) { autoTitle = file.name.replace(/\.(docx|doc)$/i, ''); $('title').value = autoTitle; }
    $('state').textContent = '待排版'; message('稿件已选择，请核对题目、作者和排版规范。');
  }
  controls();
}
async function submit(extra = null) {
  if (!extra?.analysisId) extra = null;       // 只有带识别结果编号的才算「按预览排版」，其余一律走原来的上传流程
  if ((extra ? !formReady() : $('go').disabled) || !requireLogin(() => submit(extra))) return;
  // The previous result (paid or not) stays in 我的记录, so no confirmation is needed.
  busy = true; clearResult(true); controls(); $('state').textContent = '提交中';
  const data = new FormData();
  if (extra) { data.append('analysisId', extra.analysisId); data.append('overrides', JSON.stringify(extra.overrides)); }
  else data.append('file', selectedFile);
  data.append('ruleId', selectedRule.id);
  data.append('ruleLabel', selectedRule.label || '');
  for (const id of fields) data.append(id, $(id).value.trim());
  message('正在上传稿件并提交排版…');
  try {
    const result = await request('/api/format', {method:'POST',body:data});
    if (!result.job) throw new Error('未收到任务编号，请确认服务状态。');
    job = result.job; sessionStorage.setItem('thesisJob', job);
    if (extra) aiClose();
    sessionStorage.setItem('thesisDraft', JSON.stringify({query:$('query').value, ruleId:selectedRule.id,
      ruleLabel:selectedRule.label,
      fields:Object.fromEntries(fields.map(id => [id, $(id).value]))}));
    poll();
  } catch (e) {
    // A lost response may still have created a task. Avoid automatic resubmission.
    busy = false; controls();
    if (extra && e.status === 404) {                     // 识别结果过期了：文件还在，重新检查即可
      $('state').textContent = '待排版'; aiClose(); message('识别结果已过期，请点「检查识别结果」重新检查。', 'error'); return;
    }
    $('state').textContent = '提交未确认';
    message(e.message + ' 若提交时网络中断，请先确认任务是否已创建，避免重复提交。', 'error');
  }
}
// 「付款」goes straight to Alipay. The tab is opened during the click itself (a
// window opened after the order request would be blocked as a popup) and pointed
// at the cashier once the order exists. This page stays open to watch the payment.
function openPayWindow() {
  const win = window.open('', '_blank');
  if (!win) return null;
  try {
    win.opener = null;
    win.document.title = '正在打开支付宝…';
    win.document.body.innerHTML = '<p style="font:16px sans-serif;margin:40px;color:#555">正在打开支付宝收银台…</p>';
  } catch {}
  return win;
}
function sendToCheckout(win, url, link) {
  link.href = url; link.hidden = false;
  if (win && !win.closed) {
    win.location.href = new URL(url, location.href).href;
    link.classList.remove('blocked'); link.textContent = '没看到付款页？重新打开';
    return true;
  }
  // Popup blocked: the fallback link becomes the main action.
  link.classList.add('blocked'); link.textContent = '打开支付宝付款';
  return false;
}
function closePayWindow(win) { try { if (win && !win.closed) win.close(); } catch {} }
let formatPrice = 0;
function formatPayLabel(cents = formatPrice) { $('createPayment').textContent = `支付宝付款 ¥${(cents / 100).toFixed(2)}`; }
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
      const cents = pointsInfo?.prices_cents?.format_export;
      $('paymentInfo').textContent = '支付宝支付尚未开放' + (cents ? `；Word 成品 ${dualPrice(cents)}，可用积分付款后下载。` : '，暂不能下载 Word。');
      updatePointButtons();
      return;
    }
    formatPrice = config.amount_cents; formatPayLabel();
    $('paymentInfo').textContent = `Word 成品 ${dualPrice(formatPrice)}。付款后即可下载，成品保存 24 小时，到期自动删除。`;
    $('couponControls').hidden = false;
    $('createPayment').hidden = false;
    updatePointButtons();
    const saved = sessionStorage.getItem('thesisPaymentOrder');
    if (saved) {
      try {
        const order = JSON.parse(saved);
        if (order.job === id && order.orderId) {
          paymentOrder = order.orderId;
          // Restored after a refresh: no cashier tab was opened by this page, so the link is the main action.
          sendToCheckout(null, `/api/format/jobs/${encodeURIComponent(id)}/payment/${encodeURIComponent(paymentOrder)}/checkout`, $('paymentCheckout'));
          $('createPayment').hidden = true;
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
  const id = paymentJob, button = $('createPayment'), win = openPayWindow(); button.disabled = true;
  $('paymentInfo').textContent = '正在打开支付宝…';
  try {
    const result = await request(`/api/format/jobs/${encodeURIComponent(id)}/payment`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({coupon_code:$('couponCode').value.trim()}),
    });
    if (paymentJob !== id) { closePayWindow(win); return; }
    paymentOrder = result.order_id;
    sessionStorage.setItem('thesisPaymentOrder', JSON.stringify({job:id,orderId:paymentOrder,couponCode:result.coupon_code}));
    button.hidden = true;
    $('couponCode').disabled = true; $('applyCoupon').disabled = true;
    if (result.coupon_code) $('couponInfo').textContent = `已使用优惠码 ${result.coupon_code}`;
    const amount = `应付 ${dualPrice(result.amount_cents)}`;
    if (result.status === 'TRADE_SUCCESS') closePayWindow(win);
    else $('paymentInfo').textContent = sendToCheckout(win, result.checkout_url, $('paymentCheckout'))
      ? `已打开支付宝收银台（${amount}）。付款后回到本页，会自动出现下载按钮。`
      : `浏览器拦截了付款页（${amount}），请点下面的按钮打开支付宝。`;
    checkPayment(id, paymentOrder);
  } catch (error) { closePayWindow(win); if (paymentJob === id) $('paymentInfo').textContent = error.message; }
  finally { button.disabled = false; }
}
async function createPointsPayment() {
  if (!paymentJob || !requireLogin(createPointsPayment)) return;
  const id = paymentJob, button = $('createPointsPayment'); button.disabled = true;
  $('paymentInfo').textContent = '正在用积分付款…';
  try {
    const result = await request('/api/points/pay', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({job_id:id})});
    if (paymentJob !== id) return;
    paymentOrder = result.order_id;
    sessionStorage.setItem('thesisPaymentOrder', JSON.stringify({job:id, orderId:paymentOrder}));
    $('createPayment').hidden = true; $('paymentCheckout').hidden = true;
    $('couponCode').disabled = true; $('applyCoupon').disabled = true;
    if (currentUser) { currentUser.points = result.balance; renderAccount(); }
    checkPayment(id, paymentOrder);
  } catch (error) {
    if (paymentJob === id) $('paymentInfo').textContent = error.message;
    if (error.status === 402) openPoints(error.message);
  } finally { button.disabled = false; updatePointButtons(); }
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
      $('paymentInfo').textContent = '付款页已超时，请重新点「支付宝付款」。';
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
    const disposition = response.headers.get('content-disposition') || '';
    const encoded = /filename\*=utf-8''([^;]+)/i.exec(disposition), plain = /filename="([^"]+)"/i.exec(disposition);
    let filename = ($('title').value.trim() || '排版论文') + '.docx';
    try { if (encoded) filename = decodeURIComponent(encoded[1]); else if (plain) filename = plain[1]; } catch {}
    const link = document.createElement('a'); link.href = objectUrl; link.download = filename; link.click();
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
    $('couponInfo').textContent = `${quote.coupon_code} 已应用：优惠 ${yuan(quote.discount_cents)}，应付 ${dualPrice(quote.amount_cents)}。优惠券只用于支付宝付款，积分支付按原价。`;
    formatPayLabel(quote.amount_cents);
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
    busy = false; controls(); $('resume').hidden = true;
    if (data.state === 'error') { sessionStorage.removeItem('thesisJob'); $('state').textContent = '排版失败'; message(data.stage || '稿件排版失败，请检查 Word 原稿或稍后重试。', 'error'); return; }
    // Keep thesisJob: after paying, a refresh or the Alipay return page must find this job again.
    finished = true;
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
    try {
      const saved = JSON.parse(sessionStorage.getItem('thesisPaymentOrder') || 'null');
      if (saved?.job === completedJob) setupPayment(completedJob);
    } catch { sessionStorage.removeItem('thesisPaymentOrder'); }
  } catch (e) {
    if (e.status === 410) {
      // Paid more than 24 hours ago: the server deleted the result by design.
      busy = false; finished = false; job = null; controls();
      for (const key of ['thesisJob','thesisPaymentOrder']) sessionStorage.removeItem(key);
      $('resume').hidden = true; $('state').textContent = '已过期'; message(e.message, 'error');
      return;
    }
    if (e.status === 404) {
      // The server no longer has this job; unlock the page instead of retrying forever.
      const order = (() => { try { return JSON.parse(sessionStorage.getItem('thesisPaymentOrder') || 'null')?.orderId; } catch { return null; } })();
      busy = false; finished = false; job = null; sessionStorage.removeItem('thesisJob'); controls();
      $('resume').hidden = true; $('state').textContent = '任务已失效';
      message('服务器上已找不到这个排版任务。' + (order ? `如已付款，请保留订单号 ${order} 联系客服处理，` : '') + '可以重新上传排版。', 'error');
      return;
    }
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
for (const id of fields) $(id).oninput = () => { if (!busy && !finished) clearResult(); controls(); };
$('drop').onclick = () => { if (requireLogin(() => $('file').click())) $('file').click(); };
$('file').onchange = e => { if (e.target.files[0]) selectFile(e.target.files[0]); };
for (const event of ['dragenter','dragover']) $('drop').addEventListener(event, e => { e.preventDefault(); if (!busy) $('drop').classList.add('over'); });
for (const event of ['dragleave','drop']) $('drop').addEventListener(event, e => { e.preventDefault(); $('drop').classList.remove('over'); });
$('drop').addEventListener('drop', e => {
  const file = e.dataTransfer.files[0];
  if (file && requireLogin(() => selectFile(file))) selectFile(file);
});
$('go').onclick = () => submit();          // 不能直接绑 submit：点击事件对象会被当成 extra 传进去
$('createPayment').onclick = createPayment;
$('createPointsPayment').onclick = createPointsPayment;
$('paidDownload').onclick = downloadPaid;
$('applyCoupon').onclick = applyCoupon;
$('couponCode').addEventListener('input', () => { $('couponInfo').textContent = ''; formatPayLabel(); });
$('resume').onclick = () => { $('resume').hidden = true; controls(); poll(); };
{
  // Alipay returns to return_url?job=...&order=... in a new tab with empty sessionStorage.
  const params = new URLSearchParams(location.search);
  const returnedJob = params.get('job'), returnedOrder = params.get('order');
  if (/^j[0-9a-f]{32}$/.test(returnedJob || '') && /^[A-Za-z0-9_-]{8,64}$/.test(returnedOrder || '')) {
    sessionStorage.setItem('thesisJob', returnedJob);
    sessionStorage.setItem('thesisPaymentOrder', JSON.stringify({job:returnedJob, orderId:returnedOrder}));
  }
  // API service orders: the paid text/file lives in the tab that created the order.
  if (/^TW-[0-9a-f]{32}$/.test(returnedJob || '') && /^[A-Za-z0-9_-]{8,64}$/.test(returnedOrder || '')
      && sessionStorage.getItem('twPayment') === null) {
    sessionStorage.setItem('twReturned', JSON.stringify({quote_id:returnedJob, order_id:returnedOrder}));
  }
  if (params.has('job') || params.has('out_trade_no')) history.replaceState(null, '', location.pathname);
}
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
let twMode = 'reduce_ai', twLastRewrite = 'reduce_ai', twInput = 'text', twLanguage = 'zh', twPlatform = '知网_zh';
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
    // The ✓ badge follows the selected card.
    let check = card.querySelector('.tw-check');
    if (active && !check) {
      check = document.createElement('b'); check.className = 'tw-check'; check.setAttribute('aria-hidden', 'true');
      check.textContent = '✓'; card.append(check);
    } else if (!active && check) check.remove();
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
  if (twMode !== 'detect') twLastRewrite = twMode;
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
  $('twSubmit').hidden = true;
  $('twEstimate').hidden = true;
  $('twTaskWrap').hidden = !$('twTask').value;
  twRenderModeCards(); twRenderPlatformLanguage();
  if (!twBusy) twSetOutputEmpty();
  twRefresh();
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
  for (const key of ['original_url','result_url','paraphrased_url','diff_url','docxUrl','previewUrl','downloadUrl','workbenchUrl','html_detail_url','pdf_summary_url']) {
    if (!links[key]) continue;
    try { const url = new URL(links[key]); if (!['https:','http:'].includes(url.protocol)) continue;
      const link = document.createElement('a'); link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
      link.textContent = key === 'original_url' ? '下载原文' : key === 'result_url' || key === 'paraphrased_url' ? '下载改写稿' : key === 'diff_url' ? '下载标红对比稿' : key === 'previewUrl' || key === 'workbenchUrl' ? '查看报告 / 对照' : key === 'html_detail_url' ? '查看 HTML 详细报告' : key === 'pdf_summary_url' ? '下载 PDF 检测报告' : '下载处理结果'; $('twLinks').append(link);
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
    const operation = polling ? twTask?.task_operation || 'paraphrase_task' : service;
    const values = polling ? {task_id:$('twTask').value} : twPayload();
    await twRequest(operation, values, submit, !polling, twTask?.order_id || '');
  } catch (error) { $('twMessage').textContent = error.message; }
  finally { twBusy = false; for (const id of ['twPrepare','twSubmit','twPoll','twEstimate']) $(id).disabled = false; }
}
// Orders follow the content: whatever the user selects, the next 「付款并开始」 creates
// (or reuses) the matching order. An unpaid order is never deleted early: its Alipay
// tab may still be open, so it is watched until Alipay expires it and, if paid, runs
// with the content it was bought for.
const TW_ORDER_LIFETIME = 15 * 60 * 1000, TW_ORDER_GRACE = 5 * 60 * 1000;
let twOrders = [], twTask = null, twWatchTimer = null;
const twFiles = new Map();
try { twOrders = JSON.parse(sessionStorage.getItem('twOrders') || '[]').filter(o => o?.order_id); } catch {}
function twSaveOrders() { sessionStorage.setItem('twOrders', JSON.stringify(twOrders)); }
function twKey() {
  const operation = $('twService').value, file = !$('twFileWrap').hidden ? $('twFile').files[0] || null : null;
  return JSON.stringify([operation, twPayload(), file ? [file.name, file.size, file.lastModified] : null]);
}
function twOpen(order) { return !order.done && (order.paid || Date.now() - order.created < TW_ORDER_LIFETIME + TW_ORDER_GRACE); }
function twCurrentOrder() {
  const key = twKey();
  return twOrders.find(order => order.key === key && twOpen(order) && (order.paid || Date.now() - order.created < TW_ORDER_LIFETIME)) || null;
}
function twShowOrder(order) {
  $('twBilling').hidden = !order;
  if (!order) return;
  $('twBillInfo').textContent = order.paid
    ? `订单 ${order.order_id} 已付款，正在处理。`
    : `应付 ${dualPrice(order.amount_cents)} · 订单 ${order.order_id}。在支付宝付款后，这里会自动开始处理；也可以改用积分支付。`;
  // The cashier opens by itself; this link is only for a closed or blocked tab.
  $('twCheckout').href = order.checkout_url || '#'; $('twCheckout').hidden = order.paid || !order.checkout_url;
  if (!order.blocked) { $('twCheckout').classList.remove('blocked'); $('twCheckout').textContent = '没看到付款页？重新打开'; }
  $('twPaidRun').hidden = !order.paid && !order.waiting;
}
function twPriceCents() {
  const p = twConfig?.prices, service = $('twService').value;
  if (!p) return null;
  if (service === 'detect_text') return p.detect_text_cents;
  if (service === 'detect_file') return p.detect_file_cents;
  if (service === 'paraphrase_stream') {
    const count = [...$('twText').value].filter(char => !/\s/.test(char)).length;
    return count ? Math.max(1, Math.ceil(count * p.rewrite_per_1000_cents / 1000)) : null;
  }
  return null;
}
function twPointsPrice() { const cents = twPriceCents(); return cents == null ? null : pointsFor(cents); }
function twRefresh() {
  const cents = twPriceCents();
  const price = cents == null ? '' : ` ${yuan(cents)}`;
  $('twPrepare').innerHTML = `支付宝支付${price}并开始 <span aria-hidden="true">↗</span>`;
  $('twPriceNow').textContent = cents == null ? '' : `本次费用：${dualPrice(cents)}`;
  updatePointButtons();
  if (!twBusy) twShowOrder(twCurrentOrder());
}
function twSendToCheckout(order, win) {
  order.blocked = !sendToCheckout(win, order.checkout_url, $('twCheckout'));
  twShowOrder(order);
  if (order.blocked) { $('twCheckout').classList.add('blocked'); $('twCheckout').textContent = '打开支付宝付款'; }
  $('twMessage').textContent = order.blocked
    ? '浏览器拦截了付款页，请点「打开支付宝付款」。'
    : '已打开支付宝收银台。付款后本页会自动开始处理，请不要关闭本页。';
}
async function twBuy(method = 'alipay') {
  if (twBusy || !requireLogin(() => twBuy(method))) return;
  const existing = twCurrentOrder();
  if (existing) {
    if (existing.paid) { twShowOrder(existing); $('twMessage').textContent = '这份内容已付款，正在处理。'; twRunPaid(existing); }
    else if (method === 'points') await twPayWithPoints(existing);
    else twSendToCheckout(existing, openPayWindow());
    return;
  }
  const operation = $('twService').value, values = twPayload();
  const file = !$('twFileWrap').hidden ? $('twFile').files[0] || null : null;
  const needsFile = operation === 'document_async' || operation === 'detect_file';
  if (needsFile ? !file : !String(values.text || '').trim()) {
    $('twMessage').textContent = needsFile ? '请先选择 Word 文档。' : '请先输入或粘贴要处理的文本。'; return;
  }
  const win = method === 'points' ? null : openPayWindow();
  let pending = null;
  twBusy = true; $('twPrepare').disabled = true; $('twMessage').textContent = method === 'points' ? '正在计价…' : '正在打开支付宝…';
  try {
    const key = twKey();
    const body = new FormData(); body.append('operation', operation); body.append('params', JSON.stringify(values));
    if (file) body.append('file', file);
    const quote = await request('/api/tabword/billing/quote', {method:'POST', body});
    if (method === 'points') {
      // The order only exists once the points are paid; until then nothing is watched.
      pending = {quote_id:quote.quote_id, order_id:'', checkout_url:'', amount_cents:quote.amount_cents, operation, values, key,
        fileName:file ? file.name : '', created:Date.now(), paid:false, done:false, file};
      return;
    }
    const payment = await request(`/api/tabword/billing/${quote.quote_id}/payment`, {method:'POST'});
    const order = {quote_id:quote.quote_id, order_id:payment.order_id, checkout_url:payment.checkout_url,
      amount_cents:quote.amount_cents, operation, values, key, fileName:file ? file.name : '', created:Date.now(), paid:false, done:false};
    if (file) twFiles.set(order.order_id, file);
    twOrders = [...twOrders.filter(twOpen), order]; twSaveOrders();
    twSendToCheckout(order, win);
    twWatchPayment();
  } catch (error) { closePayWindow(win); $('twMessage').textContent = error.message; }
  finally {
    twBusy = false; $('twPrepare').disabled = false;
    // Inside finally: the `return` in the points branch above would skip anything placed after it.
    if (pending) await twPayWithPoints(pending);
  }
}
async function twPayWithPoints(order) {
  $('twPayPoints').disabled = true; $('twMessage').textContent = '正在用积分付款…';
  try {
    const result = await request('/api/points/pay', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({quote_id:order.quote_id})});
    order.order_id = result.order_id; order.paid = true; order.waiting = false;
    if (order.file) { twFiles.set(order.order_id, order.file); delete order.file; }
    if (!twOrders.includes(order)) twOrders = [...twOrders.filter(twOpen), order];
    twSaveOrders();
    if (currentUser) { currentUser.points = result.balance; renderAccount(); }
    twShowOrder(order);
    await twRunPaid(order);
  } catch (error) {
    $('twMessage').textContent = error.message;
    if (error.status === 402) openPoints(error.message);
  } finally { $('twPayPoints').disabled = false; }
}
function twWatchPayment() {
  clearTimeout(twWatchTimer);
  const before = twOrders.length;
  twOrders = twOrders.filter(twOpen);
  if (twOrders.length !== before) { twSaveOrders(); twRefresh(); }
  const waiting = twOrders.filter(order => !order.paid || !order.waiting);
  if (!waiting.length) return;
  twWatchTimer = setTimeout(async () => {
    for (const order of waiting) {
      if (order.waiting) continue;
      try {
        const state = await request(`/api/tabword/billing/${order.quote_id}/${order.order_id}`);
        if (state.status === 'TRADE_SUCCESS' && !twBusy) await twRunPaid(order);
      } catch (error) {
        if (error.status === 404) { order.done = true; twSaveOrders(); }
      }
    }
    twWatchPayment();
  }, 3000);
}
async function twRunPaid(order) {
  if (twBusy || !order) return;
  twBusy = true; $('twPaidRun').disabled = true;
  try {
    const state = await request(`/api/tabword/billing/${order.quote_id}/${order.order_id}`);
    if (state.status !== 'TRADE_SUCCESS') throw new Error('尚未收到支付宝付款确认，请稍后再点。');
    order.paid = true; order.waiting = false; twSaveOrders();
    // Show the page the order belongs to, even if the user has moved on.
    const view = order.operation.startsWith('detect') ? 'detect' : 'rewrite';
    if ($('onlinePanel').hidden || (view === 'detect') !== (twMode === 'detect')) showWorkspace(view);
    twShowOrder(order);
    if (state.service_state === 'expired') {
      order.done = true; twSaveOrders(); $('twBilling').hidden = true;
      throw new Error('付款已超过 24 小时，结果已按规定删除。');
    }
    if (state.service_state === 'complete') {
      twSetOutputEmpty();
      await twConsume(await fetch(`/api/tabword/billing/${order.quote_id}/${order.order_id}/result`));
      order.done = true; twSaveOrders();
      $('twBilling').hidden = true; $('twMessage').textContent = '已付款订单的结果已打开。';
      return;
    }
    if (state.task_id) {
      twTask = {order_id:order.order_id, task_operation:state.task_operation || (order.operation === 'document_async' ? 'document_task' : 'paraphrase_task')};
      $('twTask').value = state.task_id; $('twTaskWrap').hidden = false;
      order.done = true; twSaveOrders();
      $('twBilling').hidden = true; $('twMessage').textContent = '已提交处理，点「查询任务结果」查看进度。';
      return;
    }
    if (state.service_state !== 'ready') throw new Error('该订单已经提交处理，不能重复调用。如未收到结果，请保留订单号联系客服。');
    if (!order.values) throw new Error('付款已确认，但这个页面没有保存下单时的内容。请回到下单的页面，或保留订单号联系客服。');
    let file = null;
    if (order.fileName) {
      file = twFiles.get(order.order_id) || null;
      const chosen = $('twFile').files[0];
      if (!file && chosen?.name === order.fileName) file = chosen;
      if (!file) { order.waiting = true; throw new Error(`付款已确认。页面刷新过，请重新选择购买时的文件「${order.fileName}」，再点「已付款但没有开始？点这里」。`); }
    }
    $('twMessage').textContent = '付款已确认，正在处理，请勿关闭页面…';
    twSetOutputEmpty(); $('twTask').value = ''; $('twTaskWrap').hidden = true;
    order.waiting = true; twSaveOrders();     // the server marks it as processing now: never auto-retry
    await twRequest(order.operation, order.values, true, true, order.order_id, file);
    if (twTaskFrom(order)) return;
    order.done = true; twSaveOrders();
    $('twBilling').hidden = true;
  } catch (error) {
    // A paid order that failed waits for the user's click instead of retrying every 3 s.
    if (order.paid) { order.waiting = true; twShowOrder(order); }
    $('twMessage').textContent = error.message;
  }
  finally { twBusy = false; $('twPaidRun').disabled = false; twSaveOrders(); }
}
function twTaskFrom(order) {
  // Async document jobs return a task id; polling needs the order that paid for it.
  if (!$('twTask').value) return false;
  twTask = {order_id:order.order_id, task_operation:order.operation === 'document_async' ? 'document_task' : 'paraphrase_task'};
  order.done = true; twSaveOrders(); $('twBilling').hidden = true;
  return true;
}
$('twPaidRun').addEventListener('click', () => {
  const order = twCurrentOrder() || twOrders.find(item => item.paid && !item.done) || twOrders[twOrders.length - 1];
  if (order) { order.waiting = false; twRunPaid(order); }
});
if (twOrders.length) twWatchPayment();
async function twEstimate() {
  if (twBusy || $('twFile').files.length === 0) { $('twMessage').textContent = '请先选择 Word 文档。'; return; }
  twBusy = true; $('twEstimate').disabled = true; $('twMessage').textContent = '正在本地准备费用估算请求…';
  try { await twRequest('estimate_cost', {preset_zh:$('twPreset').value.trim()}, false, true); $('twMessage').textContent = '费用估算请求已准备 · 未提交、未计费'; }
  catch (error) { $('twMessage').textContent = error.message; }
  finally { twBusy = false; $('twEstimate').disabled = false; }
}
$('twService').addEventListener('change', twChange);
$('twPrepare').addEventListener('click', () => twAction(true));
$('twPayPoints').addEventListener('click', () => twBuy('points'));
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
  $('twPriceInfo').textContent = `文本检测 ${dualPrice(p.detect_text_cents)}/次 · 文件检测 ${dualPrice(p.detect_file_cents)}/次 · 改写 ${dualPrice(p.rewrite_per_1000_cents)}/千字（非空白字符按比例计费，向上取整到分）。文档改写请用 .docx。1 元 = 10 积分。`;
}).catch(() => { $('twMessage').textContent = '服务配置加载失败。'; });
// Any change to what will be bought updates the price and which order is shown.
$('twText').addEventListener('input', twRefresh);
for (const id of ['twFile','twPreset','twDetectionMode','twEnglish','twDiff','twDetectTitle','twDetectAuthor','twBrandTitle','twBrandSubtitle'])
  $(id).addEventListener(id === 'twFile' || id === 'twPreset' || id === 'twDetectionMode' || id === 'twDiff' ? 'change' : 'input', twRefresh);
for (const id of ['twPlatforms','twLanguage']) $(id).addEventListener('click', twRefresh);
document.querySelector('label[for="twFile"]').addEventListener('click', event => {
  if (!requireLogin(() => $('twFile').click())) event.preventDefault();
});
function showWorkspace(view) {
  const format = view === 'format', detect = view === 'detect', history = view === 'history';
  $('formatPanel').hidden = !format; $('onlinePanel').hidden = format || history; $('historyPanel').hidden = !history;
  // The Word payment panel belongs to the formatting view only.
  $('paymentPanel').hidden = !format || !paymentJob;
  const title = format ? '论文排版' : detect ? 'AIGC 检测' : history ? '我的记录' : '降重 / 降 AI';
  $('pageCrumb').textContent = title;
  $('pageLede').textContent = format ? '匹配院校规范，从初稿到成稿。' : detect ? '选择检测平台，查看论文的 AI 率报告。'
    : history ? '登录后上传的论文和已付款的服务都在这里。' : '按需选择降重或降 AI 模型，保留原意，整理表达。';
  for (const button of document.querySelectorAll('[data-view]')) { const active = button.dataset.view === view; button.classList.toggle('active', active); if (active) button.setAttribute('aria-current','page'); else button.removeAttribute('aria-current'); }
  if (history) { loadHistory(); return; }
  if (format) return;
  // AIGC 检测 and 降重 / 降 AI are separate pages: detection has no rewrite cards.
  $('onlineTitle').textContent = detect ? 'AIGC 检测' : '降重 / 降 AI';
  $('onlineSub').textContent = detect ? '选择检测平台，粘贴文本或上传 Word，付款后生成 AI 率报告' : '选择处理方式和模型，付款后开始改写';
  $('twModeCards').hidden = detect;
  if (!twBusy) $('twMessage').textContent = '';
  twSetMode(detect ? 'detect' : twLastRewrite);
}
for (const button of document.querySelectorAll('[data-view]')) button.addEventListener('click', () => showWorkspace(button.dataset.view));
for (const button of document.querySelectorAll('[data-school]')) button.addEventListener('click', () => {
  if (busy) return; $('query').value = button.dataset.school; queueSearch(); searchRules();
});
// 左侧引导：点击高亮 + 随滚动高亮；第 4 步「AI预览标题」可跳过
const flowSteps = [...document.querySelectorAll('.flow-step')];
function flowActivate(link) { for (const item of flowSteps) item.classList.toggle('active', item === link); }
for (const link of flowSteps) link.addEventListener('click', () => flowActivate(link));
(function flowSpy() {
  if (!('IntersectionObserver' in window)) return;
  const targets = ['chooseSchool', 'paperInfo', 'upload', 'result'].map(id => document.getElementById(id));
  const seen = new Set();
  const spy = new IntersectionObserver(entries => {
    for (const entry of entries) entry.isIntersecting ? seen.add(entry.target) : seen.delete(entry.target);
    const current = targets.find(el => seen.has(el));
    if (!current) return;
    // 'result' 对应第 4 步；已跳过 / 面板未打开时仍落在第 4 步，用户点第 5 步再切换
    flowActivate(flowSteps[targets.indexOf(current)]);
  }, {rootMargin: '-96px 0px -55% 0px'});
  for (const el of targets) if (el) spy.observe(el);
})();
$('flowSkip').addEventListener('click', () => {
  $('flowPreview').classList.add('skipped');
  const confirmStep = flowSteps.find(el => el.dataset.flow === 'confirm');
  flowActivate(confirmStep);
  $('result').scrollIntoView({behavior: 'smooth', block: 'center'});
  if (!$('go').disabled) $('go').focus({preventScroll: true});
});

// ---- 账号 ----
function renderAccount() {
  $('accountName').hidden = !currentUser;
  $('accountName').textContent = currentUser ? (currentUser.email || currentUser.username) : '';
  $('loginOpen').hidden = !!currentUser; $('logout').hidden = !currentUser; $('historyNav').hidden = !currentUser;
  $('pointsBtn').hidden = !currentUser;
  if (currentUser) $('pointsBtn').textContent = `积分 ${fmtPoints(currentUser.points ?? 0)} · 充值`;
  $('bindEmailBtn').hidden = !currentUser || !!currentUser.email;
  updatePointButtons();
}
function setLoginTab(name) {
  loginTab = name === 'password' ? 'password' : 'email';
  for (const button of document.querySelectorAll('[data-login-tab]')) {
    const active = button.dataset.loginTab === loginTab;
    button.classList.toggle('active', active); button.setAttribute('aria-selected', String(active));
  }
  $('paneEmail').hidden = loginTab !== 'email'; $('panePassword').hidden = loginTab !== 'password';
  $('emailLoginSubmit').hidden = loginTab !== 'email';
  $('loginSubmit').hidden = $('registerSubmit').hidden = loginTab !== 'password';
  $('loginMsg').textContent = '';
}
function openLogin(then = null) {
  afterLogin = then; $('loginMsg').textContent = '';
  setLoginTab(loginTab);
  if (!$('loginDialog').open) $('loginDialog').showModal();
  (loginTab === 'email' ? $('loginEmail') : $('loginUser')).focus();
}
// Uploading needs an account so the work shows up in 我的记录.
function requireLogin(then) {
  if (currentUser) return true;
  openLogin(then); return false;
}
function startCountdown(button, seconds = 60) {
  clearInterval(button._timer);
  let left = seconds; button.disabled = true; button.textContent = `${left} 秒后重发`;
  button._timer = setInterval(() => {
    left -= 1;
    if (left <= 0) { clearInterval(button._timer); button.disabled = false; button.textContent = '发送验证码'; }
    else button.textContent = `${left} 秒后重发`;
  }, 1000);
}
async function sendEmailCode(url, emailInput, button, messageNode) {
  const email = emailInput.value.trim();
  if (!email) { messageNode.textContent = '请先输入邮箱。'; return; }
  button.disabled = true; messageNode.textContent = '';
  try {
    await request(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({email})});
    startCountdown(button);
    messageNode.textContent = '验证码已发送，请查收邮件（可能在垃圾箱里）。';
  } catch (error) { messageNode.textContent = error.message; button.disabled = false; }
}
$('loginOpen').onclick = () => openLogin();
$('loginCancel').onclick = () => { afterLogin = null; $('loginDialog').close(); };
for (const button of document.querySelectorAll('[data-login-tab]')) button.addEventListener('click', () => setLoginTab(button.dataset.loginTab));
$('emailCodeSend').addEventListener('click', () => sendEmailCode('/api/account/email/send', $('loginEmail'), $('emailCodeSend'), $('loginMsg')));
$('loginForm').addEventListener('submit', async event => {
  event.preventDefault();
  const buttons = ['emailLoginSubmit','loginSubmit','registerSubmit'];
  for (const id of buttons) $(id).disabled = true;
  $('loginMsg').textContent = '';
  try {
    let result;
    if (loginTab === 'email') {
      result = await request('/api/account/email/login', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({email:$('loginEmail').value.trim(), code:$('loginCode').value.trim()})});
    } else {
      const mode = event.submitter?.value === 'register' ? 'register' : 'login';
      result = await request('/api/account/' + mode, {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({username:$('loginUser').value.trim(), password:$('loginPass').value})});
    }
    currentUser = result.user; renderAccount();
    $('loginPass').value = ''; $('loginCode').value = ''; $('loginDialog').close();
    loadPoints().catch(() => {});
    const then = afterLogin; afterLogin = null;
    if (then) then();
  } catch (error) { $('loginMsg').textContent = error.message; }
  finally { for (const id of buttons) $(id).disabled = false; }
});

// ---- 绑定邮箱 ----
$('bindEmailBtn').onclick = () => { $('bindMsg').textContent = ''; $('bindDialog').showModal(); $('bindEmail').focus(); };
$('bindCancel').onclick = () => $('bindDialog').close();
$('bindSend').addEventListener('click', () => sendEmailCode('/api/account/email/bind/send', $('bindEmail'), $('bindSend'), $('bindMsg')));
$('bindForm').addEventListener('submit', async event => {
  event.preventDefault(); $('bindSubmit').disabled = true; $('bindMsg').textContent = '';
  try {
    const result = await request('/api/account/email/bind/verify', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({email:$('bindEmail').value.trim(), code:$('bindCode').value.trim()})});
    currentUser.email = result.email; renderAccount(); $('bindCode').value = ''; $('bindDialog').close();
  } catch (error) { $('bindMsg').textContent = error.message; }
  finally { $('bindSubmit').disabled = false; }
});

// ---- 积分 ----
const ledgerLabels = {topup:'充值', spend:'消费', signup_bonus:'注册赠送', admin:'管理员调整', refund:'退还'};
let topupTimer = null;
async function loadPoints() {
  if (!currentUser) { pointsInfo = null; updatePointButtons(); return null; }
  pointsInfo = await request('/api/points');
  currentUser.points = pointsInfo.balance; renderAccount();
  return pointsInfo;
}
function pointsMessage(text, error = false) { $('pointsMsg').textContent = text; $('pointsMsg').className = 'msg' + (error ? ' error' : ''); }
function renderPointsDialog() {
  if (!pointsInfo) return;
  $('pointsBalance').textContent = fmtPoints(pointsInfo.balance);
  const box = $('pointsPackages'); box.replaceChildren();
  for (const pack of pointsInfo.enabled ? pointsInfo.packages : []) {
    const button = document.createElement('button'); button.type = 'button';
    button.textContent = `¥${pack.yuan} → ${fmtPoints(pack.points)} 积分`;
    button.disabled = !pointsInfo.alipay_enabled;
    button.addEventListener('click', () => startTopup(pack.yuan));
    box.append(button);
  }
  const list = $('pointsLedger'); list.replaceChildren();
  if (!pointsInfo.ledger.length) { const li = document.createElement('li'); li.textContent = '暂无记录'; list.append(li); }
  for (const row of pointsInfo.ledger) {
    const li = document.createElement('li');
    const left = document.createElement('span'); left.textContent = `${ledgerLabels[row.kind] || row.kind} · ${new Date(row.created_at * 1000).toLocaleString()}`;
    const right = document.createElement('span'); right.className = row.delta > 0 ? 'plus' : 'minus';
    right.textContent = (row.delta > 0 ? '+' : '') + fmtPoints(row.delta);
    li.append(left, right); list.append(li);
  }
}
async function openPoints(note = '') {
  if (!requireLogin(() => openPoints(note))) return;
  if (!$('pointsDialog').open) $('pointsDialog').showModal();
  $('pointsCheckout').hidden = true; pointsMessage(note, !!note);
  try {
    await loadPoints(); renderPointsDialog();
    if (!note && !pointsInfo.alipay_enabled) pointsMessage('支付宝支付暂未开放，暂时无法充值；已有积分仍可正常使用。', true);
  } catch (error) { pointsMessage(error.message, true); }
}
async function startTopup(yuan) {
  const win = openPayWindow();
  pointsMessage('正在打开支付宝…');
  try {
    const order = await request('/api/points/topup', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({yuan})});
    const opened = sendToCheckout(win, order.checkout_url, $('pointsCheckout'));
    pointsMessage(opened ? '已打开支付宝收银台，付款后积分会自动到账，请不要关闭本页。' : '浏览器拦截了付款页，请点下面的链接打开支付宝。');
    watchTopup(order.order_id);
  } catch (error) { closePayWindow(win); pointsMessage(error.message, true); }
}
function watchTopup(orderId, tries = 0) {
  clearTimeout(topupTimer);
  topupTimer = setTimeout(async () => {
    try {
      const state = await request(`/api/points/topup/${encodeURIComponent(orderId)}`);
      if (state.paid) {
        await loadPoints(); renderPointsDialog(); $('pointsCheckout').hidden = true;
        pointsMessage('充值成功，积分已到账。'); return;
      }
      if (state.expired) { $('pointsCheckout').hidden = true; pointsMessage('付款页已超时，请重新选择档位。', true); return; }
    } catch {}
    if (tries < 400) watchTopup(orderId, tries + 1);
  }, 3000);
}
$('pointsBtn').onclick = () => openPoints();
$('pointsClose').onclick = () => { clearTimeout(topupTimer); $('pointsDialog').close(); };
// 积分价与人民币价一致，不取整：99 分 = 9.9 积分
function pointsFor(cents) { return cents / 10; }
function updatePointButtons() {
  const usable = !!currentUser && pointsInfo?.enabled !== false;
  const tw = $('twPayPoints'), pts = twPointsPrice();
  tw.hidden = !usable || pts == null;
  if (!tw.hidden) tw.textContent = `积分支付 ${fmtPoints(pts)} 积分`;
  const fmt = $('createPointsPayment'), exportPts = pointsInfo?.prices?.format_export;
  fmt.hidden = !usable || !paymentJob || !!paymentOrder || exportPts == null;
  if (!fmt.hidden) fmt.textContent = `积分支付 ${fmtPoints(exportPts)} 积分`;
}
$('logout').onclick = async () => {
  await request('/api/account/logout', {method:'POST'}).catch(() => {});
  // The next person on this computer must not see this account's orders.
  for (const key of ['twOrders','thesisJob','thesisPaymentOrder','thesisDraft']) sessionStorage.removeItem(key);
  location.reload();
};

// ---- 我的记录 ----
const historyServices = {detect_text:'AIGC 检测 · 文本', detect_file:'AIGC 检测 · 文件', paraphrase_stream:'降重 / 降 AI · 文本', document_async:'降重 / 降 AI · Word'};
const historyStates = {running:'排版中', done:'已完成', error:'排版失败', expired:'已过期，文件已删除',
  ready:'待处理', processing:'处理中', submitted:'已提交', complete:'已完成', failed:'异常，请联系客服'};
async function loadHistory() {
  if (!requireLogin(loadHistory)) { $('historyList').replaceChildren(); $('historyMsg').textContent = '登录后查看记录。'; return; }
  $('historyMsg').textContent = '正在读取…';
  try {
    const {items} = await request('/api/account/history');
    $('historyMsg').textContent = '';
    $('historyList').replaceChildren(...(items.length ? items.map(historyRow) : [historyEmpty()]));
  } catch (error) { $('historyMsg').textContent = error.message; }
}
function historyEmpty() {
  const li = document.createElement('li'); li.className = 'h-empty'; li.textContent = '还没有记录。付款后的排版、降重和检测会出现在这里。'; return li;
}
const historyTime = seconds => new Date(seconds * 1000).toLocaleString('zh-CN', {hour12:false, month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'});
function historyRow(item) {
  // Only paid work is listed; files and results are deleted 24 hours after payment.
  const li = document.createElement('li'), main = document.createElement('div'), title = document.createElement('b'), info = document.createElement('small');
  main.className = 'h-main';
  title.textContent = item.name;
  const service = item.kind === 'format' ? '论文排版' + (item.detail ? ' · ' + item.detail : '') : historyServices[item.detail] || '降重 / 检测';
  const expired = item.state === 'expired';
  info.textContent = `${service} · 付款 ${historyTime(item.paid_at)}` + (expired ? '' : ` · ${historyTime(item.expires_at)} 前可下载`);
  main.append(title, info);
  const tag = document.createElement('span'); tag.className = 'h-tag'; tag.textContent = historyStates[item.state] || item.state;
  if (!expired) tag.classList.add('paid');
  li.append(main, tag);
  const button = (text, onclick) => { const b = document.createElement('button'); b.type = 'button'; b.className = 'secondary'; b.textContent = text; b.onclick = onclick; return b; };
  if (item.kind === 'format' && ['running','done'].includes(item.state)) li.append(button('查看 / 下载', () => openFormatJob(item)));
  if (item.kind === 'tabword' && !expired) li.append(button('查看结果', () => {
    const order = twOrders.find(o => o.order_id === item.order_id) || {quote_id:item.ref, order_id:item.order_id, operation:item.detail, paid:true};
    if (!twOrders.includes(order)) twOrders.push(order);
    twRunPaid(order);
  }));
  return li;
}
function openFormatJob(item) {
  showWorkspace('format');
  clearTimeout(timer); clearResult(true);
  job = item.ref; sessionStorage.setItem('thesisJob', job);
  if (item.order_id) sessionStorage.setItem('thesisPaymentOrder', JSON.stringify({job, orderId:item.order_id}));
  $('title').value = item.name;
  busy = true; controls(); message('正在打开记录…'); poll();
  $('result').scrollIntoView({behavior:'smooth', block:'center'});
}
$('historyRefresh').onclick = loadHistory;

{
  const returned = (() => { try { return JSON.parse(sessionStorage.getItem('twReturned') || 'null'); } catch { return null; } })();
  sessionStorage.removeItem('twReturned');
  const open = twOrders.filter(twOpen);
  if (open.length && !sessionStorage.getItem('thesisJob')) {
    showWorkspace(open[open.length - 1].operation.startsWith('detect') ? 'detect' : 'rewrite');
  } else if (returned && !sessionStorage.getItem('thesisJob')) {
    showWorkspace('history');
    $('historyMsg').textContent = `订单 ${returned.order_id} 已从支付宝返回。付款确认后，下单的那个页面会自动开始处理；处理完的结果也可以在下面打开。`;
  } else showWorkspace('format');
}
request('/api/account/me').then(data => { currentUser = data.user; renderAccount(); return currentUser && loadPoints(); }).then(() => aiRestore()).catch(() => renderAccount());


// ---- 识别结果预览 + AI 助手 ----
// 规则先识别（免费），用户直接改；AI 只给「待确认的修改」，按实际 token 扣积分。
const AI_OPTIONS = [['h1','一级标题'],['h2','二级标题'],['h3','三级标题'],['h4','四级标题'],['body','正文'],['fig','图题'],['tab','表题']];
const AI_EDITABLE = ['heading','candidate','note','figure_caption','table_caption'];
const AI_FLAG_TEXT = {no_caption:'没有识别到题注', stray_caption:'像题注，但没被识别', bold_guess:'按加粗短行识别', demoted:'超出四级，按加粗正文'};
const aiEl = (tag, className, text) => { const n = document.createElement(tag); if (className) n.className = className; if (text != null) n.textContent = text; return n; };
const aiValueOf = row => row.kind === 'heading' ? 'h' + row.level : row.kind === 'figure_caption' ? 'fig' : row.kind === 'table_caption' ? 'tab' : 'body';
const aiRoleFor = value => value.startsWith('h') ? {role:'heading', level:Number(value[1])}
  : {role: value === 'fig' ? 'figure_caption' : value === 'tab' ? 'table_caption' : 'body'};
function aiOverrides() {
  return [...aiState.edits.values()].map(edit => {
    const out = {...edit};
    if (out.text !== undefined && !out.role) {            // 只改了文字：把原来的身份一起写上，免得改完文字就不再被认成标题/题注
      const base = aiState.base.get(out.idx);
      if (base) Object.assign(out, aiRoleFor(aiValueOf(base)));
      if (out.role === 'body') delete out.role;
    }
    return out;
  });
}
function aiSave() {
  try { sessionStorage.setItem('thesisAnalysis', JSON.stringify({id:aiState.id, edits:[...aiState.edits.values()]})); } catch {}
}
function aiStatus(text, error = false) { $('aiStatus').textContent = text; $('aiStatus').className = 'msg' + (error ? ' error' : ''); }
function aiExpired() {
  aiClose(); message('识别结果已过期，请点「检查识别结果」重新检查。', 'error');
}
function aiClose(keepStorage = false) {
  aiState.id = null; aiState.base = new Map(); aiState.rows = []; aiState.edits = new Map(); aiState.suggestions = new Map();
  aiState.similar = null; aiState.filter = 'issues'; aiState.confirmed = false; aiState.seq += 1;
  $('aiPanel').hidden = true; $('go').hidden = false; $('checkStructure').hidden = false;
  $('aiLog').replaceChildren(); $('aiMsg').value = '';
  if (!keepStorage) { try { sessionStorage.removeItem('thesisAnalysis'); } catch {} }
  controls();
}
function aiLoad(data) {
  aiState.id = data.analysis_id; aiState.rows = data.rows; aiState.info = data.ai; aiState.notes = data.notes || [];
  aiState.truncated = !!data.truncated; aiState.summary = data.summary;
  if (data.rejected?.length) aiStatus('有 ' + data.rejected.length + ' 处修改无效，已忽略：' + data.rejected[0], true);
}
async function aiOpen() {
  if (!selectedFile || !/\.docx$/i.test(selectedFile.name) || aiState.busy || !requireLogin(aiOpen)) return;
  aiState.busy = true; controls(); message('正在识别标题、图题和表题…');
  try {
    const body = new FormData(); body.append('file', selectedFile);
    const data = await request('/api/format/analyze', {method:'POST', body});
    aiClose(); aiLoad(data);
    aiState.base = new Map(data.rows.map(row => [row.idx, row]));
    $('aiPanel').hidden = false; $('go').hidden = true; $('checkStructure').hidden = true;
    message(''); aiStatus(''); aiRender(); aiSave();
    $('aiPanel').scrollIntoView({behavior:'smooth', block:'start'});
  } catch (error) { message(error.message, 'error'); }
  finally { aiState.busy = false; controls(); }
}
async function aiRestore() {
  let saved = null;
  try { saved = JSON.parse(sessionStorage.getItem('thesisAnalysis') || 'null'); } catch {}
  if (!saved?.id || sessionStorage.getItem('thesisJob') || !currentUser) return;
  try {
    const data = await request('/api/format/analyze/' + encodeURIComponent(saved.id));
    aiLoad(data);
    aiState.base = new Map(data.rows.map(row => [row.idx, row]));
    aiState.edits = new Map((saved.edits || []).map(edit => [edit.idx, edit]));
    $('aiPanel').hidden = false; $('go').hidden = true; $('checkStructure').hidden = true;
    aiStatus('已恢复上次的识别结果。'); await aiRefreshNow();
  } catch { try { sessionStorage.removeItem('thesisAnalysis'); } catch {} }
}
let aiTimer = null;
function aiRefresh(immediate = false) {
  // 先按本地修改立刻刷新（改动数、下拉框），不让用户干等服务器；连续编辑合并成一次请求
  clearTimeout(aiTimer);
  aiRender();
  aiTimer = setTimeout(aiRefreshNow, immediate ? 0 : 350);
}
async function aiRefreshNow() {
  // 把用户的修改交给服务器重新识别一遍，看到的才是「实际会变成什么」（越级标题被压缩、没图的题注被当成正文…）
  const seq = ++aiState.seq;
  aiStatus('正在按你的修改重新识别…');
  try {
    const data = await request('/api/format/analyze/' + encodeURIComponent(aiState.id) + '/preview',
      {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({overrides:aiOverrides()})});
    if (seq !== aiState.seq) return;
    aiStatus('');
    aiLoad(data);
  } catch (error) {
    if (error.status === 404) { aiExpired(); return; }
    aiStatus(error.message, true);
  }
  if (seq === aiState.seq) aiRender();
}
function aiCommit(edit) {
  if (!edit.role && edit.text === undefined) aiState.edits.delete(edit.idx); else aiState.edits.set(edit.idx, edit);
  aiState.suggestions.delete(edit.idx);
  aiSave(); aiRefresh();
}
function aiSetRole(idx, value, refresh = true) {
  const base = aiState.base.get(idx);
  const edit = {...(aiState.edits.get(idx) || {idx})};
  delete edit.role; delete edit.level;
  if (base && value !== aiValueOf(base)) Object.assign(edit, aiRoleFor(value));
  if (!edit.role && edit.text === undefined) aiState.edits.delete(idx); else aiState.edits.set(idx, edit);
  aiState.suggestions.delete(idx);
  if (refresh) { aiSave(); aiRefresh(); }
}
function aiSetText(idx, text) {
  const base = aiState.base.get(idx), clean = text.replace(/\s+/g, ' ').trim();
  if (!clean) { aiStatus('文字不能为空。', true); aiRender(); return; }
  const edit = {...(aiState.edits.get(idx) || {idx})};
  if (clean === base?.text) delete edit.text; else edit.text = clean;
  aiCommit(edit);
}
function aiRowFlags(row) {
  const out = [];
  if (row.flags?.includes('clamped')) out.push(['缺上级，已按 ' + row.level + ' 级处理（原稿 ' + row.clamped_from + ' 级）']);
  for (const flag of row.flags || []) if (AI_FLAG_TEXT[flag]) out.push([AI_FLAG_TEXT[flag]]);
  const base = aiState.base.get(row.idx);
  if (row.manual && base && aiState.edits.get(row.idx)?.text !== undefined) out.push(['原：' + base.text.slice(0, 30), 'orig']);
  return out;
}
function aiIsIssue(row) { return (row.flags || []).some(flag => flag !== 'manual') || aiState.suggestions.has(row.idx); }
function aiVisible(row) {
  switch (aiState.filter) {
    case 'issues': return aiIsIssue(row) || !!row.manual;
    case 'headings': return row.kind === 'heading';
    case 'figs': return row.kind === 'figure' || row.kind === 'figure_caption';
    case 'tabs': return row.kind === 'table' || row.kind === 'table_caption';
    case 'candidates': return row.kind === 'candidate';
    default: return true;
  }
}
function aiRenderStats() {
  const rows = aiState.rows, box = $('aiStats'); box.replaceChildren();
  const issues = rows.filter(aiIsIssue).length;
  const chips = [['issues', '待确认 ' + issues, issues ? 'warn' : ''], ['all', '全部'],
    ['headings', '标题 ' + rows.filter(r => r.kind === 'heading').length],
    ['figs', '图 ' + rows.filter(r => r.kind === 'figure').length], ['tabs', '表 ' + rows.filter(r => r.kind === 'table').length],
    ['candidates', '标题疑似行 ' + rows.filter(r => r.kind === 'candidate').length]];
  for (const [key, label, extra] of chips) {
    const b = aiEl('button', (aiState.filter === key ? 'active ' : '') + (extra || ''), label); b.type = 'button';
    if (key === 'candidates') b.title = '没被识别成标题、但长得像标题的短行（比如没有编号、没用标题样式的小标题），可能是漏识别的标题，可以在这里挑出来设成标题。';
    if (key === 'issues') b.title = '规则识别时发现可能有问题的地方（缺上级、没有题注、像题注但没被识别等）和 AI 的建议。';
    b.addEventListener('click', () => { aiState.filter = key; aiRender(); });
    box.append(b);
  }
}
function aiRenderNumbering() {
  const list = $('aiNumbering'); list.replaceChildren();
  const counts = [0, 0, 0]; let sub = 0;
  for (const row of aiState.rows) {
    if (row.kind !== 'heading') continue;
    let label;
    if (row.level <= 3) { counts[row.level - 1] += 1; for (let i = row.level; i < 3; i++) counts[i] = 0; sub = 0; label = counts.slice(0, row.level).join('.'); }
    else { sub += 1; label = '（' + sub + '）'; }
    const li = aiEl('li', '', label + '　' + row.text); li.style.paddingLeft = Math.min(row.level - 1, 3) * 14 + 'px';
    list.append(li);
  }
  if (!list.children.length) list.append(aiEl('li', '', '还没有识别到标题'));
}
function aiBuildRow(row) {
  const edit = aiState.edits.get(row.idx), suggestion = aiState.suggestions.get(row.idx);
  const plain = row.kind === 'candidate' || row.kind === 'note';
  const el = aiEl('div', 'ai-row' + (plain ? ' candidate' : '') + (edit ? ' manual' : '')
    + (aiState.similar?.set.has(row.idx) ? ' similar' : '') + (suggestion ? ' suggest' : ''));
  el.setAttribute('role', 'listitem'); el.dataset.idx = row.idx;
  const select = document.createElement('select'); select.setAttribute('aria-label', '这一行的类型');
  for (const [value, label] of AI_OPTIONS) select.append(new Option(label, value));
  const pending = edit?.role ? (edit.role === 'heading' ? 'h' + edit.level : edit.role === 'figure_caption' ? 'fig' : edit.role === 'table_caption' ? 'tab' : 'body') : null;
  select.value = pending || aiValueOf(row);
  if (plain && !pending) select.options[select.selectedIndex].text = '设为…';
  select.addEventListener('change', () => aiSetRole(row.idx, select.value));
  el.append(select);
  const cell = aiEl('div'); cell.style.cssText = 'display:flex;align-items:center;min-width:0';
  if (row.num) cell.append(aiEl('span', 'num', row.num));
  if (row.kind === 'note') cell.append(aiEl('span', 'num', '表注'));
  if (plain) cell.append(aiEl('span', '', row.text));
  else {
    const input = document.createElement('input'); input.value = edit?.text ?? row.text; input.maxLength = 200; input.style.flex = '1';
    input.setAttribute('aria-label', '文字');
    input.addEventListener('change', () => aiSetText(row.idx, input.value));
    cell.append(input);
  }
  el.append(cell);
  const tools = aiEl('div', 'tools');
  const similar = aiEl('button', '', '找同格式行'); similar.type = 'button'; similar.title = '按格式（样式、加粗、编号形式、字号）找出文档里和这一行格式相同的行，可以一次设成同一级别'; similar.addEventListener('click', () => aiFindSimilar(row.idx));
  tools.append(similar);
  if (edit) { const undo = aiEl('button', '', '撤销'); undo.type = 'button'; undo.addEventListener('click', () => { aiState.edits.delete(row.idx); aiSave(); aiRefresh(); }); tools.append(undo); }
  el.append(tools);
  const flags = aiRowFlags(row);
  if (flags.length) { const f = aiEl('div', 'ai-flags'); for (const [text, kind] of flags) f.append(aiEl('span', kind || '', text)); el.append(f); }
  if (suggestion) {
    const box = aiEl('div', 'ai-suggest');
    const target = suggestion.role === 'heading' ? AI_OPTIONS.find(o => o[0] === 'h' + suggestion.level)[1]
      : suggestion.role === 'figure_caption' ? '图题' : suggestion.role === 'table_caption' ? '表题' : '正文';
    box.append(aiEl('span', '', 'AI 建议设为「' + target + '」' + (suggestion.reason ? '：' + suggestion.reason : '')));
    const yes = aiEl('button', 'primary', '采纳'), no = aiEl('button', 'secondary', '忽略'); yes.type = no.type = 'button';
    yes.style.cssText = no.style.cssText = 'min-height:30px;padding:2px 12px;font-size:13px';
    yes.addEventListener('click', () => aiSetRole(row.idx, suggestion.role === 'heading' ? 'h' + suggestion.level : suggestion.role === 'figure_caption' ? 'fig' : suggestion.role === 'table_caption' ? 'tab' : 'body'));
    no.addEventListener('click', () => { aiState.suggestions.delete(row.idx); aiRender(); });
    box.append(yes, no); el.append(box);
  }
  return el;
}
function aiRenderList() {
  const list = $('aiList'), keep = list.scrollTop; list.replaceChildren();
  let body = null;
  const flush = () => { if (body && aiState.filter === 'all') list.append(aiEl('div', 'ai-row muted', '正文 ' + body.n + ' 段 · 共 ' + body.chars + ' 字')); body = null; };
  for (const row of aiState.rows) {
    if (row.kind === 'body') { body = body ? {n:body.n + 1, chars:body.chars + (row.chars || 0)} : {n:1, chars:row.chars || 0}; if (aiIsIssue(row) && aiState.filter === 'issues') { flush(); } continue; }
    flush();
    if (!aiVisible(row)) continue;
    if (AI_EDITABLE.includes(row.kind)) list.append(aiBuildRow(row));
    else if (row.kind === 'figure' || row.kind === 'table') {
      const warn = (row.flags || []).includes('no_caption');
      list.append(aiEl('div', 'ai-row muted', '［' + row.text + '］题注：' + (row.title || '没有识别到') + (warn ? '　⚠ 请把紧挨着它的题注行设为' + (row.kind === 'figure' ? '图题' : '表题') : '')));
    } else list.append(aiEl('div', 'ai-row muted', '——' + row.text + (row.zone ? '（以下不参与识别）' : '')));
  }
  flush();
  if (!list.children.length) list.append(aiEl('div', 'ai-row muted', aiState.filter === 'issues'
    ? '没有发现需要确认的行 ✓ 可以直接点下面的「确认并排版」；想自己看一遍，点上面的「全部」。' : '没有符合条件的行'));
  list.scrollTop = keep;
}
function aiRender() {
  if (!aiState.id) return;
  aiRenderStats(); aiRenderList(); aiRenderNumbering();
  const n = aiState.edits.size, issueCount = aiState.rows.filter(aiIsIssue).length;
  $('aiHint').textContent = issueCount
    ? '系统发现 ' + issueCount + ' 处可能需要你确认，已为你筛在下面；其余按规则识别的结果没有发现问题。想看完整列表点「全部」。'
    : '没有发现需要确认的地方，可以直接排版。想自己看一遍，点「全部」。';
  $('aiDirty').textContent = n ? '已修改 ' + n + ' 处' : '还没有修改';
  $('aiNotes').replaceChildren(...aiState.notes.slice(0, 4).map(text => aiEl('li', '', text)));
  if (aiState.truncated) $('aiNotes').append(aiEl('li', '', '文档较长，只显示前面一部分；没显示的部分仍按规则识别。'));
  const info = aiState.info || {};
  $('aiScan').disabled = aiState.busy || !info.available;
  $('aiScanFoot').disabled = $('aiScan').disabled;
  $('aiSend').disabled = aiState.busy || !info.available;
  $('aiPrice').textContent = info.available
    ? '按实际 token 用量计费：每 1000 token ' + fmtPoints(info.rate) + ' 积分，每次至少 ' + fmtPoints(info.min_charge) + ' 积分。每次先预留，用多少扣多少，出错全额退还。'
    : 'AI 助手暂未开放。你仍然可以手动修改。';
  $('aiBalance').textContent = currentUser ? '积分余额 ' + fmtPoints(currentUser.points ?? 0) : '';
  aiRenderSimilar();
}
function aiRenderSimilar() {
  const bar = $('aiSimilarBar'); bar.replaceChildren();
  if (!aiState.similar) { bar.hidden = true; return; }
  bar.hidden = false;
  bar.append(aiEl('span', '', '找到 ' + aiState.similar.set.size + ' 个和所选行格式相同的行（黄色高亮，只看格式，不看上下文）。把它们和所选行一起设为：'));
  const select = document.createElement('select'); select.style.minHeight = '32px';
  for (const [value, label] of AI_OPTIONS) select.append(new Option(label, value));
  select.value = 'h3';
  const apply = aiEl('button', 'primary', '应用'), cancel = aiEl('button', 'secondary', '取消'); apply.type = cancel.type = 'button';
  apply.style.cssText = cancel.style.cssText = 'min-height:32px;padding:2px 12px;font-size:13px';
  apply.addEventListener('click', () => {
    for (const idx of [aiState.similar.source, ...aiState.similar.set]) aiSetRole(idx, select.value, false);
    aiState.similar = null; aiSave(); aiRefresh();
  });
  cancel.addEventListener('click', () => { aiState.similar = null; aiRender(); });
  bar.append(select, apply, cancel);
}
async function aiFindSimilar(idx) {
  try {
    const data = await request('/api/format/analyze/' + encodeURIComponent(aiState.id) + '/similar',
      {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({idx})});
    if (!data.idx.length) { aiStatus('没有找到和这一行格式相同的行。', true); return; }
    aiState.filter = 'all'; aiState.similar = {source:idx, set:new Set(data.idx)}; aiStatus(''); aiRender();
  } catch (error) { if (error.status === 404) aiExpired(); else aiStatus(error.message, true); }
}
// ---- AI 助手 ----
function aiLog(kind, text, small = '') {
  const log = $('aiLog'), m = aiEl('div', 'm ' + kind, text);
  if (small) m.append(aiEl('small', '', small));
  log.append(m); log.scrollTop = log.scrollHeight;
}
function aiAskConfirm() {
  return new Promise(resolve => {
    $('aiConfirmReserve').textContent = fmtPoints(aiState.info?.reserve ?? 0);
    const dialog = $('aiConfirmDialog'); dialog.returnValue = '';
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), {once:true});
    dialog.showModal();
  });
}
async function aiCall(path, payload, shown) {
  if (aiState.busy || !aiState.id) return;
  if (!aiState.confirmed) { if (!await aiAskConfirm()) return; aiState.confirmed = true; }
  aiState.busy = true; aiRender(); controls(); aiStatus('AI 正在分析，请稍候…');
  aiLog('u', shown);
  try {
    const data = await request('/api/format/analyze/' + encodeURIComponent(aiState.id) + path,
      {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...payload, overrides:aiOverrides()})});
    for (const change of data.changes) aiState.suggestions.set(change.idx, change);
    aiLog('a', data.reply || (data.changes.length ? '我找到了几处可能认错的地方，已在左侧标出。' : '没有发现明显问题（这不代表一定没问题，你可以用「找同格式行」或直接手动修改）。'),
      '本次 ' + data.usage.total.toLocaleString() + ' tokens · 扣 ' + fmtPoints(data.charged) + ' 积分 · 本文档累计 ' + fmtPoints(data.total_charged)
      + ' 积分 · 还可调用 ' + data.calls_left + ' 次');
    if (currentUser) { currentUser.points = data.balance; renderAccount(); }
    if (aiState.info) { aiState.info.calls_left = data.calls_left; }
    aiStatus(data.changes.length ? 'AI 给出 ' + data.changes.length + ' 条建议，请逐条采纳或忽略。' : '');
    if (data.truncated) aiLog('e', '文档较长，AI 只看了前面一部分。');
  } catch (error) {
    if (error.status === 404) { aiExpired(); return; }
    aiLog('e', error.message);
    if (error.status === 402) openPoints(error.message);
    aiStatus('');
  } finally { aiState.busy = false; controls(); if (aiState.id) aiRender(); }
}
$('checkStructure').addEventListener('click', aiOpen);
$('aiCancel').addEventListener('click', () => aiClose());
$('aiReset').addEventListener('click', () => { aiState.edits = new Map(); aiState.suggestions = new Map(); aiState.similar = null; aiSave(); aiStatus(''); aiRefresh(); });
$('aiConfirm').addEventListener('click', () => {
  if (!formReady()) { aiStatus('请先在上面填好题目、作者并选择排版规范。', true); return; }
  submit({analysisId:aiState.id, overrides:aiOverrides()});
});
$('aiScanFoot').addEventListener('click', () => $('aiScan').click());
$('aiScan').addEventListener('click', () => aiCall('/ai', {}, '请扫描整份结构，找出可能识别错的地方。'));
$('aiSend').addEventListener('click', () => {
  const text = $('aiMsg').value.trim();
  if (!text) return;
  $('aiMsg').value = '';
  aiCall('/chat', {message:text}, text);
});
$('aiConfirmNo').addEventListener('click', () => $('aiConfirmDialog').close('no'));
$('aiMsg').addEventListener('keydown', event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) $('aiSend').click(); });
