import express from "express";
import request from "supertest";
import { describe, expect, it, vi } from "vitest";
import rtmpRouter, { createRtmpRouter } from "../src/router/rtmp.js";
import type { SessionLifecycle } from "../src/usecases/SessionLifecycle.js";

describe("rtmp router regression", () => {
  it("keeps the RTMP start and stop routes mounted", () => {
    const routePaths = rtmpRouter.stack
      .map((layer) => layer.route?.path)
      .filter(Boolean);

    expect(routePaths).toContain("/sessions/start");
    expect(routePaths).toContain("/sessions/stop");
  });
});

// 라우터를 실제로 태워서 응답 바디까지 본다 — P1 의 계약이 앱에게 보이는
// 형태는 이 JSON 이고, 앱은 배포되지 않으므로 여기서 어긋나면 예배 당일
// 알게 된다.
function mount(lifecycle: Partial<SessionLifecycle>) {
  const app = express();
  app.use(express.json());
  app.use(createRtmpRouter(lifecycle as SessionLifecycle));
  return app;
}

describe("POST /sessions/start — P1 D1 멱등 join", () => {
  it("join 이면 joined=true 와 라이브 세션의 언어를 응답해야 한다", async () => {
    // Arrange
    const app = mount({
      start: vi.fn(async () => ({
        sessionId: "s1",
        webSocketUrl: "wss://neemba/ws?sessionId=s1",
        joined: true,
        sourceLang: "ko-KR",
        targetLang: "en-US",
      })),
    });

    // Act
    const res = await request(app)
      .post("/sessions/start")
      .send({ sourceLang: "ko-KR", targetLang: "ja" });

    // Assert: 앱이 읽는 두 필드는 그대로고(무배포 전제), 나머지는 덧붙기만 한다
    expect(res.status).toBe(202);
    expect(res.body.sessionId).toBe("s1");
    expect(res.body.webSocketUrl).toBe("wss://neemba/ws?sessionId=s1");
    expect(res.body.joined).toBe(true);
    expect(res.body.targetLang).toBe("en-US");
  });
});

describe("POST /sessions/stop — P1 D4 강등", () => {
  it("라이브 세션의 stop 이어도 200 이고 세션은 닫히지 않아야 한다", async () => {
    // Arrange
    const stopBySessionId = vi.fn(async () => "ignored" as const);
    const app = mount({ stopBySessionId });

    // Act
    const res = await request(app).post("/sessions/stop").send({
      sessionId: "s1",
    });

    // Assert
    expect(res.status).toBe(200);
    expect(res.body).toMatchObject({ ok: true, stopped: false });
    expect(stopBySessionId).toHaveBeenCalledWith("s1");
  });

  it("남의 세션 id 로 온 stop 도 400 이 아니라 200 이어야 한다", async () => {
    // Arrange: join 한 청취자의 [정지] 는 흔한 오작동 경로다. 400 을 주면
    // 사용자에게는 '정지 실패' 로 보이는데, 실제로는 막은 게 정상 동작이다.
    const app = mount({
      stopBySessionId: vi.fn(async () => "mismatch" as const),
    });

    // Act
    const res = await request(app).post("/sessions/stop").send({
      sessionId: "someone-elses",
    });

    // Assert
    expect(res.status).toBe(200);
    expect(res.body.outcome).toBe("mismatch");
  });

  it("sessionId 가 없으면 여전히 400 이어야 한다", async () => {
    // 강등은 '세션을 안 닫는다' 이지 '입력 검증을 푼다' 가 아니다.
    const app = mount({ stopBySessionId: vi.fn() });

    const res = await request(app).post("/sessions/stop").send({});

    expect(res.status).toBe(400);
  });
});
