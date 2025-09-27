let currentSessionId: string = "";

export const setSessionId = (sessionId: string): void => {
  currentSessionId = sessionId;
};

export const getSessionId = (): string => {
  return currentSessionId;
};

export const removeSessionId = (): void => {
  currentSessionId = "";
};
