# Botzone / CCGC research notes (downloaded 2026-07-31)

## Local files
- wiki HTML: Bot, Gomoku, TexasHoldem2p/6p, 对局, 游戏桌, 天梯, 游戏, 裁判
- assets/: TexasHoldemHandType.jpg, BotRunMode_Traditional.png, BotRunMode_LongRunning.png
- game pages: Gomoku_page.html, Texas_page.html
- listusersgroupsandgames dump: listusers.json

## Botzone resource limits (Wiki Bot)
- 1 CPU core VM
- 256 MB memory default
- 1s per turn (first turn x2); language multipliers
- Errors: TLE/MLE/OLE/NJ/RE

## Games on Botzone relevant
- TexasHoldem2p: 50 hands, 20000 chips, SB50/BB100, JSON int response
- TexasHoldem6p: 18 hands
- Gomoku: 15x15, {x,y}, first {-1,-1}
- Gomoku-Swap1 / Renju-Official: color-swap variants
- NO DotsAndBoxes on Botzone wiki/game list

## CCGC 2026 general rules (saikr)
- Double RR or group double RR by entry count; seeds by history
- Group results do NOT carry into finals
- Between matches: <=10 min to tweak program/params (not hardware)
- Timed matches; timeout = loss

## Pencil (点格棋) — corrected
- Botzone game: https://botzone.org.cn/game/Pencil
- Wiki: https://wiki.botzone.org.cn/index.php?title=Pencil
- N=11 dots; red first; capture-and-continue; {x,y,pass} protocol
- Local copies: Pencil.html, Pencil_game.html
- game_id in our platform: pencil

## Platform scale note
- Daily training + school contests, relatively large n
- Prefer Swiss / group DRR over full RR; guardrail n>12 for full RR
