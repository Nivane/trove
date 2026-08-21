// jsdom (via vitest) has no localStorage by default — provide a minimal one.
class MemoryStorage implements Storage {
  private store = new Map<string, string>()
  get length() { return this.store.size }
  clear() { this.store.clear() }
  getItem(k: string) { return this.store.has(k) ? this.store.get(k)! : null }
  key(i: number) { return Array.from(this.store.keys())[i] ?? null }
  removeItem(k: string) { this.store.delete(k) }
  setItem(k: string, v: string) { this.store.set(k, String(v)) }
}

if (!globalThis.localStorage) {
  Object.defineProperty(globalThis, 'localStorage', {
    value: new MemoryStorage(),
    configurable: true,
  })
}
