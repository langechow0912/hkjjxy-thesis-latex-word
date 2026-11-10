// DSH 调试页。所有来自论文或模型的文字一律用 textContent 写入，不拼 HTML。
const $ = id => document.getElementById(id);
const state = {jobId: '', outline: null, history: [], busy: false, quota: null};

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof data.detail === 'string' && data.detail ? data.detail : '请求失败（' + response.status + '）');
    error.status = response.status;
    throw error;
  }
  return data;
}
const post = (url, body) => api(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
};

function setDebug(info) { $('debug').textContent = JSON.stringify(info, null, 2); }
function renderQuota() {
  const q = state.quota;
  $('quota').textContent = q ? `今日已用 ${q.user_used}/${q.user_limit} 次 · 全站剩余 ${q.daily_remaining}` : '';
}
function updateSend() {
  const text = $('msg').value.trim();
  $('count').textContent = `${$('msg').value.length}/500`;
  $('send').disabled = state.busy || !state.jobId || !text;
}

function renderTree() {
  const box = $('tree');
  box.replaceChildren();
  const data = state.outline;
  if (!data) { box.append(el('p', 'muted', '请先选择任务。')); return; }
  const showBody = $('showBody').checked;
  let heads = 0, pendingBody = 0;
  const flush = () => {
    if (pendingBody && showBody) box.append(el('div', 'row body', `　［正文 ${pendingBody} 字］`));
    pendingBody = 0;
  };
  for (const row of data.rows) {
    if (row.kind === 'body') { pendingBody += row.chars; continue; }
    flush();
    if (row.kind === 'table') { if (showBody) box.append(el('div', 'row table', '　［表格］')); continue; }
    heads += 1;
    const line = el('div', 'row');
    line.style.paddingLeft = Math.max(0, (row.level || 1) - 1) * 18 + 'px';
    line.append(el('span', 'lv', row.level ? 'H' + row.level : '？'));
    if (row.num) line.append(el('span', 'num', row.num));
    line.append(el('span', '', row.text));
    if (row.word_level !== row.level) line.append(el('span', 'diff', row.word_level ? `Word 原为 H${row.word_level}` : 'Word 无大纲级别'));
    box.append(line);
  }
  flush();
  $('treeInfo').textContent = `${heads} 个标题` + (data.truncated ? '（过长，已截断）' : '') + (data.suspicious ? ' · 规则层标记有存疑处' : '');
  $('modelInput').textContent = data.model_input.join('\n');
  $('wordOutline').textContent = data.word_outline.length ? data.word_outline.join('\n') : '（原稿没有 Word 标题样式）';
}

async function loadOutline(jobId) {
  state.jobId = jobId; state.outline = null; state.history = [];
  $('log').replaceChildren(); $('runOut').replaceChildren(); renderTree(); updateSend();
  if (!jobId) return;
  try {
    state.outline = await api('/api/dsh/outline?job_id=' + encodeURIComponent(jobId));
    $('pickMsg').textContent = '';
  } catch (error) { $('pickMsg').textContent = error.message; }
  renderTree(); updateSend();
}

function renderSkills(skills) {
  const body = $('skills');
  body.replaceChildren();
  for (const skill of skills) {
    const row = document.createElement('tr');
    const name = el('td');
    name.append(el('strong', '', skill.name), el('div', 'muted', skill.description + (skill.version ? ' · v' + skill.version : '')));
    const status = el('td'); status.append(el('span', 'tag' + (skill.enabled ? '' : ' off'), skill.enabled ? '已启用' : '未启用'));
    const action = el('td');
    if (skill.enabled) {
      const button = el('button', 'btn', '运行');
      button.type = 'button';
      button.addEventListener('click', () => runSkill(skill, button));
      action.append(button);
    }
    row.append(name, status, action);
    body.append(row);
  }
}

async function runSkill(skill, button) {
  if (!state.jobId) { $('pickMsg').textContent = '请先选择任务。'; return; }
  button.disabled = true; $('runOut').replaceChildren(el('p', 'muted', '运行中…'));
  try {
    const result = await post('/api/dsh/run', {job_id: state.jobId, checkpoint: skill.checkpoint});
    state.quota = result.quota; renderQuota(); setDebug(result);
    const out = $('runOut'); out.replaceChildren();
    if (!result.success) { out.append(el('p', 'muted', '没有得到可用的建议（' + (result.reason || '未知原因') + '）。')); return; }
    out.append(el('p', '', result.summary || `共 ${result.suggestions.length} 条建议`));
    for (const item of result.suggestions) {
      const box = el('div', 'sug ' + item.severity);
      box.append(el('div', '', item.message), el('div', 'muted', item.suggestion));
      out.append(box);
    }
    for (const note of result.warnings) out.append(el('p', 'muted', note));
  } catch (error) {
    $('runOut').replaceChildren(el('p', 'muted', error.message));
  } finally { button.disabled = false; }
}

function addMessage(kind, text) {
  const log = $('log');
  log.append(el('div', 'msg ' + kind, text));
  log.scrollTop = log.scrollHeight;
}

async function send() {
  const text = $('msg').value.trim();
  if (!text || state.busy || !state.jobId) return;
  state.busy = true; updateSend();
  addMessage('user', text); $('msg').value = '';
  try {
    const result = await post('/api/dsh/chat', {job_id: state.jobId, message: text, history: state.history});
    state.history.push({role: 'user', content: text}, {role: 'assistant', content: result.reply});
    state.history = state.history.slice(-12);
    state.quota = result.quota; renderQuota();
    addMessage('bot', result.reply || '（空回复）');
    setDebug({tokens: result.tokens, ...result.debug, quota: result.quota});
  } catch (error) { addMessage('err', error.message); }
  finally { state.busy = false; updateSend(); }
}

async function init() {
  let info;
  try { info = await api('/api/dsh/skills'); }
  catch (error) {
    $('pickMsg').textContent = error.status === 401 ? '请先回首页登录，再打开本页。' : error.message;
    $('job').replaceChildren(new Option('无法使用', ''));
    return;
  }
  state.quota = info.quota; renderQuota(); renderSkills(info.skills);
  if (!info.available) $('pickMsg').textContent = 'DSH 当前未启用或缺少 DeepSeek Key：目录仍可查看，对话和运行不可用。';
  const {jobs} = await api('/api/dsh/jobs');
  const select = $('job');
  select.replaceChildren(new Option(jobs.length ? '请选择…' : '还没有可查看的排版任务', ''));
  for (const job of jobs) select.append(new Option(job.name + ' · ' + new Date(job.created_at * 1000).toLocaleString(), job.job_id));
  select.disabled = !jobs.length;
  const wanted = new URLSearchParams(location.search).get('job');
  if (wanted && jobs.some(job => job.job_id === wanted)) { select.value = wanted; loadOutline(wanted); }
}

$('job').addEventListener('change', event => loadOutline(event.target.value));
$('showBody').addEventListener('change', renderTree);
$('msg').addEventListener('input', updateSend);
$('msg').addEventListener('keydown', event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) send(); });
$('send').addEventListener('click', send);
init();
