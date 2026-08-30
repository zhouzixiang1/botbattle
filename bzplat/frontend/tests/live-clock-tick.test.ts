import assert from 'node:assert/strict'
import test from 'node:test'

import { tickRemaining } from '../src/games/clock-tick.ts'

test('tickRemaining decrements only the acting seat by elapsed seconds', () => {
  const out = tickRemaining([900, 845.7], 0, 3)
  assert.deepEqual(out, [897, 845.7])
})

test('tickRemaining clamps at zero and never goes negative', () => {
  const out = tickRemaining([2, 900], 0, 30)
  assert.deepEqual(out, [0, 900])
})

test('tickRemaining leaves non-acting seats and null values untouched', () => {
  const out = tickRemaining([900, null], 1, 5)
  assert.deepEqual(out, [900, null])
})

test('tickRemaining ignores invalid acting seats and non-positive elapsed', () => {
  assert.deepEqual(tickRemaining([900, 900], null, 5), [900, 900])
  assert.deepEqual(tickRemaining([900, 900], 2, 5), [900, 900])
  assert.deepEqual(tickRemaining([900, 900], -1, 5), [900, 900])
  assert.deepEqual(tickRemaining([900, 900], 0, 0), [900, 900])
  assert.deepEqual(tickRemaining([900, 900], 0, -4), [900, 900])
})

test('tickRemaining does not mutate the input array', () => {
  const input = [900, 900]
  tickRemaining(input, 0, 10)
  assert.deepEqual(input, [900, 900])
})
