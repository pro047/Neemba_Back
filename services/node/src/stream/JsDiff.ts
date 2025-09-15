import { diffChars } from "diff";
import type { GetDiffPort } from "../ports/getDiff";

export class JsDiff implements GetDiffPort {
  diff(oldText: string, newText: string): Array<{ text: string }> {
    const chunk = diffChars(oldText, newText);

    return chunk.map((c) => ({
      text: c.value,
    }));
  }
}
