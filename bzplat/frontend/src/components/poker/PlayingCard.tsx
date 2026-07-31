import { type ReactNode } from 'react'

/* ── 卡牌渲染 ──────────────────────────────────────────────────
 * 输入兼容两种格式：
 *  - 字符串 "As" / "Td" / "2c"（rank + suit：s=♠ h=♥ d=♦ c=♣）—— 平台事件用
 *  - 字符串 "<suit,rank>"（如 "<0,12>"，suit 0-3 rank 0-12）—— 旧协议用
 */

export interface ParsedCard {
  rank: string // "2".."A"
  suit: string // ♠ ♥ ♦ ♣
  red: boolean
}

const SUIT_BY_CHAR: Record<string, string> = { s: '♠', h: '♥', d: '♦', c: '♣' }
const SUIT_BY_NUM: Record<string, string> = { '0': '♠', '1': '♥', '2': '♦', '3': '♣' }
const RANK_BY_NUM = '23456789TJQKA'

function isRedSuit(suit: string): boolean {
  return suit === '♥' || suit === '♦'
}

export function parseCard(input: unknown): ParsedCard | null {
  if (typeof input !== 'string') return null
  const s = input.trim()
  if (!s) return null

  // <suit,rank> 形式
  const m = /^<(\d+),(\d+)>$/.exec(s)
  if (m) {
    const suit = SUIT_BY_NUM[m[1]] ?? '?'
    const ri = Number(m[2])
    const rank = RANK_BY_NUM[ri] ?? '?'
    return { rank, suit, red: isRedSuit(suit) }
  }
  // rank+suit 形式，如 "As" "Td" "2c"
  if (s.length >= 2) {
    const rank = s[0].toUpperCase()
    const suitCh = s[1].toLowerCase()
    const suit = SUIT_BY_CHAR[suitCh]
    if (suit && '23456789TJQKA'.includes(rank)) {
      return { rank, suit, red: isRedSuit(suit) }
    }
  }
  return null
}

interface CardProps {
  card?: string | null
  hidden?: boolean // 显示牌背
  size?: 'sm' | 'md' | 'lg'
  highlight?: boolean
  reveal?: boolean // hidden 但半透明亮出（摊牌）
}

const SIZE: Record<NonNullable<CardProps['size']>, string> = {
  sm: 'h-9 w-7 text-xs',
  md: 'h-14 w-10 text-base',
  lg: 'h-20 w-14 text-2xl',
}

export default function PlayingCard({ card, hidden, size = 'md', highlight, reveal }: CardProps) {
  if (hidden && !reveal) {
    return (
      <div
        className={`flex items-center justify-center rounded-md border border-brand-700/60 bg-gradient-to-br from-brand-700 to-brand-900 font-sans text-brand-200 shadow-md ${SIZE[size]}`}
      >
        ♠
      </div>
    )
  }
  const parsed = parseCard(card)
  if (!parsed) {
    return (
      <div
        className={`flex items-center justify-center rounded-md border border-dashed border-slate-500/50 bg-slate-900/40 text-slate-600 ${SIZE[size]}`}
      >
        ?
      </div>
    )
  }
  const ring = highlight ? 'ring-2 ring-amber-300' : ''
  const opa = reveal ? 'opacity-60' : ''
  return (
    <div
      className={`relative flex flex-col items-center justify-center rounded-md border border-slate-300 bg-white font-sans font-bold shadow-md ${ring} ${opa} ${SIZE[size]}`}
    >
      <span className={parsed.red ? 'leading-none text-error-600' : 'leading-none text-slate-900'}>
        {parsed.rank}
      </span>
      <span className={parsed.red ? 'leading-none text-error-600' : 'leading-none text-slate-900'}>
        {parsed.suit}
      </span>
    </div>
  )
}

/** 一组卡牌（手牌 / 公共牌），空槽位用占位 */
export function CardRow({
  cards,
  hidden,
  count,
  size,
  highlight,
  reveal,
}: {
  cards?: (string | null | undefined)[] | null
  hidden?: boolean
  count?: number
  size?: CardProps['size']
  highlight?: boolean
  reveal?: boolean
}) {
  const list = cards && cards.length ? cards : []
  const slots = count ? Math.max(count, list.length) : list.length || 0
  const nodes: ReactNode[] = []
  for (let i = 0; i < slots; i++) {
    const c = list[i]
    nodes.push(
      <PlayingCard
        key={i}
        card={c}
        hidden={hidden || !c}
        size={size}
        highlight={highlight}
        reveal={reveal}
      />,
    )
  }
  return <div className="flex gap-1">{nodes}</div>
}
