import express from "express";
import request from "supertest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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

// on_publish 는 인증 경계인데 이 파일에는 그 테스트가 없었다. 아래는 2026-08-09
// dev 스택 실측으로 확정된 사실 위에 서 있다:
//   - nginx-rtmp 는 두 훅 본문에 `name` 을 실어 보낸다
//   - 같은 이름의 두 번째 publisher 만 'Already publishing' 으로 거절되고,
//     다른 이름은 그냥 받아준다 → node 가 막지 않으면 남의 스트림이 세션 슬롯을
//     차지한다
//   - node 가 403 을 주면 nginx 는 그 publisher 의 on_publish_done 을 보내지
//     않는다 → 403 한 번으로 lifecycle 오염이 전부 차단된다
describe("on_publish — 스트림 이름 게이트", () => {
  const ORIGINAL_PULL_URL = process.env.RTMP_PULL_URL;

  function mountHooks() {
    const lifecycle = {
      publisherReturned: vi.fn(),
      publisherDone: vi.fn(),
    } as unknown as SessionLifecycle;
    const app = express();
    app.use(createRtmpRouter(lifecycle));
    return { app, lifecycle };
  }

  beforeEach(() => {
    process.env.RTMP_PULL_URL = "rtmp://rtmp:1935/live/translation";
    delete process.env.RTMP_PUBLISH_KEY;
  });

  afterEach(() => {
    if (ORIGINAL_PULL_URL === undefined) delete process.env.RTMP_PULL_URL;
    else process.env.RTMP_PULL_URL = ORIGINAL_PULL_URL;
    delete process.env.RTMP_PUBLISH_KEY;
  });

  it("RTMP_PULL_URL 의 스트림 이름으로 온 publish 는 200 이고 publisher 를 등록해야 한다", async () => {
    // Arrange
    const { app, lifecycle } = mountHooks();

    // Act
    const res = await request(app)
      .post("/rtmp/on-publish")
      .type("form")
      .send({ name: "translation", clientid: "7", addr: "10.0.0.1" });

    // Assert
    expect(res.status).toBe(200);
    expect(lifecycle.publisherReturned).toHaveBeenCalledWith("7");
  });

  it("다른 스트림 이름의 publish 는 403 이고 publisher 를 등록하지 않아야 한다", async () => {
    // Arrange: nginx 는 이걸 거절하지 않으므로 여기서 막지 않으면 그대로 통과한다
    const { app, lifecycle } = mountHooks();

    // Act
    const res = await request(app)
      .post("/rtmp/on-publish")
      .type("form")
      .send({ name: "bogus", clientid: "9", addr: "10.0.0.2" });

    // Assert
    expect(res.status).toBe(403);
    expect(lifecycle.publisherReturned).not.toHaveBeenCalled();
  });

  it("키가 맞아도 스트림 이름이 다르면 403 이어야 한다", async () => {
    // Arrange: 이름 검사는 키 검사에 종속되지 않는다 — 키를 가진 운영자가
    // OBS 프로파일을 잘못 골라도 방송 슬롯을 뺏으면 안 된다
    process.env.RTMP_PUBLISH_KEY = "secret";
    const { app, lifecycle } = mountHooks();

    // Act
    const res = await request(app)
      .post("/rtmp/on-publish")
      .type("form")
      .send({ name: "bogus", key: "secret", clientid: "9" });

    // Assert
    expect(res.status).toBe(403);
    expect(lifecycle.publisherReturned).not.toHaveBeenCalled();
  });

  it("다른 스트림 이름의 publish_done 은 403 이고 자동 종료를 예약하지 않아야 한다", async () => {
    // Arrange: 이 라우트는 인터넷에서 도달 가능하다. 이름 검사가 없으면 남의
    // 스트림 이름으로 온 POST 하나가 유예 뒤 방송을 끝낼 수 있다.
    const { app, lifecycle } = mountHooks();

    // Act
    const res = await request(app)
      .post("/rtmp/on-publish-done")
      .type("form")
      .send({ name: "bogus", clientid: "9" });

    // Assert
    expect(res.status).toBe(403);
    expect(lifecycle.publisherDone).not.toHaveBeenCalled();
  });

  it("훅 본문에 name 이 없으면 검사를 건너뛰고 허용해야 한다", async () => {
    // Arrange: RTMP_PUBLISH_KEY 미설정과 같은 fail-open. 검사할 수 없을 때
    // 전부 막으면 설정 실수 하나로 예배 송출이 통째로 멈춘다.
    const { app, lifecycle } = mountHooks();

    // Act
    const res = await request(app)
      .post("/rtmp/on-publish")
      .type("form")
      .send({ clientid: "7" });

    // Assert
    expect(res.status).toBe(200);
    expect(lifecycle.publisherReturned).toHaveBeenCalledWith("7");
  });

  it("RTMP_PULL_URL 이 바뀌면 허용 이름도 따라가야 한다", async () => {
    // Arrange: 값의 출처가 하나라는 것의 정의 — ffmpeg 가 당기는 스트림과
    // 훅이 허용하는 스트림은 어긋날 수 없다
    process.env.RTMP_PULL_URL = "rtmp://rtmp:1935/live/sanctuary";
    const { app } = mountHooks();

    // Act
    const allowed = await request(app)
      .post("/rtmp/on-publish")
      .type("form")
      .send({ name: "sanctuary", clientid: "1" });
    const denied = await request(app)
      .post("/rtmp/on-publish")
      .type("form")
      .send({ name: "translation", clientid: "2" });

    // Assert
    expect(allowed.status).toBe(200);
    expect(denied.status).toBe(403);
  });
});

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
