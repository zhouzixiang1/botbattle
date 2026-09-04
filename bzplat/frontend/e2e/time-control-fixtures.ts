export const HOLDEM_TEMPLATE_TIME_CONTROL = {
  time_controls: [{
    id: 'holdem_per_decision_60s_v1',
    mode: 'per_decision',
    seconds: 60,
    applies_to: 'both_bots',
    label: '每次决策 60 秒',
    is_default: true,
  }],
  default_time_control_id: 'holdem_per_decision_60s_v1',
} as const

export const GOMOKU_TEMPLATE_TIME_CONTROLS = {
  time_controls: [{
    id: 'gomoku_per_side_total_900s_v1',
    mode: 'per_side_total',
    seconds: 900,
    applies_to: 'both_bots',
    label: '每方累计 15 分钟',
    is_default: true,
  }, {
    id: 'gomoku_per_side_total_300s_v1',
    mode: 'per_side_total',
    seconds: 300,
    applies_to: 'both_bots',
    label: '每方累计 5 分钟',
    is_default: false,
  }],
  default_time_control_id: 'gomoku_per_side_total_900s_v1',
} as const

export const PENCIL_TEMPLATE_TIME_CONTROLS = {
  time_controls: [{
    id: 'pencil_per_side_total_900s_v1',
    mode: 'per_side_total',
    seconds: 900,
    applies_to: 'both_bots',
    label: '每方累计 15 分钟',
    is_default: true,
  }, {
    id: 'pencil_per_decision_1s_v1',
    mode: 'per_decision',
    seconds: 1,
    applies_to: 'both_bots',
    label: '每步最多 1 秒',
    is_default: false,
  }],
  default_time_control_id: 'pencil_per_side_total_900s_v1',
} as const

export const GAME_TIME_CONTROL_REGISTRY_RESPONSE = {
  games: [{
    game_id: 'holdem',
    label: '德州扑克',
    ...HOLDEM_TEMPLATE_TIME_CONTROL,
  }, {
    game_id: 'gomoku',
    label: '五子棋',
    ...GOMOKU_TEMPLATE_TIME_CONTROLS,
  }, {
    game_id: 'pencil',
    label: '点格棋',
    ...PENCIL_TEMPLATE_TIME_CONTROLS,
  }],
  source: 'code',
  mutable: false,
} as const
