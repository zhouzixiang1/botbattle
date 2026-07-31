"""Glicko-2 评分（自实现，无外部依赖）。"""
from __future__ import annotations

import math
from dataclasses import dataclass

SCALE = 173.7178  # 400/ln(10) * ln(10)/ln(10)... Glicko-2: 400/math.log(10) ≈ 173.7178


@dataclass
class Rating:
    mu: float = 1500.0  # displayed rating
    phi: float = 350.0  # RD
    sigma: float = 0.06  # volatility

    def to_g2(self) -> tuple[float, float, float]:
        return (self.mu - 1500) / SCALE, self.phi / SCALE, self.sigma

    @classmethod
    def from_g2(cls, mu: float, phi: float, sigma: float) -> "Rating":
        return cls(mu * SCALE + 1500, phi * SCALE, sigma)


def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def _E(mu: float, mu_j: float, phi_j: float) -> float:
    return 1.0 / (1.0 + math.exp(-_g(phi_j) * (mu - mu_j)))


def update_rating(r: Rating, opponents: list[tuple[Rating, float]],
                  tau: float = 0.5) -> Rating:
    """opponents: list of (opponent_rating, score) score in {0, 0.5, 1}."""
    if not opponents:
        # only increase RD slightly for inactivity — skip for match path
        return r
    mu, phi, sigma = r.to_g2()
    v_inv = 0.0
    delta_sum = 0.0
    for opp, score in opponents:
        mu_j, phi_j, _ = opp.to_g2()
        g_j = _g(phi_j)
        e = _E(mu, mu_j, phi_j)
        v_inv += g_j * g_j * e * (1 - e)
        delta_sum += g_j * (score - e)
    v = 1.0 / v_inv
    delta = v * delta_sum

    a = math.log(sigma * sigma)
    def f(x: float) -> float:
        ex = math.exp(x)
        num = ex * (delta * delta - phi * phi - v - ex)
        den = 2.0 * (phi * phi + v + ex) ** 2
        return num / den - (x - a) / (tau * tau)

    A = a
    if delta * delta > phi * phi + v:
        B = math.log(delta * delta - phi * phi - v)
    else:
        k = 1
        while f(a - k * tau) < 0:
            k += 1
            if k > 100:
                break
        B = a - k * tau
    fA, fB = f(A), f(B)
    for _ in range(50):
        if abs(B - A) < 1e-6:
            break
        C = A + (A - B) * fA / (fB - fA)
        fC = f(C)
        if fC * fB < 0:
            A, fA = B, fB
        else:
            fA /= 2.0
        B, fB = C, fC
    sigma_new = math.exp(A / 2.0)
    phi_star = math.sqrt(phi * phi + sigma_new * sigma_new)
    phi_new = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    mu_new = mu + phi_new * phi_new * delta_sum
    return Rating.from_g2(mu_new, phi_new, sigma_new)


def match_scores(winner: int | None) -> tuple[float, float]:
    """Return (score_a, score_b) for Glicko."""
    if winner == 0:
        return 1.0, 0.0
    if winner == 1:
        return 0.0, 1.0
    return 0.5, 0.5
