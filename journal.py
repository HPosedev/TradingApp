"""Journal de señales en vivo (punto 2): SQLite stdlib, sin dependencias.

Cada señal LONG/WEAK se registra una vez por (ticker, fecha). En cada scan,
con el histórico recién descargado se resuelven las pendientes: ¿tocó TP
antes que SL? Lógica conservadora: si una misma barra toca ambos, gana el SL.

Resultados: WIN | LOSS | EXPIRED (max_hold_days sin tocar nada) | PENDING.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    price REAL NOT NULL,
    sl REAL NOT NULL,
    tp REAL NOT NULL,
    spy_regime TEXT NOT NULL DEFAULT 'UNKNOWN',
    kind TEXT NOT NULL DEFAULT 'LONG',
    result TEXT NOT NULL DEFAULT 'PENDING',
    resolved_date TEXT,
    UNIQUE(ticker, date)
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.execute(SCHEMA)
    con.commit()
    return con


def log_signal(
    con: sqlite3.Connection,
    ticker: str,
    date: str,
    price: float,
    sl: float,
    tp: float,
    spy_regime: str = "UNKNOWN",
    kind: str = "LONG",
) -> bool:
    """Registra la señal. Retorna True si es nueva (para alertar solo 1 vez)."""
    try:
        con.execute(
            "INSERT INTO signals (ticker, date, price, sl, tp, spy_regime, kind)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticker, date, float(price), float(sl), float(tp), spy_regime, kind),
        )
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False  # ya estaba registrada


def resolve_outcome(
    entry_date: str, sl: float, tp: float, future: pd.DataFrame, max_hold_days: int = 60
) -> tuple[str, str | None]:
    """Determina el desenlace con barras posteriores a la entrada.

    future: DataFrame con índice DatetimeIndex y columnas High/Low,
    solo sesiones estrictamente posteriores a entry_date.
    Retorna (resultado, fecha_resolución).
    """
    if future is None or future.empty:
        return "PENDING", None
    entry = pd.Timestamp(entry_date)
    # yfinance devuelve índice tz-aware (America/New_York; UTC en cripto):
    # localizar la entrada para no comparar aware vs naive (TypeError).
    tz = getattr(future.index, "tz", None)
    if tz is not None and entry.tzinfo is None:
        entry = entry.tz_localize(tz)
    bars = future[future.index > entry].head(max_hold_days)
    for ts, row in bars.iterrows():
        hit_sl = bool(row["Low"] <= sl)
        hit_tp = bool(row["High"] >= tp)
        if hit_sl and hit_tp:
            return "LOSS", ts.date().isoformat()  # conservador: primero el SL
        if hit_sl:
            return "LOSS", ts.date().isoformat()
        if hit_tp:
            return "WIN", ts.date().isoformat()
    if len(future[future.index > entry]) >= max_hold_days:
        last = bars.index[-1] if len(bars) else entry
        return "EXPIRED", last.date().isoformat() if len(bars) else None
    return "PENDING", None


def resolve_pending_for_ticker(
    con: sqlite3.Connection, ticker: str, df: pd.DataFrame, max_hold_days: int = 60
) -> int:
    """Resuelve las PENDING de un ticker usando su histórico recién descargado.
    Retorna cuántas se resolvieron."""
    rows = con.execute(
        "SELECT id, date, sl, tp FROM signals WHERE ticker = ? AND result = 'PENDING'",
        (ticker,),
    ).fetchall()
    resolved = 0
    for sid, date, sl, tp in rows:
        result, resolved_date = resolve_outcome(date, sl, tp, df, max_hold_days)
        if result != "PENDING":
            con.execute(
                "UPDATE signals SET result = ?, resolved_date = ? WHERE id = ?",
                (result, resolved_date, sid),
            )
            resolved += 1
    if resolved:
        con.commit()
    return resolved


def stats(con: sqlite3.Connection) -> dict:
    """Win-rate y conteos sobre señales ya resueltas (WIN/LOSS)."""
    rows = con.execute(
        "SELECT result, COUNT(*) FROM signals WHERE result IN ('WIN', 'LOSS') GROUP BY result"
    ).fetchall()
    counts = {r: c for r, c in rows}
    wins = counts.get("WIN", 0)
    losses = counts.get("LOSS", 0)
    total = wins + losses
    pending = con.execute(
        "SELECT COUNT(*) FROM signals WHERE result = 'PENDING'"
    ).fetchone()[0]
    return {
        "wins": wins,
        "losses": losses,
        "total": total,
        "pending": pending,
        "win_rate": (wins / total * 100.0) if total else 0.0,
    }
