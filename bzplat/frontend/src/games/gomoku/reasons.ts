import { createTerminalReasonResolver } from '@/games/reasons'

export const gomokuTerminalReason = createTerminalReasonResolver({
  five: { label: '连成五子', tone: 'neutral' },
  board_full: { label: '棋盘已满，平局', tone: 'neutral' },
  double_pass: { label: '双方连续 PASS，平局', tone: 'neutral' },
  draw: { label: '平局', tone: 'neutral' },
  forbidden_overline: { label: '黑方长连禁手', tone: 'danger' },
  forbidden_double_four: { label: '黑方四四禁手', tone: 'danger' },
  forbidden_double_three: { label: '黑方三三禁手', tone: 'danger' },
  illegal_opening: { label: '指定开局不合法', tone: 'danger' },
  illegal_swap: { label: '交换动作不合法', tone: 'danger' },
  illegal_candidates: { label: '五手候选不合法', tone: 'danger' },
  illegal_selection: { label: '保留点选择不合法', tone: 'danger' },
  illegal: { label: '非法落子', tone: 'danger' },
  error: { label: '决策异常', tone: 'danger' },
})
