import { expect, test, type Page, type Route } from '@playwright/test'

import { monitorBrowser } from './helpers'

// 「赛事归档」：默认列表不显示归档赛事，显式筛选才可见；组织者可对已结束
// 赛事归档/取消归档；归档后详情仍可达并带「已归档」标记。全程 mock，不落库。

const ORGANIZER = {
  id: 7,
  username: 'archive_org',
  email: 'archive_org@example.test',
  role: 'organizer' as const,
  display_name: '归档组织者',
  is_active: 1,
}

const ARCHIVED_CONTEST = {
  id: 501,
  title: '很久以前的归档赛',
  status: 'finished',
  archived_at: '2026-08-01T10:00:00',
  game_id: 'holdem',
  created_at: '2026-07-30T09:00:00',
  template_id: 'holdem_rr',
}
const ACTIVE_CONTEST = {
  id: 502,
  title: '仍在列表的赛事',
  status: 'finished',
  archived_at: null,
  game_id: 'holdem',
  created_at: '2026-09-01T09:00:00',
  template_id: 'holdem_rr',
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
}

function mockVisitorShell(page: Page) {
  return page.route('**/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (pathname === '/api/auth/me') return json(route, { detail: '未登录' }, 401)
    if (pathname === '/api/contests') {
      const query = new URL(route.request().url()).searchParams
      // 后端契约：默认 exclude；显式筛选才带 archived=only
      const archivedOnly = query.get('archived') === 'only'
      return json(route, {
        contests: archivedOnly ? [ARCHIVED_CONTEST] : [ACTIVE_CONTEST],
        page: 1,
        per_page: 20,
        total: 1,
      })
    }
    return json(route, {})
  })
}

test('contest list defaults to unarchived and archived filter switches the query', async ({
  page,
}) => {
  const monitored = monitorBrowser(page)
  await mockVisitorShell(page)
  const listQueries: string[] = []
  page.on('request', (request) => {
    const url = new URL(request.url())
    if (url.pathname === '/api/contests') listQueries.push(url.searchParams.get('archived') || '')
  })

  await page.goto('/#/contests')
  await expect(page.getByRole('link', { name: '仍在列表的赛事' })).toBeVisible()
  await expect(page.getByText('很久以前的归档赛')).toHaveCount(0)
  expect(listQueries.at(-1)).toBe('')

  await page.getByRole('combobox', { name: '归档筛选' }).click()
  await page.getByRole('option', { name: '已归档', exact: true }).click()
  await expect(page.getByRole('link', { name: '很久以前的归档赛' })).toBeVisible()
  await expect(page.getByText('仍在列表的赛事')).toHaveCount(0)
  await expect(page.locator('li').filter({ hasText: '很久以前的归档赛' }).getByText('已归档')).toBeVisible()
  expect(listQueries.at(-1)).toBe('only')
  await monitored.expectClean()
})

test('organizer archives a finished contest through the confirm dialog', async ({ page }) => {
  const monitored = monitorBrowser(page)
  const contestId = 502
  let archivedAt: string | null = null
  let archivePosts = 0
  await page.route('**/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (pathname === '/api/auth/me') return json(route, { user: ORGANIZER })
    if (pathname === '/api/notifications/unread-count') return json(route, { count: 0 })
    if (pathname === `/api/contests/${contestId}` && route.request().method() === 'GET') {
      return json(route, {
        contest: {
          ...ACTIVE_CONTEST,
          organizer_id: ORGANIZER.id,
          current_stage_idx: 0,
          stages_json: JSON.stringify([{ key: 'rr', type: 'round_robin' }]),
          archived_at: archivedAt,
          is_organizer: true,
        },
        entries: [],
        pairings: [],
        standings: [],
        entries_total: 0,
        my_entry: null,
      })
    }
    if (pathname === `/api/contests/${contestId}/archive`) {
      expect(route.request().method()).toBe('POST')
      archivePosts += 1
      archivedAt = '2026-09-09T10:00:00'
      return json(route, { contest: { id: contestId, archived_at: archivedAt } })
    }
    if (pathname === `/api/contests/${contestId}/official-results`) {
      return json(route, { results: [] })
    }
    return json(route, {})
  })

  await page.goto(`/#/contests/${contestId}`)
  const archive = page.getByRole('button', { name: '归档赛事', exact: true })
  await expect(archive).toBeVisible()

  const archiveRequest = page.waitForRequest(
    (request) => request.method() === 'POST' && new URL(request.url()).pathname === `/api/contests/${contestId}/archive`,
  )
  await archive.click()
  await expect(page.getByRole('dialog')).toContainText('默认不再出现在列表中')
  await page.getByRole('dialog').getByRole('button', { name: '确认归档', exact: true }).click()
  await archiveRequest
  expect(archivePosts).toBe(1)
  // 重载后头部出现「已归档」徽章，操作切换为「取消归档」
  await expect(page.getByText('已归档', { exact: true }).first()).toBeVisible()
  await expect(page.getByRole('button', { name: '取消归档', exact: true })).toBeVisible()
  await monitored.expectClean()
})

test('archived contest detail stays reachable for visitors with a badge', async ({ page }) => {
  const monitored = monitorBrowser(page)
  const contestId = 501
  await page.route('**/api/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname
    if (pathname === '/api/auth/me') return json(route, { detail: '未登录' }, 401)
    if (pathname === `/api/contests/${contestId}`) {
      return json(route, {
        contest: {
          ...ARCHIVED_CONTEST,
          organizer_id: 7,
          current_stage_idx: 0,
          stages_json: JSON.stringify([{ key: 'rr', type: 'round_robin' }]),
        },
        entries: [],
        pairings: [],
        standings: [],
        entries_total: 0,
        my_entry: null,
      })
    }
    if (pathname === `/api/contests/${contestId}/official-results`) {
      return json(route, { results: [] })
    }
    return json(route, {})
  })

  await page.goto(`/#/contests/${contestId}`)
  await expect(page.getByRole('heading', { name: '很久以前的归档赛' })).toBeVisible()
  await expect(page.getByText('已归档', { exact: true }).first()).toBeVisible()
  await monitored.expectClean()
})
