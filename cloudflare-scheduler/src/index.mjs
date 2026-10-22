// A clock only: OCI credentials and instance-launch logic stay in GitHub Actions.
const DISPATCH_URL = "https://api.github.com/repos/gerardli/oci-free-arm-instance/actions/workflows/hunt-free-vm.yml/dispatches";

export async function dispatchGitHub(env, fetcher = fetch) {
  // Safe for staging: no GitHub call until the operator explicitly enables it.
  if (env.DISPATCH_ENABLED !== "true") {
    return "disabled";
  }
  if (!env.GITHUB_DISPATCH_TOKEN) {
    throw new Error("Cloudflare secret GITHUB_DISPATCH_TOKEN is missing");
  }

  const response = await fetcher(DISPATCH_URL, {
    method: "POST",
    headers: {
      "Accept": "application/vnd.github+json",
      "Authorization": "Bearer " + env.GITHUB_DISPATCH_TOKEN,
      "Content-Type": "application/json",
      "User-Agent": "oci-hunt-cloudflare-scheduler",
      "X-GitHub-Api-Version": "2022-11-28"
    },
    body: JSON.stringify({ ref: "main", inputs: { diagnostic_only: false } })
  });

  if (!response.ok) {
    // Do not print the token, URL with query parameters, or response body.
    throw new Error("GitHub workflow_dispatch rejected: HTTP " + response.status);
  }
  return "accepted";
}

export default {
  async scheduled(controller, env) {
    const result = await dispatchGitHub(env);
    console.log("OCI scheduler: " + result + "; scheduled=" +
      new Date(controller.scheduledTime).toISOString());
  },
  // No public HTTP trigger: a random visitor must never be able to launch a VM.
  async fetch() {
    return new Response("Not Found", { status: 404 });
  }
};
