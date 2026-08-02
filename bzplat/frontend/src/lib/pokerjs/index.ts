// bzplat/frontend/src/lib/pokerjs/index.ts
let injected = false
const SRC = `/* eslint-disable */
// @ts-nocheck
` + (await import('./poker.min.js?raw')).default

/** 把 Poker.JS 的 drawPokerCard/drawPokerBack 挂到 ctx 的 prototype（幂等）。 */
export function ensurePokerJS(ctx: CanvasRenderingContext2D): void {
  if (injected) return
  // Poker.JS 自执行函数会给 CanvasRenderingContext2D.prototype 挂方法
  // 用 new Function 在全局作用域执行一次
  // eslint-disable-next-line @typescript-eslint/no-implied-eval
  const fn = new Function(SRC)
  fn.call(globalThis)
  injected = true
  void ctx
}
