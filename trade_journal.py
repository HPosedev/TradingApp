"""Journal persistente de operaciones cerradas (journal.json).

Al cerrar una posición con 'x', la app registra aquí el resultado antes
de borrarla de positions.json. Sirve para medir el rendimiento real de
la estrategia (win rate, R promedio, expectancy).

Aislamiento: FINANZAS_JOURNAL redirige a un fichero temporal (tests y
pilots nunca tocan el real); bajo test runner, escribir a la ruta real
lanza RuntimeError igual que en positions.py.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from positions import _running_under_test

REAL_JOURNAL_FILE = Path(__file__).resolve().parent / "journal.json"

MIN_SAMPLE = 5  # bajo este nº, las estadísticas no se muestran como fiables


def journal_file(path: str | Path | None = None) -> Path:
    """Ruta del journal. FINANZAS_JOURNAL la redirige (tests/pilots)."""
    if path is not None:
        return Path(path)
    try:
        override = (os.environ.get("FINANZAS_JOURNAL") or "").strip()
        if override:
            return Path(override)
    except Exception:
        pass
    return Path("journal.json")


def _is_real_file(target: Path) -> bool:
    try:
        return target.resolve() == REAL_JOURNAL_FILE.resolve()
    except Exception:
        return False


def load_journal(path: str | Path | None = None) -> list:
    """Lee el journal. Ausente/corrupto -> [] (fail-open, best-effort)."""
    p = journal_file(path)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    return [t for t in raw if isinstance(t, dict)]


def _parse_day(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except Exception:
        return None


def close_trade(
    ticker: str,
    position: dict,
    precio_cierre: float,
    fecha_cierre: str | None = None,
    motivo_cierre: str = "manual",
) -> dict:
    """Calcula el registro de cierre a partir de la posición abierta.

    Puro (sin IO): resultado_usd = (cierre - entrada) * unidades;
    resultado_r = usd / (riesgo_inicial * unidades) con riesgo_inicial =
    entrada - stop_inicial. Sin unidades o sin riesgo positivo, R = 0.0.
    Nunca lanza (best-effort).
    """
    try:
        entry = float(position.get("precio_entrada", 0.0))
        exit_px = float(precio_cierre)
        units = float(position.get("unidades", 0.0) or 0.0)
        stop_init = position.get("stop_inicial")
        risk_unit = entry - float(stop_init) if stop_init is not None else 0.0
        usd = (exit_px - entry) * units
        risk_total = risk_unit * units
        r_mult = usd / risk_total if risk_total > 0 else 0.0
        d_in = _parse_day(position.get("fecha_entrada"))
        d_out = _parse_day(fecha_cierre) or date.today()
        days = (d_out - d_in).days if d_in is not None else 0
        return {
            "ticker": str(ticker),
            "fecha_entrada": str(position.get("fecha_entrada", "")),
            "fecha_cierre": d_out.isoformat(),
            "dias_en_mercado": max(0, int(days)),
            "precio_entrada": entry,
            "precio_cierre": exit_px,
            "unidades": units,
            "riesgo_inicial": max(0.0, float(risk_unit)),
            "resultado_usd": float(usd),
            "resultado_r": float(r_mult),
            "motivo_cierre": str(motivo_cierre),
        }
    except Exception:
        return {
            "ticker": str(ticker),
            "fecha_entrada": "",
            "fecha_cierre": str(fecha_cierre or date.today().isoformat()),
            "dias_en_mercado": 0,
            "precio_entrada": 0.0,
            "precio_cierre": 0.0,
            "unidades": 0.0,
            "riesgo_inicial": 0.0,
            "resultado_usd": 0.0,
            "resultado_r": 0.0,
            "motivo_cierre": str(motivo_cierre),
        }


def log_close(
    ticker: str,
    position: dict,
    precio_cierre: float,
    fecha_cierre: str | None = None,
    motivo_cierre: str = "manual",
    path: str | Path | None = None,
) -> dict:
    """Añade el cierre al journal y persiste. Retorna el registro."""
    target = journal_file(path)
    if _is_real_file(target) and _running_under_test():
        raise RuntimeError(
            "trade_journal: escritura al journal.json REAL bloqueada bajo "
            "test (usa FINANZAS_JOURNAL o un path temporal)"
        )
    rec = close_trade(ticker, position, precio_cierre, fecha_cierre, motivo_cierre)
    try:
        trades = load_journal(target)
        trades.append(rec)
        target.write_text(json.dumps(trades, indent=2), encoding="utf-8")
    except RuntimeError:
        raise
    except Exception:
        pass
    return rec


def stats(trades: list | None) -> dict:
    """Agregados sobre el journal. Puro, nunca lanza.

    Ganadora = resultado_usd > 0. Expectancy =
    win_rate * R_ganadoras - (1 - win_rate) * |R_perdedoras|.
    """
    try:
        rows = [t for t in (trades or []) if isinstance(t, dict)]
        n = len(rows)

        def _is_win(t: dict) -> bool:
            try:
                return float(t.get("resultado_usd", 0.0) or 0.0) > 0
            except Exception:
                return False

        wins = [t for t in rows if _is_win(t)]
        losses = [t for t in rows if not _is_win(t)]
        wr = len(wins) / n if n else 0.0

        def _mean_rs(group: list) -> float:
            if not group:
                return 0.0
            return sum(float(t.get("resultado_r", 0.0) or 0.0) for t in group) / len(group)

        def _mean_days(group: list) -> float:
            if not group:
                return 0.0
            return sum(int(t.get("dias_en_mercado", 0) or 0) for t in group) / len(group)

        avg_w = _mean_rs(wins)
        avg_l = _mean_rs(losses)
        return {
            "n": n,
            "win_rate": wr,
            "avg_r_winners": avg_w,
            "avg_r_losers": avg_l,
            "expectancy": wr * avg_w - (1.0 - wr) * abs(avg_l),
            "avg_days": _mean_days(rows),
            "avg_days_winners": _mean_days(wins),
            "avg_days_losers": _mean_days(losses),
            "reliable": n >= MIN_SAMPLE,
        }
    except Exception:
        return {
            "n": 0, "win_rate": 0.0, "avg_r_winners": 0.0,
            "avg_r_losers": 0.0, "expectancy": 0.0, "avg_days": 0.0,
            "avg_days_winners": 0.0, "avg_days_losers": 0.0,
            "reliable": False,
        }
