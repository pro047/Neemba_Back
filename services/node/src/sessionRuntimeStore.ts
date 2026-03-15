export type MicRuntime = {
  inputWritable: NodeJS.WritableStream;
  stop: () => Promise<void>;
};

export interface SessionRuntimeStore {
  set(sessionId: string, runtime: MicRuntime): void;
  get(sessionId: string): MicRuntime | undefined;
  delete(sessionId: string): void;
  setActiveSessionId(sessionId: string | undefined): void;
  getActiveSessionId(): string | undefined;
  getActiveRuntime(): MicRuntime | undefined;
  clear(): void;
}

class InMemorySessionRuntimeStore implements SessionRuntimeStore {
  private readonly runtimes = new Map<string, MicRuntime>();
  private activeSessionId: string | undefined;

  set(sessionId: string, runtime: MicRuntime): void {
    this.runtimes.set(sessionId, runtime);
  }

  get(sessionId: string): MicRuntime | undefined {
    return this.runtimes.get(sessionId);
  }

  delete(sessionId: string): void {
    this.runtimes.delete(sessionId);
    if (this.activeSessionId === sessionId) {
      this.activeSessionId = undefined;
    }
  }

  setActiveSessionId(sessionId: string | undefined): void {
    this.activeSessionId = sessionId;
  }

  getActiveSessionId(): string | undefined {
    return this.activeSessionId;
  }

  getActiveRuntime(): MicRuntime | undefined {
    if (!this.activeSessionId) {
      return undefined;
    }

    return this.runtimes.get(this.activeSessionId);
  }

  clear(): void {
    this.runtimes.clear();
    this.activeSessionId = undefined;
  }
}

export function createSessionRuntimeStore(): SessionRuntimeStore {
  return new InMemorySessionRuntimeStore();
}

export const micRuntimeStore = createSessionRuntimeStore();
