export type TimeControlMode = 'per_decision' | 'per_side_total'
export type TimeControlAppliesTo = 'both_bots' | 'bot_only'

export interface TimeControlOption {
  id: string
  mode: TimeControlMode
  seconds: number
  applies_to: TimeControlAppliesTo
  label?: string
  is_default?: boolean
}

export interface TimeControlRegistry {
  game_id: string
  label?: string
  time_controls: TimeControlOption[]
  default_time_control_id: string
}

// Mirrors the backend TimeControlSpec contract: stable snake-case name plus a
// positive, non-zero version suffix.  Keeping this exact prevents the read side
// from accepting IDs that a later write/claim path must reject.
const CONTROL_ID = /^[a-z0-9]+(?:_[a-z0-9]+)*_v[1-9][0-9]*$/
const GAME_ID = /^[a-z0-9]+(?:_[a-z0-9]+)*$/
const REGISTRY_CONTROL_KEYS = new Set(['id', 'mode', 'seconds', 'applies_to', 'label', 'is_default'])
const MATCH_CONTROL_KEYS = new Set(['id', 'mode', 'seconds', 'applies_to'])
const GAME_REGISTRY_KEYS = new Set(['game_id', 'label', 'default_time_control_id', 'time_controls'])
const REGISTRY_RESPONSE_KEYS = new Set(['games', 'source', 'mutable'])

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function parseTimeControl(value: unknown): TimeControlOption | null {
  if (!isRecord(value)) return null
  if (Object.keys(value).some((key) => !REGISTRY_CONTROL_KEYS.has(key))) return null
  if (typeof value.id !== 'string' || !CONTROL_ID.test(value.id)) return null
  if (value.mode !== 'per_decision' && value.mode !== 'per_side_total') return null
  if (!Number.isInteger(value.seconds) || Number(value.seconds) < 1) return null
  if (value.applies_to !== 'both_bots' && value.applies_to !== 'bot_only') return null
  if (
    value.label !== undefined &&
    (typeof value.label !== 'string' || value.label.trim().length === 0 || value.label !== value.label.trim())
  ) return null
  if (value.is_default !== undefined && typeof value.is_default !== 'boolean') return null
  return {
    id: value.id,
    mode: value.mode,
    seconds: Number(value.seconds),
    applies_to: value.applies_to,
    ...(typeof value.label === 'string' && value.label.trim() ? { label: value.label.trim() } : {}),
    ...(typeof value.is_default === 'boolean' ? { is_default: value.is_default } : {}),
  }
}

/**
 * Parse the frozen match_start projection.  It is deliberately narrower than
 * registry metadata: display-only labels/default markers and unknown fields do
 * not belong in a replay event, and a control from another game must not drive
 * this reducer's clock.
 */
export function parseMatchTimeControl(value: unknown, gameId: string): TimeControlOption | null {
  if (!isRecord(value) || Object.keys(value).some((key) => !MATCH_CONTROL_KEYS.has(key))) return null
  const parsed = parseTimeControl(value)
  if (!parsed || !parsed.id.startsWith(`${gameId}_`)) return null
  return parsed
}

export function parseTimeControlRegistry(value: unknown): TimeControlRegistry | null {
  if (!isRecord(value) || Object.keys(value).some((key) => !GAME_REGISTRY_KEYS.has(key))) return null
  if (typeof value.game_id !== 'string' || !GAME_ID.test(value.game_id)) return null
  if (
    value.label !== undefined &&
    (typeof value.label !== 'string' || value.label.trim().length === 0 || value.label !== value.label.trim())
  ) return null
  if (
    typeof value.default_time_control_id !== 'string' ||
    !CONTROL_ID.test(value.default_time_control_id) ||
    !Array.isArray(value.time_controls)
  ) return null
  const controls = value.time_controls.map(parseTimeControl)
  if (controls.some((control) => control === null)) return null
  const parsed = controls as TimeControlOption[]
  if (parsed.length === 0 || new Set(parsed.map((control) => control.id)).size !== parsed.length) return null
  if (parsed.some((control) => !control.id.startsWith(`${value.game_id}_`))) return null
  if (parsed.some((control) => control.applies_to !== 'both_bots')) return null
  if (parsed.some((control) => typeof control.is_default !== 'boolean')) return null
  const defaults = parsed.filter((control) => control.is_default)
  if (defaults.length !== 1 || defaults[0]?.id !== value.default_time_control_id) return null
  return {
    game_id: value.game_id,
    ...(typeof value.label === 'string' ? { label: value.label } : {}),
    time_controls: parsed,
    default_time_control_id: value.default_time_control_id,
  }
}

export function parseTimeControlRegistries(value: unknown): TimeControlRegistry[] | null {
  if (!isRecord(value) || Object.keys(value).some((key) => !REGISTRY_RESPONSE_KEYS.has(key))) return null
  if (!Array.isArray(value.games) || value.source !== 'code' || value.mutable !== false) return null
  const registries = value.games.map(parseTimeControlRegistry)
  if (registries.some((registry) => registry === null)) return null
  const parsed = registries as TimeControlRegistry[]
  if (parsed.length === 0 || new Set(parsed.map((registry) => registry.game_id)).size !== parsed.length) return null
  if (parsed.some((registry) => registry.label === undefined)) return null
  return parsed
}

export function timeControlLabel(control: TimeControlOption): string {
  if (control.label) return control.label
  if (control.mode === 'per_decision') return `每步最多 ${control.seconds} 秒`
  const minutes = control.seconds / 60
  return Number.isInteger(minutes)
    ? `每方累计 ${minutes} 分钟`
    : `每方累计 ${control.seconds} 秒`
}

export function timeControlDescription(control: TimeControlOption, human = false): string {
  const scope = human || control.applies_to === 'bot_only'
    ? '只计 Bot 的完整请求到完整响应；真人沿用防挂机时限'
    : '双方对称计时，从完整请求交给已就绪 Bot 到完整响应到达'
  const reset = control.mode === 'per_decision' ? '每次决策重新计时' : '每局分别累计、下一局重置'
  return `${scope}；${reset}，排队、启动和容器预热不计入。`
}
