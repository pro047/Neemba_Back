export type MicRuntime = {
  inputWritable: NodeJS.WritableStream;
  stop: () => Promise<void>;
};

// Mic sessions are fully independent (one listener = one session = one STT
// stream), so this is a plain per-session map — the single activeSessionId
// slot died with the kill-and-replace start behavior it existed for.
export interface SessionRuntimeStore {
  set(sessionId: string, runtime: MicRuntime): void;
  get(sessionId: string): MicRuntime | undefined;
  delete(sessionId: string): void;
  count(): number;
  clear(): void;
}

class InMemorySessionRuntimeStore implements SessionRuntimeStore {
  private readonly runtimes = new Map<string, MicRuntime>();

  set(sessionId: string, runtime: MicRuntime): void {
    this.runtimes.set(sessionId, runtime);
  }

  get(sessionId: string): MicRuntime | undefined {
    return this.runtimes.get(sessionId);
  }

  delete(sessionId: string): void {
    this.runtimes.delete(sessionId);
  }

  count(): number {
    return this.runtimes.size;
  }

  clear(): void {
    this.runtimes.clear();
  }
}

export function createSessionRuntimeStore(): SessionRuntimeStore {
  return new InMemorySessionRuntimeStore();
}

export const micRuntimeStore = createSessionRuntimeStore();
