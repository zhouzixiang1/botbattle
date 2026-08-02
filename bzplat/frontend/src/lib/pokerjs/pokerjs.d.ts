// bzplat/frontend/src/lib/pokerjs/pokerjs.d.ts
export {}
declare global {
  interface CanvasRenderingContext2D {
    drawPokerCard(x: number, y: number, size: number, suit: 'h'|'d'|'s'|'c', point: string): void
    drawPokerBack(x: number, y: number, size: number): void
  }
}
