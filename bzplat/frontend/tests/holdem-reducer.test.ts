import assert from 'node:assert/strict'
import test from 'node:test'

import { reduceHoldemEvents } from '../src/games/holdem/reducer.ts'

test('duplicate second scoring game resets current stats but preserves actual combined chips', () => {
  const firstGame = reduceHoldemEvents([
    { type: 'match_start', leg: 0 },
    { type: 'hand_start', leg: 0, hand: 0, sb: 0, chips: [19950, 19900] },
    { type: 'settle', leg: 0, hand: 0, winners: [0], deltas: [100, -100] },
    { type: 'match_start', leg: 1 },
  ])
  assert.equal(firstGame.isDuplicate, true)
  assert.equal(firstGame.leg, 1)
  assert.deepEqual(firstGame.seats.map((seat) => seat.net), [0, 0])
  assert.deepEqual(firstGame.combinedNets, [100, -100])
  assert.equal(firstGame.currentGameCompletedHands, 0)

  const technicalSecondGame = reduceHoldemEvents([
    { type: 'match_start', leg: 0 },
    { type: 'hand_start', leg: 0, hand: 0, sb: 0, chips: [19950, 19900] },
    { type: 'settle', leg: 0, hand: 0, winners: [0], deltas: [100, -100] },
    { type: 'match_start', leg: 1 },
    { type: 'hand_start', leg: 1, hand: 0, sb: 0, chips: [19950, 19900] },
    { type: 'settle', leg: 1, hand: 0, winners: [0], deltas: [50, -50] },
    { type: 'match_end', leg: 1, winner: 1, reason: 'protocol_error', deltas: [-1, 1] },
  ])
  // leg=1 swaps engine seats back to the fixed physical Bot identities.
  assert.deepEqual(technicalSecondGame.seats.map((seat) => seat.net), [-50, 50])
  assert.deepEqual(technicalSecondGame.combinedNets, [50, -50])
  assert.equal(technicalSecondGame.currentGameCompletedHands, 1)
  assert.equal(technicalSecondGame.matchWinner, 1)
})

test('a new duplicate scoring game clears an older raw leg terminal state', () => {
  const state = reduceHoldemEvents([
    { type: 'match_start', leg: 0 },
    { type: 'hand_start', leg: 0, hand: 0, sb: 0, chips: [19_950, 19_900] },
    { type: 'settle', leg: 0, hand: 0, winners: [0], deltas: [100, -100] },
    // Current public replay filters engine-level match_end events.  Keep this
    // raw/historical shape as a compatibility regression: the next explicit
    // leg boundary must still reopen the view model.
    { type: 'match_end', leg: 0, winner: 0, reason: 'completed' },
    { type: 'match_start', leg: 1 },
    { type: 'hand_start', leg: 1, hand: 0, sb: 0, chips: [19_950, 19_900] },
  ])

  assert.equal(state.leg, 1)
  assert.equal(state.legStarted, true)
  assert.equal(state.matchOver, false)
  assert.equal(state.matchWinner, null)
  assert.equal(state.status, 'live')
  assert.equal(state.currentGameHandsStarted, 1)
  assert.equal(state.currentGameCompletedHands, 0)
  assert.deepEqual(state.seats.map((seat) => seat.net), [0, 0])
  assert.deepEqual(state.combinedNets, [100, -100])
})

test('a zero-hand duplicate technical terminal remains visibly duplicate without fake chip delta', () => {
  const state = reduceHoldemEvents([
    { type: 'match_start', leg: 0 },
    { type: 'match_end', leg: 0, winner: 1, reason: 'protocol_error', deltas: [-1, 1] },
  ])
  assert.equal(state.isDuplicate, true)
  assert.equal(state.totalLegs, 2)
  assert.equal(state.completedHands, 0)
  assert.deepEqual(state.seats.map((seat) => seat.net), [0, 0])
  assert.deepEqual(state.combinedNets, [0, 0])
})

test('a second-game zero-hand technical terminal keeps first-game totals but resets current stats', () => {
  const state = reduceHoldemEvents([
    { type: 'match_start', leg: 0 },
    { type: 'hand_start', leg: 0, hand: 0, sb: 0, chips: [19_950, 19_900] },
    { type: 'settle', leg: 0, hand: 0, winners: [0], deltas: [100, -100] },
    { type: 'match_start', leg: 1 },
    { type: 'match_end', leg: 1, winner: 1, reason: 'protocol_error', deltas: [0, 0] },
  ])
  assert.equal(state.isDuplicate, true)
  assert.equal(state.leg, 1)
  assert.equal(state.matchOver, true)
  assert.equal(state.currentGameHandsStarted, 0)
  assert.equal(state.currentGameCompletedHands, 0)
  assert.equal(state.completedHands, 1)
  assert.deepEqual(state.seats.map((seat) => seat.net), [0, 0])
  assert.deepEqual(state.combinedNets, [100, -100])
})
