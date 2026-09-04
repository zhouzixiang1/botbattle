export type GroupDrawAlgorithm =
  | 'secure_random_balanced_v1'
  | 'protected_seed_random_balanced_v1'

export interface CrossGroupTiebreak {
  group_rank: number
  points_rate: number
  opponent_strength: number
  normalized_delta_rate: number
  technical_loss_rate: number
  draw_order: number
}

export interface ProtectedSeedAudit {
  entry_id: number
  user_id: number
  source_entry_id: number
  source_rank: number
}

export interface ContestFormatSnapshot {
  version: 1
  algorithm: GroupDrawAlgorithm
  audit_digest: string
  group_count: number
  group_size_min: number
  group_size_max: number
  group_sizes?: Record<string, number>
  expected_match_count?: number
  source?: {
    contest_id: number
    protected: ProtectedSeedAudit[]
  }
}

export interface RankingCoordinates {
  overall_rank: number | null
  group_id: string | null
  rank_in_group: number | null
}

export type RankingCoordinateMode = 'overall' | 'group_only' | 'cross_group' | 'official'

export interface StageFormatConfig {
  stage_key: string
  field: 'group_count'
  min: number
  max?: number
}

const CROSS_GROUP_TIEBREAK_KEYS = [
  'group_rank',
  'points_rate',
  'opponent_strength',
  'normalized_delta_rate',
  'technical_loss_rate',
  'draw_order',
] as const

// The backend appends the six cross-group coordinates to its existing public
// tie-break envelope.  Accept either the focused six-key projection or that
// complete legacy envelope, but never aliases, partial legacy fields, or
// arbitrary persisted payload keys.
const LEGACY_TIEBREAK_KEYS = [
  'points',
  'buchholz',
  'buchholz_cut1',
  'sonneborn_berger',
  'head_to_head',
  'normalized_delta',
  'technical_losses',
  'seed',
] as const

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function hasOnlyKeys(value: Record<string, unknown>, allowed: readonly string[]): boolean {
  const allow = new Set(allowed)
  return Object.keys(value).every((key) => allow.has(key))
}

function positiveInteger(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 1
}

function finiteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

/** Parse the organizer-editable stage-format capability advertised by a template. */
export function parseStageFormatConfigs(value: unknown): StageFormatConfig[] | null {
  if (value === undefined) return []
  if (!Array.isArray(value)) return null
  const parsed: StageFormatConfig[] = []
  const seen = new Set<string>()
  for (const config of value) {
    if (!isRecord(config) || !hasOnlyKeys(config, ['stage_key', 'field', 'min', 'max'])) return null
    if (
      typeof config.stage_key !== 'string'
      || !/^[a-z][a-z0-9_]*$/.test(config.stage_key)
      || config.field !== 'group_count'
      || !positiveInteger(config.min)
      || config.min < 2
      || (config.max !== undefined && (!positiveInteger(config.max) || config.max < config.min))
      || seen.has(config.stage_key)
    ) return null
    seen.add(config.stage_key)
    parsed.push({
      stage_key: config.stage_key,
      field: 'group_count',
      min: config.min,
      ...(config.max !== undefined ? { max: config.max } : {}),
    })
  }
  return parsed
}

/** Parse only the six frozen cross-group keys; legacy aliases are never read. */
export function parseCrossGroupTiebreak(value: unknown): CrossGroupTiebreak | null {
  if (!isRecord(value) || !hasOnlyKeys(value, [
    ...CROSS_GROUP_TIEBREAK_KEYS,
    ...LEGACY_TIEBREAK_KEYS,
  ])) return null
  const legacyFieldsPresent = LEGACY_TIEBREAK_KEYS.filter((key) => value[key] !== undefined)
  if (legacyFieldsPresent.length !== 0 && legacyFieldsPresent.length !== LEGACY_TIEBREAK_KEYS.length) return null
  if (legacyFieldsPresent.length === LEGACY_TIEBREAK_KEYS.length) {
    if (
      !finiteNumber(value.points)
      || !finiteNumber(value.buchholz)
      || !finiteNumber(value.buchholz_cut1)
      || !finiteNumber(value.sonneborn_berger)
      || !finiteNumber(value.head_to_head)
      || !finiteNumber(value.normalized_delta)
      || typeof value.technical_losses !== 'number'
      || !Number.isInteger(value.technical_losses)
      || value.technical_losses < 0
      || typeof value.seed !== 'number'
      || !Number.isInteger(value.seed)
      || value.seed < 0
    ) return null
  }
  if (!positiveInteger(value.group_rank) || !positiveInteger(value.draw_order)) return null
  if (
    !finiteNumber(value.points_rate)
    || value.points_rate < 0
    || value.points_rate > 1
    || !finiteNumber(value.opponent_strength)
    || value.opponent_strength < 0
    || value.opponent_strength > 1
    || !finiteNumber(value.normalized_delta_rate)
    || !finiteNumber(value.technical_loss_rate)
    || value.technical_loss_rate < 0
    || value.technical_loss_rate > 1
  ) return null
  return {
    group_rank: value.group_rank,
    points_rate: value.points_rate,
    opponent_strength: value.opponent_strength,
    normalized_delta_rate: value.normalized_delta_rate,
    technical_loss_rate: value.technical_loss_rate,
    draw_order: value.draw_order,
  }
}

/**
 * Interpret backend ranks only under the frozen stage marker.  New cross-group
 * formats require both coordinates; legacy group stages keep `rank` as their
 * authoritative group rank and never invent an overall rank.
 */
export function parseRankingCoordinates(
  value: unknown,
  mode: RankingCoordinateMode = 'overall',
): RankingCoordinates | null {
  if (!isRecord(value)) return null
  if (!positiveInteger(value.rank)) return null
  if (
    mode === 'official'
    && (
      !positiveInteger(value.overall_rank)
      || value.overall_rank !== value.rank
    )
  ) return null
  // The backend's canonical non-grouped sentinel is the exact empty string.
  // Treat it as ungrouped while keeping whitespace, non-string and malformed
  // group coordinates fail-closed below.
  const grouped = value.group_id !== undefined && value.group_id !== null && value.group_id !== ''
  if (grouped) {
    if (
      typeof value.group_id !== 'string'
      || value.group_id.trim() !== value.group_id
      || value.group_id.length === 0
    ) return null
    if (mode === 'cross_group') {
      if (!positiveInteger(value.overall_rank) || !positiveInteger(value.rank_in_group)) return null
      return {
        overall_rank: value.overall_rank,
        group_id: value.group_id,
        rank_in_group: value.rank_in_group,
      }
    }
    if (mode === 'group_only') {
      if (value.rank_in_group !== undefined && value.rank_in_group !== null && !positiveInteger(value.rank_in_group)) return null
      return {
        overall_rank: null,
        group_id: value.group_id,
        rank_in_group: positiveInteger(value.rank_in_group) ? value.rank_in_group : value.rank,
      }
    }
    if (mode === 'official' && !positiveInteger(value.rank_in_group)) return null
    if (value.overall_rank !== undefined && value.overall_rank !== null && !positiveInteger(value.overall_rank)) return null
    if (value.rank_in_group !== undefined && value.rank_in_group !== null && !positiveInteger(value.rank_in_group)) return null
    return {
      overall_rank: positiveInteger(value.overall_rank) ? value.overall_rank : value.rank,
      group_id: value.group_id,
      rank_in_group: positiveInteger(value.rank_in_group) ? value.rank_in_group : null,
    }
  }
  if (mode === 'cross_group' || mode === 'group_only') return null
  if (value.rank_in_group !== undefined && value.rank_in_group !== null) return null
  if (value.overall_rank !== undefined && value.overall_rank !== null && !positiveInteger(value.overall_rank)) return null
  return {
    overall_rank: positiveInteger(value.overall_rank) ? value.overall_rank : value.rank,
    group_id: null,
    rank_in_group: null,
  }
}

export function parseContestFormatSnapshot(value: unknown): ContestFormatSnapshot | null {
  if (!isRecord(value) || !hasOnlyKeys(value, [
    'version',
    'algorithm',
    'audit_digest',
    'group_count',
    'group_size_min',
    'group_size_max',
    'group_sizes',
    'expected_match_count',
    'source',
  ])) return null
  if (value.version !== 1) return null
  if (
    value.algorithm !== 'secure_random_balanced_v1'
    && value.algorithm !== 'protected_seed_random_balanced_v1'
  ) return null
  if (typeof value.audit_digest !== 'string' || !/^[a-f0-9]{64}$/.test(value.audit_digest)) return null
  if (!positiveInteger(value.group_count) || value.group_count < 2) return null
  if (
    !positiveInteger(value.group_size_min)
    || value.group_size_min < 2
    || !positiveInteger(value.group_size_max)
    || value.group_size_max < value.group_size_min
    || value.group_size_max - value.group_size_min > 1
  ) return null

  let groupSizes: Record<string, number> | undefined
  if (value.group_sizes !== undefined) {
    if (value.group_count > 64) return null
    if (!isRecord(value.group_sizes) || Object.keys(value.group_sizes).length !== value.group_count) return null
    groupSizes = {}
    for (const [groupId, size] of Object.entries(value.group_sizes)) {
      if (!groupId || groupId.trim() !== groupId || !positiveInteger(size) || size < 2) return null
      groupSizes[groupId] = size
    }
    const sizes = Object.values(groupSizes)
    if (Math.min(...sizes) !== value.group_size_min || Math.max(...sizes) !== value.group_size_max) return null
  }

  const expectedMatchCount = value.expected_match_count
  if (expectedMatchCount !== undefined && !positiveInteger(expectedMatchCount)) return null

  let source: ContestFormatSnapshot['source']
  if (value.source !== undefined) {
    if (!isRecord(value.source) || !hasOnlyKeys(value.source, ['contest_id', 'protected'])) return null
    if (!positiveInteger(value.source.contest_id) || !Array.isArray(value.source.protected)) return null
    if (value.source.protected.length < 4 || value.source.protected.length > 5) return null
    const protectedSeeds: ProtectedSeedAudit[] = []
    for (const row of value.source.protected) {
      if (!isRecord(row) || !hasOnlyKeys(row, ['entry_id', 'user_id', 'source_entry_id', 'source_rank'])) return null
      if (
        !positiveInteger(row.entry_id)
        || !positiveInteger(row.user_id)
        || !positiveInteger(row.source_entry_id)
        || !positiveInteger(row.source_rank)
      ) return null
      protectedSeeds.push({
        entry_id: row.entry_id,
        user_id: row.user_id,
        source_entry_id: row.source_entry_id,
        source_rank: row.source_rank,
      })
    }
    if (
      new Set(protectedSeeds.map((row) => row.entry_id)).size !== protectedSeeds.length
      || new Set(protectedSeeds.map((row) => row.user_id)).size !== protectedSeeds.length
      || new Set(protectedSeeds.map((row) => row.source_entry_id)).size !== protectedSeeds.length
      || new Set(protectedSeeds.map((row) => row.source_rank)).size !== protectedSeeds.length
      || protectedSeeds.some((row, index) => index > 0 && protectedSeeds[index - 1]!.source_rank > row.source_rank)
    ) return null
    source = { contest_id: value.source.contest_id, protected: protectedSeeds }
  }
  if (
    value.algorithm === 'protected_seed_random_balanced_v1'
    && (!source || source.protected.length !== value.group_count)
  ) return null
  if (value.algorithm === 'secure_random_balanced_v1' && source) return null

  return {
    version: 1,
    algorithm: value.algorithm,
    audit_digest: value.audit_digest,
    group_count: value.group_count,
    group_size_min: value.group_size_min,
    group_size_max: value.group_size_max,
    ...(groupSizes ? { group_sizes: groupSizes } : {}),
    ...(expectedMatchCount !== undefined ? { expected_match_count: expectedMatchCount } : {}),
    ...(source ? { source } : {}),
  }
}

export function formatDrawAlgorithm(algorithm: GroupDrawAlgorithm): string {
  return algorithm === 'protected_seed_random_balanced_v1'
    ? '保护种子安全随机均衡分组'
    : '安全随机均衡分组'
}
