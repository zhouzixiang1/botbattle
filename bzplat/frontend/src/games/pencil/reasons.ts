import { createTerminalReasonResolver } from '@/games/reasons'

export const pencilTerminalReason = createTerminalReasonResolver({
  majority: { label: '已取得过半格子', tone: 'neutral' },
  score: { label: '按最终得分判定', tone: 'neutral' },
  draw: { label: '最终得分相同，平局', tone: 'neutral' },
  illegal: { label: '非法连边', tone: 'danger' },
  error: { label: '决策异常', tone: 'danger' },
})
