"""Configuración externa de la estrategia (punto 7).

Los umbrales viven en ``config.json`` (se crea con estos valores por defecto
la primera vez) y pueden sobreescribirse con variables de entorno.
Así se itera sobre el backtesting cambiando números, sin tocar código.

Prioridad entre fuentes: variables de entorno > config.json > defaults.

Variables de entorno reconocidas:
    RISK_PER_TRADE, TOTAL_CAPITAL, EXPOSURE_CAP,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DISCORD_WEBHOOK,
    JOURNAL_PATH, REQUIRE_SPY_BULLISH, REQUIRE_WEEKLY_TREND
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

CONFIG_PATH = Path("config.json")


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name, "").strip().lower()
    if raw in ("1", "true", "yes", "y", "on"):
        return True
    if raw in ("0", "false", "no", "n", "off"):
        return False
    return None


@dataclass
class StrategyConfig:
    # Filtros de entrada (antes hardcodeados en update_scan)
    max_dist_ema8: float = 1.5
    min_rsi: float = 45.0
    max_rsi: float = 65.0
    min_vol_ratio: float = 1.2
    adx_min: float = 20.0
    # Fuerza relativa vs SPY: diferencia de retornos en puntos porcentuales
    # (ticker - SPY) en RS_WINDOW sesiones. 0.0 = al menos igual que SPY.
    rs_min: float = 0.0
    require_spy_bullish: bool = True
    # Gestión de la posición (ATR multiples)
    # SL inicial 2.5x: aguanta ruido semanas/meses sin saltar antes de tiempo.
    atr_sl_mult: float = 2.5
    # TP fijo deprecated como salida (paso 2: lo sustituye el trailing stop);
    # se conserva por compat (journal/backtest históricos y config existente).
    atr_tp_mult: float = 3.0
    # Chandelier Exit: trailing = max_desde_entrada - trail_mult * ATR.
    atr_trail_mult: float = 3.0
    # Capital y riesgo
    risk_per_trade: float = 100.0
    total_capital: float = 10000.0
    # Costes del Broker Naranja de ING (solo para expectancy NETO del
    # backtest; no afectan a señal, sizing ni gestión). Comisión por
    # operación: fijo + % del importe con tope; FX 0,50% por cada lado
    # (compra y venta requieren conversión EUR/USD). Divisa $1 = 1 EUR
    # como en el resto de la app (aproximación, no contabilidad exacta).
    broker_commission_fixed: float = 3.0
    broker_commission_pct: float = 0.001
    broker_commission_cap: float = 20.0
    broker_fx_pct: float = 0.005
    # Interactive Brokers (segundo modelo, mismas unidades $1 = 1 EUR):
    # comisión por acción con mínimo por operación; FX casi interbancario.
    # En cripto (sizing fraccional, sin acciones enteras) no hay comisión
    # por acción — solo FX — igual que el sizing fraccional del vivo.
    ib_commission_per_share: float = 0.005
    ib_commission_min: float = 1.0
    ib_fx_pct: float = 0.00002
    # EXPOSURE_CAP: capital total disponible para ESTA estrategia (no todo
    # el patrimonio del usuario). Tope del contador de exposición: suma del
    # capital invertido al abrir (unidades x precio_entrada) de positions.
    # Divisa: la app trata $1 = 1 EUR para este control (sin conversion
    # real). Aproximacion intencional para una salvaguarda de riesgo,
    # no para contabilidad exacta.
    exposure_cap: float = 8000.0
    # Multi-timeframe
    require_weekly_trend: bool = True
    # Backtest / journal
    max_hold_days: int = 60
    journal_path: str = "signals.db"
    # Alertas (vacío = desactivadas; mejor via entorno para no subir secretos)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook: str = ""

    @property
    def rr_ratio(self) -> float:
        """Reward:Risk derivado de los multiplicadores ATR (3.0/1.5 = 2.0)."""
        if self.atr_sl_mult <= 0:
            return 0.0
        return self.atr_tp_mult / self.atr_sl_mult

    @property
    def rr_label(self) -> str:
        return f"1:{self.rr_ratio:.1f}"

    def to_rules_dict(self) -> dict:
        """Vista legacy compatible con STRATEGY_RULES (misma fuente de verdad)."""
        return {
            "MAX_DIST_EMA8": self.max_dist_ema8,
            "MIN_RSI": self.min_rsi,
            "MAX_RSI": self.max_rsi,
            "MIN_VOL_RATIO": self.min_vol_ratio,
            "REQUIRE_SPY_BULLISH": self.require_spy_bullish,
        }

    def to_json_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def _apply_env(cls, cfg: "StrategyConfig") -> "StrategyConfig":
        for name, attr, kind in (
            ("RISK_PER_TRADE", "risk_per_trade", float),
            ("TOTAL_CAPITAL", "total_capital", float),
            ("EXPOSURE_CAP", "exposure_cap", float),
            ("JOURNAL_PATH", "journal_path", str),
            ("TELEGRAM_BOT_TOKEN", "telegram_bot_token", str),
            ("TELEGRAM_CHAT_ID", "telegram_chat_id", str),
            ("DISCORD_WEBHOOK", "discord_webhook", str),
        ):
            raw = os.environ.get(name)
            if raw is None or raw == "":
                continue
            setattr(cfg, attr, kind(raw))
        for name, attr in (
            ("REQUIRE_SPY_BULLISH", "require_spy_bullish"),
            ("REQUIRE_WEEKLY_TREND", "require_weekly_trend"),
        ):
            val = _env_bool(name)
            if val is not None:
                setattr(cfg, attr, val)
        return cfg

    @classmethod
    def load(cls, path: str | Path = CONFIG_PATH) -> "StrategyConfig":
        """Carga config.json si existe (claves desconocidas se ignoran);
        si no existe, lo crea con los defaults. Luego aplica entorno."""
        p = Path(path)
        cfg = cls()
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    known = {f.name for f in fields(cls)}
                    for k, v in raw.items():
                        if k in known:
                            setattr(cfg, k, v)
                    # Backfill: si el fichero es de una versión anterior y le
                    # faltan claves nuevas, se reescribe con los valores del
                    # usuario preservados (nunca se tocan sus números).
                    if any(k not in raw for k in known):
                        p.write_text(
                            json.dumps(cfg.to_json_dict(), indent=2), encoding="utf-8"
                        )
            except Exception:
                pass  # fichero corrupto -> defaults en memoria, sin tocarlo
        else:
            try:
                p.write_text(json.dumps(cfg.to_json_dict(), indent=2), encoding="utf-8")
            except Exception:
                pass
        return cls._apply_env(cfg)

    def save(self, path: str | Path = CONFIG_PATH) -> None:
        Path(path).write_text(json.dumps(self.to_json_dict(), indent=2), encoding="utf-8")
