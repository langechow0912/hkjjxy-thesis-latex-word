const $ = (id) => document.getElementById(id);
const tokenKey = "liangpinAffiliateToken";

async function portalApi(url, options = {}) {
  const token = sessionStorage.getItem(tokenKey);
  const response = await fetch(url, {
    ...options,
    headers: {Authorization: `Bearer ${token}`, ...(options.headers || {})},
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
  return data;
}

function money(cents) { return `¥${(Number(cents || 0) / 100).toFixed(2)}`; }

async function loadPortal() {
  try {
    const data = await portalApi("/api/affiliate/me");
    $("loginPanel").hidden = true; $("dashboard").hidden = false;
    $("affiliateName").textContent = `${data.affiliate.name} · 推广码 ${data.affiliate.code}`;
    $("pendingTotal").textContent = money(data.pending_cents);
    $("availableTotal").textContent = money(data.available_cents);
    $("paidTotal").textContent = money(data.paid_cents);
    $("withdrawButton").disabled = data.available_cents <= 0 || data.has_open_withdrawal;
    $("withdrawButton").textContent = data.has_open_withdrawal ? "提现申请处理中" : "申请提现";
    const body = $("withdrawalRows"); body.replaceChildren();
    if (!data.withdrawals.length) {
      const tr = document.createElement("tr"); const td = document.createElement("td");
      td.textContent = "暂无提现记录"; td.colSpan = 4; tr.append(td); body.append(tr);
    }
    for (const withdrawal of data.withdrawals) {
      const tr = document.createElement("tr");
      const state = ({pending:"待审核",approved:"审核通过，待转账",paid:"已转账",rejected:"已驳回"})[withdrawal.status];
      for (const value of [new Date(withdrawal.created_at * 1000).toLocaleString(), money(withdrawal.amount_cents), state, withdrawal.payout_ref || "-"]) {
        const td = document.createElement("td"); td.textContent = value; tr.append(td);
      }
      body.append(tr);
    }
  } catch (error) {
    sessionStorage.removeItem(tokenKey);
    $("loginPanel").hidden = false; $("dashboard").hidden = true;
    if ($("portalToken").value) { $("loginMessage").textContent = error.message; $("loginMessage").classList.add("error"); }
  }
}

$("loginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  sessionStorage.setItem(tokenKey, $("portalToken").value.trim());
  await loadPortal();
});

$("withdrawForm").addEventListener("submit", async (event) => {
  event.preventDefault(); const button = $("withdrawButton"); button.disabled = true;
  try {
    const result = await portalApi("/api/affiliate/withdrawals", {method:"POST",
      headers:{"Content-Type":"application/json"}, body:JSON.stringify({payout_info:$("payoutInfo").value.trim()})});
    $("withdrawMessage").classList.remove("error");
    $("withdrawMessage").textContent = `提现申请已提交，金额 ${money(result.amount_cents)}。`;
    event.target.reset(); await loadPortal();
  } catch (error) {
    $("withdrawMessage").classList.add("error"); $("withdrawMessage").textContent = error.message;
  } finally { button.disabled = false; }
});

$("logoutButton").addEventListener("click", () => { sessionStorage.removeItem(tokenKey); location.reload(); });
if (sessionStorage.getItem(tokenKey)) loadPortal();
