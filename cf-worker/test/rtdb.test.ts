import { describe, expect, it, vi } from "vitest";
import { putEventOnce } from "../src/rtdb";

describe("idempotent RTDB writes", () => {
  it("uses a proof-derived child key and a create-only conditional PUT", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("ok", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const result = await putEventOnce(
      "token", "https://test.firebaseio.com", "telemetry", "a".repeat(64), { value: 1 },
    );
    expect(result.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      `https://test.firebaseio.com/telemetry/${"a".repeat(64)}.json`,
      expect.objectContaining({
        method: "PUT",
        redirect: "error",
        headers: expect.objectContaining({ "if-match": "null_etag" }),
      }),
    );
    vi.unstubAllGlobals();
  });

  it("surfaces Firebase's 412 replay result without creating another row", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("duplicate", { status: 412 })));
    const result = await putEventOnce(
      "token", "https://test.firebaseio.com", "error_reports", "b".repeat(64), { value: 1 },
    );
    expect(result).toEqual({ ok: false, status: 412, body: "duplicate" });
    vi.unstubAllGlobals();
  });

  it("rejects an invalid database host before sending the bearer token", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(
      putEventOnce("secret-token", "https://attacker.example", "telemetry", "a".repeat(64), {}),
    ).rejects.toThrow("invalid Firebase database URL");
    expect(fetchMock).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("rejects a non-digest event id before issuing a request", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(
      putEventOnce("token", "https://test.firebaseio.com", "telemetry", "../escape", {}),
    ).rejects.toThrow("invalid event id");
    expect(fetchMock).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
