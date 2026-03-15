import { describe, expect, it } from "vitest";
import rtmpRouter from "../src/router/rtmp.js";

describe("rtmp router regression", () => {
  it("keeps the RTMP start and stop routes mounted", () => {
    const routePaths = rtmpRouter.stack
      .map((layer) => layer.route?.path)
      .filter(Boolean);

    expect(routePaths).toContain("/sessions/start");
    expect(routePaths).toContain("/sessions/stop");
  });
});
