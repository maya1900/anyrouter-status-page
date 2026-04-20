/**
 * AnyRouter 自动签到 - Cloudflare Workers（支持 Cron 定时触发）
 *
 * 部署：
 * 1. Workers & Pages -> Workers -> Create Worker
 * 2. 把本文件完整内容粘贴进去并保存
 * 3. Settings -> Variables 添加：
 *    - COOKIES（必填）：多账号 session 值，支持换行分隔或 JSON 数组
 *    - BASE_URL（可选）：默认 https://anyrouter.top
 *    - TG_BOT_TOKEN（可选）：Telegram Bot Token
 *    - TG_CHAT_ID（可选）：Telegram Chat ID
 *    - RUN_TOKEN（可选）：手动触发 /run 的鉴权令牌
 * 4. Triggers -> Cron Triggers 添加：
 *    - 每天北京时间 08:30：30 0 * * *
 */

const DEFAULT_BASE_URL = "https://anyrouter.top";
const USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " +
  "AppleWebKit/537.36 (KHTML, like Gecko) " +
  "Chrome/131.0.0.0 Safari/537.36";

function normalizeBaseUrl(raw) {
  const base = (raw || DEFAULT_BASE_URL).trim();
  return base.replace(/\/+$/, "");
}

function parseCookies(raw) {
  if (!raw) return [];
  const trimmed = String(raw).trim();
  if (!trimmed) return [];

  if (trimmed.startsWith("[")) {
    try {
      const arr = JSON.parse(trimmed);
      if (Array.isArray(arr)) {
        return arr.map((item) => String(item).trim()).filter(Boolean);
      }
    } catch {
      // ignore invalid JSON and fall back
    }
  }

  return trimmed
    .split(/[\n,]+/g)
    .map((item) => item.trim())
    .filter(Boolean);
}

async function sendTelegram(env, messageHtml) {
  const token = (env.TG_BOT_TOKEN || "").trim();
  const chatId = (env.TG_CHAT_ID || "").trim();
  if (!token || !chatId) return { sent: false, reason: "not_configured" };

  const response = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: chatId,
      text: messageHtml,
      parse_mode: "HTML",
    }),
  });

  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    return { sent: false, reason: `http_${response.status}`, detail };
  }
  return { sent: true };
}

async function signIn(baseUrl, cookie) {
  const response = await fetch(`${baseUrl}/api/user/sign_in`, {
    method: "POST",
    headers: {
      "User-Agent": USER_AGENT,
      Cookie: `session=${cookie}`,
      "Content-Type": "application/json",
    },
    body: "",
  }).catch((error) => ({ ok: false, error }));

  if (!response || response.ok === false && "error" in response) {
    return { ok: false, msg: `❌ 请求异常: ${String(response.error)}` };
  }
  if (response.status === 401) return { ok: false, msg: "❌ Cookie 无效(401)" };

  const bodyText = await response.text().catch(() => "");
  if (!response.ok) return { ok: false, msg: `❌ HTTP ${response.status}: ${bodyText}` };

  let data;
  try {
    data = JSON.parse(bodyText);
  } catch {
    return { ok: false, msg: `❌ 响应非 JSON: ${bodyText}` };
  }

  if (!data || typeof data !== "object") {
    return { ok: true, msg: `✅ 返回: ${String(data)}` };
  }

  const success = data.success;
  const message = String(data.message || "").trim();
  if (success === true) return { ok: true, msg: message ? `✅ ${message}` : "✅ 今日已签到" };
  if (success === false) return { ok: false, msg: message ? `❌ ${message}` : `❌ 签到失败: ${JSON.stringify(data)}` };
  return { ok: true, msg: `✅ 返回: ${JSON.stringify(data)}` };
}

async function runOnce(env) {
  const baseUrl = normalizeBaseUrl(env.BASE_URL);
  const cookies = parseCookies(env.COOKIES);

  if (cookies.length === 0) {
    return {
      ok: false,
      results: [],
      summary: "❌ 未配置 COOKIES（请在 Workers Variables 里添加）",
      telegram: { sent: false, reason: "missing_cookies" },
    };
  }

  const results = ["🔔 <b>AnyRouter 签到结果</b>\n"];
  let successCount = 0;
  let failCount = 0;

  for (let index = 0; index < cookies.length; index += 1) {
    const result = await signIn(baseUrl, cookies[index]);
    results.push(`账号 #${index + 1}: ${result.msg}`);
    if (result.ok) successCount += 1;
    else failCount += 1;
  }

  const summary = `\n📊 <b>汇总</b>: 成功 ${successCount} / 失败 ${failCount} / 共 ${cookies.length}`;
  results.push(summary);

  const telegram = await sendTelegram(env, results.join("\n"));
  return {
    ok: failCount === 0,
    results,
    summary,
    telegram,
  };
}

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(
      (async () => {
        const startedAt = new Date().toISOString();
        const result = await runOnce(env);
        console.log(`[anyrouter-signin] startedAt=${startedAt} baseUrl=${normalizeBaseUrl(env.BASE_URL)} ${result.summary}`);
        if (!result.telegram.sent) {
          console.log(`[anyrouter-signin] telegram_not_sent reason=${result.telegram.reason || "unknown"}`);
        }
      })(),
    );
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/run") {
      const runToken = (env.RUN_TOKEN || "").trim();
      if (runToken) {
        const tokenFromQuery = (url.searchParams.get("token") || "").trim();
        const auth = (request.headers.get("Authorization") || "").trim();
        const tokenFromAuth = auth.toLowerCase().startsWith("bearer ") ? auth.slice(7).trim() : "";
        if (tokenFromQuery !== runToken && tokenFromAuth !== runToken) {
          return new Response("Unauthorized", { status: 401 });
        }
      }

      const startedAt = new Date().toISOString();
      const result = await runOnce(env);
      return jsonResponse({ startedAt, ...result }, result.ok ? 200 : 500);
    }

    if (request.method === "GET") {
      return jsonResponse({
        ok: true,
        message: "AnyRouter sign-in worker is running. Use GET /run to trigger manually.",
        requiredEnv: ["COOKIES"],
        optionalEnv: ["BASE_URL", "TG_BOT_TOKEN", "TG_CHAT_ID", "RUN_TOKEN"],
        exampleCronUTC: "30 0 * * *  (北京时间 08:30)",
      });
    }

    return new Response("Method Not Allowed", { status: 405 });
  },
};
