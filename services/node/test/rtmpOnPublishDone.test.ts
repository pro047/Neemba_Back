import { afterEach, describe, expect, it, vi } from "vitest";
import express from "express";
import request from "supertest";
import { createRtmpRouter } from "../src/router/rtmp.js";
import type { SessionLifecycle } from "../src/usecases/SessionLifecycle.js";

// on_publish_done is reachable from the internet: nginx only Basic-Auths
// /api/monitor/, so an unauthenticated POST here would end the broadcast one
// grace window later. The route therefore repeats the on_publish key check.

function buildApp() {
  const lifecycle = {
    publisherDone: vi.fn(),
    publisherReturned: vi.fn(),
  } as unknown as SessionLifecycle;
  const app = express();
  app.use("/api", createRtmpRouter(lifecycle));
  return { app, lifecycle };
}

describe("rtmp on_publish_done — 유예 타이머 트리거", () => {
  afterEach(() => {
    delete process.env.RTMP_PUBLISH_KEY;
  });

  it("키가 일치하면 200과 함께 자동 종료를 예약해야 한다", async () => {
    // Arrange
    process.env.RTMP_PUBLISH_KEY = "secret-key";
    const { app, lifecycle } = buildApp();

    // Act
    const response = await request(app)
      .post("/api/rtmp/on-publish-done")
      .type("form")
      .send({ name: "translation", key: "secret-key", clientid: "5" });

    // Assert
    expect(response.status).toBe(200);
    expect(lifecycle.publisherDone).toHaveBeenCalledWith("5");
  });

  it("키가 틀리면 403을 반환하고 자동 종료를 예약하지 않아야 한다", async () => {
    // Arrange
    process.env.RTMP_PUBLISH_KEY = "secret-key";
    const { app, lifecycle } = buildApp();

    // Act
    const response = await request(app)
      .post("/api/rtmp/on-publish-done")
      .type("form")
      .send({ name: "translation", key: "wrong", clientid: "5" });

    // Assert
    expect(response.status).toBe(403);
    expect(lifecycle.publisherDone).not.toHaveBeenCalled();
  });

  it("RTMP_PUBLISH_KEY가 설정되지 않았으면 허용해야 한다", async () => {
    // Arrange: 2단계 롤아웃 — 교회 OBS 가 키를 넣기 전에도 동작해야 한다
    delete process.env.RTMP_PUBLISH_KEY;
    const { app, lifecycle } = buildApp();

    // Act
    const response = await request(app)
      .post("/api/rtmp/on-publish-done")
      .type("form")
      .send({ name: "translation", clientid: "5" });

    // Assert
    expect(response.status).toBe(200);
    expect(lifecycle.publisherDone).toHaveBeenCalledWith("5");
  });

  it("키가 틀린 on_publish 는 유예 타이머를 취소하지 않아야 한다", async () => {
    // Arrange: 403 이 세션 연장 수단이 되면 안 된다
    process.env.RTMP_PUBLISH_KEY = "secret-key";
    const { app, lifecycle } = buildApp();

    // Act
    const response = await request(app)
      .post("/api/rtmp/on-publish")
      .type("form")
      .send({ name: "translation", key: "wrong", clientid: "5" });

    // Assert
    expect(response.status).toBe(403);
    expect(lifecycle.publisherReturned).not.toHaveBeenCalled();
  });
});
