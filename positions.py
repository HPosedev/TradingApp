"""Registro simple de posiciones abiertas (punto 2 del cambio de salidas).

Formato en disco (positions.json, similar a tickers.json):
    {ticker: {"fecha_entrada": "2026-09-10",
              "precio_entrada": 150.0,
              "maximo_desde_entrada": 155.0}}

La app NO asume que una señal ENTRADA LARGO sea una compra: la posición
solo existe cuando el usuario la abre manualmente (tecla ``o``).
El trailing stop / breakeven (pasos 2-3) leerán este registro; aquí solo
tracking + persistencia, con el mismo manejo de errores best-effort del
resto de la app (nunca lanzar por IO corrupto).
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

# Fichero REAL del proyecto (anclado al repo, no al cwd): la app lo usa
# cuando corre desde la raíz del proyecto. Los tests/pilots nunca deben
# escribir aquí (fixture autouse + guard anti-pytest abajo).
REAL_POSITIONS_FILE = Path(__file__).resolve().parent / "positions.json"
BACKUP_DIR = Path(__file__).resolve().parent / "positions_backups"
MAX_BACKUPS = 20


def _running_under_test() -> bool:
    """True si hay un test en curso (pytest o unittest).

    pytest: variable PYTEST_CURRENT_TEST. unittest (runner documentado
    de este repo, sin fixtures globales): se detecta por la pila —
    algún marco activo de unittest/ ejecutando run/main.
    """
    if "PYTEST_CURRENT_TEST" in os.environ:
        return True
    try:
        import sys
        import threading
        f = sys._current_frames().get(threading.get_ident())
        while f is not None:
            name = (f.f_code.co_filename or "").replace("\\", "/")
            if "/unittest/" in name or name.endswith("/unittest.py"):
                if f.f_code.co_name in (
                    "run", "main", "_run_suite", "runTests", "__call__",
                ):
                    return True
            f = f.f_back
    except Exception:
        pass
    return False


def _is_real_file(target: Path) -> bool:
    """True si la ruta resuelta es el positions.json real del proyecto."""
    try:
        return target.resolve() == REAL_POSITIONS_FILE.resolve()
    except Exception:
        return False


def _backup_real_file() -> None:
    """Copia el fichero real con timestamp, purgando hasta MAX_BACKUPS.

    Solo para escrituras reales (no de test). Best-effort: nunca lanza.
    Nombres positions_YYYYMMDD_HHMMSS.json (sufijo _NN si colisiona el
    segundo); se conservan los 20 más recientes por orden de nombre.
    """
    try:
        src = REAL_POSITIONS_FILE
        if not src.exists():
            return
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = BACKUP_DIR / f"positions_{stamp}.json"
        n = 0
        while dest.exists():
            n += 1
            dest = BACKUP_DIR / f"positions_{stamp}_{n:02d}.json"
        dest.write_bytes(src.read_bytes())
        existing = sorted(BACKUP_DIR.glob("positions_*.json"), key=lambda p: p.name)
        for old in existing[:-MAX_BACKUPS]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception:
        pass


def positions_file(path: str | Path | None = None) -> Path:
    """Ruta del JSON de posiciones.

    Por defecto ``positions.json`` del cwd. La variable de entorno
    ``FINANZAS_POSITIONS`` la redirige (tests/pilots: fichero temporal
    aislado, nunca el real del usuario). Acepta path explícito, que
    tiene prioridad sobre la variable.
    """
    if path is not None:
        return Path(path)
    try:
        override = (os.environ.get("FINANZAS_POSITIONS") or "").strip()
        if override:
            return Path(override)
    except Exception:
        pass
    return Path("positions.json")


POSITIONS_FILE = positions_file()


def load_positions(path: str | Path | None = None) -> dict:
    """Lee el JSON de posiciones. Fichero ausente/corrupto -> {} (fail-open)."""
    p = positions_file(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for ticker, v in raw.items():
        try:
            if not isinstance(v, dict):
                continue
            entry = float(v["precio_entrada"])
            high = float(v.get("maximo_desde_entrada", entry))
            rec = {
                "fecha_entrada": str(v.get("fecha_entrada", date.today().isoformat())),
                "precio_entrada": entry,
                "maximo_desde_entrada": max(entry, high),
            }
            # Campos del paso 3 (opcionales en ficheros viejos): se
            # preservan si existen; si no, se derivan en el refresco.
            if "stop_inicial" in v:
                rec["stop_inicial"] = float(v["stop_inicial"])
            if "stop_actual" in v:
                rec["stop_actual"] = float(v["stop_actual"])
            if v.get("breakeven"):
                rec["breakeven"] = True
            # Exposición (capital invertido al abrir): se preserva si
            # existe; si no, la app lo deriva con ensure_sizing().
            if "unidades" in v:
                rec["unidades"] = float(v["unidades"])
            if "invertido" in v:
                rec["invertido"] = float(v["invertido"])
            out[str(ticker)] = rec
        except Exception:
            continue  # entrada corrupta -> se ignora, como tickers corruptos
    return out


def save_positions(positions: dict, path: str | Path | None = None) -> None:
    """Persiste el registro. Best-effort: nunca lanza (igual que save_tickers).

    Punto único de escritura (open/close/update_stop/update_maximum/
    ensure_sizing pasan por aquí): si el destino es el fichero REAL y
    hay un test en curso (pytest o unittest), se rechaza con
    RuntimeError en vez de escribir (el fixture autouse ya redirige a
    tmp; esto para en seco al test que se lo salte). Antes de una
    escritura real se guarda backup con timestamp (máx 20).
    """
    target = positions_file(path)
    if _is_real_file(target) and _running_under_test():
        raise RuntimeError(
            "positions: escritura al positions.json REAL bloqueada bajo "
            "test (usa FINANZAS_POSITIONS o un path temporal)"
        )
    try:
        if _is_real_file(target):
            _backup_real_file()
        target.write_text(json.dumps(positions, indent=2), encoding="utf-8")
    except RuntimeError:
        raise
    except Exception:
        pass


def sizing_units(
    ticker: str, price: float, sl: float, risk_per_trade: float
) -> tuple[float, float]:
    """Unidades e invertido con LA fórmula del sizing de la app.

    Fuente única de verdad para el tamaño: riesgo/op dividido por el
    riesgo por unidad (precio - SL). Cripto ("USD" en el ticker) en
    fracciones; acciones en nº entero (truncado). Riesgo insuficiente
    o risk_unit <= 0 -> (0.0, 0.0). Misma matemática que muestran el
    sidebar, el modal y el copiar-orden; nunca lanza (best-effort).
    Retorna (unidades, invertido = unidades * precio_entrada).
    """
    try:
        price_f, sl_f, risk_f = float(price), float(sl), float(risk_per_trade)
        risk_unit = price_f - sl_f
        if not (risk_unit > 0) or not (risk_f > 0):
            return 0.0, 0.0
        raw = risk_f / risk_unit
        units = float(raw) if "USD" in str(ticker) else float(int(raw))
        return units, units * price_f
    except Exception:
        return 0.0, 0.0


def position_exposure(rec: dict | None) -> float:
    """Exposición de una posición: capital invertido al abrir."""
    try:
        if not isinstance(rec, dict):
            return 0.0
        return max(0.0, float(rec.get("invertido", 0.0)))
    except Exception:
        return 0.0


def total_exposure(positions: dict | None) -> float:
    """Exposición total: suma del invertido de TODAS las abiertas.

    Divisa: $1 = 1 EUR (sin conversion real). Aproximacion intencional
    para una salvaguarda de riesgo, no contabilidad exacta.
    """
    try:
        if not isinstance(positions, dict):
            return 0.0
        return sum(position_exposure(r) for r in positions.values())
    except Exception:
        return 0.0


def ensure_sizing(
    positions: dict,
    ticker: str,
    risk_per_trade: float,
    path: str | Path | None = None,
) -> dict | None:
    """Deriva unidades/invertido si el registro no los trae (legacy).

    Usa precio_entrada + stop_inicial ya guardados con la misma fórmula
    de sizing_units. Sin stop_inicial no hay dato (queda en 0 hasta que
    el refresco derive el stop). Persiste best-effort. Retorna el
    registro o None si no hay posición.
    """
    pos = positions.get(str(ticker))
    if pos is None:
        return None
    try:
        if "unidades" in pos and "invertido" in pos:
            return pos
        if "stop_inicial" not in pos:
            return pos
        units, invested = sizing_units(
            ticker, pos["precio_entrada"], pos["stop_inicial"], risk_per_trade
        )
        pos["unidades"] = units
        pos["invertido"] = invested
        save_positions(positions, path)
        return pos
    except RuntimeError:
        raise  # guard anti-test: nunca tragarlo, debe parar en seco
    except Exception:
        return pos


def open_position(
    positions: dict,
    ticker: str,
    precio_actual: float,
    fecha: str | None = None,
    stop_inicial: float | None = None,
    unidades: float | None = None,
    invertido: float | None = None,
    path: str | Path | None = None,
) -> dict:
    """Registra apertura manual. Sobrescribe si ya existía (re-entrada)."""
    f = fecha or date.today().isoformat()
    rec = {
        "fecha_entrada": f,
        "precio_entrada": float(precio_actual),
        "maximo_desde_entrada": float(precio_actual),
    }
    if stop_inicial is not None:
        try:
            rec["stop_inicial"] = float(stop_inicial)
            rec["stop_actual"] = float(stop_inicial)
        except Exception:
            pass
    if unidades is not None:
        try:
            rec["unidades"] = float(unidades)
        except Exception:
            pass
    if invertido is not None:
        try:
            rec["invertido"] = float(invertido)
        except Exception:
            pass
    positions[str(ticker)] = rec
    save_positions(positions, path)
    return positions[str(ticker)]


def update_stop(
    positions: dict,
    ticker: str,
    atr_actual: float,
    atr_trail_mult: float,
    atr_sl_mult: float | None = None,
    path: str | Path | None = None,
) -> dict | None:
    """Recalcula el stop efectivo con breakeven + trailing (paso 3).

    Ratchet: el stop guardado solo sube, nunca baja. Si el registro viejo
    no trae stop_inicial (fichero del paso 1/2), se deriva como
    entrada - sl_mult * ATR actual cuando se aporta atr_sl_mult.
    Retorna el registro o None si no hay posición / ATR inválido.
    """
    import math as _m

    pos = positions.get(str(ticker))
    if pos is None:
        return None
    try:
        atr = float(atr_actual)
        if not _m.isfinite(atr) or atr <= 0:
            return pos
        entry = float(pos["precio_entrada"])
        mpx = float(pos.get("maximo_desde_entrada", entry))
        init = pos.get("stop_inicial")
        if init is None:
            if atr_sl_mult is None:
                return pos
            init = entry - float(atr_sl_mult) * atr
            pos["stop_inicial"] = init
            pos.setdefault("stop_actual", init)
        # Import tardío: evita ciclo positions <-> strategy.
        from strategy import stop_with_breakeven as _swb

        new_stop, triggered = _swb(
            entry, float(init), mpx, atr,
            float(atr_trail_mult), pos.get("stop_actual"),
        )
        pos["stop_actual"] = new_stop
        if triggered:
            pos["breakeven"] = True
        save_positions(positions, path)
        return pos
    except RuntimeError:
        raise  # guard anti-test: nunca tragarlo, debe parar en seco
    except Exception:
        return pos


def close_position(
    positions: dict, ticker: str, path: str | Path | None = None
) -> bool:
    """Elimina el registro (venta manual). Retorna True si existía."""
    if str(ticker) in positions:
        del positions[str(ticker)]
        save_positions(positions, path)
        return True
    return False


def update_maximum(
    positions: dict, ticker: str, precio_actual: float,
    path: str | Path | None = None,
) -> dict | None:
    """Actualiza el máximo histórico solo hacia arriba (nunca baja).

    Retorna el registro actualizado o None si no hay posición abierta.
    Persiste en disco (best-effort).
    """
    pos = positions.get(str(ticker))
    if pos is None:
        return None
    try:
        px = float(precio_actual)
    except Exception:
        return pos
    if px > float(pos.get("maximo_desde_entrada", px)):
        pos["maximo_desde_entrada"] = px
        save_positions(positions, path)
    return pos
