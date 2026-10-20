export function deploymentUrls(origin: string) {
  const root = new URL("/swarm", origin).href;
  return { root, api: `${root}/api`, openai: `${root}/api/v1`, agent: `${root}/agent/agent.py` };
}
