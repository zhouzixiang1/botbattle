import { useEffect, useState } from 'react'
import { PlayCircle } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import type { HumanActionPanelProps } from '@/games/base'
import type { HoldemViewModel } from './reducer'

/** 从唯一的 Botzone TexasHoldem2p 请求字段推导双方本街投入。 */
function streetBets(request: Record<string, unknown> | null): [number, number] {
  if (!request) return [0, 0]
  const dealerId = Number(request.dealer_id ?? 0)
  const bets: [number, number] = [0, 0]
  bets[dealerId] = 50
  bets[1 - dealerId] = 100
  let lastRound = 0
  const history = (request.history as Array<Record<string, unknown>> | undefined) ?? []
  for (const item of history) {
    const round = Number(item.round ?? 0)
    if (round > lastRound) {
      bets[0] = 0
      bets[1] = 0
      lastRound = round
    }
    const player = Number(item.player_id)
    const action = Number(item.action)
    if ((player !== 0 && player !== 1) || !Number.isInteger(action)) continue
    if (action === -1 || action === -2) bets[player] = -1
    else if (action > 0) bets[player] += action
    else if (action === 0) bets[player] = Math.max(bets[0], bets[1])
  }
  return bets
}

function deriveToCall(request: Record<string, unknown> | null): number {
  if (!request) return 0
  const myId = Number(request.my_id ?? 0)
  const bets = streetBets(request)
  return Math.max(0, Math.max(bets[0], bets[1]) - (bets[myId] ?? 0))
}

function deriveMyBet(request: Record<string, unknown> | null): number {
  if (!request) return 0
  const myId = Number(request.my_id ?? 0)
  return Math.max(0, streetBets(request)[myId] ?? 0)
}

/** 德州的人类动作输入与整数 response 序列化全部留在游戏包内。 */
export function HoldemHumanActions({
  disabled,
  legal,
  request,
  onSubmit,
}: HumanActionPanelProps) {
  const toCall = deriveToCall(request)
  const myChips = Number(request?.my_chips ?? 20000)
  const myBet = deriveMyBet(request)
  const canCheck = toCall === 0
  const canCall = toCall > 0 && myChips > 0
  const minRaise = Math.max(toCall * 2, 200)
  const [raiseTo, setRaiseTo] = useState(minRaise)

  useEffect(() => {
    setRaiseTo(Math.max(minRaise, toCall + 100))
  }, [minRaise, toCall])

  const dis = disabled || !legal
  const submit = (response: number) => onSubmit({ response })
  const raiseDelta = Math.max(1, Math.round(raiseTo - myBet))
  const validRaise = Number.isFinite(raiseTo) && raiseTo > myBet && raiseDelta <= myChips

  return (
    <Card className="flex flex-row flex-wrap items-center gap-2 py-3">
      <Button type="button" variant="destructive" size="sm" disabled={dis} onClick={() => submit(-1)}>
        弃牌
      </Button>
      <Button type="button" variant="outline" size="sm" disabled={dis || !canCheck} onClick={() => submit(0)}>
        过牌
      </Button>
      <Button type="button" variant="outline" size="sm" disabled={dis || !canCall} onClick={() => submit(0)}>
        跟注{toCall > 0 ? ` ${toCall}` : ''}
      </Button>
      <Label className="flex min-w-0 items-center gap-2 text-sm text-muted-foreground">
        加注到
        <Input
          type="number"
          min={myBet + 1}
          max={myBet + myChips}
          className="w-24"
          value={raiseTo}
          disabled={dis}
          onChange={(event) => setRaiseTo(Number(event.target.value))}
        />
      </Label>
      <Button type="button" size="sm" disabled={dis || !validRaise} onClick={() => submit(raiseDelta)}>
        加注
      </Button>
      <Button type="button" size="sm" disabled={dis || myChips <= 0} onClick={() => submit(-2)}>
        All-in
      </Button>
      {legal && (
        <span className="flex min-w-0 items-center gap-1 text-xs text-success">
          <PlayCircle className="size-3.5" />
          轮到你{toCall > 0 ? ` · 需跟 ${toCall}` : ' · 可过牌'}
        </span>
      )}
    </Card>
  )
}

function formatNet(value: number): string {
  return `${value >= 0 ? '+' : ''}${value.toLocaleString('en-US')}`
}

export function holdemEndSummary(vm: unknown): string | null {
  const state = vm as HoldemViewModel | null
  if (!state?.seats) return null
  return `累计 ${formatNet(state.seats[0]?.net ?? 0)} / ${formatNet(state.seats[1]?.net ?? 0)}`
}
