import { createTerminalReasonResolver } from '@/games/reasons'

export const gomokuTerminalReason = createTerminalReasonResolver({
  five: { label: '连成五子', tone: 'neutral' },
  draw: { label: '棋盘下满，平局', tone: 'neutral' },
  illegal: { label: '非法落子', tone: 'danger' },
  error: { label: '决策异常', tone: 'danger' },
})
