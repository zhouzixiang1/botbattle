import assert from 'node:assert/strict'
import test from 'node:test'

import {
  defaultStageSeriesSettings,
  formatContestDuration,
  projectStageSeriesEstimate,
  stageSeriesSettingsValid,
  type StageSeriesConfig,
} from '../src/components/contest/stage-series.ts'

const configs: StageSeriesConfig[] = [{
  stage_key: 'prelim',
  label: '瑞士预赛',
  games_per_pair: { default: 2, allowed_values: [1, 2, 4] },
  swiss_extra_rounds: { default: 2, min: 0, max: 4 },
}]

test('defaults preserve valid persisted values and fill missing Swiss settings', () => {
  assert.deepEqual(defaultStageSeriesSettings(configs), {
    prelim: { games_per_pair: 2, swiss_extra_rounds: 2 },
  })
  assert.deepEqual(defaultStageSeriesSettings(configs, {
    prelim: { games_per_pair: 4 },
  }), {
    prelim: { games_per_pair: 4, swiss_extra_rounds: 2 },
  })
})

test('strict allowed values reject unsupported K and Swiss rounds', () => {
  assert.equal(stageSeriesSettingsValid(configs, {
    prelim: { games_per_pair: 4, swiss_extra_rounds: 4 },
  }), true)
  assert.equal(stageSeriesSettingsValid(configs, {
    prelim: { games_per_pair: 3, swiss_extra_rounds: 4 },
  }), false)
  assert.equal(stageSeriesSettingsValid(configs, {
    prelim: { games_per_pair: 2, swiss_extra_rounds: 5 },
  }), false)
})

test('projection updates opponent encounters, effective rounds, matches and ETA', () => {
  const projected = projectStageSeriesEstimate({
    stage_key: 'prelim',
    participant_count: 16,
    conceptual_pairings: 48,
    effective_rounds: 6,
    games_per_pair: 2,
    estimated_matches: 96,
    estimated_execution_legs: 96,
    eta_seconds: 13_440,
  }, {
    games_per_pair: 4,
    swiss_extra_rounds: 4,
  })
  assert.deepEqual(projected, {
    stage_key: 'prelim',
    participant_count: 16,
    conceptual_pairings: 64,
    effective_rounds: 8,
    games_per_pair: 4,
    estimated_matches: 256,
    estimated_execution_legs: 256,
    eta_seconds: 35_840,
  })
})

test('Swiss projection applies no-repeat coverage caps for small cohorts', () => {
  const fourPlayers = projectStageSeriesEstimate({
    stage_key: 'prelim',
    participant_count: 4,
    conceptual_pairings: 6,
    effective_rounds: 3,
    games_per_pair: 2,
    estimated_matches: 12,
    estimated_execution_legs: 12,
    eta_seconds: 1_680,
  }, {
    games_per_pair: 2,
    swiss_extra_rounds: 0,
  })
  assert.equal(fourPlayers?.effective_rounds, 2)
  assert.equal(fourPlayers?.conceptual_pairings, 4)
  assert.equal(fourPlayers?.estimated_matches, 8)

  const cappedFourPlayers = projectStageSeriesEstimate({
    ...fourPlayers!,
    conceptual_pairings: 6,
    effective_rounds: 3,
    estimated_matches: 12,
    estimated_execution_legs: 12,
    eta_seconds: 1_680,
  }, {
    games_per_pair: 4,
    swiss_extra_rounds: 4,
  })
  assert.equal(cappedFourPlayers?.effective_rounds, 3)
  assert.equal(cappedFourPlayers?.conceptual_pairings, 6)
  assert.equal(cappedFourPlayers?.estimated_matches, 24)

  const twoPlayers = projectStageSeriesEstimate({
    stage_key: 'prelim',
    participant_count: 2,
    conceptual_pairings: 1,
    effective_rounds: 1,
    games_per_pair: 2,
    estimated_matches: 2,
    estimated_execution_legs: 2,
    eta_seconds: 280,
  }, {
    games_per_pair: 4,
    swiss_extra_rounds: 4,
  })
  assert.equal(twoPlayers?.effective_rounds, 1)
  assert.equal(twoPlayers?.conceptual_pairings, 1)
  assert.equal(twoPlayers?.estimated_matches, 4)
})

test('duration rounds up to user-facing minutes', () => {
  assert.equal(formatContestDuration(undefined), '待估算')
  assert.equal(formatContestDuration(60), '约 1 分钟')
  assert.equal(formatContestDuration(43_960), '约 12 小时 13 分')
})
