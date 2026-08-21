"""全国机器博弈竞赛五子棋纯裁判（游戏规则，0 平台依赖）。

只管游戏规则：15×15 棋盘、26 种指定开局的几何约束、五手二打候选
不同形、落子、胜负与计分。
黑方三三/四四/长连判定由同包纯模块 :mod:`forbidden` 提供。
不 import protocol/result/engine/orchestrator/runner —— 可独立审计/复用/单测。

适配层（engine.py GomokuSession）调用本模块做规则判定，自己做协议/事件/decide。
"""
from __future__ import annotations

BOARD_SIZE = 15
CENTER = BOARD_SIZE // 2
BLACK = 0
WHITE = 1
EMPTY = -1
BLACK5_CANDIDATE_COUNT = 2

# 方向：横、竖、两斜
_DIRS = ((1, 0), (0, 1), (1, 1), (1, -1))


def in_board(x: int, y: int, size: int = BOARD_SIZE) -> bool:
    """坐标是否在棋盘内。"""
    return 0 <= x < size and 0 <= y < size


def line_lengths(
    board: list[list[int]],
    x: int,
    y: int,
    player: int,
    size: int = BOARD_SIZE,
) -> tuple[int, int, int, int]:
    """返回经过 ``(x,y)`` 的横、竖、两斜最大连续长度。"""
    lengths: list[int] = []
    for dx, dy in _DIRS:
        count = 1
        for sign in (1, -1):
            cx, cy = x + sign * dx, y + sign * dy
            while in_board(cx, cy, size) and board[cx][cy] == player:
                count += 1
                cx += sign * dx
                cy += sign * dy
        lengths.append(count)
    return tuple(lengths)  # type: ignore[return-value]


def check_win(
    board: list[list[int]],
    x: int,
    y: int,
    player: int,
    size: int = BOARD_SIZE,
) -> bool:
    """白方连续至少五子即胜；黑方须由禁手分类器裁定恰好五连。"""
    lengths = line_lengths(board, x, y, player, size)
    return any(length >= 5 for length in lengths)


def _transforms(x: int, y: int) -> tuple[tuple[int, int], ...]:
    """正方形 D4 群的八种旋转/镜像，用于把开局归入 26 类。"""
    return (
        (x, y),
        (-y, x),
        (-x, -y),
        (y, -x),
        (-x, y),
        (x, -y),
        (y, x),
        (-y, -x),
    )


def _canonical_opening_key(
    white2: tuple[int, int], black3: tuple[int, int]
) -> tuple[str, int, int]:
    """把一个有向三手开局规整为斜指/直指下的 13 个对称类之一。"""
    wx, wy = white2[0] - CENTER, white2[1] - CENTER
    bx, by = black3[0] - CENTER, black3[1] - CENTER
    family = "straight" if wx == 0 or wy == 0 else "diagonal"
    target = (0, 1) if family == "straight" else (1, 1)
    candidates: list[tuple[int, int]] = []
    transformed_w = _transforms(wx, wy)
    transformed_b = _transforms(bx, by)
    for index, value in enumerate(transformed_w):
        if value == target:
            candidates.append(transformed_b[index])
    if not candidates:
        raise ValueError("白2必须位于天元相邻交叉点")
    cbx, cby = min(candidates)
    return family, cbx, cby


def _opening_catalog() -> dict[tuple[str, int, int], str]:
    keys: set[tuple[str, int, int]] = set()
    center = (CENTER, CENTER)
    for wx in range(CENTER - 1, CENTER + 2):
        for wy in range(CENTER - 1, CENTER + 2):
            if (wx, wy) == center:
                continue
            for bx in range(CENTER - 2, CENTER + 3):
                for by in range(CENTER - 2, CENTER + 3):
                    if (bx, by) in {center, (wx, wy)}:
                        continue
                    keys.add(_canonical_opening_key((wx, wy), (bx, by)))
    ordered = sorted(keys)
    straight = [key for key in ordered if key[0] == "straight"]
    diagonal = [key for key in ordered if key[0] == "diagonal"]
    if len(straight) != 13 or len(diagonal) != 13:
        raise RuntimeError("指定开局目录必须恰好包含直指/斜指各 13 类")
    catalog: dict[tuple[str, int, int], str] = {}
    for index, key in enumerate(straight, 1):
        catalog[key] = f"S{index:02d}"
    for index, key in enumerate(diagonal, 1):
        catalog[key] = f"D{index:02d}"
    return catalog


OPENING_CATALOG = _opening_catalog()


def validate_opening(
    white2: tuple[int, int],
    black3: tuple[int, int],
    n: int,
) -> str | None:
    """验证指定开局并返回稳定开局编号；非法时返回 ``None``。

    黑1固定 H8（内部坐标 ``(7,7)``）；白2必须相邻；黑3须位于中心
    5×5 且不能占用已有两点。对称规整后恰好形成直指/斜指各 13 类。
    """
    if (
        isinstance(n, bool)
        or not isinstance(n, int)
        or n != BLACK5_CANDIDATE_COUNT
    ):
        return None
    wx, wy = white2
    bx, by = black3
    if not (in_board(wx, wy) and in_board(bx, by)):
        return None
    if max(abs(wx - CENTER), abs(wy - CENTER)) != 1:
        return None
    if not (
        CENTER - 2 <= bx <= CENTER + 2
        and CENTER - 2 <= by <= CENTER + 2
    ):
        return None
    if (bx, by) in {(CENTER, CENTER), (wx, wy)}:
        return None
    return OPENING_CATALOG.get(_canonical_opening_key(white2, black3))


def validate_black5_candidates(
    board: list[list[int]],
    candidates: list[tuple[int, int]],
    size: int = BOARD_SIZE,
) -> bool:
    """验证五手二打候选点是当前彩色四子盘面下的“不同形”。

    两个候选点若能被某个保持当前黑/白子集合不变的 D4
    旋转或镜像互相映射，它们便是同形打点，不能同时提交。
    无剩余对称时稳定群只含恒等变换，因而任意不同空点均不同形。

    本函数是纯裁判边界：棋盘、四子颜色数量、坐标、空点或
    候选唯一性任一不合法均 fail closed。
    """
    if size != BOARD_SIZE:
        return False
    if len(board) != size or any(len(column) != size for column in board):
        return False
    if len(candidates) != BLACK5_CANDIDATE_COUNT:
        return False

    black: set[tuple[int, int]] = set()
    white: set[tuple[int, int]] = set()
    for x, column in enumerate(board):
        for y, cell in enumerate(column):
            if cell == BLACK:
                black.add((x, y))
            elif cell == WHITE:
                white.add((x, y))
            elif cell != EMPTY:
                return False
    if len(black) != 2 or len(white) != 2:
        return False

    normalized: list[tuple[int, int]] = []
    for point in candidates:
        if (
            not isinstance(point, tuple)
            or len(point) != 2
            or isinstance(point[0], bool)
            or isinstance(point[1], bool)
            or not isinstance(point[0], int)
            or not isinstance(point[1], int)
        ):
            return False
        x, y = point
        if not is_legal_move(board, x, y, size):
            return False
        normalized.append(point)
    if len(set(normalized)) != len(normalized):
        return False

    def transform(point: tuple[int, int], index: int) -> tuple[int, int]:
        x, y = point
        tx, ty = _transforms(x - CENTER, y - CENTER)[index]
        return tx + CENTER, ty + CENTER

    stable_transforms = [
        index
        for index in range(8)
        if {transform(point, index) for point in black} == black
        and {transform(point, index) for point in white} == white
    ]
    if not stable_transforms:  # pragma: no cover - identity always survives
        return False

    shape_keys = {
        min(transform(point, index) for index in stable_transforms)
        for point in normalized
    }
    return len(shape_keys) == len(normalized)


def board_full(board: list[list[int]]) -> bool:
    """棋盘是否已下满（无空位 -1）。"""
    return all(cell != -1 for row in board for cell in row)


def is_legal_move(board: list[list[int]], x: int | None, y: int | None, size: int = BOARD_SIZE) -> bool:
    """落子是否合法：坐标有效且该位为空。"""
    if x is None or y is None:
        return False
    if not in_board(x, y, size):
        return False
    return board[x][y] == -1


def new_board(size: int = BOARD_SIZE) -> list[list[int]]:
    """创建空棋盘（-1=空，0=黑，1=白）。"""
    return [[EMPTY for _ in range(size)] for _ in range(size)]


def compute_scores(winner: int | None) -> list[int]:
    """根据胜者计算比分 [黑分, 白分]：胜=1，负=0，平=0/0。"""
    scores = [0, 0]
    if winner is not None:
        scores[winner] = 1
    return scores


def compute_deltas(winner: int | None) -> list[int]:
    """根据胜者计算 deltas（零和）：黑胜 [+1,-1]，白胜 [-1,+1]，平 [0,0]。"""
    if winner == 0:
        return [1, -1]
    elif winner == 1:
        return [-1, 1]
    return [0, 0]


__all__ = [
    "BOARD_SIZE",
    "CENTER",
    "BLACK",
    "WHITE",
    "EMPTY",
    "BLACK5_CANDIDATE_COUNT",
    "OPENING_CATALOG",
    "in_board",
    "line_lengths",
    "check_win",
    "validate_opening",
    "validate_black5_candidates",
    "board_full",
    "is_legal_move",
    "new_board",
    "compute_scores",
    "compute_deltas",
]
