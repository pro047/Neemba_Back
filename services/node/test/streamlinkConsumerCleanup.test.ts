import { describe, expect, it, vi } from "vitest";
import { PassThrough } from "node:stream";
import { StreamlinkToConsumerService } from "../src/usecases/StreamlinkToConsumerService.js";
import type { AudioTranscoder } from "../src/ports/ports.js";
import type { AudioConsumerPort } from "../src/ports/audioConsumerPort.js";

// run() 은 ffmpeg 를 먼저 띄우고 나서 orchestra.start 를 기다린다. start 가
// throw 하면(STT 설정 실패 등) 반환된 stop 클로저를 받을 수 없으므로, run()
// 자신이 트랜스코더를 정리하고 던져야 한다 — 안 그러면 /sessions/start 재시도
// 마다 RTMP 를 당기는 고아 ffmpeg 가 1개씩 쌓인다.

describe("StreamlinkToConsumerService — 시작 실패 정리", () => {
  it("오케스트레이터 시작이 실패하면 트랜스코더를 정리하고 에러를 다시 던져야 한다", async () => {
    // Arrange
    const stopTranscoder = vi.fn();
    const inputWritable = new PassThrough();
    const ffmpeg = {
      startTranscoder: () => ({
        inputWritable,
        pcmReadable: new PassThrough(),
        stop: stopTranscoder,
      }),
    } as unknown as AudioTranscoder;
    const orchestra: AudioConsumerPort = {
      start: vi.fn(async () => {
        throw new Error("stt configure failed");
      }),
    };
    const service = new StreamlinkToConsumerService(ffmpeg, orchestra, "s1");

    // Act & Assert
    await expect(service.run()).rejects.toThrow("stt configure failed");
    expect(stopTranscoder).toHaveBeenCalledTimes(1);
    expect(inputWritable.writableEnded).toBe(true);
  });
});
