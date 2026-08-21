/** 侧边栏宽度夹取(220–400px),保证拖拽与持久化值恒在合法区间。 */
export function clampSidebarWidth(w: number): number {
  return Math.min(400, Math.max(220, Math.round(w)))
}
