import { afterEach, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";
afterEach(() => vi.unstubAllGlobals());
it("rejects a 503 response instead of reporting saved data", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ error: "storage_unavailable" }), {
        status: 503,
      }),
    ),
  );
  await expect(api("/campaigns")).rejects.toMatchObject({
    status: 503,
    code: "storage_unavailable",
  });
});
it("uses session cookies and csrf without a persistent browser token", async () => {
  document.cookie = "ats_csrf=test-csrf";
  const fetcher = vi
    .fn()
    .mockResolvedValue(new Response("{}", { status: 200 }));
  vi.stubGlobal("fetch", fetcher);
  await api("/contacts", { method: "POST", body: "{}" });
  expect(fetcher.mock.calls[0][1]).toMatchObject({
    credentials: "same-origin",
    headers: { "X-CSRF-Token": "test-csrf" },
  });
});
it("rejects invalid non-JSON gateway errors", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response("Bad gateway", { status: 502 })),
  );
  await expect(api("/contacts")).rejects.toBeInstanceOf(ApiError);
});
