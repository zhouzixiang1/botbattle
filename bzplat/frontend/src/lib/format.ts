/**
 * 全站统一的展示格式化工具。
 *
 * 设计：所有对外展示的时间/数字都走这里，避免各页面散落的 slice/replace/硬编码，
 * 保证一致性（同一字段在首页/Bot 详情/admin 展示格式相同）。
 */

/**
 * 把后端 ISO 字符串（如 `2026-08-03T14:02:11Z`）格式化为 `YYYY-MM-DD HH:MM`（本地时区）。
 *
 * - 输入为空/非法时返回 fallback（默认 '—'）。
 * - 去掉末尾的时区标记（T/Z），转成本地时区，对中文用户更友好。
 * - 不带秒，与全站表格「时间」列口径一致。
 */
export function fmtTime(iso: string | null | undefined, fallback = '—'): string {
  if (!iso) return fallback
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return fallback
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/**
 * 仅日期格式 `YYYY-MM-DD`（用于「创建于」「注册时间」等不需要时刻的场景）。
 */
export function fmtDate(iso: string | null | undefined, fallback = '—'): string {
  if (!iso) return fallback
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return fallback
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}
