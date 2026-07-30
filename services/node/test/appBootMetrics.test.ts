import { afterEach, describe, expect, it, vi } from "vitest";

// 2026-07-30: monitor 가 매일 "RTMP 인증 꺼짐" 오탐을 냈다. 게이지를 세우는
// 곳이 on_publish 핸들러 안뿐이라, 배포 후 첫 publish 전까지 prom-client 기본값
// 0 이 노출됐기 때문이다. 배포할 때마다 재발하는 종류였다.
//
// createApp 은 재진입이 안 된다 — demo_request_total 이 prom-client 기본
// 레지스트리에 등록돼 두 번째 호출이 "already registered" 로 죽는다. 그래서
// 케이스마다 레지스트리를 비우고 모듈 그래프를 리셋한다. clear() 가 먼저여야
// 한다 — prom-client 는 node_modules 싱글턴이라 resetModules 로는 안 지워지고,
// 리셋된 metrics.ts 가 다시 등록할 자리를 미리 만들어줘야 한다.
async function bootWithFreshRegistry() {
  const { register } = await import("prom-client");
  register.clear();
  vi.resetModules();
  const { createApp } = await import("../src/app.js");
  createApp();
  const metric = (await register.getMetricsAsJSON()).find(
    (m) => m.name === "neemba_rtmp_auth_enabled"
  );
  return (metric as { values?: { value: number }[] } | undefined)?.values?.[0]
    ?.value;
}

describe("createApp — 부팅 시 메트릭 시딩", () => {
  afterEach(() => {
    delete process.env.RTMP_PUBLISH_KEY;
  });

  it("RTMP_PUBLISH_KEY 가 있으면 첫 publish 전에도 인증 게이지가 1이어야 한다", async () => {
    // Arrange
    process.env.RTMP_PUBLISH_KEY = "secret-key";

    // Act
    const value = await bootWithFreshRegistry();

    // Assert
    expect(value).toBe(1);
  });

  it("RTMP_PUBLISH_KEY 가 없으면 인증 게이지가 0이어야 한다", async () => {
    // Arrange
    delete process.env.RTMP_PUBLISH_KEY;

    // Act
    const value = await bootWithFreshRegistry();

    // Assert
    expect(value).toBe(0);
  });
});
