import assert from 'node:assert/strict'
import test from 'node:test'

import {
  contestScheduleRisk,
  estimatedScoringGames,
  matchingTemplateAlternatives,
  recommendedRangeLabel,
  templateFitMessage,
  templateHasUnboundedTiebreak,
  templateParticipantFit,
  templatePurposeLabel,
  templateTimeClassLabel,
  type ContestTemplateGuidance,
} from '../src/components/contest/template-guidance.ts'

const fullRoundRobin: ContestTemplateGuidance = {
  id: 'full_rr',
  name: '完整循环',
  recommended_min: 2,
  recommended_max: 8,
  purpose: 'fairness',
  time_class: 'long',
}

const swiss: ContestTemplateGuidance = {
  id: 'swiss',
  name: '瑞士制最终排名',
  recommended_min: 9,
  recommended_max: null,
  purpose: 'ranking',
  time_class: 'medium',
}

test('template metadata produces stable organizer-facing labels', () => {
  assert.equal(recommendedRangeLabel(fullRoundRobin), '建议 2–8 人')
  assert.equal(recommendedRangeLabel(swiss), '建议 9 人以上')
  assert.equal(templatePurposeLabel(fullRoundRobin.purpose), '公平优先')
  assert.equal(templateTimeClassLabel(fullRoundRobin.time_class), '长赛程')
})

test('participant fit is advisory and offers matching alternatives', () => {
  assert.equal(templateParticipantFit(fullRoundRobin, 8), 'within')
  assert.equal(templateParticipantFit(fullRoundRobin, 17), 'above')
  assert.match(templateFitMessage(fullRoundRobin, 17) || '', /仍可发布/)
  assert.deepEqual(
    matchingTemplateAlternatives([fullRoundRobin, swiss], fullRoundRobin.id, 17)
      .map((template) => template.id),
    ['swiss'],
  )
})

test('strict participant bands are described as publish requirements', () => {
  const seededFinal: ContestTemplateGuidance = {
    id: 'seeded',
    name: '保护种子正式赛',
    recommended_min: 22,
    recommended_max: 26,
    participant_range_is_strict: true,
  }
  assert.equal(recommendedRangeLabel(seededFinal), '限 22–26 人')
  assert.match(templateFitMessage(seededFinal, 21) || '', /不符合发布人数要求/)
  assert.match(templateFitMessage(seededFinal, 24) || '', /符合发布人数要求/)
})

test('unbounded paired-swap tiebreak is explicit rather than inferred from KO', () => {
  assert.equal(templateHasUnboundedTiebreak({
    stages: [{ type: 'single_elimination', tiebreak: 'paired_swap_until_decided' }],
  }), true)
  assert.equal(templateHasUnboundedTiebreak({
    stages: [{ type: 'single_elimination' }],
  }), false)
})

test('scale summary distinguishes physical matches from scoring games and risks', () => {
  assert.equal(estimatedScoringGames({
    estimated_matches: 3,
    eta_seconds: 300,
    stages: [{
      stage_key: 'duplicate',
      participant_count: 3,
      conceptual_pairings: 3,
      games_per_pair: 1,
      estimated_matches: 3,
      estimated_execution_legs: 6,
      eta_seconds: 300,
    }],
  }), 6)
  assert.equal(contestScheduleRisk(8 * 60 * 60), 'none')
  assert.equal(contestScheduleRisk(8 * 60 * 60 + 1), 'long')
  assert.equal(contestScheduleRisk(24 * 60 * 60 + 1), 'very_long')
})
