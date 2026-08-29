import assert from 'node:assert/strict'
import test from 'node:test'

import { bracketConnectorEdges } from '../src/components/contest/bracket-connectors.ts'

test('paired-swap tiebreak cards produce one logical advancement connector', () => {
  const edges = bracketConnectorEdges([
    { id: 10, round_num: 1, bracket_slot: 0 },
    { id: 11, round_num: 1, bracket_slot: 0, tiebreak_group: 1, tiebreak_game: 1 },
    { id: 12, round_num: 1, bracket_slot: 0, tiebreak_group: 1, tiebreak_game: 2 },
    { id: 20, round_num: 2, bracket_slot: 0 },
    { id: 21, round_num: 2, bracket_slot: 0, tiebreak_group: 1, tiebreak_game: 1 },
    { id: 22, round_num: 2, bracket_slot: 0, tiebreak_group: 1, tiebreak_game: 2 },
  ])

  assert.deepEqual(edges, [{ sourceId: 12, targetId: 20 }])
})

test('ordinary elimination encounters retain their existing tree topology', () => {
  assert.deepEqual(
    bracketConnectorEdges([
      { id: 1, round_num: 1, bracket_slot: 0 },
      { id: 2, round_num: 1, bracket_slot: 1 },
      { id: 3, round_num: 2, bracket_slot: 0 },
    ]),
    [
      { sourceId: 1, targetId: 3 },
      { sourceId: 2, targetId: 3 },
    ],
  )
})
