import { describe, expect, it, vi } from "vitest";
import { putEventOnce } from "../src/rtdb";

describe("idempotent RTDB writes", () => {
  it("uses a proof-derived child key and a create-only conditional PUT", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("ok", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const result = await putEventOnce(
      "token", "https://db.example", "telemetry", "abc123", { value: 1 },
    );
    expect(result.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      "https://db.example/telemetry/abc123.json",
      expect.objectContaining({
        method: "PUT",
        headers: expect.objectContaining({ "if-match": "null_etag" }),
      }),
    );
    vi.unstubAllGlobals();
  });

  it("surfaces Firebase's 412 replay result without creating another row", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("duplicate", { status: 412 })));
    const result = await putEventOnce(
      "token", "https://db.example", "error_reports", "same-proof", { value: 1 },
    );
    expect(result).toEqual({ ok: false, status: 412, body: "duplicate" });
    vi.unstubAllGlobals();
  });
});
