export interface GetDiffPort {
  diff(oldText: string, newText: string): Array<{ text: string }>;
}
