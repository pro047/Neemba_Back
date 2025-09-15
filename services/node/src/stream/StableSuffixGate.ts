export class StableSuffixGate {
    private history: string[] = [];
    constructor(private readonly windowSize = 4) {}

    update(currentText?: string | null): number {
        if (currentText == null || currentText == '') return -1;

        this.history.push(currentText);
        
        if (this.history.length > this.windowSize) this.history.shift();

        if (this.history.length < 2) return -1;

        const shortest = Math.min(...this.history.map(t => t.length));
        const baseline = this.history[0];
        
        let index = 0;
        for (; index < shortest; index++) {
            const baseChar = baseline?.charAt(index);
            const allMatch = this.history.every(s => s.charAt(index) === baseChar)
            if (!allMatch) break;
        }
        
        return index;
    }

    reset() { this.history = []}
}