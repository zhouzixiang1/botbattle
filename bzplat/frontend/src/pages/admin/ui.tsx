import { type ReactNode } from 'react'

/* ── 管理端共享 UI 小组件（浅色卡片风格） ─────────────────── */

export function Card({ title, children }: { title?: string; children: ReactNode }) {
  return (
    <div className="card p-4">
      {title && <h3 className="mb-3 text-sm font-semibold text-slate-700">{title}</h3>}
      {children}
    </div>
  )
}

export function MetricCard({
  label,
  value,
  hint,
  danger,
}: {
  label: string
  value: ReactNode
  hint?: string
  danger?: boolean
}) {
  return (
    <div className="card p-4">
      <div className="text-xs font-medium uppercase tracking-wide text-slate-400">{label}</div>
      <div
        className={`mt-1 font-mono text-2xl font-bold ${
          danger ? 'text-error-600' : 'text-slate-800'
        }`}
      >
        {value}
      </div>
      {hint && <div className="mt-0.5 text-xs text-slate-400">{hint}</div>}
    </div>
  )
}

export function EmptyState({ text }: { text: string }) {
  return <div className="py-8 text-center text-sm text-slate-400">{text}</div>
}

export function Loading({ text = '加载中…' }: { text?: string }) {
  return <div className="py-8 text-center text-sm text-slate-400">{text}</div>
}

export function ErrorMsg({ msg }: { msg: string }) {
  return msg ? <p className="mb-3 text-sm text-error-500">{msg}</p> : null
}

export function StatusBadge({ status }: { status: string }) {
  const color: Record<string, string> = {
    completed: 'bg-success-50 text-success-600',
    running: 'bg-brand-50 text-brand-700',
    pending: 'bg-slate-100 text-slate-500',
    aborted: 'bg-error-50 text-error-600',
    finished: 'bg-success-50 text-success-600',
    open: 'bg-brand-50 text-brand-700',
    draft: 'bg-slate-100 text-slate-500',
    cancelled: 'bg-error-50 text-error-600',
    sent: 'bg-success-50 text-success-600',
    failed: 'bg-error-50 text-error-600',
    skipped: 'bg-slate-100 text-slate-500',
  }
  return (
    <span
      className={`inline-block rounded-full px-2 py-0.5 text-[10px] font-medium ${
        color[status] || 'bg-slate-100 text-slate-500'
      }`}
    >
      {status}
    </span>
  )
}

export function RefreshBtn({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-50"
    >
      ⟳ 刷新
    </button>
  )
}
