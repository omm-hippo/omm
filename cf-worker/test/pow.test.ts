import { describe, expect, it } from "vitest";
import { isTimestampFresh, sha256Hex, verifyProofOfWork } from "../src/pow";

describe("isTimestampFresh", () => {
  it("accepts a timestamp within the skew window", () => {
    expect(isTimestampFresh(1000, 500, 1400)).toBe(true);
  });
  it("rejects a timestamp older than the skew window", () => {
    expect(isTimestampFresh(1000, 500, 2000)).toBe(false);
  });
  it("rejects a timestamp from the future beyond the skew window", () => {
    expect(isTimestampFresh(3000, 500, 1000)).toBe(false);
  });
  it("rejects a non-finite timestamp", () => {
    expect(isTimestampFresh(NaN, 500)).toBe(false);
  });
});

describe("verifyProofOfWork", () => {
  it("accepts a nonce that actually satisfies the difficulty", async () => {
    const eventJson = '{"a":1}';
    const timestamp = 1234567890;
    const difficulty = 2;
    let nonce = 0;
    while (true) {
      const hash = await sha256Hex(`${eventJson}:${timestamp}:${nonce}`);
      if (hash.startsWith("00")) break;
      nonce += 1;
    }
    expect(await verifyProofOfWork(eventJson, timestamp, nonce, difficulty)).toBe(true);
  });

  it("rejects a nonce that does not satisfy the difficulty", async () => {
    expect(await verifyProofOfWork('{"a":1}', 1234567890, 0, 8)).toBe(false);
  });

  it("rejects a negative or non-integer nonce", async () => {
    expect(await verifyProofOfWork('{"a":1}', 1234567890, -1, 1)).toBe(false);
    expect(await verifyProofOfWork('{"a":1}', 1234567890, 1.5, 1)).toBe(false);
  });

  it("ties the solution to the exact payload string", async () => {
    const timestamp = 1234567890;
    const difficulty = 2;
    let nonce = 0;
    while (true) {
      const hash = await sha256Hex(`{"a":1}:${timestamp}:${nonce}`);
      if (hash.startsWith("00")) break;
      nonce += 1;
    }
    expect(await verifyProofOfWork('{"a":2}', timestamp, nonce, difficulty)).toBe(false);
  });
});
