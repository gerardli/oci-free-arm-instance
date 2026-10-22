import test from "node:test";
import assert from "node:assert/strict";
import worker, { dispatchGitHub } from "../src/index.mjs";

test("staging is disabled and never calls GitHub", async () => {
  const result = await dispatchGitHub({ DISPATCH_ENABLED: "false" }, () => {
    throw new Error("Unexpected network call");
  });
  assert.equal(result, "disabled");
});

test("enabled worker requires the secret", async () => {
  await assert.rejects(
    dispatchGitHub({ DISPATCH_ENABLED: "true" }, () => {
      throw new Error("Unexpected network call");
    }),
    /GITHUB_DISPATCH_TOKEN is missing/
  );
});

test("enabled cron dispatches only the intended workflow and ref", async () => {
  const result = await dispatchGitHub(
    { DISPATCH_ENABLED: "true", GITHUB_DISPATCH_TOKEN: "fake-test-token" },
    async (url, options) => {
      assert.equal(url, "https://api.github.com/repos/gerardli/oci-free-arm-instance/actions/workflows/hunt-free-vm.yml/dispatches");
      assert.equal(options.method, "POST");
      assert.equal(options.headers.Authorization, "Bearer fake-test-token");
      assert.deepEqual(JSON.parse(options.body), {
        ref: "main", inputs: { diagnostic_only: false }
      });
      return { ok: true, status: 200 };
    }
  );
  assert.equal(result, "accepted");
});

test("authorization failures stop the run without leaking the secret", async () => {
  await assert.rejects(
    dispatchGitHub(
      { DISPATCH_ENABLED: "true", GITHUB_DISPATCH_TOKEN: "sensitive" },
      async () => ({ ok: false, status: 403 })
    ),
    (error) => error.message === "GitHub workflow_dispatch rejected: HTTP 403"
  );
});

test("public HTTP access cannot trigger the workflow", async () => {
  const response = await worker.fetch();
  assert.equal(response.status, 404);
});
