import assert from 'node:assert/strict'
import test from 'node:test'

import {
  parseContestFormatSnapshot,
  parseCrossGroupTiebreak,
  parseRankingCoordinates,
  parseStageFormatConfigs,
} from '../src/lib/contest-format.ts'

const DIGEST = 'a'.repeat(64)

test('cross-group tiebreak requires the exact six keys inside a bounded public envelope', () => {
  const crossGroup = {
    group_rank: 2,
    points_rate: 0.75,
    opponent_strength: 0.625,
    normalized_delta_rate: -0.25,
    technical_loss_rate: 0,
    draw_order: 7,
  }
  assert.deepEqual(parseCrossGroupTiebreak(crossGroup), crossGroup)
  assert.deepEqual(parseCrossGroupTiebreak({
    points: 8,
    buchholz: 22,
    buchholz_cut1: 17,
    sonneborn_berger: 12.5,
    head_to_head: 0.5,
    normalized_delta: -1,
    technical_losses: 0,
    seed: 7,
    ...crossGroup,
  }), crossGroup)
  assert.equal(parseCrossGroupTiebreak({
    points: 8,
    ...crossGroup,
  }), null)
  assert.equal(parseCrossGroupTiebreak({
    group_rank: 2,
    points_rate: 0.75,
    normalized_opponent_strength: 0.625,
    normalized_delta_per_game: -0.25,
    technical_loss_rate: 0,
    draw_order: 7,
  }), null)
  assert.equal(parseCrossGroupTiebreak({
    group_rank: 2,
    points_rate: 0.75,
    opponent_strength: 0.625,
    normalized_delta_rate: -0.25,
    technical_loss_rate: 0,
    draw_order: 7,
    private_seed: 'must-not-pass',
  }), null)
})

test('new cross-group coordinates fail closed instead of falling back to array rank', () => {
  assert.deepEqual(parseRankingCoordinates({
    rank: 3,
    overall_rank: 4,
    group_id: 'B',
    rank_in_group: 2,
  }, 'cross_group'), { overall_rank: 4, group_id: 'B', rank_in_group: 2 })
  assert.equal(parseRankingCoordinates({ rank: 3, group_id: 'B', rank_in_group: 2 }, 'cross_group'), null)
  assert.equal(parseRankingCoordinates({ rank: 3, overall_rank: 4, group_id: 'B' }, 'cross_group'), null)
  assert.equal(parseRankingCoordinates({ rank: 3, overall_rank: '4' }), null)
})

test('legacy group stages keep authoritative group rank without inventing overall rank', () => {
  assert.deepEqual(parseRankingCoordinates({ rank: 2, group_id: 'B' }, 'group_only'), {
    overall_rank: null,
    group_id: 'B',
    rank_in_group: 2,
  })
  assert.deepEqual(parseRankingCoordinates({
    rank: 8,
    overall_rank: 8,
    group_id: 'B',
    rank_in_group: 2,
  }, 'group_only'), {
    overall_rank: null,
    group_id: 'B',
    rank_in_group: 2,
  })
})

test('official results require the authoritative overall rank while retaining group coordinates', () => {
  assert.deepEqual(parseRankingCoordinates({
    rank: 9,
    overall_rank: 9,
    group_id: 'B',
    rank_in_group: 3,
  }, 'official'), {
    overall_rank: 9,
    group_id: 'B',
    rank_in_group: 3,
  })
  assert.equal(parseRankingCoordinates({
    rank: 9,
    group_id: 'B',
    rank_in_group: 3,
  }, 'official'), null)
  assert.equal(parseRankingCoordinates({
    rank: 3,
    overall_rank: 9,
    group_id: 'B',
    rank_in_group: 3,
  }, 'official'), null)
  assert.equal(parseRankingCoordinates({
    rank: 9,
    overall_rank: 9,
    group_id: 'B',
  }, 'official'), null)
})

test('canonical empty group sentinel keeps ordinary stage ranks available', () => {
  assert.deepEqual(parseRankingCoordinates({
    rank: 2,
    overall_rank: 2,
    group_id: '',
    rank_in_group: null,
  }), {
    overall_rank: 2,
    group_id: null,
    rank_in_group: null,
  })
  assert.equal(parseRankingCoordinates({ rank: 2, group_id: '' }, 'group_only'), null)
  assert.equal(parseRankingCoordinates({ rank: 2, overall_rank: 2, group_id: '' }, 'cross_group'), null)
  assert.equal(parseRankingCoordinates({ rank: 2, group_id: ' ' }), null)
})

test('stage format capability accepts only exact bounded group-count metadata', () => {
  assert.deepEqual(parseStageFormatConfigs([{
    stage_key: 'groups',
    field: 'group_count',
    min: 2,
    max: 8,
  }]), [{ stage_key: 'groups', field: 'group_count', min: 2, max: 8 }])
  assert.deepEqual(parseStageFormatConfigs(undefined), [])
  assert.equal(parseStageFormatConfigs([{
    stage_key: 'groups', field: 'group_count', min: 2, private_seed: 'must-not-pass',
  }]), null)
  assert.equal(parseStageFormatConfigs([{ stage_key: 'groups', field: 'group_count', min: 1 }]), null)
  assert.equal(parseStageFormatConfigs([{ stage_key: 'groups', field: 'group_count', min: 2, max: null }]), null)
  assert.equal(parseStageFormatConfigs([
    { stage_key: 'groups', field: 'group_count', min: 2 },
    { stage_key: 'groups', field: 'group_count', min: 3 },
  ]), null)
})

test('format snapshot exposes only bounded public audit fields', () => {
  const parsed = parseContestFormatSnapshot({
    version: 1,
    algorithm: 'protected_seed_random_balanced_v1',
    audit_digest: DIGEST,
    group_count: 4,
    group_size_min: 5,
    group_size_max: 6,
    group_sizes: { A: 6, B: 6, C: 5, D: 5 },
    expected_match_count: 156,
    source: {
      contest_id: 77,
      protected: [1, 2, 3, 4].map((sourceRank) => ({
        entry_id: sourceRank,
        user_id: sourceRank + 10,
        source_entry_id: sourceRank + 20,
        source_rank: sourceRank,
      })),
    },
  })
  assert.equal(parsed?.source?.contest_id, 77)
  assert.deepEqual(parsed?.group_sizes, { A: 6, B: 6, C: 5, D: 5 })
  assert.equal(parseContestFormatSnapshot({ ...parsed, private_seed: 'must-not-pass' }), null)
  assert.equal(parseContestFormatSnapshot({ ...parsed, groups: { A: [1] } }), null)
  assert.equal(parseContestFormatSnapshot({ ...parsed, draw_order: [1, 2, 3, 4] }), null)
})

test('format snapshot rejects malformed digest, topology, and source shape', () => {
  const base = {
    version: 1,
    algorithm: 'secure_random_balanced_v1',
    audit_digest: DIGEST,
    group_count: 2,
    group_size_min: 2,
    group_size_max: 3,
    group_sizes: { A: 2, B: 3 },
  }
  assert.ok(parseContestFormatSnapshot(base))
  assert.equal(parseContestFormatSnapshot({ ...base, audit_digest: 'not-a-digest' }), null)
  assert.equal(parseContestFormatSnapshot({ ...base, group_sizes: { A: 2 } }), null)
  assert.equal(parseContestFormatSnapshot({ ...base, source: { contest_id: 1, protected: [] } }), null)
  assert.equal(parseContestFormatSnapshot({
    version: 1,
    algorithm: 'protected_seed_random_balanced_v1',
    audit_digest: DIGEST,
    group_count: 4,
    group_size_min: 5,
    group_size_max: 6,
    source: {
      contest_id: 1,
      protected: [1, 2, 2, 4].map((sourceRank, index) => ({
        entry_id: index + 1,
        user_id: index + 11,
        source_entry_id: index + 21,
        source_rank: sourceRank,
      })),
    },
  }), null)
  assert.equal(parseContestFormatSnapshot({
    version: 1,
    algorithm: 'protected_seed_random_balanced_v1',
    audit_digest: DIGEST,
    group_count: 4,
    group_size_min: 5,
    group_size_max: 6,
    source: {
      contest_id: 1,
      protected: [2, 1, 4, 3].map((sourceRank, index) => ({
        entry_id: index + 1,
        user_id: index + 11,
        source_entry_id: index + 21,
        source_rank: sourceRank,
      })),
    },
  }), null)
  assert.equal(parseContestFormatSnapshot({
    version: 1,
    algorithm: 'protected_seed_random_balanced_v1',
    audit_digest: DIGEST,
    group_count: 5,
    group_size_min: 5,
    group_size_max: 5,
    source: {
      contest_id: 1,
      protected: [1, 2, 3, 4].map((sourceRank) => ({
        entry_id: sourceRank,
        user_id: sourceRank + 10,
        source_entry_id: sourceRank + 20,
        source_rank: sourceRank,
      })),
    },
  }), null)
  assert.equal(parseContestFormatSnapshot({
    ...base,
    group_count: 65,
    group_size_min: 2,
    group_size_max: 2,
    group_sizes: Object.fromEntries(Array.from({ length: 65 }, (_, index) => [`G${index + 1}`, 2])),
  }), null)
})
