export class LcpStabilizer {
  private committedText = "";
  private recentHypotheses: string[] = [];
  private windowSize = 3;
  private requiredConfirmations = 2;
  private minGrowth = 4;

  onInterim(currentInterim: string): string | null {
    this.recentHypotheses.push(currentInterim);
    console.log("lcp - recent : ", this.recentHypotheses);
    console.log("--------------------------------------");

    if (this.recentHypotheses.length > this.windowSize) {
      this.recentHypotheses.shift();
    }

    const slice = this.recentHypotheses.slice(this.requiredConfirmations);
    console.log("lcp - slice :", slice);
    console.log("---------------------");

    const commonPrefix = this.getCommonPrefixOfArray(slice);

    console.log("lcp - common prefix", commonPrefix);
    console.log("----------------------------------");

    if (commonPrefix.length >= this.committedText.length + this.minGrowth) {
      const delta = commonPrefix.slice(this.committedText.length);
      this.committedText = commonPrefix;
      console.log("lcp - delta :", delta);
      console.log("--------------------");

      return delta;
    }

    return null;
  }

  private getCommonPrefixOfArray(items: string[]): string {
    if (items.length === 0) return "";
    let prefix = items[0]!;
    for (let i = 1; i < items.length; i++) {
      prefix = this.getLongestCommonPrefixLength(prefix, items[i]!);
      if (prefix.length === 0) break;
    }
    return prefix;
  }

  private getLongestCommonPrefixLength(a: string, b: string): string {
    const n = Math.min(a.length, b.length);
    let i = 0;
    while (i < n && a[i] === b[i]) i++;
    return a.slice(0, i);
  }
}
