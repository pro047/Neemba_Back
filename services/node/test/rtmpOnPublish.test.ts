import { afterEach, describe, expect, it } from "vitest";
import express from "express";
import request from "supertest";
import rtmpRouter from "../src/router/rtmp.js";

// nginx-rtmp calls on_publish before accepting a publisher (OBS). The stream
// key rides in the stream-name args (`translation?key=SECRET`), which
// nginx-rtmp forwards as urlencoded form fields. 2xx allows, non-2xx denies.
//
// Two-phase rollout: with RTMP_PUBLISH_KEY unset the route ALLOWS everything
// (auth off) so deploying this code cannot break the live broadcast before
// the church OBS and ENV_PROD are updated.

function buildApp() {
  const app = express();
  app.use("/api", rtmpRouter);
  return app;
}

describe("rtmp on_publish — 스트림 키 인증", () => {
  afterEach(() => {
    delete process.env.RTMP_PUBLISH_KEY;
  });

  it("키가 일치하면 200을 반환해야 한다", async () => {
    // Arrange
    process.env.RTMP_PUBLISH_KEY = "secret-key";

    // Act
    const response = await request(buildApp())
      .post("/api/rtmp/on-publish")
      .type("form")
      .send({ app: "live", name: "translation", key: "secret-key" });

    // Assert
    expect(response.status).toBe(200);
  });

  it("키가 틀리면 403을 반환해야 한다", async () => {
    // Arrange
    process.env.RTMP_PUBLISH_KEY = "secret-key";

    // Act
    const response = await request(buildApp())
      .post("/api/rtmp/on-publish")
      .type("form")
      .send({ app: "live", name: "translation", key: "wrong" });

    // Assert
    expect(response.status).toBe(403);
  });

  it("키가 없으면 403을 반환해야 한다", async () => {
    // Arrange
    process.env.RTMP_PUBLISH_KEY = "secret-key";

    // Act
    const response = await request(buildApp())
      .post("/api/rtmp/on-publish")
      .type("form")
      .send({ app: "live", name: "translation" });

    // Assert
    expect(response.status).toBe(403);
  });

  it("RTMP_PUBLISH_KEY가 설정되지 않았으면 허용해야 한다", async () => {
    // Arrange: auth disabled (two-phase rollout)
    delete process.env.RTMP_PUBLISH_KEY;

    // Act
    const response = await request(buildApp())
      .post("/api/rtmp/on-publish")
      .type("form")
      .send({ app: "live", name: "translation" });

    // Assert
    expect(response.status).toBe(200);
  });
});
