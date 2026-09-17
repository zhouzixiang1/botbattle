import { Check, Copy, ShieldCheck } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { OverflowText } from '@/components/ui/overflow-text'
import {
  formatDrawAlgorithm,
  parseContestFormatSnapshot,
  type ContestFormatSnapshot,
} from '@/lib/contest-format'
import { cn } from '@/lib/utils'

function groupSizeLabel(snapshot: ContestFormatSnapshot): string {
  return snapshot.group_size_min === snapshot.group_size_max
    ? `每组 ${snapshot.group_size_min} 人`
    : `每组 ${snapshot.group_size_min}–${snapshot.group_size_max} 人`
}

/**
 * 分组抽签记录。访客只看到一行中文摘要（variant="summary"）；
 * 组织者/管理员可用 variant="full" 展开完整面板并复核抽签值。
 */
export function FormatSnapshotAudit({
  value,
  variant = 'summary',
  className,
}: {
  value: unknown
  variant?: 'summary' | 'full'
  className?: string
}) {
  const [copied, setCopied] = useState(false)
  if (value === null || value === undefined) return null
  const snapshot = parseContestFormatSnapshot(value)
  if (!snapshot) {
    return (
      <p role="status" className={cn('rounded-lg border border-warning/35 bg-warning/10 px-3 py-2 text-xs text-warning-foreground', className)}>
        分组方式记录暂不可用，页面不会据此推测分组结果。
      </p>
    )
  }

  if (variant !== 'full') {
    return (
      <p className={cn('min-w-0 rounded-lg border border-primary/20 bg-primary/5 px-3 py-2 text-xs leading-relaxed text-muted-foreground', className)}>
        <span className="font-medium text-foreground">分组方式</span>
        ：{formatDrawAlgorithm(snapshot.algorithm)} · {snapshot.group_count} 组 · {groupSizeLabel(snapshot)}
      </p>
    )
  }

  const copyDigest = async () => {
    try {
      await navigator.clipboard.writeText(snapshot.audit_digest)
      setCopied(true)
      toast.success('抽签记录值已复制')
      window.setTimeout(() => setCopied(false), 1_500)
    } catch {
      toast.error('复制失败，请手动选择后复制')
    }
  }
  const sizeText = snapshot.group_sizes
    ? Object.entries(snapshot.group_sizes).map(([group, size]) => `${group} ${size} 人`).join(' · ')
    : groupSizeLabel(snapshot)
  const shortDigest = `${snapshot.audit_digest.slice(0, 12)}…${snapshot.audit_digest.slice(-8)}`

  return (
    <section
      aria-label="分组抽签审计"
      className={cn('min-w-0 rounded-lg border border-primary/20 bg-primary/5 px-3 py-2', className)}
    >
      <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-0.5">
        <h3 className="inline-flex shrink-0 items-center gap-1.5 text-sm font-semibold text-foreground">
          <ShieldCheck aria-hidden="true" className="size-3.5 shrink-0 text-primary" />
          分组抽签审计
        </h3>
        <p className="min-w-0 text-xs leading-relaxed text-muted-foreground">
          {formatDrawAlgorithm(snapshot.algorithm)}
        </p>
      </div>
      <dl className="mt-1.5 flex min-w-0 flex-wrap items-baseline gap-x-4 gap-y-1 text-xs">
        <div className="inline-flex min-w-0 items-baseline gap-1.5">
          <dt className="shrink-0 text-muted-foreground">分组规模</dt>
          <dd className="min-w-0 break-words font-medium text-foreground">
            {snapshot.group_count} 组 · {sizeText}
          </dd>
        </div>
        {snapshot.expected_match_count !== undefined && (
          <div className="inline-flex min-w-0 items-baseline gap-1.5">
            <dt className="shrink-0 text-muted-foreground">计划总场数</dt>
            <dd className="font-mono font-medium tabular-nums text-foreground">
              {snapshot.expected_match_count} 场
            </dd>
          </div>
        )}
        <div className="inline-flex min-w-0 items-baseline gap-1.5">
          <dt className="shrink-0 text-muted-foreground">抽签记录值</dt>
          <dd className="flex min-w-0 flex-wrap items-center gap-1.5">
            <code className="max-w-full rounded bg-muted px-1.5 py-1 font-mono text-xs text-foreground">
              <OverflowText tooltip={snapshot.audit_digest} className="block break-all">
                {shortDigest}
              </OverflowText>
            </code>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className="h-11 shrink-0 px-3"
              onClick={() => void copyDigest()}
              aria-label="复制完整抽签记录值"
            >
              {copied ? <Check aria-hidden="true" className="size-3.5" /> : <Copy aria-hidden="true" className="size-3.5" />}
              {copied ? '已复制' : '复制'}
            </Button>
          </dd>
        </div>
      </dl>
      {snapshot.source && (
        <div className="mt-1.5 border-t border-primary/15 pt-1.5">
          <p className="text-xs text-muted-foreground">
            保护种子来源：
            <Link className="font-medium text-primary hover:underline" to={`/contests/${snapshot.source.contest_id}`}>
              五子棋模拟赛 #{snapshot.source.contest_id}
            </Link>
          </p>
          <ol className="mt-1 grid min-w-0 gap-x-3 gap-y-0.5 sm:grid-cols-2">
            {snapshot.source.protected.map((seed) => (
              <li key={seed.entry_id} className="min-w-0 rounded-md bg-background/70 px-2 py-1 text-xs leading-relaxed">
                <span className="font-semibold text-foreground">来源第 {seed.source_rank} 名</span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </section>
  )
}
