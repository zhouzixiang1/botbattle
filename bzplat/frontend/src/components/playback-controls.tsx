import {
  Play,
  Pause,
  ChevronLeft,
  ChevronRight,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Slider } from '@/components/ui/slider'
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
      <Select value={String(speedIdx)} onValueChange={(v) => onSpeedChange(Number(v))}>
        <SelectTrigger size="sm" className="h-8 w-[5rem] text-xs">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {SPEEDS.map((s, i) => (
            <SelectItem key={i} value={String(i)}>
              {s.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {/* 进度条 */}
      <div className="flex min-w-[120px] flex-1 items-center gap-2">
        <span className="font-mono text-[10px] text-muted-foreground">
          {total > 0 ? `${cur + 1}/${total}` : '—'}
          {lag > 0 && <span className="ml-1 text-warning">落后{lag}</span>}
          {atLive && total > 0 && <span className="ml-1 text-primary">●直播</span>}
        </span>
        <Slider
          min={0}
          max={Math.max(0, total - 1)}
          value={[cur]}
          onValueChange={(v) => onSeek(v[0])}
          disabled={total === 0}
          className="h-1 flex-1"
        />
      </div>
    </div>
  )
}
