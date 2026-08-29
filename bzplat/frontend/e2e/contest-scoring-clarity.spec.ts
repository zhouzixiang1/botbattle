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
  await expect(stagePanel).toContainText('2 场计分')

  await expect(main.getByText('德扑友谊赛2', { exact: true })).toBeVisible()
  await assertNoRootOverflow(page)
  await monitor.expectClean()
})
