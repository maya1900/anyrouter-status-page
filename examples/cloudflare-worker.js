export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(triggerGithubDispatch(env));
  },

  async fetch(_request, env) {
    const result = await triggerGithubDispatch(env);
    return new Response(JSON.stringify(result, null, 2), {
      status: result.ok ? 200 : 500,
      headers: { "content-type": "application/json; charset=utf-8" },
    });
  },
};

async function triggerGithubDispatch(env) {
  const owner = env.GITHUB_OWNER || "maya1900";
  const repo = env.GITHUB_REPO || "anyrouter-status-page";
  const eventType = env.GITHUB_EVENT_TYPE || "status-check";

  const response = await fetch(`https://api.github.com/repos/${owner}/${repo}/dispatches`, {
    method: "POST",
    headers: {
      "accept": "application/vnd.github+json",
      "authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "x-github-api-version": "2022-11-28",
      "content-type": "application/json",
      "user-agent": "anyrouter-status-worker",
    },
    body: JSON.stringify({ event_type: eventType }),
  });

  return {
    ok: response.ok,
    status: response.status,
    text: await response.text(),
  };
}
