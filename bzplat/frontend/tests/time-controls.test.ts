import assert from 'node:assert/strict'
import test from 'node:test'

import {
  parseTimeControl,
  parseMatchTimeControl,
  parseTimeControlRegistry,
  parseTimeControlRegistries,
  timeControlDescription,
  timeControlLabel,
} from '../src/lib/time-controls.ts'

test('time-control parser accepts the stable public contract', () => {
  assert.deepEqual(parseTimeControl({
    id: 'pencil_per_decision_1s_v1',
    mode: 'per_decision',
    seconds: 1,
    applies_to: 'both_bots',
    is_default: false,
  }), {
    id: 'pencil_per_decision_1s_v1',
    mode: 'per_decision',
    seconds: 1,
    applies_to: 'both_bots',
    is_default: false,
  })
})

test('time-control registry fails closed on malformed or inconsistent values', () => {
  assert.equal(parseTimeControl({ id: 'bad', mode: 'arbitrary', seconds: 1, applies_to: 'both_bots' }), null)
  assert.equal(parseTimeControl({ id: 'bad', mode: 'per_decision', seconds: 0, applies_to: 'both_bots' }), null)
  assert.equal(parseTimeControl({
    id: 'pencil_per_decision_1s_v1',
    mode: 'per_decision',
    seconds: 1,
    applies_to: 'both_bots',
    private_seed: 'must-not-pass',
  }), null)
  for (const id of [
    'pencil_per_decision_1s',
    ' pencil_per_decision_1s_v1',
    'pencil_per_decision_1s_v0',
    'pencil_per_decision_1s_v01',
    'pencil__per_decision_1s_v1',
  ]) {
    assert.equal(parseTimeControl({ id, mode: 'per_decision', seconds: 1, applies_to: 'both_bots' }), null)
  }
  assert.equal(parseTimeControlRegistry({
    game_id: 'pencil',
    default_time_control_id: 'missing',
    time_controls: [{
      id: 'pencil_per_decision_1s_v1',
      mode: 'per_decision',
      seconds: 1,
      applies_to: 'both_bots',
      is_default: true,
    }],
  }), null)
  assert.equal(parseTimeControlRegistry({
    game_id: 'pencil',
    default_time_control_id: 'pencil_per_decision_1s_v1',
    time_controls: [{
      id: 'gomoku_per_decision_1s_v1',
      mode: 'per_decision',
      seconds: 1,
      applies_to: 'both_bots',
      is_default: true,
    }],
  }), null)
  assert.equal(parseTimeControlRegistry({
    game_id: 'pencil',
    default_time_control_id: 'pencil_per_decision_1s_v1',
    time_controls: [{
      id: 'pencil_per_decision_1s_v1',
      mode: 'per_decision',
      seconds: 1,
      applies_to: 'bot_only',
      is_default: true,
    }],
  }), null)
})

test('match event parser accepts only exact same-game clock fields', () => {
  const control = {
    id: 'pencil_per_decision_1s_v1',
    mode: 'per_decision',
    seconds: 1,
    applies_to: 'both_bots',
  } as const
  assert.deepEqual(parseMatchTimeControl(control, 'pencil'), control)
  assert.equal(parseMatchTimeControl({ ...control, id: 'gomoku_per_decision_1s_v1' }, 'pencil'), null)
  assert.equal(parseMatchTimeControl({ ...control, label: 'not a replay field' }, 'pencil'), null)
  assert.equal(parseMatchTimeControl({ ...control, private_seed: 'must-not-pass' }, 'pencil'), null)
})

test('game registry rejects one malformed game instead of exposing a partial selector', () => {
  assert.equal(parseTimeControlRegistries({
    games: [
      {
        game_id: 'pencil',
        label: '点格棋',
        default_time_control_id: 'pencil_per_decision_1s_v1',
        time_controls: [{
          id: 'pencil_per_decision_1s_v1',
          mode: 'per_decision',
          seconds: 1,
          applies_to: 'both_bots',
          is_default: true,
        }],
      },
      { game_id: 'gomoku', label: '五子棋', default_time_control_id: 'missing', time_controls: [] },
    ],
    source: 'code',
    mutable: false,
  }), null)
})

test('public game registry requires exact immutable code-owned metadata', () => {
  const valid = {
    games: [{
      game_id: 'pencil',
      label: '点格棋',
      default_time_control_id: 'pencil_per_side_total_900s_v1',
      time_controls: [{
        id: 'pencil_per_side_total_900s_v1',
        mode: 'per_side_total',
        seconds: 900,
        applies_to: 'both_bots',
        is_default: true,
      }, {
        id: 'pencil_per_decision_1s_v1',
        mode: 'per_decision',
        seconds: 1,
        applies_to: 'both_bots',
        is_default: false,
      }],
    }],
    source: 'code',
    mutable: false,
  } as const
  assert.deepEqual(parseTimeControlRegistries(valid), valid.games)
  assert.equal(parseTimeControlRegistries({ ...valid, source: 'database' }), null)
  assert.equal(parseTimeControlRegistries({ ...valid, mutable: true }), null)
  assert.equal(parseTimeControlRegistries({ ...valid, private_seed: 'must-not-pass' }), null)
  assert.equal(parseTimeControlRegistries({
    ...valid,
    games: [{ ...valid.games[0], label: ' 点格棋' }],
  }), null)
  assert.equal(parseTimeControlRegistries({
    ...valid,
    games: [{ ...valid.games[0], internal_name: 'must-not-pass' }],
  }), null)
})

test('time-control copy distinguishes cumulative and human Bot-only timing', () => {
  assert.equal(timeControlLabel({
    id: 'gomoku_per_side_total_300s_v1',
    mode: 'per_side_total',
    seconds: 300,
    applies_to: 'both_bots',
  }), '每方累计 5 分钟')
  assert.match(timeControlDescription({
    id: 'pencil_per_decision_1s_v1',
    mode: 'per_decision',
    seconds: 1,
    applies_to: 'both_bots',
  }, true), /只计 Bot/)
})
