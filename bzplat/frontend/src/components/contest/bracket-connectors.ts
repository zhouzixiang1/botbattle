export interface BracketConnectorNode {
  id: number
  round_num?: number | null
  bracket_slot?: number | null
  tiebreak_group?: number | null
  tiebreak_game?: number | null
}

export interface BracketConnectorEdge {
  sourceId: number
  targetId: number
}

function exactCoordinate(value: number | null | undefined): number {
  return Number.isInteger(value) && Number(value) > 0 ? Number(value) : 0
}

function encounterCardOrder(a: BracketConnectorNode, b: BracketConnectorNode): number {
  const groupDelta = exactCoordinate(a.tiebreak_group) - exactCoordinate(b.tiebreak_group)
  if (groupDelta !== 0) return groupDelta
  const gameDelta = exactCoordinate(a.tiebreak_game) - exactCoordinate(b.tiebreak_game)
  if (gameDelta !== 0) return gameDelta
  return a.id - b.id
}

/**
 * Return one connector per logical elimination encounter.
 *
 * A primary game and every appended paired-swap tiebreak share the same
 * round/slot.  The tree line leaves the latest physical game in that encounter
 * and enters the next encounter's primary card.  This avoids drawing several
 * false advancement lines or targeting a later tiebreak card.
 */
export function bracketConnectorEdges(
  pairings: readonly BracketConnectorNode[],
): BracketConnectorEdge[] {
  const encounters = new Map<string, BracketConnectorNode[]>()
  for (const pairing of pairings) {
    const round = pairing.round_num
    const slot = pairing.bracket_slot
    if (!Number.isInteger(round) || Number(round) < 1) continue
    if (!Number.isInteger(slot) || Number(slot) < 0) continue
    const key = `${round}:${slot}`
    const rows = encounters.get(key) ?? []
    rows.push(pairing)
    encounters.set(key, rows)
  }

  const edges: BracketConnectorEdge[] = []
  for (const [key, rows] of encounters) {
    const [roundText, slotText] = key.split(':')
    const round = Number(roundText)
    const slot = Number(slotText)
    const targetRows = encounters.get(`${round + 1}:${Math.floor(slot / 2)}`)
    if (!targetRows?.length) continue
    const orderedSources = [...rows].sort(encounterCardOrder)
    const orderedTargets = [...targetRows].sort(encounterCardOrder)
    edges.push({
      sourceId: orderedSources[orderedSources.length - 1].id,
      targetId: orderedTargets[0].id,
    })
  }
  return edges
}
