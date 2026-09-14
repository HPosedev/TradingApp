"""Backtesting de la estrategia con la MISMA gestión que el scanner en vivo.

Motor trailing-stop (nuevo): simula día a día sobre el máximo histórico
disponible usando las mismas funciones que la app en vivo — indicadores,
8 filtros, trailing Chandelier 3.0x, breakeven +1R, sizing y registro de
trades — sin reescribir la lógica en paralelo.

Motor legacy TP-vs-SL (run_backtest): se conserva intacto por compatibilidad
con los tests existentes; ya no es el informe principal.

Uso:
    .venv/bin/python backtest.py [TICKER ...]   # por defecto tickers.json
    .venv/bin/python backtest.py --universe=etfs   # tickers_etf_test.json
    .venv/bin/python backtest.py --universe=RUTA.json [TICKER ...]

Supuestos del motor trailing (documentados):
- Sin look-ahead: al evaluar el día D solo se usan datos hasta D
  inclusive. Los indicadores rolling/ewm son causales por construcción;
  el régimen SPY y la serie semanal se leen indexados en D; el filtro
  semanal truncado en D solo ve cierres <= D (ver tests de equivalencia).
- Entrada al cierre del día de señal LONG (8/8 + SPY alcista), SL
  inicial = cierre - 2.5xATR(D), unidades con sizing_units (igual que 'o').
- Gestión diaria con stop_with_breakeven (la MISMA función que usa
  positions.update_stop): máximo de cierres, trailing 3.0x con ATR del
  día, breakeven a +1R, ratchet (el stop nunca baja).
- Salida el primer día con cierre < stop efectivo (simplificación
  deliberada a cierres: el journal en vivo es conservador intra-barra).
- Una posición a la vez por ticker (sin pirámides); sizing 0 -> se omite.
- Posición abierta al final del historial: se cierra al último cierre
  con motivo "fin-historial".
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

from journal import resolve_outcome
from positions import sizing_units
from strategy import (
    RS_WINDOW,
    WEEKLY_MIN_BARS,
    WEEKLY_SLOPE_LOOKBACK,
    WEEKLY_SMA,
    compute_indicators,
    decide_signal,
    initial_stop,
    relative_strength,
    spy_regime,
    stop_with_breakeven,
)
from strategy_config import StrategyConfig
from trade_journal import close_trade, stats as journal_stats

EVAL_YEARS = 3
HISTORY_PERIOD = "max"
RESULTS_PATH = Path("backtest_results.json")


def _flat(df: pd.DataFrame) -> pd.DataFrame:
    """Colapsa columnas MultiIndex de yfinance a un solo nivel."""
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df


def download(ticker: str, period: str = HISTORY_PERIOD) -> pd.DataFrame:
    df = _flat(yf.Ticker(ticker).history(period=period, interval="1d"))
    df = df.dropna(subset=["Close"])
    return df


def weekly_state_series(daily: pd.DataFrame) -> pd.Series:
    """Serie bool por fecha: filtro semanal OK con solo datos pasados.

    Resamplea a semanas W-FRI, SMA200 semanal + pendiente 4 semanas,
    reindexa a fechas diarias con ffill. Sin 205 semanas -> True (fail-open,
    igual que el scanner en vivo).
    """
    idx = daily.index
    weekly_close = daily["Close"].resample("W-FRI").last().dropna()
    ok = pd.Series(True, index=idx)
    if len(weekly_close) < WEEKLY_MIN_BARS:
        return ok
    sma200w = weekly_close.rolling(window=WEEKLY_SMA).mean()
    slope_ok = sma200w > sma200w.shift(WEEKLY_SLOPE_LOOKBACK)
    price_ok = weekly_close > sma200w
    weekly_ok = (price_ok & slope_ok).reindex(idx, method="ffill").fillna(True)
    return weekly_ok.astype(bool)


def rs_diff_series(ticker_df: pd.DataFrame, spy_df: pd.DataFrame,
                   window: int = RS_WINDOW) -> pd.Series:
    """Fuerza relativa por fecha (puntos porcentuales, ticker − SPY).

    Retornos causales (close/close.shift(window)); el retorno SPY se alinea
    por fecha con ffill (para barras sin SPY, p. ej. cripto en fin de
    semana, vale el último disponible). Sin datos -> NaN (= no supera).
    """
    t_ret = ticker_df["Close"] / ticker_df["Close"].shift(window) - 1
    if spy_df is None or spy_df.empty or "Close" not in spy_df.columns:
        return pd.Series(float("nan"), index=ticker_df.index)
    s_ret = spy_df["Close"] / spy_df["Close"].shift(window) - 1
    s_aligned = s_ret.reindex(ticker_df.index, method="ffill")
    return (t_ret - s_aligned) * 100


def spy_regime_series(spy: pd.DataFrame, index: pd.DatetimeIndex) -> pd.Series:
    """Régimen SPY por fecha, alineado al calendario del ticker (ffill)."""
    sma50 = spy["Close"].rolling(window=50).mean()
    sma200 = spy["Close"].rolling(window=200).mean()
    regimes = pd.Series("UNKNOWN", index=spy.index)
    valid = sma50.notna() & sma200.notna()
    bull = (spy["Close"] > sma200) & (spy["Close"] > sma50)
    pull = (spy["Close"] > sma200) & (spy["Close"] <= sma50)
    regimes[bull & valid] = "BULLISH"
    regimes[pull & valid] = "PULLBACK"
    regimes[~bull & ~pull & valid] = "BEARISH"
    return regimes.reindex(index, method="ffill").fillna("UNKNOWN")


def run_backtest(ticker: str, cfg: StrategyConfig,
                 df: pd.DataFrame, spy: pd.DataFrame) -> dict:
    """Ejecuta el backtest sobre frames ya descargados (testeable)."""
    if len(df) < 250:
        return {"ticker": ticker, "error": "datos insuficientes"}
    ind = compute_indicators(df, cfg.atr_sl_mult, cfg.atr_tp_mult)
    weekly_ok = weekly_state_series(df)
    regimes = spy_regime_series(spy, df.index)
    rs_diff = rs_diff_series(df, spy)

    cutoff = df.index[-1] - pd.DateOffset(years=EVAL_YEARS)
    eval_idx = df.index[df.index >= cutoff]

    long_trades: list[str] = []
    weak_trades: list[str] = []
    for ts in eval_idx:
        row = ind.loc[ts]
        if pd.isna(row["SMA200"]) or pd.isna(row["RSI"]) or pd.isna(row["ATR"]):
            continue
        key = decide_signal(
            bool(row["Close"] > row["SMA200"]),
            bool(row["Close"] > row["EMA8"]),
            bool(row["dist_ema8"] <= cfg.max_dist_ema8),
            bool(cfg.min_rsi <= row["RSI"] <= cfg.max_rsi),
            bool(row["vol_ratio"] >= cfg.min_vol_ratio),
            bool(weekly_ok.loc[ts]),
            bool(row["ADX"] >= cfg.adx_min),  # NaN -> False
            bool(rs_diff.loc[ts] >= cfg.rs_min),  # NaN -> False
            str(regimes.loc[ts]),
            cfg.require_spy_bullish,
            cfg.require_weekly_trend,
        )
        future = df[df.index > ts]
        if key == "LONG":
            res, _ = resolve_outcome(ts.date().isoformat(), float(row["SL"]),
                                     float(row["TP"]), future, cfg.max_hold_days)
            long_trades.append(res)
        elif key == "WEAK":
            res, _ = resolve_outcome(ts.date().isoformat(), float(row["SL"]),
                                     float(row["TP"]), future, cfg.max_hold_days)
            weak_trades.append(res)

    def summarize(trades: list[str]) -> dict:
        wins = trades.count("WIN")
        losses = trades.count("LOSS")
        expired = trades.count("EXPIRED")
        total = wins + losses
        rr = cfg.rr_ratio
        return {
            "signals": len(trades),
            "wins": wins,
            "losses": losses,
            "expired": expired,
            "win_rate": round(wins / total * 100, 1) if total else 0.0,
            "expectancy_R": round((wins * rr - losses) / total, 2) if total else 0.0,
        }

    return {"ticker": ticker, "LONG": summarize(long_trades), "WEAK": summarize(weak_trades)}


# ---------------------------------------------------------------------------
# Motor trailing-stop: misma estrategia que el scanner en vivo.
# ---------------------------------------------------------------------------

def _day_flags(row: pd.Series, weekly_ok: bool, rs_val: float, cfg: StrategyConfig,
               rules: dict) -> tuple[bool, ...]:
    """Los 8 booleanos de entrada con las MISMAS expresiones que app.py.

    Incluye los guards idénticos del vivo: vol_ratio NaN -> 0.0, RSI NaN
    no supera, ADX/RS con guard isfinite. NaN en comparaciones -> False.
    """
    price = float(row["Close"])
    sma200 = float(row["SMA200"])
    ema8 = float(row["EMA8"])
    rsi = float(row["RSI"])
    atr = float(row["ATR"])
    adx = float(row["ADX"])
    vol_ratio = float(row["vol_ratio"])
    if not math.isfinite(vol_ratio):
        vol_ratio = 0.0
    if not math.isfinite(rsi):
        rsi = float("nan")
    dist_ema8 = float(row["dist_ema8"])
    c_sma = price > sma200
    c_ema = price > ema8
    c_dist = dist_ema8 <= rules["MAX_DIST_EMA8"]
    c_rsi = rules["MIN_RSI"] <= rsi <= rules["MAX_RSI"]
    c_vol = vol_ratio >= rules["MIN_VOL_RATIO"]
    c_weekly = bool(weekly_ok)
    c_adx = bool(math.isfinite(adx) and adx >= cfg.adx_min)
    c_rs = bool(math.isfinite(rs_val) and rs_val >= cfg.rs_min)
    return c_sma, c_ema, c_dist, c_rsi, c_vol, c_weekly, c_adx, c_rs


def simulate_ticker(ticker: str, cfg: StrategyConfig,
                    df: pd.DataFrame, spy: pd.DataFrame) -> dict:
    """Simula un ticker día a día. Puro (sin IO): devuelve trades + conteos.

    Usa compute_indicators / decide_signal / initial_stop /
    stop_with_breakeven / sizing_units / close_trade — las mismas
    funciones del vivo. Las series weekly/RS/régimen-SPY se precalculan
    una vez (operaciones causales) y se leen indexadas en cada día D;
    los tests prueban su equivalencia con la evaluación truncada en D.
    """
    rules = cfg.to_rules_dict()
    if len(df) < 250 or spy is None or spy.empty or "Close" not in spy.columns:
        return {"ticker": ticker, "trades": [],
                "skipped_zero_size": 0, "error": "datos insuficientes"}
    ind = compute_indicators(df, cfg.atr_sl_mult, cfg.atr_tp_mult)
    # Serie semanal: vía rápida probada equivalente a weekly_trend_ok(df, D)
    # (ver tests); RS y régimen SPY se evalúan por día con las funciones
    # exactas del vivo sobre series truncadas en D (as-of, sin look-ahead).
    weekly_ok = weekly_state_series(df)
    spy_close = spy["Close"]
    spy_sma50 = spy_close.rolling(window=50).mean()
    spy_sma200 = spy_close.rolling(window=200).mean()

    trades: list[dict] = []
    skipped_zero_size = 0
    pos: dict | None = None  # estado virtual: entry/init/max/stop/units/fecha

    for ts in df.index:
        row = ind.loc[ts]
        # Warmup: sin SMA200/RSI/ATR no hay señal posible (los NaN dan
        # False en cada filtro, igual que en vivo); se salta por velocidad.
        if pd.isna(row["SMA200"]) or pd.isna(row["RSI"]) or pd.isna(row["ATR"]):
            continue
        close = float(row["Close"])
        atr = float(row["ATR"])
        if pos is None:
            rs_val = relative_strength(df["Close"].loc[:ts], spy_close.loc[:ts])
            flags = _day_flags(row, bool(weekly_ok.loc[ts]),
                               float(rs_val), cfg, rules)
            try:
                s_now = spy_close.loc[:ts].iloc[-1]
                m50 = spy_sma50.loc[:ts].iloc[-1]
                m200 = spy_sma200.loc[:ts].iloc[-1]
            except (KeyError, IndexError):
                s_now, m50, m200 = float("nan"), float("nan"), float("nan")
            if math.isfinite(float(s_now)) and math.isfinite(float(m50)) \
                    and math.isfinite(float(m200)):
                regime = spy_regime(float(s_now), float(m50), float(m200))
            else:
                regime = "UNKNOWN"  # fuera de la región definida en vivo
            key = decide_signal(*flags, regime,
                                cfg.require_spy_bullish,
                                cfg.require_weekly_trend)
            if key != "LONG":
                continue
            init = initial_stop(close, atr, cfg.atr_sl_mult)
            units, _invested = sizing_units(ticker, close, init, cfg.risk_per_trade)
            if units <= 0:
                skipped_zero_size += 1
                continue
            pos = {"entry": close, "init": init, "max": close,
                   "stop": init, "units": units,
                   "fecha": ts.date().isoformat()}
        else:
            # Gestión con la MISMA función que positions.update_stop usa.
            new_max = max(pos["max"], close)
            stop, _trig = stop_with_breakeven(
                pos["entry"], pos["init"], new_max, atr,
                cfg.atr_trail_mult, pos["stop"])
            pos["max"], pos["stop"] = new_max, stop
            if close < stop:
                trades.append(close_trade(
                    ticker,
                    {"precio_entrada": pos["entry"],
                     "stop_inicial": pos["init"],
                     "unidades": pos["units"],
                     "fecha_entrada": pos["fecha"]},
                    close, ts.date().isoformat(), "stop"))
                pos = None

    if pos is not None:
        last_ts = df.index[-1]
        last_close = float(df["Close"].iloc[-1])
        trades.append(close_trade(
            ticker,
            {"precio_entrada": pos["entry"],
             "stop_inicial": pos["init"],
             "unidades": pos["units"],
             "fecha_entrada": pos["fecha"]},
            last_close, last_ts.date().isoformat(), "fin-historial"))
    return {"ticker": ticker, "trades": trades,
            "skipped_zero_size": skipped_zero_size}


def save_results(trades: list[dict], path: str | Path = RESULTS_PATH) -> None:
    """Guarda los trades con el esquema de journal.json (compatible con
    trade_journal.load_journal). Solo main() la llama con el default."""
    Path(path).write_text(json.dumps(trades, indent=2), encoding="utf-8")


def side_cost(amount: float, cfg: StrategyConfig, broker: str = "ing",
              units: float = 0.0, ticker: str = "") -> float:
    """Coste de UN lado (apertura o cierre) según el bróker.

    ing: comisión = min(tope, fijo + % * importe) + FX % del importe
      (idéntico resultado que antes de parametrizar).
    ib: comisión = max(mínimo, por_acción * unidades) en acciones,
      topada al 1% del nocional de la orden (regla IB); en cripto
      ("USD" en el ticker, sizing fraccional) comisión 0; más FX %
      del importe en ambos casos.
    Puro, nunca lanza (importe inválido -> 0.0).
    """
    try:
        amt = max(0.0, float(amount))
        if broker == "ib":
            if "USD" in str(ticker):
                commission = 0.0
            else:
                commission = max(float(cfg.ib_commission_min),
                                 float(cfg.ib_commission_per_share)
                                 * max(0.0, float(units)))
                # Tope IB: 1% del nocional (amount == units * price).
                commission = min(commission, 0.01 * amt)
            return commission + float(cfg.ib_fx_pct) * amt
        commission = min(float(cfg.broker_commission_cap),
                         float(cfg.broker_commission_fixed)
                         + float(cfg.broker_commission_pct) * amt)
        return commission + float(cfg.broker_fx_pct) * amt
    except Exception:
        return 0.0


def _cost_keys(broker: str) -> tuple[str, str, str]:
    """Nombres de campo por bróker (los de ING conservan los originales)."""
    if broker == "ib":
        return "coste_total_ib", "resultado_usd_neto_ib", "resultado_r_neto_ib"
    return "coste_total", "resultado_usd_neto", "resultado_r_neto"


def apply_costs(trade: dict, cfg: StrategyConfig, broker: str = "ing") -> dict:
    """Devuelve copia del trade con coste total y resultados NETOS del bróker.

    Coste = lado(apertura sobre unidades*precio_entrada) + lado(cierre
    sobre unidades*precio_cierre). No toca los campos brutos; añade los
    campos netos del bróker (R sobre el mismo denominador
    riesgo_inicial * unidades).
    """
    out = dict(trade)
    cost_key, usd_key, r_key = _cost_keys(broker)
    try:
        units = float(trade.get("unidades", 0.0) or 0.0)
        entry = float(trade.get("precio_entrada", 0.0) or 0.0)
        exit_px = float(trade.get("precio_cierre", 0.0) or 0.0)
        risk = float(trade.get("riesgo_inicial", 0.0) or 0.0)
        ticker = str(trade.get("ticker", ""))
        cost = side_cost(units * entry, cfg, broker, units, ticker) \
            + side_cost(units * exit_px, cfg, broker, units, ticker)
        usd_net = float(trade.get("resultado_usd", 0.0) or 0.0) - cost
        denom = risk * units
        r_net = usd_net / denom if denom > 0 else 0.0
    except Exception:
        cost, usd_net, r_net = 0.0, 0.0, 0.0
    out[cost_key] = cost
    out[usd_key] = usd_net
    out[r_key] = r_net
    return out


def net_view(trades: list[dict], broker: str = "ing") -> list[dict]:
    """Vista de trades con brutos sustituidos por netos del bróker (para
    journal_stats, misma fórmula que la vista 'j'). No muta la entrada."""
    _, usd_key, r_key = _cost_keys(broker)
    return [{**t, "resultado_usd": t.get(usd_key, 0.0),
             "resultado_r": t.get(r_key, 0.0)} for t in trades]


def avg_cost(trades: list[dict], broker: str = "ing") -> tuple[float, float]:
    """Coste medio por trade en $ y en R (R sobre riesgo*unidades de cada
    trade; denominador <= 0 aporta 0)."""
    if not trades:
        return 0.0, 0.0
    cost_key, _, _ = _cost_keys(broker)
    dollars = sum(float(t.get(cost_key, 0.0) or 0.0) for t in trades)
    r_units = 0.0
    for t in trades:
        try:
            denom = float(t.get("riesgo_inicial", 0.0) or 0.0) \
                * float(t.get("unidades", 0.0) or 0.0)
            r_units += (float(t.get(cost_key, 0.0) or 0.0) / denom
                        if denom > 0 else 0.0)
        except Exception:
            pass
    return dollars / len(trades), r_units / len(trades)


UNIVERSES = {
    "default": "tickers.json",
    "etfs": "tickers_etf_test.json",
    "growth": "tickers_growth_test.json",
}


def _fmt_stats(name: str, trades: list[dict], data_range: str = "") -> str:
    """Línea de resumen con la MISMA fórmula que la vista 'j': expectancy
    bruto, neto ING y neto IB lado a lado, más coste medio por trade
    ($ y R) de cada bróker."""
    s = journal_stats(trades)
    s_ing = journal_stats(net_view(trades, "ing"))
    s_ib = journal_stats(net_view(trades, "ib"))
    c_ing = avg_cost(trades, "ing")
    c_ib = avg_cost(trades, "ib")
    line = (
        f"{name:10s} n={s['n']:3d} win={s['win_rate'] * 100:5.1f}% "
        f"Rw={s['avg_r_winners']:+.2f} Rl={s['avg_r_losers']:+.2f} "
        f"expR bruto={s['expectancy']:+.2f} "
        f"neto_ing={s_ing['expectancy']:+.2f} neto_ib={s_ib['expectancy']:+.2f} "
        f"coste_ing=${c_ing[0]:.2f} ({c_ing[1]:.3f}R) "
        f"coste_ib=${c_ib[0]:.2f} ({c_ib[1]:.3f}R) "
        f"días={s['avg_days']:.1f} (W{s['avg_days_winners']:.1f}/"
        f"L{s['avg_days_losers']:.1f})"
    )
    if data_range:
        line += f" | datos {data_range}"
    return line


def main(argv: list[str]) -> int:
    cfg = StrategyConfig.load()
    universe = "default"
    rest = []
    for a in argv:
        if a.startswith("--universe="):
            universe = a.split("=", 1)[1].strip().lower()
        else:
            rest.append(a)
    universe_file = UNIVERSES.get(universe, universe)
    if rest:
        tickers = [t.strip().upper() for t in rest if t.strip()]
    else:
        try:
            tickers = json.loads(Path(universe_file).read_text(encoding="utf-8"))
        except Exception:
            tickers = ["AAPL", "MSFT", "NVDA"]
    print(f"Backtest trailing (SL {cfg.atr_sl_mult}x, trail {cfg.atr_trail_mult}x, "
          f"BE +1R) | universo={universe_file} | weekly={cfg.require_weekly_trend} "
          f"| spy_filter={cfg.require_spy_bullish}")
    try:
        spy = download("SPY")
    except Exception as e:
        print(f"SPY: sin datos ({e}); régimen UNKNOWN para todo.")
        spy = pd.DataFrame()
    all_trades: list[dict] = []
    per_ticker: dict[str, list[dict]] = {}
    skipped_total = 0
    for t in tickers:
        try:
            df = download(t)
        except Exception as e:
            print(f"{t:10s} ERROR descarga: {e}")
            continue
        r = simulate_ticker(t, cfg, df, spy)
        if "error" in r:
            print(f"{t:10s} {r['error']}")
            continue
        netted = [apply_costs(apply_costs(t, cfg, "ing"), cfg, "ib")
                  for t in r["trades"]]
        per_ticker[t] = netted
        all_trades.extend(netted)
        skipped_total += r["skipped_zero_size"]
        data_range = (f"{df.index[0].date().isoformat()}→"
                      f"{df.index[-1].date().isoformat()}")
        print(_fmt_stats(t, netted, data_range))
    save_results(all_trades)
    print(f"Guardados {len(all_trades)} trades en {RESULTS_PATH} "
          f"(omitidas {skipped_total} señales con sizing 0).")
    s = journal_stats(all_trades)
    s_ing = journal_stats(net_view(all_trades, "ing"))
    s_ib = journal_stats(net_view(all_trades, "ib"))
    c_ing = avg_cost(all_trades, "ing")
    c_ib = avg_cost(all_trades, "ib")
    print(f"TOTAL n={s['n']} win={s['win_rate'] * 100:.1f}% "
          f"Rw={s['avg_r_winners']:+.2f} Rl={s['avg_r_losers']:+.2f} "
          f"expR bruto={s['expectancy']:+.2f} "
          f"neto_ing={s_ing['expectancy']:+.2f} neto_ib={s_ib['expectancy']:+.2f} "
          f"coste_ing/trade=${c_ing[0]:.2f} ({c_ing[1]:.3f}R) "
          f"coste_ib/trade=${c_ib[0]:.2f} ({c_ib[1]:.3f}R) "
          f"días={s['avg_days']:.1f} "
          f"(W{s['avg_days_winners']:.1f}/L{s['avg_days_losers']:.1f})")
    print(f"Backtest sobre el universo actual de {universe_file} (sesgo de "
          "supervivencia: no incluye tickers que pudieras haber quitado de "
          "la lista) y sujeto a los límites de historial que ofrece "
          "yfinance para cada ticker.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
