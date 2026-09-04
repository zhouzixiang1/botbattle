import { Check, Copy, ShieldCheck } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import {
  formatDrawAlgorithm,
  parseContestFormatSnapshot,
} from '@/lib/contest-format'
import { cn } from '@/lib/utils'

export function FormatSnapshotAudit({
  value,
  className,
}: {
  value: unknown
  className?: string
}) {
  const [copied, setCopied] = useState(false)
  if (value === null || value === undefined) return null
  const snapshot = parseContestFormatSnapshot(value)
  if (!snapshot) {
    return (
      <p role="status" className={cn('rounded-lg border border-warning/35 bg-warning/10 px-3 py-2 text-xs text-warning-foreground', className)}>
        分组抽签审计信息格式无效，页面已停止展示或推断抽签数据。
      </p>
    )
  }

  const copyDigest = async () => {
    try {
      await navigator.clipboard.writeText(snapshot.audit_digest)
      setCopied(true)
      toast.success('抽签审计值已复制')
      window.setTimeout(() => setCopied(false), 1_500)
    } catch {
      toast.error('复制失败，请手动选择审计值')
    }
  }
  const sizeText = snapshot.group_sizes
    ? Object.entries(snapshot.group_sizes).map(([group, size]) => `${group}组 ${size} 人`).join(' · ')
    : snapshot.group_size_min === snapshot.group_size_max
      ? `每组 ${snapshot.group_size_min} 人`
      : `每组 ${snapshot.group_size_min}–${snapshot.group_size_max} 人`
  const shortDigest = `${snapshot.audit_digest.slice(0, 12)}…${snapshot.audit_digest.slice(-8)}`

  return (
    <section
      aria-label="分组抽签审计"
      className={cn('min-w-0 rounded-lg border border-primary/20 bg-primary/[0.035] px-3 py-3', className)}
    >
      <div className="flex min-w-0 items-start gap-2">
        <ShieldCheck aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-primary" />
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold text-foreground">分组抽签审计</h3>
          <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
            {formatDrawAlgorithm(snapshot.algorithm)} · 算法 {snapshot.algorithm} · 审计格式 v{snapshot.version}
          </p>
        </div>
      </div>
      <dl className="mt-3 grid min-w-0 gap-2 text-xs sm:grid-cols-2 lg:grid-cols-3">
        <div className="min-w-0">
          <dt className="text-muted-foreground">分组规模</dt>
          <dd className="mt-0.5 break-words font-medium text-foreground">
            {snapshot.group_count} 组 · {sizeText}
          </dd>
        </div>
        {snapshot.expected_match_count !== undefined && (
          <div className="min-w-0">
            <dt className="text-muted-foreground">冻结总场数</dt>
            <dd className="mt-0.5 font-mono font-medium tabular-nums text-foreground">
              {snapshot.expected_match_count} 场
            </dd>
          </div>
        )}
        <div className="min-w-0 sm:col-span-2 lg:col-span-1">
          <dt className="text-muted-foreground">审计值</dt>
          <dd className="mt-0.5 flex min-w-0 flex-wrap items-center gap-1.5">
            <code
              title={snapshot.audit_digest}
              className="max-w-full break-all rounded bg-muted px-1.5 py-1 font-mono text-[11px] text-foreground"
            >
              {shortDigest}
            </code>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className="h-11 shrink-0 px-3"
              onClick={() => void copyDigest()}
              aria-label="复制完整抽签审计值"
            >
              {copied ? <Check aria-hidden="true" className="size-3.5" /> : <Copy aria-hidden="true" className="size-3.5" />}
              {copied ? '已复制' : '复制'}
            </Button>
          </dd>
        </div>
      </dl>
      {snapshot.source && (
        <div className="mt-3 border-t border-primary/15 pt-3">
          <p className="text-xs text-muted-foreground">
            保护种子来源：
            <Link className="font-medium text-primary hover:underline" to={`/contests/${snapshot.source.contest_id}`}>
              五子棋模拟赛 #{snapshot.source.contest_id}
            </Link>
          </p>
          <ol className="mt-2 grid min-w-0 gap-1.5 sm:grid-cols-2">
            {snapshot.source.protected.map((seed) => (
              <li key={seed.entry_id} className="min-w-0 rounded-md bg-background/70 px-2.5 py-2 text-xs leading-relaxed">
                <span className="font-semibold text-foreground">来源第 {seed.source_rank} 名</span>
                <span className="text-muted-foreground">
                  {' '}· 当前报名 #{seed.entry_id} · 来源报名 #{seed.source_entry_id} · 用户 #{seed.user_id}
                </span>
              </li>
            ))}
          </ol>
        </div>
      )}
      <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
        审计投影不包含私有随机种子、完整抽签顺序或重复的分组成员表。
      </p>
    </section>
  )
}
