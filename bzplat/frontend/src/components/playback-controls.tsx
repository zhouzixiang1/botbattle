import {
  Play,
  Pause,
  ChevronLeft,
  ChevronRight,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { SPEEDS } from '@/components/use-playback'

/** 回放控制条：播放/暂停 + 步进 + 速度档 + 进度。MatchDetail / ArenaWatch 共用。 */
export function PlaybackControls({
  cur,
  total,
  playing,
  speedIdx,
  atLive,
  lag,
  onTogglePlay,
  onStep,
  onSeek,
  onSpeedChange,
  isBoard,
  compact,
}: {
  cur: number
  total: number
  playing: boolean
  speedIdx: number
  atLive: boolean
  lag: number
  onTogglePlay: () => void
  onStep: (delta: number) => void
  onSeek: (idx: number) => void
  onSpeedChange: (idx: number) => void
  isBoard?: boolean
  compact?: boolean
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {!isBoard && (
        <Button variant="outline" size="sm" onClick={() => onStep(-1)} className="gap-1" disabled={total === 0}>
          <ChevronLeft className="size-4" />
          {!compact && '上一步'}
        </Button>
      )}
      <Button
        variant="default"
        size="sm"
        onClick={onTogglePlay}
        className="gap-1.5"
        disabled={total === 0}
      >
        {playing ? (
          <>
            <Pause className="size-4" />暂停
          </>
        ) : (
          <>
            <Play className="size-4" />
            {atLive ? '播放' : '继续'}
          </>
        )}
      </Button>
      <Button variant="outline" size="sm" onClick={() => onStep(1)} className="gap-1" disabled={total === 0}>
        {!compact && '下一步'}
        <ChevronRight className="size-4" />
      </Button>
      <select
        value={speedIdx}
        onChange={(e) => onSpeedChange(Number(e.target.value))}
        className="h-8 rounded-lg border border-input bg-background px-2 text-xs text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
      >
        {SPEEDS.map((s, i) => (
          <option key={i} value={i}>
            {s.label}
          </option>
        ))}
      </select>
      {/* 进度条 */}
      <div className="flex min-w-[120px] flex-1 items-center gap-2">
        <span className="font-mono text-[10px] text-muted-foreground">
          {total > 0 ? `${cur + 1}/${total}` : '—'}
          {lag > 0 && <span className="ml-1 text-warning">落后{lag}</span>}
          {atLive && total > 0 && <span className="ml-1 text-primary">●直播</span>}
        </span>
        <input
          type="range"
          min={0}
          max={Math.max(0, total - 1)}
          value={cur}
          onChange={(e) => onSeek(Number(e.target.value))}
          disabled={total === 0}
          className="h-1 flex-1 cursor-pointer accent-primary"
        />
      </div>
    </div>
  )
}
