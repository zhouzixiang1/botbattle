import { expect, test, type Page } from '@playwright/test'

import { monitorBrowser } from './helpers'

const CONTEST_ID = 12

const stageRows = [
  {
    entry_id: 101,
    bot_id: 901,
    bot_name: 'test',
    owner_name: 'winner',
    points: 9,
    wins: 3,
    draws: 0,
    losses: 0,
    byes: 0,
    delta_total: 4300,
    group_id: '',
    rank: 1,
    advancement: null,
    counts: { unique_opponents: 3, encounter_groups: 3, match_jobs: 3, scoring_games: 3 },
  },
  {
    entry_id: 104,
    bot_id: 904,
    bot_name: '测01',
    owner_name: 'amc',
    points: 6,
    wins: 1,
    draws: 0,
    losses: 1,
    byes: 1,
    delta_total: 1200,
    group_id: '',
    rank: 2,
    advancement: null,
    counts: { unique_opponents: 2, encounter_groups: 2, match_jobs: 2, scoring_games: 2 },
  },
]

function contestDetail() {
  return {
    contest: {
      id: CONTEST_ID,
      title: '德扑友谊赛2',
      description: '瑞士轮计分展示回归',
      status: 'finished',
      organizer_id: 1,
      template_id: 'holdem_prelim_swiss',
      template_name: '德州：大规模预赛（瑞士快速）',
      game_id: 'holdem',
      stages_json: JSON.stringify([
        {
          key: 'prelim',
          type: 'swiss',
          rounds: 3,
          scoring: 'poker_3_1_0',
        },
      ]),
      current_stage_idx: 0,
      official_results_ready: 1,
    },
    entries: [],
    entries_total: 7,
    entries_page: 1,
    entries_per_page: 20,
    pairings: [
      {
        id: 1,
        bot_a_id: 901,
        bot_b_id: 902,
        match_id: 'holdem-r1',
        status: 'completed',
        stage_idx: 0,
        stage_key: 'prelim',
        round_num: 1,
      },
      {
        id: 2,
        bot_a_id: 904,
        bot_b_id: null,
        match_id: null,
        is_bye: true,
        status: 'completed',
        stage_idx: 0,
        stage_key: 'prelim',
        round_num: 2,
      },
    ],
    standings: stageRows.map(({ entry_id: _entryId, rank: _rank, advancement: _advancement, ...row }) => row),
    stage_standings: [
      {
        stage_idx: 0,
        stage_key: 'prelim',
        status: 'completed',
        source: 'persisted',
        completed_pairings: 12,
        total_pairings: 12,
        advancement_final: true,
        counts: {
          encounter_groups: { completed: 12, total: 12 },
          match_jobs: { completed: 9, total: 9 },
          scoring_games: { completed: 9, planned: 9, terminal_unplayed: 0 },
        },
        rows: stageRows,
      },
    ],
    estimate: { estimated_matches: 9, eta_seconds: 1800 },
    my_entry: null,
  }
}

function officialResults() {
  return {
    results: stageRows.map((row) => ({
      rank: row.rank,
      overall_rank: row.rank,
      group_id: '',
      rank_in_group: null,
      entry_id: row.entry_id,
      bot_id: row.bot_id,
      user_id: row.entry_id,
      points: row.points,
      bot_name: row.bot_name,
      owner_name: row.owner_name,
      source_stage: 0,
      ranking_cohort: 'stage:0',
      tiebreaks: {
        points: row.points,
        buchholz_cut1: 3,
        sonneborn_berger: 3,
        head_to_head: 0,
        normalized_delta: row.delta_total / 100,
        technical_losses: 0,
        seed: row.rank,
      },
    })),
  }
}

const tiedRoundRobinRows = [
  {
    entry_id: 201,
    bot_id: 1001,
    bot_name: 'cbot',
    owner_name: 'cyz',
    points: 27,
    wins: 9,
    draws: 0,
    losses: 7,
    byes: 0,
    delta_total: 12_378,
    group_id: '',
    rank: 6,
    advancement: null,
    counts: { unique_opponents: 16, encounter_groups: 16, match_jobs: 16, scoring_games: 16 },
    tiebreaks: {
      points: 27,
      buchholz: 381,
      buchholz_cut1: 381,
      sonneborn_berger: 195,
      head_to_head: 0,
      normalized_delta: 123.78,
      technical_losses: 0,
      seed: 17,
    },
  },
  {
    entry_id: 202,
    bot_id: 1002,
    bot_name: 'bluffing',
    owner_name: 'ree4',
    points: 27,
    wins: 9,
    draws: 0,
    losses: 7,
    byes: 0,
    delta_total: 68_716,
    group_id: '',
    rank: 7,
    advancement: null,
    counts: { unique_opponents: 16, encounter_groups: 16, match_jobs: 16, scoring_games: 16 },
    tiebreaks: {
      points: 27,
      buchholz: 381,
      buchholz_cut1: 381,
      sonneborn_berger: 171,
      head_to_head: 1,
      normalized_delta: 687.16,
      technical_losses: 0,
      seed: 2,
    },
  },
  {
    entry_id: 203,
    bot_id: 1003,
    bot_name: 'bot3',
    owner_name: 'skrinooo',
    points: 27,
    wins: 9,
    draws: 0,
    losses: 7,
    byes: 0,
    delta_total: -2_053,
    group_id: '',
    rank: 8,
    advancement: null,
    counts: { unique_opponents: 16, encounter_groups: 16, match_jobs: 16, scoring_games: 16 },
    tiebreaks: {
      points: 27,
      buchholz: 381,
      buchholz_cut1: 381,
      sonneborn_berger: 162,
      head_to_head: 0.5,
      normalized_delta: -20.53,
      technical_losses: 0,
      seed: 15,
    },
  },
  {
    entry_id: 204,
    bot_id: 1004,
    bot_name: 'lower-points',
    owner_name: 'lower-owner',
    points: 24,
    wins: 8,
    draws: 0,
    losses: 8,
    byes: 0,
    delta_total: 99_999,
    group_id: '',
    rank: 9,
    advancement: null,
    counts: { unique_opponents: 16, encounter_groups: 16, match_jobs: 16, scoring_games: 16 },
    tiebreaks: {
      points: 24,
      buchholz: 384,
      buchholz_cut1: 384,
      sonneborn_berger: 192,
      head_to_head: 0,
      normalized_delta: 999.99,
      technical_losses: 0,
      seed: 12,
    },
  },
]

function tiedRoundRobinDetail() {
  const base = contestDetail()
  return {
    ...base,
    contest: {
      ...base.contest,
      title: '德扑模拟赛3',
      template_id: 'holdem_rr',
      stages_json: JSON.stringify([{
        key: 'rr',
        type: 'round_robin',
        scoring: 'poker_3_1_0',
        games_per_pair: 1,
        series_scoring: 'independent_scoring_game_points_v1',
      }]),
    },
    standings: tiedRoundRobinRows.map(({
      entry_id: _entryId,
      advancement: _advancement,
      ...row
    }) => row),
    stage_standings: [{
      ...base.stage_standings[0],
      stage_key: 'rr',
      rows: tiedRoundRobinRows,
    }],
  }
}

function tiedRoundRobinOfficialResults() {
  return {
    results: tiedRoundRobinRows.map((row) => ({
      ...row,
      overall_rank: row.rank,
      user_id: row.entry_id,
      source_stage: 0,
      ranking_cohort: 'stage:0',
    })),
  }
}

const groupedRows = [
  {
    ...tiedRoundRobinRows[0],
    entry_id: 301,
    bot_id: 1101,
    bot_name: 'group-a-first',
    group_id: 'A',
    points: 12,
    rank: 1,
    rank_in_group: 1,
    tiebreaks: { ...tiedRoundRobinRows[0].tiebreaks, points: 12, sonneborn_berger: 90 },
  },
  {
    ...tiedRoundRobinRows[1],
    entry_id: 302,
    bot_id: 1102,
    bot_name: 'group-a-second',
    group_id: 'A',
    points: 12,
    rank: 2,
    rank_in_group: 2,
    tiebreaks: { ...tiedRoundRobinRows[1].tiebreaks, points: 12, sonneborn_berger: 80 },
  },
  {
    ...tiedRoundRobinRows[2],
    entry_id: 303,
    bot_id: 1103,
    bot_name: 'group-b-only',
    group_id: 'B',
    points: 12,
    rank: 1,
    rank_in_group: 1,
    tiebreaks: { ...tiedRoundRobinRows[2].tiebreaks, points: 12, sonneborn_berger: 70 },
  },
]

const crossGroupRows = [
  {
    ...groupedRows[0],
    entry_id: 401,
    bot_id: 1201,
    bot_name: 'cross-a',
    rank: 2,
    overall_rank: 2,
    rank_in_group: 1,
    points: 10,
    tiebreaks: {
      ...groupedRows[0].tiebreaks,
      points: 10,
      group_rank: 1,
      points_rate: 0.625,
      opponent_strength: 0.55,
      normalized_delta_rate: 6.25,
      technical_loss_rate: 0,
      draw_order: 9,
    },
  },
  {
    ...groupedRows[2],
    entry_id: 402,
    bot_id: 1202,
    bot_name: 'cross-b',
    rank: 1,
    overall_rank: 1,
    rank_in_group: 1,
    points: 9,
    tiebreaks: {
      ...groupedRows[2].tiebreaks,
      points: 9,
      group_rank: 1,
      points_rate: 0.7,
      opponent_strength: 0.5,
      normalized_delta_rate: 4.5,
      technical_loss_rate: 0.1,
      draw_order: 10,
    },
  },
]

function rankingStageDetail({
  title,
  gameId,
  templateId,
  stage,
  rows,
}: {
  title: string
  gameId: string
  templateId: string
  stage: Record<string, unknown>
  rows: Array<Record<string, unknown>>
}) {
  const base = contestDetail()
  return {
    ...base,
    contest: {
      ...base.contest,
      title,
      game_id: gameId,
      template_id: templateId,
      stages_json: JSON.stringify([stage]),
    },
    standings: rows.map(({ entry_id: _entryId, advancement: _advancement, ...row }) => row),
    stage_standings: [{
      ...base.stage_standings[0],
      stage_key: String(stage.key || 'groups'),
      rows,
    }],
  }
}

function groupToKnockoutDetail() {
  const base = contestDetail()
  return {
    ...base,
    contest: {
      ...base.contest,
      title: '分组晋级淘汰正式榜',
      game_id: 'gomoku',
      template_id: 'gomoku_group_to_ko',
      current_stage_idx: 1,
      stages_json: JSON.stringify([{
        key: 'groups',
        type: 'group_double_round_robin',
        scoring: 'ccgc_2_1_0',
        group_count: 2,
        advance_per_group: 1,
      }, {
        key: 'knockout',
        type: 'single_elimination',
        scoring: 'ccgc_2_1_0',
      }]),
    },
    standings: [],
    stage_standings: [{
      ...base.stage_standings[0],
      stage_idx: 0,
      stage_key: 'groups',
      rows: groupedRows,
    }, {
      ...base.stage_standings[0],
      stage_idx: 1,
      stage_key: 'knockout',
      rows: [
        { ...groupedRows[0], rank: 1 },
        { ...groupedRows[2], rank: 2 },
      ],
    }],
  }
}

function groupToKnockoutOfficialResults() {
  return {
    results: [
      { ...groupedRows[0], rank: 1, overall_rank: 1, source_stage: 1, ranking_cohort: 'stage:1' },
      { ...groupedRows[2], rank: 2, overall_rank: 2, source_stage: 1, ranking_cohort: 'stage:1' },
      { ...groupedRows[1], rank: 3, overall_rank: 3, source_stage: 0, ranking_cohort: 'stage:0' },
    ].map((row) => ({ ...row, user_id: row.entry_id })),
  }
}

const groupedOfficialTieScopeRows = [
  {
    ...groupedRows[0],
    entry_id: 501,
    bot_id: 1301,
    bot_name: 'group-a-unique-points',
    group_id: 'A',
    points: 12,
    rank: 1,
    overall_rank: 1,
    rank_in_group: 1,
    tiebreaks: { ...groupedRows[0].tiebreaks, points: 12, buchholz_cut1: 40, sonneborn_berger: 30 },
  },
  {
    ...groupedRows[2],
    entry_id: 502,
    bot_id: 1302,
    bot_name: 'group-b-unique-points',
    group_id: 'B',
    points: 12,
    rank: 2,
    overall_rank: 2,
    rank_in_group: 1,
    tiebreaks: { ...groupedRows[2].tiebreaks, points: 12, buchholz_cut1: 35, sonneborn_berger: 25 },
  },
  {
    ...groupedRows[0],
    entry_id: 503,
    bot_id: 1303,
    bot_name: 'group-c-tied-first',
    group_id: 'C',
    points: 9,
    rank: 3,
    overall_rank: 3,
    rank_in_group: 1,
    tiebreaks: { ...groupedRows[0].tiebreaks, points: 9, buchholz_cut1: 28, sonneborn_berger: 18 },
  },
  {
    ...groupedRows[1],
    entry_id: 504,
    bot_id: 1304,
    bot_name: 'group-c-tied-second',
    group_id: 'C',
    points: 9,
    rank: 4,
    overall_rank: 4,
    rank_in_group: 2,
    tiebreaks: { ...groupedRows[1].tiebreaks, points: 9, buchholz_cut1: 24, sonneborn_berger: 16 },
  },
  {
    ...groupedRows[0],
    entry_id: 505,
    bot_id: 1305,
    bot_name: 'group-malformed-id',
    group_id: ' D ',
    points: 6,
    rank: 5,
    overall_rank: 5,
    rank_in_group: 1,
    tiebreaks: { ...groupedRows[0].tiebreaks, points: 6, buchholz_cut1: 20, sonneborn_berger: 10 },
  },
]

const invalidOfficialProvenanceRows = [
  {
    ...groupedRows[0],
    entry_id: 506,
    bot_id: 1306,
    bot_name: 'unknown-source-a',
    group_id: 'A',
    points: 3,
    rank: 6,
    overall_rank: 6,
    rank_in_group: 2,
    source_stage: null,
    ranking_cohort: 'unknown',
    tiebreaks: { ...groupedRows[0].tiebreaks, points: 3, buchholz_cut1: 12, sonneborn_berger: 8 },
  },
  {
    ...groupedRows[2],
    entry_id: 507,
    bot_id: 1307,
    bot_name: 'unknown-source-b',
    group_id: 'B',
    points: 3,
    rank: 7,
    overall_rank: 7,
    rank_in_group: 2,
    source_stage: null,
    ranking_cohort: 'unknown',
    tiebreaks: { ...groupedRows[2].tiebreaks, points: 3, buchholz_cut1: 10, sonneborn_berger: 6 },
  },
  {
    ...groupedRows[0],
    entry_id: 508,
    bot_id: 1308,
    bot_name: 'out-of-range-source',
    group_id: 'A',
    points: 2,
    rank: 8,
    overall_rank: 8,
    rank_in_group: 3,
    source_stage: 99,
    ranking_cohort: 'stage:99',
    tiebreaks: { ...groupedRows[0].tiebreaks, points: 2, buchholz_cut1: 9, sonneborn_berger: 5 },
  },
  {
    ...groupedRows[2],
    entry_id: 509,
    bot_id: 1309,
    bot_name: 'contradictory-source',
    group_id: 'B',
    points: 1,
    rank: 9,
    overall_rank: 9,
    rank_in_group: 3,
    source_stage: 0,
    ranking_cohort: 'stage:1',
    tiebreaks: { ...groupedRows[2].tiebreaks, points: 1, buchholz_cut1: 8, sonneborn_berger: 4 },
  },
]

function groupedOfficialTieScopeDetail() {
  return rankingStageDetail({
    title: '正式榜组内破同分范围',
    gameId: 'gomoku',
    templateId: 'gomoku_group_drr',
    stage: {
      key: 'groups',
      type: 'group_double_round_robin',
      scoring: 'ccgc_2_1_0',
      group_count: 4,
    },
    rows: groupedOfficialTieScopeRows,
  })
}

function groupedOfficialTieScopeResults() {
  return {
    results: [
      ...groupedOfficialTieScopeRows.map((row) => ({
        ...row,
        user_id: row.entry_id,
        source_stage: 0,
        ranking_cohort: 'stage:0',
      })),
      ...invalidOfficialProvenanceRows.map((row) => ({
        ...row,
        user_id: row.entry_id,
      })),
    ],
  }
}

async function assertNoRootOverflow(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - window.innerWidth,
  )
  expect(overflow).toBeLessThanOrEqual(1)
}

test('Swiss bye points stay separate from actual wins in stage and official standings', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await page.route(`**/api/contests/${CONTEST_ID}**`, async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === `/api/contests/${CONTEST_ID}/official-results`) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(officialResults()),
      })
      return
    }
    if (url.pathname === `/api/contests/${CONTEST_ID}`) {
      expect(url.searchParams.get('entries_page')).toBe('1')
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(contestDetail()),
      })
      return
    }
    await route.fallback()
  })

  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/#/contests/${CONTEST_ID}`)
  const main = page.getByRole('main')
  await expect(main.getByText('德扑友谊赛2', { exact: true })).toBeVisible()
  await expect(main.getByRole('tab', { name: /正式名次/ })).toHaveAttribute('data-state', 'active')
  await expect(main.getByText(/赛事积分不改变平台 Rating/)).toBeVisible()

  const officialRow = main.getByRole('row').filter({ hasText: '测01' })
  await expect(officialRow.getByRole('cell').first()).toHaveText('2')
  await expect(officialRow).toContainText('6')
  await expect(officialRow).toContainText('1 胜 / 0 平 / 1 负 · 轮空 1')

  await page.setViewportSize({ width: 390, height: 844 })
  const officialTableRegion = main.getByRole('region', { name: '赛事正式名次表', exact: true })
  await expect(officialTableRegion).toBeVisible()
  await expect(officialTableRegion).toHaveAttribute('tabindex', '0')
  await officialTableRegion.focus()
  await expect(officialTableRegion).toBeFocused()
  await expect(officialRow).toContainText('1 胜 / 0 平 / 1 负 · 轮空 1')
  await assertNoRootOverflow(page)

  await main.getByRole('tab', { name: /阶段积分/ }).click()
  await expect(main.getByText(/本阶段计分：胜 3 \/ 平 1 \/ 负 0/)).toBeVisible()
  await expect(main.getByText(/计分场战绩不包含瑞士轮轮空/)).toBeVisible()
  const stageRow = main.getByRole('row').filter({ hasText: '测01' })
  await expect(stageRow.getByRole('cell').first()).toHaveText('2')
  await expect(stageRow).toContainText('1 胜 / 0 平 / 1 负 · 轮空 1')
  await expect(stageRow).toContainText('2 场计分')
  const stageTableRegion = main.getByRole('region', { name: '阶段积分表', exact: true })
  await expect(stageTableRegion).toBeVisible()
  await expect(stageTableRegion).toHaveAttribute('tabindex', '0')
  await stageTableRegion.focus()
  await expect(stageTableRegion).toBeFocused()
  await assertNoRootOverflow(page)

  await main.getByRole('tab', { name: /对阵/ }).click()
  const stagePanel = main
    .getByRole('heading', { name: '阶段排名与晋级', exact: true })
    .locator('xpath=ancestor::*[@data-slot="data-region"][1]')
  await expect(stagePanel).toContainText(
    '1 胜 / 0 平 / 1 负 · 轮空 1',
  )
  await expect(stagePanel).not.toContainText('名次不可用')
  await expect(stagePanel.getByRole('row').filter({ hasText: '测01' }).getByRole('cell').first()).toHaveText('2')
  await expect(stagePanel).toContainText('2 场计分')

  await expect(main.getByText('德扑友谊赛2', { exact: true })).toBeVisible()
  await assertNoRootOverflow(page)
  await monitor.expectClean()
})

test('stage standings expose the authoritative tie-break chain instead of implying delta order', async ({ page }) => {
  const monitor = monitorBrowser(page)
  let detailPayload: unknown = tiedRoundRobinDetail()
  await page.route(`**/api/contests/${CONTEST_ID}**`, async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === `/api/contests/${CONTEST_ID}/official-results`) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(tiedRoundRobinOfficialResults()),
      })
      return
    }
    if (url.pathname === `/api/contests/${CONTEST_ID}`) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(detailPayload),
      })
      return
    }
    await route.fallback()
  })

  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`/#/contests/${CONTEST_ID}`)
  const main = page.getByRole('main')
  await expect(main.getByText('德扑模拟赛3', { exact: true })).toBeVisible()
  await main.getByRole('tab', { name: /阶段积分/ }).click()

  const stageTable = main.getByRole('region', { name: '阶段积分表', exact: true })
  await expect(stageTable.getByRole('columnheader', { name: '排名依据', exact: true })).toBeVisible()
  await expect(stageTable.getByRole('columnheader', { name: '阶段合计分差', exact: true })).toHaveCount(0)

  const cbotRow = stageTable.getByRole('row').filter({ hasText: 'cbot' })
  const bluffingRow = stageTable.getByRole('row').filter({ hasText: 'bluffing' })
  const bot3Row = stageTable.getByRole('row').filter({ hasText: 'bot3' })
  const lowerRow = stageTable.getByRole('row').filter({ hasText: 'lower-points' })
  await expect(cbotRow.getByRole('cell').first()).toHaveText('6')
  await expect(bluffingRow.getByRole('cell').first()).toHaveText('7')
  await expect(bot3Row.getByRole('cell').first()).toHaveText('8')
  await expect(cbotRow).toContainText('对手分 Cut1 381')
  await expect(cbotRow).toContainText('胜者分 SB 195')
  await expect(bluffingRow).toContainText('胜者分 SB 171')
  await expect(bluffingRow).toContainText('直接交手 100%')
  await expect(bluffingRow).toContainText('归一分差 687.16')
  await expect(bot3Row).toContainText('胜者分 SB 162')
  await expect(lowerRow).toContainText('积分已区分')
  await expect(lowerRow).not.toContainText('对手分 Cut1')

  await page.setViewportSize({ width: 390, height: 844 })
  await stageTable.focus()
  await expect(stageTable).toBeFocused()
  await expect(bluffingRow).toContainText('胜者分 SB 171')
  await assertNoRootOverflow(page)

  detailPayload = rankingStageDetail({
    title: '同组破同分范围',
    gameId: 'gomoku',
    templateId: 'custom',
    stage: {
      key: 'groups',
      type: 'group_double_round_robin',
      scoring: 'ccgc_2_1_0',
      group_count: 2,
    },
    rows: groupedRows,
  })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.reload()
  await expect(main.getByText('同组破同分范围', { exact: true })).toBeVisible()
  await main.getByRole('tab', { name: /阶段积分/ }).click()
  const groupedTable = main.getByRole('region', { name: '阶段积分表', exact: true })
  const groupAFirst = groupedTable.getByRole('row').filter({ hasText: 'group-a-first' })
  const groupASecond = groupedTable.getByRole('row').filter({ hasText: 'group-a-second' })
  const groupBOnly = groupedTable.getByRole('row').filter({ hasText: 'group-b-only' })
  await expect(groupAFirst).toContainText('胜者分 SB 90')
  await expect(groupASecond).toContainText('胜者分 SB 80')
  await expect(groupBOnly).toContainText('积分已区分')
  await expect(groupBOnly).not.toContainText('胜者分 SB 70')

  detailPayload = rankingStageDetail({
    title: '跨组六项链',
    gameId: 'pencil',
    templateId: 'pencil_group_drr',
    stage: {
      key: 'groups',
      type: 'group_double_round_robin',
      scoring: 'ccgc_2_1_0',
      group_count: 2,
      group_assignment: 'secure_random_balanced_v1',
      overall_ranking: 'cross_group_fair_v1',
    },
    // The fair overall chain may place group B before group A.  The public
    // read model is already authoritative and the page must preserve it.
    rows: [crossGroupRows[1], crossGroupRows[0]],
  })
  await page.reload()
  await expect(main.getByText('跨组六项链', { exact: true })).toBeVisible()
  await main.getByRole('tab', { name: /阶段积分/ }).click()
  const crossTable = main.getByRole('region', { name: '阶段积分表', exact: true })
  const crossA = crossTable.getByRole('row').filter({ hasText: 'cross-a' })
  const crossB = crossTable.getByRole('row').filter({ hasText: 'cross-b' })
  await expect(crossA.getByRole('cell').first()).toHaveText('2')
  await expect(crossB.getByRole('cell').first()).toHaveText('1')
  const crossRows = crossTable.locator('tbody tr')
  await expect(crossRows.nth(0)).toContainText('cross-b')
  await expect(crossRows.nth(1)).toContainText('cross-a')
  await expect(crossA).toContainText('组内第 1 名')
  await expect(crossA).toContainText('每局积分率 62.5%')
  await expect(crossA).toContainText('标准化对手强度 55%')
  await expect(crossA).toContainText('每局归一分差 6.25')
  await expect(crossA).toContainText('技术负率 0%')
  await expect(crossA).toContainText('冻结抽签序 9')
  await expect(crossB).toContainText('每局积分率 70%')
  await expect(crossB).toContainText('技术负率 10%')
  await monitor.expectClean()
})

test('group-to-knockout official results keep overall and group ranks independent', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await page.route(`**/api/contests/${CONTEST_ID}**`, async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === `/api/contests/${CONTEST_ID}/official-results`) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(groupToKnockoutOfficialResults()),
      })
      return
    }
    if (url.pathname === `/api/contests/${CONTEST_ID}`) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(groupToKnockoutDetail()),
      })
      return
    }
    await route.fallback()
  })

  await page.goto(`/#/contests/${CONTEST_ID}`)
  const main = page.getByRole('main')
  await expect(main.getByText('分组晋级淘汰正式榜', { exact: true })).toBeVisible()
  await expect(main.getByRole('tab', { name: /正式名次/ })).toHaveAttribute('data-state', 'active')
  const officialTable = main.getByRole('region', { name: '赛事正式名次表', exact: true })
  const winner = officialTable.getByRole('row').filter({ hasText: 'group-a-first' })
  const eliminated = officialTable.getByRole('row').filter({ hasText: 'group-a-second' })
  await expect(winner.getByRole('cell').first()).toHaveText('1')
  await expect(winner.getByRole('cell').nth(1)).toHaveText('A组 · 1')
  await expect(eliminated.getByRole('cell').first()).toHaveText('3')
  await expect(eliminated.getByRole('cell').nth(1)).toHaveText('A组 · 2')
  await monitor.expectClean()
})

test('official results scope ordinary tiebreak details to equal points inside the same traditional group', async ({ page }) => {
  const monitor = monitorBrowser(page)
  await page.route('**/api/auth/me', async (route) => {
    await route.fulfill({
      status: 401,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Not authenticated' }),
    })
  })
  await page.route(`**/api/contests/${CONTEST_ID}**`, async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname === `/api/contests/${CONTEST_ID}/official-results`) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(groupedOfficialTieScopeResults()),
      })
      return
    }
    if (url.pathname === `/api/contests/${CONTEST_ID}`) {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(groupedOfficialTieScopeDetail()),
      })
      return
    }
    await route.fallback()
  })

  await page.goto(`/#/contests/${CONTEST_ID}`)
  const main = page.getByRole('main')
  await expect(main.getByText('正式榜组内破同分范围', { exact: true })).toBeVisible()
  const officialTable = main.getByRole('region', { name: '赛事正式名次表', exact: true })
  const groupAUnique = officialTable.getByRole('row').filter({ hasText: 'group-a-unique-points' })
  const groupBUnique = officialTable.getByRole('row').filter({ hasText: 'group-b-unique-points' })
  const groupCTiedFirst = officialTable.getByRole('row').filter({ hasText: 'group-c-tied-first' })
  const groupCTiedSecond = officialTable.getByRole('row').filter({ hasText: 'group-c-tied-second' })
  const malformedGroup = officialTable.getByRole('row').filter({ hasText: 'group-malformed-id' })
  const unknownSourceA = officialTable.getByRole('row').filter({ hasText: 'unknown-source-a' })
  const unknownSourceB = officialTable.getByRole('row').filter({ hasText: 'unknown-source-b' })
  const outOfRangeSource = officialTable.getByRole('row').filter({ hasText: 'out-of-range-source' })
  const contradictorySource = officialTable.getByRole('row').filter({ hasText: 'contradictory-source' })

  await expect(groupAUnique).toContainText('积分已区分')
  await expect(groupAUnique).not.toContainText('对手分 Cut1')
  await expect(groupAUnique).not.toContainText('胜者分 SB')
  await expect(groupAUnique).not.toContainText('直接交手')
  await expect(groupBUnique).toContainText('积分已区分')
  await expect(groupBUnique).not.toContainText('对手分 Cut1')
  await expect(groupBUnique).not.toContainText('胜者分 SB')
  await expect(groupBUnique).not.toContainText('直接交手')
  await expect(groupCTiedFirst).toContainText('对手分 Cut1 28')
  await expect(groupCTiedFirst).toContainText('胜者分 SB 18')
  await expect(groupCTiedSecond).toContainText('对手分 Cut1 24')
  await expect(groupCTiedSecond).toContainText('胜者分 SB 16')
  await expect(malformedGroup).toContainText('破同分范围不可用')
  await expect(malformedGroup).not.toContainText('对手分 Cut1')
  for (const invalidProvenanceRow of [
    unknownSourceA,
    unknownSourceB,
    outOfRangeSource,
    contradictorySource,
  ]) {
    await expect(invalidProvenanceRow).toContainText('破同分范围不可用')
    await expect(invalidProvenanceRow).not.toContainText('对手分 Cut1')
    await expect(invalidProvenanceRow).not.toContainText('胜者分 SB')
    await expect(invalidProvenanceRow).not.toContainText('直接交手')
  }
  await monitor.expectClean()
})
