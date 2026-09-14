"""Lógica cuantitativa pura de la estrategia (testeable sin red ni TUI).

Toda la matemática que usa ``update_scan`` vive aquí para que los tests
unitarios (punto 8) la cubran con datos fijos y detecten desincronizaciones
de umbrales. ``app.py`` debe limitarse a llamar a estas funciones.
"""
from __future__ import annotations

import pandas as pd

RSI_PERIOD = 14
ATR_PERIOD = 14
WEEKLY_SMA = 200
WEEKLY_MIN_BARS = WEEKLY_SMA + 5  # 205 semanas para SMA200 + pendiente
WEEKLY_SLOPE_LOOKBACK = 4


def wilder_rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """RSI con suavizado de Wilder (idéntico al usado en el scanner)."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    high_low = high - low
    high_close = (high - close.shift(1)).abs()
    low_close = (low - close.shift(1)).abs()
    return pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)


def wilder_atr(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = ATR_PERIOD
) -> pd.Series:
    """ATR con suavizado de Wilder sobre el True Range."""
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


ADX_PERIOD = 14


def wilder_adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = ADX_PERIOD
) -> pd.Series:
    """ADX estándar con suavizado de Wilder (+DI/-DI intermedios).

    Solo usa OHLC ya descargado, sin llamadas adicionales.
    Primeros valores NaN (warmup del doble suavizado); si no hay
    movimiento direccional el resultado es NaN (0/0) -> el llamante
    debe tratarlo como filtro no superado.
    """
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    tr = true_range(high, low, close)
    tr_s = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    plus_s = plus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    minus_s = minus_dm.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    plus_di = 100 * plus_s / tr_s.replace(0, float("nan"))
    minus_di = 100 * minus_s / tr_s.replace(0, float("nan"))
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))
    return dx.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def compute_indicators(
    df: pd.DataFrame, atr_sl_mult: float, atr_tp_mult: float
) -> pd.DataFrame:
    """Añade SMA200, EMA8, VolSMA20, RSI, ATR, dist_ema8, vol_ratio, SL y TP.

    Los multiplicadores son obligatorios (sin defaults) para que solo
    StrategyConfig pueda aportarlos: una única fuente de verdad.
    Devuelve una copia; no modifica el frame de entrada.
    """
    out = df.copy()
    out["SMA200"] = out["Close"].rolling(window=200).mean()
    out["EMA8"] = out["Close"].ewm(span=8, adjust=False).mean()
    out["Vol_SMA20"] = out["Volume"].rolling(window=20).mean()
    out["RSI"] = wilder_rsi(out["Close"])
    out["ATR"] = wilder_atr(out["High"], out["Low"], out["Close"])
    out["ADX"] = wilder_adx(out["High"], out["Low"], out["Close"])
    out["dist_ema8"] = ((out["Close"] - out["EMA8"]) / out["EMA8"]) * 100
    out["vol_ratio"] = out["Volume"] / out["Vol_SMA20"].replace(0, float("nan"))
    out["SL"] = out["Close"] - (atr_sl_mult * out["ATR"])
    # TP fijo: deprecated como condición de salida (paso 2, trailing stop);
    # se sigue calculando por compat (journal/backtest históricos).
    out["TP"] = out["Close"] + (atr_tp_mult * out["ATR"])
    return out


def initial_stop(entry_price: float, atr: float, atr_sl_mult: float) -> float:
    """Stop inicial: entrada - mult * ATR (mult = 2.5x)."""
    return float(entry_price) - float(atr_sl_mult) * float(atr)


def trailing_stop(max_price: float, atr: float, atr_trail_mult: float) -> float:
    """Chandelier Exit: max(cierres desde entrada) - trail_mult * ATR.

    Solo sube si max_price sube (el llamante guarda el máximo con
    positions.update_maximum, que nunca baja). Sin estado interno.
    """
    return float(max_price) - float(atr_trail_mult) * float(atr)


def stop_with_breakeven(
    entry_price: float,
    initial_stop: float,
    max_price: float,
    atr_now: float,
    atr_trail_mult: float,
    prev_stop: float | None = None,
) -> tuple[float, bool]:
    """Stop efectivo con breakeven + trailing (paso 3). Ratchet: solo sube.

    - riesgo = entrada - stop_inicial; objetivo +1R = entrada + riesgo.
    - Si max_price >= +1R, el candidato sube al menos a la entrada exacta.
    - Candidato = max(trailing, entrada si triggered, stop_inicial).
    - Resultado = max(candidato, prev_stop): nunca baja.
    Retorna (stop_efectivo, breakeven_activado).
    """
    entry = float(entry_price)
    init = float(initial_stop)
    mpx = float(max_price)
    risk = entry - init
    trail = trailing_stop(mpx, float(atr_now), float(atr_trail_mult))
    level = max(trail, init)
    triggered = False
    if risk > 0 and mpx >= entry + risk:
        triggered = True
        level = max(level, entry)
    if prev_stop is not None:
        try:
            level = max(level, float(prev_stop))
        except Exception:
            pass
    return level, triggered


def weekly_trend_ok(daily: pd.DataFrame, cutoff=None) -> bool:
    """Filtro multi-timeframe (punto 6): cierre semanal > SMA200 semanal
    y SMA200 semanal con pendiente positiva (vs 4 semanas atrás).

    Sin 205+ barras semanales no se puede calcular -> fail-open (True)
    documentado: mejor no bloquear IPOs que inventar el dato.

    cutoff: etiqueta de fecha opcional — evalúa solo con datos hasta
    cutoff inclusive (backtest día a día, sin look-ahead: la semana en
    curso solo aporta cierres <= cutoff). Sin cutoff (scanner en vivo),
    comportamiento idéntico al de siempre.
    """
    frame = daily.loc[:cutoff] if cutoff is not None else daily
    weekly_close = frame["Close"].resample("W-FRI").last().dropna()
    if len(weekly_close) < WEEKLY_MIN_BARS:
        return True
    sma200w = weekly_close.rolling(window=WEEKLY_SMA).mean()
    if pd.isna(sma200w.iloc[-1]) or pd.isna(sma200w.iloc[-1 - WEEKLY_SLOPE_LOOKBACK]):
        return True
    price_ok = bool(weekly_close.iloc[-1] > sma200w.iloc[-1])
    slope_ok = bool(sma200w.iloc[-1] > sma200w.iloc[-1 - WEEKLY_SLOPE_LOOKBACK])
    return price_ok and slope_ok


def spy_regime(price: float, sma50: float, sma200: float) -> str:
    """Régimen SPY a partir de valores ya calculados (sin lookahead)."""
    if price > sma200 and price > sma50:
        return "BULLISH"
    if price > sma200 and price <= sma50:
        return "PULLBACK"
    return "BEARISH"


RS_WINDOW = 20


def relative_strength(
    ticker_close: pd.Series, spy_close: pd.Series, window: int = RS_WINDOW
) -> float:
    """Fuerza relativa vs SPY: retorno_ticker − retorno_SPY en puntos
    porcentuales sobre `window` sesiones (misma ventana que VolSMA20).

    Compara el último cierre con el de `window` sesiones atrás; para SPY
    usa la última barra disponible en cada fecha (as-of, p. ej. el viernes
    para una barra de cripto en fin de semana). Diferencia en vez de
    cociente: el cociente se rompe con SPY ~0% o negativo.
    Retorna NaN si no es calculable (el llamante lo trata como no superado).
    """
    try:
        t = ticker_close.dropna()
        s = spy_close.dropna()
        if len(t) < window + 1 or len(s) < 2:
            return float("nan")
        # Normalizar zonas horarias para no comparar aware vs naive (TypeError)
        if getattr(t.index, "tz", None) is not None:
            t = t.copy()
            t.index = t.index.tz_localize(None)
        if getattr(s.index, "tz", None) is not None:
            s = s.copy()
            s.index = s.index.tz_localize(None)
        t_now, t_then = float(t.iloc[-1]), float(t.iloc[-(window + 1)])
        d_now, d_then = t.index[-1], t.index[-(window + 1)]
        s_now = s[s.index <= d_now]
        s_then = s[s.index <= d_then]
        if len(s_now) == 0 or len(s_then) == 0:
            return float("nan")
        s_now_v, s_then_v = float(s_now.iloc[-1]), float(s_then.iloc[-1])
        if t_then == 0 or s_then_v == 0:
            return float("nan")
        return (t_now / t_then - 1) * 100 - (s_now_v / s_then_v - 1) * 100
    except Exception:
        return float("nan")


def decide_signal(
    c_sma: bool,
    c_ema: bool,
    c_dist: bool,
    c_rsi: bool,
    c_vol: bool,
    c_weekly: bool,
    c_adx: bool,
    c_rs: bool,
    spy_regime_: str,
    require_spy_bullish: bool = True,
    require_weekly_trend: bool = True,
) -> str:
    """Clasifica la señal. Retorna LONG | WEAK | WAIT_SPY | WAIT.

    - Setup técnico incompleto (o semanal/ADX/RS fallando) -> WAIT.
    - Setup completo + SPY BULLISH (o filtro desactivado) -> LONG.
    - Setup completo + SPY PULLBACK -> WEAK (señal débil, punto 3).
    - Setup completo + SPY BEARISH/UNKNOWN -> WAIT_SPY (bloqueada).
    """
    weekly_ok = c_weekly if require_weekly_trend else True
    if not (c_sma and c_ema and c_dist and c_rsi and c_vol and weekly_ok and c_adx and c_rs):
        return "WAIT"
    market_ok = (spy_regime_ == "BULLISH") if require_spy_bullish else True
    if market_ok:
        return "LONG"
    if spy_regime_ == "PULLBACK":
        return "WEAK"
    return "WAIT_SPY"


def exposure_status(n_signals: int, risk_per_trade: float, max_total_risk: float) -> tuple[float, bool]:
    """Exposición simultánea estimada y si supera el tope (punto 5)."""
    exposure = max(0, n_signals) * max(0.0, risk_per_trade)
    return exposure, bool(exposure > max_total_risk)
