const $ = (id) => document.getElementById(id);
let commissionStatus = "";
const ADMIN_TOKEN_KEY = "liangpin_admin_token";
let adminToken = sessionStorage.getItem(ADMIN_TOKEN_KEY) || "";

function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}

async function api(url, options = {}) {
  const headers = {"Content-Type": "application/json", ...(options.headers || {})};
  if (adminToken) headers.Authorization = `Bearer ${adminToken}`;
  const response = await fetch(url, {
    ...options,
    headers,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
  return data;
}

function setMessage(el, text, error = false) {
  el.textContent = text;
  el.classList.toggle("error", error);
}

function money(cents) {
  return `¥${(Number(cents || 0) / 100).toFixed(2)}`;
}

function statusLabel(status) {
  return ({pending: "待审核", approved: "已审核", paid: "已结算", reversed: "已撤销"})[status] || status;
}

async function loadAffiliates() {
  const body = $("affiliateRows");
  body.replaceChildren(node("tr", undefined));
  const rows = (await api("/api/admin/affiliates")).affiliates;
  body.replaceChildren();
  if (!rows.length) {
    const tr = node("tr"); const td = node("td", "暂无推广员", "empty"); td.colSpan = 8; tr.append(td); body.append(tr);
    return;
  }
  for (const affiliate of rows) {
    const tr = node("tr");
    const identity = node("td"); identity.append(node("strong", affiliate.name), node("br"), node("small", affiliate.email || ""));
    const linkCell = node("td");
    const baseLink = `${location.origin}/r/${encodeURIComponent(affiliate.code)}`;
    const anchor = node("a", baseLink); anchor.href = baseLink; anchor.target = "_blank"; anchor.rel = "noreferrer";
    const campaign = node("input"); campaign.maxLength = 100; campaign.placeholder = "活动名称（可选）"; campaign.setAttribute("aria-label", "活动名称");
    campaign.addEventListener("input", () => {
      const url = new URL(baseLink);
      if (campaign.value.trim()) url.searchParams.set("campaign", campaign.value.trim());
      anchor.href = url.toString(); anchor.textContent = url.toString();
    });
    linkCell.append(anchor, document.createElement("br"), campaign);
    const rateCell = node("td"); const rate = node("input"); rate.type = "number"; rate.min = "0"; rate.max = "100"; rate.step = "0.01"; rate.value = affiliate.commission_rate;
    rateCell.append(rate);
    tr.append(identity, linkCell, rateCell, node("td", String(affiliate.click_count)),
      node("td", String(affiliate.commission_count)), node("td", money(affiliate.earned_cents)),
      node("td", affiliate.active ? "启用" : "停用", `badge${affiliate.active ? " paid" : " reversed"}`));
    const actionCell = node("td");
    const save = node("button", "保存比例", "btn"); save.type = "button";
    save.addEventListener("click", async () => {
      try { await api(`/api/admin/affiliates/${affiliate.id}`, {method: "PATCH", body: JSON.stringify({commission_rate: Number(rate.value)})}); await reload(); }
      catch (error) { alert(error.message); }
    });
    const toggle = node("button", affiliate.active ? "停用" : "启用", "btn"); toggle.type = "button";
    toggle.addEventListener("click", async () => {
      try { await api(`/api/admin/affiliates/${affiliate.id}`, {method: "PATCH", body: JSON.stringify({active: !affiliate.active})}); await reload(); }
      catch (error) { alert(error.message); }
    });
    const rotate = node("button", "重置访问令牌", "btn"); rotate.type = "button";
    rotate.addEventListener("click", async () => {
      try {
        const result = await api(`/api/admin/affiliates/${affiliate.id}/portal-token`, {method: "POST"});
        setMessage($("affiliateMessage"), `推广员 ${affiliate.code} 新访问令牌：${result.portal_token}`);
      } catch (error) { alert(error.message); }
    });
    actionCell.append(save, document.createTextNode(" "), toggle, document.createTextNode(" "), rotate); tr.append(actionCell); body.append(tr);
  }
}

async function loadCommissions() {
  const body = $("commissionRows");
  const query = commissionStatus ? `?status=${encodeURIComponent(commissionStatus)}` : "";
  const rows = (await api(`/api/admin/commissions${query}`)).commissions;
  body.replaceChildren();
  if (!rows.length) {
    const tr = node("tr"); const td = node("td", "暂无佣金记录", "empty"); td.colSpan = 9; tr.append(td); body.append(tr);
    return;
  }
  for (const commission of rows) {
    const tr = node("tr");
    tr.append(node("td", commission.order_no), node("td", `${commission.affiliate_name} · ${commission.affiliate_code}`),
      node("td", commission.campaign || "-"), node("td", money(commission.basis_cents)),
      node("td", `${(commission.commission_bps / 100).toFixed(2)}%`), node("td", money(commission.amount_cents)),
      node("td", statusLabel(commission.status), `badge ${commission.status}`), node("td", commission.payout_ref || "-"));
    const actions = node("td");
    if (commission.status === "pending") {
      actions.append(actionButton("审核通过", async () => updateCommission(commission.id, "approved")),
        document.createTextNode(" "), actionButton("撤销", async () => updateCommission(commission.id, "reversed")));
    } else if (commission.status === "approved") {
      actions.append(node("span", "可由推广员申请提现", "empty"));
    }
    tr.append(actions); body.append(tr);
  }
}

async function loadCoupons() {
  const body = $("couponRows");
  const rows = (await api("/api/admin/coupons")).coupons;
  body.replaceChildren();
  if (!rows.length) {
    const tr = node("tr"); const td = node("td", "暂无优惠券", "empty"); td.colSpan = 6; tr.append(td); body.append(tr);
    return;
  }
  for (const coupon of rows) {
    const tr = node("tr");
    const value = coupon.discount_kind === "percent" ? `优惠 ${coupon.discount_value}%` : `减 ¥${coupon.discount_value.toFixed(2)}`;
    tr.append(node("td", coupon.code), node("td", value), node("td", coupon.school_key || "全平台"),
      node("td", `${coupon.used_count} / ${coupon.max_uses ?? "不限"}`),
      node("td", coupon.active ? "启用" : "停用", `badge${coupon.active ? " paid" : " reversed"}`));
    const action = node("td"); action.append(actionButton(coupon.active ? "停用" : "启用", async () => {
      await api(`/api/admin/coupons/${coupon.id}`, {method:"PATCH", body:JSON.stringify({active:!coupon.active})});
      await loadCoupons();
    }));
    tr.append(action); body.append(tr);
  }
}

async function loadWithdrawals() {
  const body = $("withdrawalRows");
  const rows = (await api("/api/admin/withdrawals")).withdrawals;
  body.replaceChildren();
  if (!rows.length) {
    const tr = node("tr"); const td = node("td", "暂无提现申请", "empty"); td.colSpan = 8; tr.append(td); body.append(tr);
    return;
  }
  for (const withdrawal of rows) {
    const tr = node("tr");
    const proof = node("td");
    if (withdrawal.proof_path) { const a = node("a", "查看凭证"); a.href = `/api/admin/withdrawals/${withdrawal.id}/proof`; a.target = "_blank"; a.rel = "noreferrer"; proof.append(a); }
    else proof.textContent = "-";
    tr.append(node("td", `${withdrawal.affiliate_name} · ${withdrawal.affiliate_code}`),
      node("td", money(withdrawal.amount_cents)), node("td", withdrawal.payout_info),
      node("td", new Date(withdrawal.created_at * 1000).toLocaleString()),
      node("td", ({pending:"待审核",approved:"待转账",paid:"已结算",rejected:"已驳回"})[withdrawal.status], `badge ${withdrawal.status}`),
      node("td", withdrawal.payout_ref || "-"), proof);
    const action = node("td");
    if (withdrawal.status === "pending") {
      action.append(actionButton("审核通过", async () => {
        await api(`/api/admin/withdrawals/${withdrawal.id}`, {method:"PATCH",body:JSON.stringify({status:"approved"})}); await reload();
      }), document.createTextNode(" "), actionButton("驳回", async () => {
        await api(`/api/admin/withdrawals/${withdrawal.id}`, {method:"PATCH",body:JSON.stringify({status:"rejected"})}); await reload();
      }));
    } else if (withdrawal.status === "approved") {
      const ref = node("input"); ref.placeholder = "转账流水号"; ref.maxLength = 120; ref.setAttribute("aria-label", "转账流水号");
      const file = node("input"); file.type = "file"; file.accept = ".png,.jpg,.jpeg,.pdf"; file.setAttribute("aria-label", "转账凭证");
      const paid = node("button", "登记已转账", "btn"); paid.type = "button";
      paid.addEventListener("click", async () => {
        if (!ref.value.trim() || !file.files[0]) { alert("请填写流水号并选择凭证文件。"); return; }
        const form = new FormData(); form.append("payout_ref", ref.value.trim()); form.append("proof", file.files[0]);
        const response = await fetch(`/api/admin/withdrawals/${withdrawal.id}/paid`, {method:"POST",body:form});
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.detail || "登记失败");
        await reload();
      });
      action.append(ref, file, paid);
    }
    tr.append(action); body.append(tr);
  }
}

function actionButton(label, action) {
  const button = node("button", label, "btn"); button.type = "button";
  button.addEventListener("click", async () => { try { await action(); } catch (error) { alert(error.message); } });
  return button;
}

async function updateCommission(id, status, payoutRef = "") {
  try { await api(`/api/admin/commissions/${id}`, {method: "PATCH", body: JSON.stringify({status, payout_ref: payoutRef})}); await reload(); }
  catch (error) { alert(error.message); }
}

async function reload() {
  await Promise.all([loadAffiliates(), loadCommissions(), loadCoupons(), loadWithdrawals(), loadServicePrices(), loadServiceOrders()]);
}

async function loadServicePrices() {
  const prices = await api('/api/admin/service-prices');
  $('formatExportPrice').value = (prices.format_export_cents / 100).toFixed(2);
  $('detectTextPrice').value = (prices.detect_text_cents / 100).toFixed(2);
  $('detectFilePrice').value = (prices.detect_file_cents / 100).toFixed(2);
  $('rewritePrice').value = (prices.rewrite_per_1000_cents / 100).toFixed(2);
}
async function loadServiceOrders() {
  const result = await api('/api/admin/service-orders');
  const body = $('serviceOrderRows'); body.replaceChildren();
  for (const order of result.orders) {
    const row = node('tr');
    const service = ({detect_text:'文本检测',detect_file:'文件检测'})[order.operation] || '论文改写';
    const state = ({ready:'待提交',processing:'处理中',submitted:'已提交',complete:'已完成',failed:'异常，待核查'})[order.state] || order.state;
    row.append(node('td', order.out_trade_no || '未创建支付订单'), node('td',service), node('td',money(order.amount_cents)),
      node('td',String(order.characters)), node('td',order.payment_status === 'TRADE_SUCCESS' ? '已支付' : '未支付'), node('td',state));
    body.append(row);
  }
}
$('servicePriceForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = event.target.querySelector('button'); button.disabled = true;
  try {
    await api('/api/admin/service-prices', {method:'PUT', body:JSON.stringify({
      format_export_cents:Math.round(Number($('formatExportPrice').value) * 100),
      detect_text_cents:Math.round(Number($('detectTextPrice').value) * 100),
      detect_file_cents:Math.round(Number($('detectFilePrice').value) * 100),
      rewrite_per_1000_cents:Math.round(Number($('rewritePrice').value) * 100),
    })});
    setMessage($('servicePriceMessage'), '价格已保存，新报价立即生效。');
  } catch (error) { setMessage($('servicePriceMessage'), error.message, true); }
  finally { button.disabled = false; }
});

$("affiliateForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = $("affiliateMessage");
  try {
    const result = await api("/api/admin/affiliates", {method: "POST", body: JSON.stringify({
      name: $("name").value, email: $("email").value, code: $("code").value || null,
      commission_rate: Number($("rate").value),
    })});
    event.target.reset(); $("rate").value = "10";
    setMessage(message, `推广码：${result.code}；推广员入口：${location.origin}/affiliate；一次性访问令牌：${result.portal_token}`);
    await reload();
  } catch (error) { setMessage(message, error.message, true); }
});

$("refreshAffiliates").addEventListener("click", () => reload().catch((error) => alert(error.message)));
$("refreshWithdrawals").addEventListener("click", () => loadWithdrawals().catch((error) => alert(error.message)));
$("commissionFilters").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-status]");
  if (!button) return;
  commissionStatus = button.dataset.status;
  for (const item of $("commissionFilters").querySelectorAll("button")) item.setAttribute("aria-pressed", String(item === button));
  loadCommissions().catch((error) => alert(error.message));
});

$("couponForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const result = await api("/api/admin/coupons", {method:"POST", body:JSON.stringify({
      code:$("couponAdminCode").value, discount_kind:$("couponKind").value,
      discount_value:Number($("couponValue").value), school_key:$("couponSchool").value.trim() || null,
      max_uses:$("couponMaxUses").value ? Number($("couponMaxUses").value) : null,
    })});
    event.target.reset(); setMessage($("couponMessage"), `已创建 ${result.code}`); await loadCoupons();
  } catch (error) { setMessage($("couponMessage"), error.message, true); }
});

api("/api/schools").then((schools) => {
  const list = $("schoolKeys");
  for (const school of schools) {
    const option = node("option"); option.value = `school:${school.id}`; option.label = school.name; list.append(option);
  }
}).catch(() => {});

async function showDashboard() {
  $("adminAuth").hidden = true;
  $("adminContent").hidden = false;
  await reload();
}

async function initializeAdmin() {
  const status = await fetch("/api/admin/auth/status").then((response) => response.json());
  if (status.mode === "basic") {
    $("adminContent").hidden = false;
    await reload();
    return;
  }
  $("adminAuth").hidden = false;
  $("adminContent").hidden = true;
  $("adminRegisterForm").hidden = status.registered;
  $("adminLoginForm").hidden = !status.registered;
  $("authIntro").textContent = status.registered ? "请输入管理员访问令牌。" : "首次使用请注册管理员。注册仅允许从服务器本机进行。";
  if (adminToken) {
    const verify = await fetch("/api/admin/auth/verify", {method:"POST",headers:{Authorization:`Bearer ${adminToken}`}}).then((response) => response.json());
    if (verify.valid) { await showDashboard(); return; }
    sessionStorage.removeItem(ADMIN_TOKEN_KEY); adminToken = "";
  }
}

$("adminLoginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const candidate = $("adminToken").value.trim();
  const response = await fetch("/api/admin/auth/verify", {method:"POST",headers:{Authorization:`Bearer ${candidate}`}});
  const result = await response.json();
  if (!result.valid) { setMessage($("authMessage"), "令牌无效，请检查后重试。", true); return; }
  adminToken = candidate; sessionStorage.setItem(ADMIN_TOKEN_KEY, candidate);
  await showDashboard();
});

$("adminRegisterForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const response = await fetch("/api/admin/auth/register", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:$("adminName").value})});
  const result = await response.json();
  if (!response.ok) { setMessage($("authMessage"), result.detail || "注册失败。", true); return; }
  adminToken = result.access_token; sessionStorage.setItem(ADMIN_TOKEN_KEY, adminToken);
  $("adminRegisterForm").hidden = true; $("newTokenValue").textContent = adminToken; $("newToken").hidden = false;
});

$("continueAdmin").addEventListener("click", () => showDashboard().catch((error) => setMessage($("authMessage"), error.message, true)));
initializeAdmin().catch((error) => setMessage($("authMessage"), error.message, true));
