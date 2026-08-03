/** 播放速度档（ms = 每步间隔毫秒）。

MatchViewer 内联实现了自己的 buffer/stepper 逻辑（不调 usePlayback hook），仅消费
本文件的 SPEEDS 常量。历史 usePlayback hook 已随 playback-controls.tsx 一并删除。 */
export const SPEEDS = [
  { label: '0.5x', ms: 1400 },
  { label: '1x', ms: 700 },
  { label: '2x', ms: 350 },
  { label: '4x', ms: 175 },
] as const
