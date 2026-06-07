import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createMicTtsHandler,
  createStartMicSessionHandler,
  createStopMicSessionHandler,
  type MicTtsSynthesizer,
  type PythonSessionClient,
} from "../src/router/mic.js";
import { removeSessionId } from "../src/ports/sessionStore.js";
import {
  createSessionRuntimeStore,
  type MicRuntime,
} from "../src/sessionRuntimeStore.js";

function createMockResponse() {
  const response = {
    statusCode: 200,
    body: undefined as unknown,
    status(code: number) {
      this.statusCode = code;
      return this;
    },
    json(payload: unknown) {
      this.body = payload;
      return this;
    },
  };

  return response;
}

describe("mic router", () => {
  afterEach(() => {
    removeSessionId();
  });

  it("POST /api/mic/start returns 202 and stores runtime", async () => {
    const runtimeStore = createSessionRuntimeStore();
    const runtime: MicRuntime = {
      inputWritable: new (await import("node:stream")).PassThrough(),
      stop: vi.fn(async () => {}),
    };
    const pythonClient: PythonSessionClient = {
      startSession: vi.fn(async () => ({
        sessionId: "session-1",
        webSocketUrl: "ws://localhost:3000/api/mic",
      })),
      stopSession: vi.fn(async () => {}),
    };

    const handler = createStartMicSessionHandler({
      pythonClient,
      runtimeStore,
      micPipelineFactory: async (sessionId) => {
        expect(sessionId).toBe("session-1");
        return runtime;
      },
      sessionIdFactory: () => "session-1",
    });
    const response = createMockResponse();

    await handler(
      { body: { sourceLang: "ko-KR", targetLang: "en-US" } } as never,
      response as never,
      vi.fn()
    );

    expect(response.statusCode).toBe(202);
    expect(response.body).toEqual({
      sessionId: "session-1",
      webSocketUrl: "ws://localhost:3000/api/mic",
    });
    expect(runtimeStore.get("session-1")).toBe(runtime);
  });

  it("does not store runtime if Python start fails", async () => {
    const runtimeStore = createSessionRuntimeStore();
    const pythonClient: PythonSessionClient = {
      startSession: vi.fn(async () => {
        throw new Error("python down");
      }),
      stopSession: vi.fn(async () => {}),
    };

    const handler = createStartMicSessionHandler({
      pythonClient,
      runtimeStore,
      micPipelineFactory: vi.fn(async (_sessionId) => {
        throw new Error("should not run");
      }),
      sessionIdFactory: () => "session-1",
    });
    const response = createMockResponse();

    await handler({ body: {} } as never, response as never, vi.fn());

    expect(response.statusCode).toBe(500);
    expect(runtimeStore.get("session-1")).toBeUndefined();
  });

  it("fails cleanly if Node mic pipeline creation fails", async () => {
    const runtimeStore = createSessionRuntimeStore();
    const pythonClient: PythonSessionClient = {
      startSession: vi.fn(async () => ({
        sessionId: "session-1",
        webSocketUrl: "ws://localhost:3000/api/mic",
      })),
      stopSession: vi.fn(async () => {}),
    };

    const handler = createStartMicSessionHandler({
      pythonClient,
      runtimeStore,
      micPipelineFactory: vi.fn(async (_sessionId) => {
        throw new Error("pipeline failed");
      }),
      sessionIdFactory: () => "session-1",
    });
    const response = createMockResponse();

    await handler({ body: {} } as never, response as never, vi.fn());

    expect(response.statusCode).toBe(500);
    expect(runtimeStore.get("session-1")).toBeUndefined();
    expect(pythonClient.stopSession).toHaveBeenCalledWith("session-1");
  });

  it("stops the previous active mic session before starting a new one", async () => {
    const runtimeStore = createSessionRuntimeStore();
    const previousRuntime: MicRuntime = {
      inputWritable: new (await import("node:stream")).PassThrough(),
      stop: vi.fn(async () => {}),
    };
    const nextRuntime: MicRuntime = {
      inputWritable: new (await import("node:stream")).PassThrough(),
      stop: vi.fn(async () => {}),
    };
    runtimeStore.set("session-old", previousRuntime);
    runtimeStore.setActiveSessionId("session-old");

    const pythonClient: PythonSessionClient = {
      startSession: vi.fn(async () => ({
        sessionId: "session-new",
        webSocketUrl: "ws://localhost:3000/api/mic",
      })),
      stopSession: vi.fn(async () => {}),
    };

    const handler = createStartMicSessionHandler({
      pythonClient,
      runtimeStore,
      micPipelineFactory: async (sessionId) => {
        expect(sessionId).toBe("session-new");
        return nextRuntime;
      },
      sessionIdFactory: () => "session-new",
    });
    const response = createMockResponse();

    await handler({ body: {} } as never, response as never, vi.fn());

    expect(previousRuntime.stop).toHaveBeenCalled();
    expect(pythonClient.stopSession).toHaveBeenCalledWith("session-old");
    expect(runtimeStore.get("session-old")).toBeUndefined();
    expect(runtimeStore.get("session-new")).toBe(nextRuntime);
    expect(runtimeStore.getActiveSessionId()).toBe("session-new");
    expect(response.statusCode).toBe(202);
  });

  it("POST /api/mic/stop stops runtime, removes it, and calls Python stop", async () => {
    const runtimeStore = createSessionRuntimeStore();
    const runtime: MicRuntime = {
      inputWritable: new (await import("node:stream")).PassThrough(),
      stop: vi.fn(async () => {}),
    };
    runtimeStore.set("session-1", runtime);
    runtimeStore.setActiveSessionId("session-1");

    const pythonClient: PythonSessionClient = {
      startSession: vi.fn(async () => ({
        sessionId: "session-1",
        webSocketUrl: "ws://localhost:3000/api/mic",
      })),
      stopSession: vi.fn(async () => {}),
    };

    const handler = createStopMicSessionHandler({
      pythonClient,
      runtimeStore,
    });
    const response = createMockResponse();

    await handler(
      { body: { sessionId: "session-1" } } as never,
      response as never,
      vi.fn()
    );

    expect(response.statusCode).toBe(200);
    expect(runtime.stop).toHaveBeenCalled();
    expect(runtimeStore.get("session-1")).toBeUndefined();
    expect(pythonClient.stopSession).toHaveBeenCalledWith("session-1");
  });

  it("returns 400 when sessionId is missing on stop", async () => {
    const handler = createStopMicSessionHandler({
      pythonClient: {
        startSession: vi.fn(async () => ({
          sessionId: "session-1",
          webSocketUrl: "ws://localhost:3000/api/mic",
        })),
        stopSession: vi.fn(async () => {}),
      },
      runtimeStore: createSessionRuntimeStore(),
    });
    const response = createMockResponse();

    await handler({ body: {} } as never, response as never, vi.fn());

    expect(response.statusCode).toBe(400);
  });

  it("POST /api/mic/tts returns synthesized audio for requested language", async () => {
    const synthesizer: MicTtsSynthesizer = {
      synthesize: vi.fn(async ({ languageCode }) => ({
        audioContent: `audio-${languageCode}`,
        audioMimeType: "audio/mpeg",
      })),
    };
    const handler = createMicTtsHandler({ ttsSynthesizer: synthesizer });
    const response = createMockResponse();

    await handler(
      {
        body: {
          text: "Habari",
          language: "sw-KE",
          fallbackLanguage: "en-US",
        },
      } as never,
      response as never,
      vi.fn()
    );

    expect(response.statusCode).toBe(200);
    expect(response.body).toEqual({
      audioContent: "audio-sw-KE",
      audioMimeType: "audio/mpeg",
      requestedLanguage: "sw-KE",
      resolvedLanguage: "sw-KE",
      usedFallback: false,
    });
    expect(synthesizer.synthesize).toHaveBeenCalledWith({
      text: "Habari",
      languageCode: "sw-KE",
    });
  });

  it("POST /api/mic/tts falls back to English when requested language synthesis fails", async () => {
    const synthesizer: MicTtsSynthesizer = {
      synthesize: vi.fn(async ({ languageCode }) => {
        if (languageCode === "sw-KE") {
          throw new Error("voice unavailable");
        }
        return {
          audioContent: "audio-en-US",
          audioMimeType: "audio/mpeg",
        };
      }),
    };
    const handler = createMicTtsHandler({ ttsSynthesizer: synthesizer });
    const response = createMockResponse();

    await handler(
      {
        body: {
          text: "Habari",
          language: "sw-KE",
          fallbackLanguage: "en-US",
        },
      } as never,
      response as never,
      vi.fn()
    );

    expect(response.statusCode).toBe(200);
    expect(response.body).toEqual({
      audioContent: "audio-en-US",
      audioMimeType: "audio/mpeg",
      requestedLanguage: "sw-KE",
      resolvedLanguage: "en-US",
      usedFallback: true,
    });
    expect(synthesizer.synthesize).toHaveBeenNthCalledWith(1, {
      text: "Habari",
      languageCode: "sw-KE",
    });
    expect(synthesizer.synthesize).toHaveBeenNthCalledWith(2, {
      text: "Habari",
      languageCode: "en-US",
    });
  });
});
