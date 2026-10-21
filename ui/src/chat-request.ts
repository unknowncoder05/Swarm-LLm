export type ChatPhase = "preparing" | "waiting_for_capacity" | "waiting_for_route";

interface ChatRequestOptions {
  signal: AbortSignal;
  onPhase: (phase: ChatPhase) => void;
  capacityWaitMs?: number;
}

function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const cancel = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", cancel);
      reject(signal.reason);
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", cancel);
      resolve();
    }, ms);
    signal.addEventListener("abort", cancel, { once: true });
    if (signal.aborted) cancel();
  });
}

/** Retry only explicit pre-admission waits; never replay accepted inference. */
export async function requestChatCompletion(
  apiKey: string,
  payload: { model: string; messages: { role: string; content: string }[] },
  options: ChatRequestOptions,
) {
  const body = JSON.stringify(payload);
  const deadline = Date.now() + (options.capacityWaitMs ?? 30 * 60 * 1000);
  let waiting = false;
  while (true) {
    if (options.signal.aborted) throw options.signal.reason;
    if (waiting && Date.now() >= deadline) {
      throw new Error(`Timed out waiting for available GPUs or a working route for '${payload.model}'. Check the Machines tab and try again.`);
    }
    options.onPhase("preparing");
    const response = await fetch("/swarm/api/v1/chat/completions", {
      method: "POST",
      headers: { "X-API-Key": apiKey, "Content-Type": "application/json" },
      body,
      signal: options.signal,
    });
    if (response.ok) return response.json();
    const error = await response.json().catch(() => ({})) as { detail?: unknown };
    const detail = typeof error.detail === "string" ? error.detail : `HTTP ${response.status}`;
    const phase = response.headers.get("X-Swarm-Status");
    if (response.status !== 503 || (phase !== "waiting_for_capacity" && phase !== "waiting_for_route")) {
      throw new Error(detail);
    }
    waiting = true;
    options.onPhase(phase);
    const retry = Number(response.headers.get("Retry-After") ?? 5);
    const delay = Number.isFinite(retry) ? Math.max(1, Math.min(retry, 30)) * 1000 : 5000;
    await pause(Math.max(0, Math.min(delay, deadline - Date.now())), options.signal);
  }
}
