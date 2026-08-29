import assert from 'node:assert/strict'
import test from 'node:test'

import {
  describeMatchOutcome,
  isPublicMatchOutcome,
  outcomeLabelForSeat,
  outcomeParticipantStates,
  singleOutcomeWinner,
  type PublicMatchOutcome,
} from '../src/lib/match-outcome.ts'

const duplicate: PublicMatchOutcome = {
  kind: 'duplicate',
  planned_games: 2,
  completed_games: 2,
  score: { wins_a: 1, draws: 0, wins_b: 1 },
  rounds_played: 140,
  normalized_delta_a: 0,
  games: [
    { index: 1, winner: 0, rounds_played: 70, normalized_delta_a: 70 },
    { index: 2, winner: 1, rounds_played: 70, normalized_delta_a: -70 },
  ],
  termination: { kind: 'normal', reason: 'completed', loser: null },
}

test('duplicate outcome reports two scoring games without inventing one overall winner', () => {
  assert.equal(isPublicMatchOutcome(duplicate), true)
  assert.deepEqual(outcomeParticipantStates(duplicate), ['neutral', 'neutral'])
  assert.equal(singleOutcomeWinner(duplicate), undefined)
  assert.equal(outcomeLabelForSeat(duplicate, 0), '复式 · 1胜 / 0平 / 1负')
  assert.deepEqual(
    describeMatchOutcome(
      { status: 'completed', outcome: duplicate },
      { seatLabels: ['Alpha', 'Beta'], normalizedUnit: 'BB' },
    ),
    {
      availability: 'available',
      kind: 'duplicate',
      primary: 'Alpha 1胜 · 平 0 · Beta 1胜',
      secondary: '已完成 2/2 场计分 · 交锋组合计分差（Alpha） 0 BB',
      games: ['第 1 场：Alpha胜', '第 2 场：Beta胜'],
      winner: undefined,
      technical: false,
    },
  )
})

test('single draw requires an authoritative completed game', () => {
  const draw: PublicMatchOutcome = {
    kind: 'single',
    planned_games: 1,
    completed_games: 1,
    score: { wins_a: 0, draws: 1, wins_b: 0 },
    rounds_played: 70,
    normalized_delta_a: 0,
    games: [{ index: 1, winner: null, rounds_played: 70, normalized_delta_a: 0 }],
    termination: { kind: 'normal', reason: 'completed', loser: null },
  }
  assert.equal(describeMatchOutcome({ status: 'completed', outcome: draw }).primary, '平局')
  assert.equal(outcomeLabelForSeat(draw, 1), '平')
  assert.deepEqual(outcomeParticipantStates(draw), ['neutral', 'neutral'])
})

test('missing or incomplete authoritative outcome never falls through to a draw', () => {
  assert.equal(
    describeMatchOutcome({ status: 'completed', outcome: null }).primary,
    '赛果暂不可用',
  )
  const planned: PublicMatchOutcome = {
    kind: 'single',
    planned_games: 1,
    completed_games: 0,
    score: { wins_a: 0, draws: 0, wins_b: 0 },
    rounds_played: 0,
    normalized_delta_a: 0,
    games: [],
    termination: { kind: 'normal', reason: '', loser: null },
  }
  assert.equal(isPublicMatchOutcome(planned), false)
  assert.equal(describeMatchOutcome({ status: 'running', outcome: planned }).primary, '赛果待定')
  assert.equal(outcomeLabelForSeat(planned, 0), '赛果暂不可用')
})

test('runtime guard rejects malformed scoring-game order, score and plan size', () => {
  assert.equal(isPublicMatchOutcome({ ...duplicate, planned_games: 3 }), false)
  assert.equal(isPublicMatchOutcome({
    ...duplicate,
    games: duplicate.games.map((game) => ({ ...game, index: game.index - 1 })),
  }), false)
  assert.equal(isPublicMatchOutcome({
    ...duplicate,
    games: [{ ...duplicate.games[0] }, { ...duplicate.games[1], index: 1 }],
  }), false)
  assert.equal(isPublicMatchOutcome({
    ...duplicate,
    score: { wins_a: 2, draws: 0, wins_b: 0 },
  }), false)
  assert.equal(isPublicMatchOutcome({
    ...duplicate,
    completed_games: 1,
    score: { wins_a: 1, draws: 0, wins_b: 0 },
    games: [duplicate.games[0]],
  }), false)
  assert.equal(isPublicMatchOutcome({
    ...duplicate,
    termination: { kind: 'normal', reason: 'completed', loser: 1 },
  }), false)
  assert.equal(isPublicMatchOutcome({
    ...duplicate,
    games: duplicate.games.map((game, index) => index === 0
      ? { ...game, normalized_delta_a: -Math.abs(game.normalized_delta_a) }
      : game),
  }), false)
  assert.equal(isPublicMatchOutcome({
    ...duplicate,
    normalized_delta_a: duplicate.normalized_delta_a + 1,
  }), false)
  assert.equal(isPublicMatchOutcome({
    ...duplicate,
    rounds_played: duplicate.rounds_played - 1,
  }), false)
})

test('technical termination identifies the failed seat without claiming a duplicate winner', () => {
  const technical: PublicMatchOutcome = {
    ...duplicate,
    completed_games: 1,
    score: { wins_a: 1, draws: 0, wins_b: 0 },
    rounds_played: 12,
    normalized_delta_a: 0,
    games: [{ ...duplicate.games[0], index: 2, rounds_played: 12, normalized_delta_a: 0 }],
    termination: { kind: 'technical', reason: 'timeout', loser: 1 },
  }
  const description = describeMatchOutcome(
    { status: 'completed', outcome: technical },
    { seatLabels: ['Alpha', 'Beta'] },
  )
  assert.equal(description.technical, true)
  assert.equal(description.primary, '技术终局 · 已计 1/2 场')
  assert.match(description.secondary ?? '', /Alpha 1胜 · 平 0 · Beta 0胜/)
  assert.match(description.secondary ?? '', /Beta 技术判负/)
  assert.match(description.secondary ?? '', /原因：超时/)
  assert.deepEqual(description.games, ['第 2 场：Alpha胜'])
  assert.equal(description.winner, undefined)
})

test('runtime guard rejects contradictory technical termination payloads', () => {
  const technical: PublicMatchOutcome = {
    ...duplicate,
    completed_games: 1,
    score: { wins_a: 1, draws: 0, wins_b: 0 },
    rounds_played: 12,
    normalized_delta_a: 0,
    games: [{ ...duplicate.games[0], rounds_played: 12, normalized_delta_a: 0 }],
    termination: { kind: 'technical', reason: 'timeout', loser: 1 },
  }
  assert.equal(isPublicMatchOutcome({
    ...technical,
    completed_games: 0,
    score: { wins_a: 0, draws: 0, wins_b: 0 },
    games: [],
  }), false)
  assert.equal(isPublicMatchOutcome({
    ...technical,
    completed_games: 2,
    score: duplicate.score,
    games: duplicate.games,
  }), false)
  assert.equal(isPublicMatchOutcome({
    ...technical,
    termination: { ...technical.termination, loser: null },
  }), false)
  assert.equal(isPublicMatchOutcome({
    ...technical,
    games: [{ ...technical.games[0], winner: 1 }],
    score: { wins_a: 0, draws: 0, wins_b: 1 },
  }), false)
  assert.equal(isPublicMatchOutcome({
    ...technical,
    games: [{ ...technical.games[0], winner: null }],
    score: { wins_a: 0, draws: 1, wins_b: 0 },
  }), false)
  assert.equal(isPublicMatchOutcome({
    ...technical,
    games: [{ ...technical.games[0], index: 3 }],
  }), false)
  const singleTechnical: PublicMatchOutcome = {
    kind: 'single',
    planned_games: 1,
    completed_games: 1,
    score: { wins_a: 1, draws: 0, wins_b: 0 },
    rounds_played: 12,
    normalized_delta_a: 0,
    games: [{ index: 1, winner: 0, rounds_played: 12, normalized_delta_a: 0 }],
    termination: { kind: 'technical', reason: 'timeout', loser: 1 },
  }
  assert.equal(isPublicMatchOutcome(singleTechnical), true)
  assert.equal(isPublicMatchOutcome({
    ...singleTechnical,
    games: [{ ...singleTechnical.games[0], winner: 1 }],
    score: { wins_a: 0, draws: 0, wins_b: 1 },
  }), false)
})
