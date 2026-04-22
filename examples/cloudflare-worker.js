const DEFAULT_TIMEZONE = "Asia/Shanghai";
const DEFAULT_TARGET_RUNS_PER_DAY = 8;
const DEFAULT_WINDOW_START_HOUR = 8;
const DEFAULT_WINDOW_END_HOUR = 23;
const DEFAULT_SLOT_MINUTES = 30;

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(handleScheduled(env));
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/run") {
      return jsonResponse(await triggerGithubDispatch(env));
    }

    if (request.method === "GET" && url.pathname === "/decide") {
      return jsonResponse(await handleDecision(env));
    }

    return jsonResponse({
      ok: true,
      message: "Randomized status worker is running.",
      endpoints: {
        run: "/run",
        decide: "/decide",
      },
      requiredEnv: ["GITHUB_TOKEN", "STATE_STORE"],
      optionalEnv: [
        "GITHUB_OWNER",
        "GITHUB_REPO",
        "GITHUB_EVENT_TYPE",
        "TARGET_RUNS_PER_DAY",
        "WINDOW_START_HOUR",
        "WINDOW_END_HOUR",
        "SLOT_MINUTES",
        "TIMEZONE",
      ],
    });
  },
};

async function handleScheduled(env) {
  const result = await handleDecision(env);
  console.log(JSON.stringify(result));
}

async function handleDecision(env) {
  const config = readConfig(env);
  const now = new Date();
  const local = getLocalParts(now, config.timezone);
  const stateKey = `random-status:${local.date}`;

  if (!isInsideWindow(local.hour, config.windowStartHour, config.windowEndHour)) {
    return {
      ok: true,
      executed: false,
      reason: "outside_window",
      localDate: local.date,
      localTime: local.time,
      config,
    };
  }

  const slotIndex = getSlotIndex(local.hour, local.minute, config.windowStartHour, config.slotMinutes);
  const totalSlots = getTotalSlots(config.windowStartHour, config.windowEndHour, config.slotMinutes);
  if (slotIndex < 0 || slotIndex >= totalSlots) {
    return {
      ok: true,
      executed: false,
      reason: "invalid_slot",
      localDate: local.date,
      localTime: local.time,
      config,
    };
  }

  const state = await readState(env, stateKey);
  const uniqueSlots = new Set(Array.isArray(state.executedSlots) ? state.executedSlots : []);
  if (uniqueSlots.has(slotIndex)) {
    return {
      ok: true,
      executed: false,
      reason: "slot_already_processed",
      localDate: local.date,
      localTime: local.time,
      state,
      config,
    };
  }

  const runsDone = Number(state.runsDone || 0);
  const remainingRuns = Math.max(0, config.targetRunsPerDay - runsDone);
  const remainingSlots = Math.max(1, totalSlots - slotIndex);

  if (remainingRuns <= 0) {
    uniqueSlots.add(slotIndex);
    await writeState(env, stateKey, {
      ...state,
      date: local.date,
      runsDone,
      executedSlots: [...uniqueSlots].sort((a, b) => a - b),
      updatedAt: now.toISOString(),
    });
    return {
      ok: true,
      executed: false,
      reason: "target_reached",
      localDate: local.date,
      localTime: local.time,
      state: {
        runsDone,
        targetRunsPerDay: config.targetRunsPerDay,
      },
      config,
    };
  }

  const probability = Math.min(1, remainingRuns / remainingSlots);
  const forced = remainingRuns >= remainingSlots;
  const randomValue = Math.random();
  const shouldExecute = forced || randomValue < probability;

  uniqueSlots.add(slotIndex);

  if (!shouldExecute) {
    await writeState(env, stateKey, {
      ...state,
      date: local.date,
      runsDone,
      executedSlots: [...uniqueSlots].sort((a, b) => a - b),
      updatedAt: now.toISOString(),
    });
    return {
      ok: true,
      executed: false,
      reason: "random_skip",
      localDate: local.date,
      localTime: local.time,
      probability,
      randomValue,
      state: {
        runsDone,
        remainingRuns,
        remainingSlots,
      },
      config,
    };
  }

  const dispatch = await triggerGithubDispatch(env);
  const nextRunsDone = dispatch.ok ? runsDone + 1 : runsDone;
  await writeState(env, stateKey, {
    ...state,
    date: local.date,
    runsDone: nextRunsDone,
    executedSlots: [...uniqueSlots].sort((a, b) => a - b),
    lastDispatchAt: dispatch.ok ? now.toISOString() : state.lastDispatchAt || null,
    lastDispatchStatus: dispatch.status,
    updatedAt: now.toISOString(),
  });

  return {
    ok: dispatch.ok,
    executed: dispatch.ok,
    reason: dispatch.ok ? "dispatch_sent" : "dispatch_failed",
    localDate: local.date,
    localTime: local.time,
    probability,
    randomValue,
    forced,
    dispatch,
    state: {
      runsDone: nextRunsDone,
      remainingRuns: Math.max(0, config.targetRunsPerDay - nextRunsDone),
      remainingSlots: Math.max(0, totalSlots - slotIndex - 1),
    },
    config,
  };
}

function readConfig(env) {
  return {
    owner: env.GITHUB_OWNER || "maya1900",
    repo: env.GITHUB_REPO || "anyrouter-status-page",
    eventType: env.GITHUB_EVENT_TYPE || "status-check",
    timezone: env.TIMEZONE || DEFAULT_TIMEZONE,
    targetRunsPerDay: parsePositiveInt(env.TARGET_RUNS_PER_DAY, DEFAULT_TARGET_RUNS_PER_DAY),
    windowStartHour: parseBoundedInt(env.WINDOW_START_HOUR, DEFAULT_WINDOW_START_HOUR, 0, 23),
    windowEndHour: parseBoundedInt(env.WINDOW_END_HOUR, DEFAULT_WINDOW_END_HOUR, 1, 24),
    slotMinutes: parseSlotMinutes(env.SLOT_MINUTES, DEFAULT_SLOT_MINUTES),
  };
}

function parsePositiveInt(value, fallback) {
  const parsed = Number.parseInt(String(value || ""), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function parseBoundedInt(value, fallback, min, max) {
  const parsed = Number.parseInt(String(value || ""), 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, parsed));
}

function parseSlotMinutes(value, fallback) {
  const parsed = Number.parseInt(String(value || ""), 10);
  if (!Number.isFinite(parsed)) return fallback;
  if (parsed <= 0 || 60 % parsed !== 0) return fallback;
  return parsed;
}

function getLocalParts(date, timezone) {
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  });

  const parts = Object.fromEntries(
    formatter.formatToParts(date).filter((part) => part.type !== "literal").map((part) => [part.type, part.value]),
  );

  return {
    date: `${parts.year}-${parts.month}-${parts.day}`,
    time: `${parts.hour}:${parts.minute}:${parts.second}`,
    hour: Number.parseInt(parts.hour, 10),
    minute: Number.parseInt(parts.minute, 10),
  };
}

function isInsideWindow(hour, startHour, endHour) {
  return hour >= startHour && hour < endHour;
}

function getSlotIndex(hour, minute, startHour, slotMinutes) {
  if (minute % slotMinutes !== 0) return -1;
  return (hour - startHour) * (60 / slotMinutes) + Math.floor(minute / slotMinutes);
}

function getTotalSlots(startHour, endHour, slotMinutes) {
  return Math.max(1, (endHour - startHour) * (60 / slotMinutes));
}

async function readState(env, key) {
  if (!env.STATE_STORE) {
    throw new Error("Missing KV binding: STATE_STORE");
  }
  const raw = await env.STATE_STORE.get(key);
  if (!raw) {
    return {
      date: key.split(":").pop(),
      runsDone: 0,
      executedSlots: [],
      lastDispatchAt: null,
      lastDispatchStatus: null,
      updatedAt: null,
    };
  }
  try {
    return JSON.parse(raw);
  } catch {
    return {
      date: key.split(":").pop(),
      runsDone: 0,
      executedSlots: [],
      lastDispatchAt: null,
      lastDispatchStatus: null,
      updatedAt: null,
    };
  }
}

async function writeState(env, key, state) {
  await env.STATE_STORE.put(key, JSON.stringify(state));
}

async function triggerGithubDispatch(env) {
  const config = readConfig(env);
  const response = await fetch(`https://api.github.com/repos/${config.owner}/${config.repo}/dispatches`, {
    method: "POST",
    headers: {
      accept: "application/vnd.github+json",
      authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "x-github-api-version": "2022-11-28",
      "content-type": "application/json",
      "user-agent": "anyrouter-status-worker",
    },
    body: JSON.stringify({ event_type: config.eventType }),
  });

  return {
    ok: response.ok,
    status: response.status,
    text: await response.text(),
  };
}

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}
