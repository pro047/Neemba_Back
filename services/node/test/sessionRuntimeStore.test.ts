import { PassThrough } from "node:stream";
import { describe, expect, it } from "vitest";
import { createSessionRuntimeStore } from "../src/sessionRuntimeStore.js";

describe("sessionRuntimeStore", () => {
  it("stores runtime by sessionId", () => {
    const store = createSessionRuntimeStore();
    const runtime = { inputWritable: new PassThrough(), stop: async () => {} };

    store.set("session-1", runtime);

    expect(store.get("session-1")).toBe(runtime);
  });

  it("returns runtime by sessionId", () => {
    const store = createSessionRuntimeStore();
    const runtime = { inputWritable: new PassThrough(), stop: async () => {} };
    store.set("session-1", runtime);

    expect(store.get("session-1")).toBe(runtime);
  });

  it("deletes runtime by sessionId", () => {
    const store = createSessionRuntimeStore();
    store.set("session-1", {
      inputWritable: new PassThrough(),
      stop: async () => {},
    });

    store.delete("session-1");

    expect(store.get("session-1")).toBeUndefined();
  });

  it("returns undefined for missing sessionId", () => {
    const store = createSessionRuntimeStore();

    expect(store.get("missing")).toBeUndefined();
  });
});
