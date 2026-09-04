import assert from 'node:assert/strict'
import test from 'node:test'

import { reduceGomokuEvents } from '../src/games/gomoku/reducer.ts'
import { reducePencilEvents } from '../src/games/pencil/reducer.ts'

const PENCIL_DECISION = {
  id: 'pencil_per_decision_1s_v1',
  mode: 'per_decision',
  seconds: 1,
  applies_to: 'both_bots',
} as const

test('board reducers initialize frozen totals and reset each authoritative decision', () => {
  const pencil = reducePencilEvents([
    { type: 'match_start', n_dots: 6, size: 11, time_control: PENCIL_DECISION },
    { type: 'turn', player: 0, pass_: 0 },
    { type: 'time_used', seat: 0, used: 0.7, remaining: 0.3, budget: 1 },
    { type: 'turn', player: 0, pass_: 0 },
  ])
  assert.deepEqual(pencil.timeUsed, [0, 0])
  assert.deepEqual(pencil.timeRemaining, [1, 1])

  const gomoku = reduceGomokuEvents([{
    type: 'match_start',
    size: 15,
    time_control: {
      id: 'gomoku_per_side_total_300s_v1',
      mode: 'per_side_total',
      seconds: 300,
      applies_to: 'both_bots',
    },
  }])
  assert.equal(gomoku.timeBudget, 300)
  assert.deepEqual(gomoku.timeRemaining, [300, 300])
})

test('Bot-only clocks never invent a game clock for the human seat', () => {
  const pencil = reducePencilEvents([
    {
      type: 'match_start',
      n_dots: 6,
      size: 11,
      time_control: { ...PENCIL_DECISION, applies_to: 'bot_only' },
    },
    { type: 'turn', player: 0, pass_: 0 },
    { type: 'time_used', seat: 0, used: 0.4, remaining: 0.6, budget: 1 },
    { type: 'turn', player: 1, pass_: 0 },
  ])
  assert.deepEqual(pencil.timeUsed, [0.4, null])
  assert.deepEqual(pencil.timeRemaining, [0.6, null])

  const gomoku = reduceGomokuEvents([
    {
      type: 'match_start',
      size: 15,
      time_control: {
        id: 'gomoku_per_side_total_300s_v1',
        mode: 'per_side_total',
        seconds: 300,
        applies_to: 'bot_only',
      },
    },
    { type: 'time_used', seat: 0, used: 1, remaining: 299, budget: 300 },
  ])
  assert.equal(gomoku.timeBudget, 300)
  assert.deepEqual(gomoku.timeRemaining, [299, null])
})

test('malformed frozen controls cannot be rescued by legacy budget fields', () => {
  const pencil = reducePencilEvents([
    {
      type: 'match_start',
      n_dots: 6,
      size: 11,
      // The backend emits this bounded sentinel when either the stored event
      // clock or the authoritative Match config is damaged.
      time_control: null,
    },
    { type: 'time_used', seat: 0, used: 1, remaining: 899, budget: 900 },
  ])
  assert.equal(pencil.timeRemaining, null)

  const gomoku = reduceGomokuEvents([
    {
      type: 'match_start',
      size: 15,
      time_control: null,
    },
    { type: 'time_used', seat: 0, used: 1, remaining: 899, budget: 900 },
  ])
  assert.equal(gomoku.timeBudget, null)
  assert.deepEqual(gomoku.timeRemaining, [null, null])
})
