"""Tests unitarios de la estrategia (punto 8): RSI/ATR/señal con datos fijos.

Sin red ni TUI: `..venv/bin/python -m unittest discover -s tests -v`
Si alguien toca update_scan/strategy.py y desajusta un umbral, estos
tests lo detectan.
"""
import math
import os
import sqlite3
import sys
import tempfile
import unittest

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import alerts
import journal
from backtest import spy_regime_series, weekly_state_series
from strategy import (
    RS_WINDOW,
    compute_indicators,
    decide_signal,
    exposure_status,
    relative_strength,
    spy_regime,
    weekly_trend_ok,
    wilder_adx,
    wilder_atr,
    wilder_rsi,
)
from strategy_config import StrategyConfig


def rising(n=60, start=100.0, step=1.0):
    return pd.Series([start + i * step for i in range(n)])


def falling(n=60, start=100.0, step=1.0):
    return pd.Series([start - i * step for i in range(n)])


def ohlc_from(close, spread=0.5, vol=1_000_000):
    # to_numpy(): evita que pandas alinee la Series por etiqueta con el índice
    vals = pd.Series(close).to_numpy(dtype=float)
    return pd.DataFrame({
        "Open": vals,
        "High": vals + spread,
        "Low": vals - spread,
        "Close": vals,
        "Volume": [vol] * len(vals),
    }, index=pd.date_range("2020-01-01", periods=len(vals), freq="B"))


class TestWilderRSI(unittest.TestCase):
    def test_rising_without_losses_is_nan(self):
        # Serie estrictamente creciente: avg_loss = -0.0 -> replace(0, NaN) ->
        # RS indefinido -> NaN. Comportamiento heredado del scanner (las
        # comparaciones con NaN son False, así que cuenta como NO señal).
        rsi = wilder_rsi(rising(60))
        self.assertTrue(math.isnan(rsi.iloc[-1]), rsi.iloc[-1])

    def test_mostly_rising_is_high(self):
        vals = [100.0 + i for i in range(60)]
        for i in range(9, 60, 10):  # pequeños retrocesos periódicos
            vals[i] -= 3.0
        rsi = wilder_rsi(pd.Series(vals))
        self.assertTrue(rsi.iloc[-1] > 70, rsi.iloc[-1])

    def test_falling_tends_to_0(self):
        rsi = wilder_rsi(falling(60))
        self.assertTrue(rsi.iloc[-1] < 10, rsi.iloc[-1])

    def test_flat_is_nan(self):
        rsi = wilder_rsi(pd.Series([50.0] * 30))
        self.assertTrue(math.isnan(rsi.iloc[-1]))

    def test_matches_manual_recursion(self):
        # Implementación independiente del suavizado de Wilder
        close = pd.Series([100 + ((i * 7) % 11) - 5 for i in range(50)], dtype=float)
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        a = 1 / 14
        ag, al = gain.iloc[1], loss.iloc[1]
        for t in range(2, len(close)):
            ag = (1 - a) * ag + a * gain.iloc[t]
            al = (1 - a) * al + a * loss.iloc[t]
        expected = 100 - 100 / (1 + ag / al)
        self.assertAlmostEqual(wilder_rsi(close).iloc[-1], expected, places=8)

    def test_first_values_are_nan(self):
        rsi = wilder_rsi(rising(30))
        self.assertTrue(rsi.iloc[:13].isna().all())


class TestWilderATR(unittest.TestCase):
    def test_positive_and_matches_manual(self):
        close = rising(40, start=100.0, step=0.5)
        df = ohlc_from(close, spread=1.0)
        atr = wilder_atr(df["High"], df["Low"], df["Close"])
        self.assertTrue((atr.dropna() > 0).all())
        # Recursión manual sobre el TR
        tr = pd.concat([
            df["High"] - df["Low"],
            (df["High"] - df["Close"].shift(1)).abs(),
            (df["Low"] - df["Close"].shift(1)).abs(),
        ], axis=1).max(axis=1)
        a = 1 / 14
        y = tr.iloc[1]
        for t in range(2, len(tr)):
            y = (1 - a) * y + a * tr.iloc[t]
        self.assertAlmostEqual(atr.iloc[-1], y, places=8)


class TestWilderADX(unittest.TestCase):
    def test_strong_trend_is_high(self):
        df = ohlc_from(rising(300, step=0.3))
        adx = wilder_adx(df["High"], df["Low"], df["Close"])
        self.assertTrue(adx.iloc[-1] > 40, adx.iloc[-1])

    def test_sideways_chop_is_low(self):
        import math as _m

        vals = [100.0 + 2.0 * _m.sin(i * 0.6) for i in range(300)]
        df = ohlc_from(pd.Series(vals))
        adx = wilder_adx(df["High"], df["Low"], df["Close"])
        self.assertTrue(adx.iloc[-1] < 20, adx.iloc[-1])

    def test_bounded_and_warmup_nan(self):
        df = ohlc_from(rising(300, step=0.3))
        adx = wilder_adx(df["High"], df["Low"], df["Close"])
        defined = adx.dropna()
        self.assertTrue(((defined >= 0) & (defined <= 100)).all())
        self.assertTrue(adx.iloc[:20].isna().all())

    def test_indicators_include_adx(self):
        df = ohlc_from(rising(300, step=0.3))
        ind = compute_indicators(df, atr_sl_mult=1.5, atr_tp_mult=3.0)
        self.assertIn("ADX", ind.columns)
        self.assertTrue(ind["ADX"].iloc[-1] > 40)


class TestRelativeStrength(unittest.TestCase):
    def _frames(self, t0, t1, s0, s1, n=21):
        idx = pd.date_range("2024-01-01", periods=n, freq="B")
        t = pd.Series([t0 + (t1 - t0) * i / (n - 1) for i in range(n)], index=idx)
        s = pd.Series([s0 + (s1 - s0) * i / (n - 1) for i in range(n)], index=idx)
        return t, s

    def test_exact_difference(self):
        # ticker +10 %, SPY +5 % -> +5.0 pp
        t, s = self._frames(100.0, 110.0, 200.0, 210.0)
        self.assertAlmostEqual(relative_strength(t, s), 5.0, places=8)

    def test_negative_when_lagging(self):
        t, s = self._frames(100.0, 102.0, 200.0, 220.0)  # +2 % vs +10 %
        self.assertAlmostEqual(relative_strength(t, s), -8.0, places=6)
        self.assertLess(relative_strength(t, s), 0.0)

    def test_nan_without_enough_history(self):
        t, s = self._frames(100.0, 110.0, 200.0, 210.0, n=10)
        self.assertTrue(math.isnan(relative_strength(t, s)))

    def test_nan_without_spy(self):
        t, _ = self._frames(100.0, 110.0, 200.0, 210.0)
        self.assertTrue(math.isnan(relative_strength(t, pd.Series([], dtype=float))))

    def test_weekend_bars_use_friday_spy(self):
        # ticker con barras de fin de semana (cripto), SPY solo laborables
        t_idx = pd.date_range("2024-01-01", periods=23, freq="D")
        t = pd.Series(100.0, index=t_idx)
        t.iloc[-1] = 110.0
        s_idx = pd.date_range("2024-01-01", periods=17, freq="B")
        s = pd.Series(200.0, index=s_idx)
        s.iloc[-1] = 210.0
        # 20 sesiones atrás del ticker cae en fin de semana -> SPY usa el
        # viernes anterior (200.0); ticker +10 %, SPY +5 % -> +5.0 pp
        self.assertAlmostEqual(relative_strength(t, s), 5.0, places=8)

    def test_tz_aware_vs_naive(self):
        # Uno tz-aware (ej. yfinance real) y otro tz-naive no deben lanzar TypeError
        t_idx = pd.date_range("2024-01-01", periods=25, freq="B", tz="America/New_York")
        t = pd.Series(100.0, index=t_idx)
        t.iloc[-1] = 110.0
        s_idx = pd.date_range("2024-01-01", periods=25, freq="B")
        s = pd.Series(200.0, index=s_idx)
        s.iloc[-1] = 205.0
        self.assertAlmostEqual(relative_strength(t, s), 7.5, places=6)

    def test_matches_backtest_series(self):
        # La versión vectorial del backtest coincide con la del vivo al cierre
        from backtest import rs_diff_series

        idx = pd.date_range("2020-01-01", periods=300, freq="B")
        tdf = ohlc_from(rising(300, step=0.2))
        sdf = ohlc_from(rising(300, start=400.0, step=0.1))
        live = relative_strength(tdf["Close"], sdf["Close"])
        series = rs_diff_series(tdf, sdf)
        self.assertAlmostEqual(live, float(series.iloc[-1]), places=8)


class TestIndicators(unittest.TestCase):
    def test_columns_and_sl_tp_math(self):
        df = ohlc_from(rising(300, step=0.3))
        ind = compute_indicators(df, atr_sl_mult=1.5, atr_tp_mult=3.0)
        for col in ("SMA200", "EMA8", "Vol_SMA20", "RSI", "ATR",
                    "dist_ema8", "vol_ratio", "SL", "TP"):
            self.assertIn(col, ind.columns)
        last = ind.iloc[-1]
        self.assertAlmostEqual(last["TP"] - last["Close"],
                               2.0 * (last["Close"] - last["SL"]), places=6)
        # No muta el frame original
        self.assertNotIn("RSI", df.columns)


class TestDecideSignal(unittest.TestCase):
    def base(self, **kw):
        args = dict(c_sma=True, c_ema=True, c_dist=True, c_rsi=True,
                    c_vol=True, c_weekly=True, c_adx=True, c_rs=True,
                    spy_regime_="BULLISH")
        args.update(kw)
        return args

    def test_long_all_green_bullish(self):
        self.assertEqual(decide_signal(**self.base()), "LONG")

    def test_weak_on_pullback(self):
        self.assertEqual(decide_signal(**self.base(spy_regime_="PULLBACK")), "WEAK")

    def test_blocked_on_bearish(self):
        self.assertEqual(decide_signal(**self.base(spy_regime_="BEARISH")), "WAIT_SPY")

    def test_blocked_on_unknown(self):
        self.assertEqual(decide_signal(**self.base(spy_regime_="UNKNOWN")), "WAIT_SPY")

    def test_wait_when_setup_fails(self):
        for k in ("c_sma", "c_ema", "c_dist", "c_rsi", "c_vol"):
            self.assertEqual(decide_signal(**self.base(**{k: False})), "WAIT")

    def test_wait_when_weekly_fails(self):
        self.assertEqual(decide_signal(**self.base(c_weekly=False)), "WAIT")

    def test_wait_when_adx_fails(self):
        self.assertEqual(decide_signal(**self.base(c_adx=False)), "WAIT")

    def test_wait_when_rs_fails(self):
        self.assertEqual(decide_signal(**self.base(c_rs=False)), "WAIT")

    def test_filter_off_allows_bearish(self):
        self.assertEqual(
            decide_signal(**self.base(spy_regime_="BEARISH", require_spy_bullish=False)),
            "LONG",
        )

    def test_weekly_off_ignores_weekly(self):
        self.assertEqual(
            decide_signal(**self.base(c_weekly=False, require_weekly_trend=False)),
            "LONG",
        )


class TestRegimes(unittest.TestCase):
    def test_spy_regime_thirds(self):
        self.assertEqual(spy_regime(500, 480, 470), "BULLISH")
        self.assertEqual(spy_regime(475, 480, 470), "PULLBACK")
        self.assertEqual(spy_regime(460, 480, 470), "BEARISH")

    def test_weekly_fail_open_short_history(self):
        df = ohlc_from(rising(100))  # ~20 semanas < 205
        self.assertTrue(weekly_trend_ok(df))

    def test_weekly_true_on_sustained_uptrend(self):
        df = ohlc_from(rising(1500, step=0.2))  # ~300 semanas al alza
        self.assertTrue(weekly_trend_ok(df))

    def test_weekly_false_on_sustained_downtrend(self):
        df = ohlc_from(falling(1500, start=500.0, step=0.2))
        self.assertFalse(weekly_trend_ok(df))

    def test_series_helpers_align(self):
        df = ohlc_from(rising(1200, step=0.2))
        spy = ohlc_from(rising(1200, start=400.0, step=0.1))
        w = weekly_state_series(df)
        r = spy_regime_series(spy, df.index)
        self.assertEqual(len(w), len(df))
        self.assertEqual(len(r), len(df))
        self.assertEqual(r.iloc[-1], "BULLISH")


class TestExposure(unittest.TestCase):
    def test_under_cap(self):
        exp, breached = exposure_status(2, 100.0, 300.0)
        self.assertEqual(exp, 200.0)
        self.assertFalse(breached)

    def test_over_cap(self):
        exp, breached = exposure_status(4, 100.0, 300.0)
        self.assertEqual(exp, 400.0)
        self.assertTrue(breached)


class TestConfig(unittest.TestCase):
    def test_defaults_match_legacy_rules(self):
        cfg = StrategyConfig()
        self.assertEqual(cfg.to_rules_dict(), {
            "MAX_DIST_EMA8": 1.5, "MIN_RSI": 45.0, "MAX_RSI": 65.0,
            "MIN_VOL_RATIO": 1.2, "REQUIRE_SPY_BULLISH": True,
        })
        # Salidas semanas/meses: SL 2.5x (antes 1.5x) -> R:R 3.0/2.5 = 1:1.2.
        # El TP fijo queda deprecated como salida (lo sustituye el trailing).
        self.assertEqual(cfg.atr_sl_mult, 2.5)
        self.assertEqual(cfg.atr_trail_mult, 3.0)
        self.assertEqual(cfg.rr_label, "1:1.2")
        self.assertEqual(cfg.adx_min, 20.0)
        self.assertEqual(cfg.rs_min, 0.0)
        self.assertEqual(RS_WINDOW, 20)

    def test_roundtrip_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            cfg = StrategyConfig.load(p)
            self.assertTrue(os.path.exists(p))  # se crea con defaults
            cfg2 = StrategyConfig.load(p)
            self.assertEqual(cfg2.max_dist_ema8, 1.5)
            cfg2.max_dist_ema8 = 2.0
            cfg2.save(p)
            self.assertEqual(StrategyConfig.load(p).max_dist_ema8, 2.0)

    def test_env_override(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            os.environ["EXPOSURE_CAP"] = "999"
            try:
                self.assertEqual(StrategyConfig.load(p).exposure_cap, 999.0)
            finally:
                del os.environ["EXPOSURE_CAP"]

    def test_exposure_cap_default(self):
        self.assertEqual(StrategyConfig().exposure_cap, 8000.0)


class TestJournal(unittest.TestCase):
    def setUp(self):
        self.con = journal.connect(":memory:")

    def tearDown(self):
        self.con.close()

    def future(self, highs, lows, start="2024-01-02"):
        idx = pd.date_range(start, periods=len(highs), freq="B")
        return pd.DataFrame({"High": highs, "Low": lows}, index=idx)

    def test_log_dedupes(self):
        self.assertTrue(journal.log_signal(self.con, "AAPL", "2024-01-02", 100, 97, 106))
        self.assertFalse(journal.log_signal(self.con, "AAPL", "2024-01-02", 100, 97, 106))

    def test_win(self):
        f = self.future([101, 107], [99, 100])
        res, day = journal.resolve_outcome("2024-01-01", 97, 106, f)
        self.assertEqual((res, day), ("WIN", f.index[1].date().isoformat()))

    def test_loss(self):
        f = self.future([101, 102], [96, 99])
        res, _ = journal.resolve_outcome("2024-01-01", 97, 106, f)
        self.assertEqual(res, "LOSS")

    def test_same_bar_is_loss(self):
        f = self.future([110], [90])  # toca ambos -> conservador
        res, _ = journal.resolve_outcome("2024-01-01", 97, 106, f)
        self.assertEqual(res, "LOSS")

    def test_tz_aware_index(self):
        # yfinance real devuelve índice tz-aware: no debe lanzar TypeError
        idx = pd.date_range("2024-01-02", periods=2, freq="B", tz="America/New_York")
        f = pd.DataFrame({"High": [101, 107], "Low": [99, 100]}, index=idx)
        res, day = journal.resolve_outcome("2024-01-01", 97, 106, f)
        self.assertEqual(res, "WIN")

    def test_expired_and_pending(self):
        f = self.future([101] * 70, [99] * 70)
        res, _ = journal.resolve_outcome("2024-01-01", 97, 106, f, max_hold_days=60)
        self.assertEqual(res, "EXPIRED")
        f2 = self.future([101, 102], [99, 99])
        res2, _ = journal.resolve_outcome("2024-01-01", 97, 106, f2)
        self.assertEqual(res2, "PENDING")

    def test_resolve_pending_and_stats(self):
        journal.log_signal(self.con, "AAPL", "2024-01-01", 100, 97, 106)
        f = self.future([101, 107], [99, 100])
        n = journal.resolve_pending_for_ticker(self.con, "AAPL", f)
        self.assertEqual(n, 1)
        s = journal.stats(self.con)
        self.assertEqual((s["wins"], s["losses"], s["win_rate"]), (1, 0, 100.0))


class TestAlerts(unittest.TestCase):
    def test_format_contains_fields(self):
        text = alerts.format_signal("LONG", "AAPL", 150.0, 147.0, 156.0,
                                    "33 acc", 100.0, "1:2.0", "BULLISH")
        for bit in ("AAPL", "150.00", "147.00", "156.00", "33 acc", "1:2.0", "BULLISH"):
            self.assertIn(bit, text)

    def test_no_remote_channels_without_config(self):
        import shutil

        cfg = StrategyConfig()  # sin tokens ni webhook
        sig = dict(kind="LONG", ticker="AAPL", price=1.0, sl=0.9, tp=1.2,
                   units="1 acc", risk=100.0, rr_label="1:2.0", spy_regime="BULLISH")
        res = alerts.notify_signals([sig], cfg)
        # Sin credenciales nunca se toca Telegram/Discord; desktop solo si
        # hay notify-send en el sistema (best-effort).
        self.assertNotIn("telegram", res)
        self.assertNotIn("discord", res)
        if shutil.which("notify-send") is None:
            self.assertEqual(res, {})
        self.assertEqual(alerts.notify_signals([], cfg), {})


class TestBacktestSmoke(unittest.TestCase):
    def test_run_backtest_structure(self):
        from backtest import run_backtest
        cfg = StrategyConfig()
        df = ohlc_from(rising(400, step=0.2))
        spy = ohlc_from(rising(400, start=400.0, step=0.1))
        r = run_backtest("TEST", cfg, df, spy)
        self.assertEqual(r["ticker"], "TEST")
        for side in ("LONG", "WEAK"):
            for k in ("signals", "wins", "losses", "expired", "win_rate", "expectancy_R"):
                self.assertIn(k, r[side])

    def test_insufficient_data(self):
        from backtest import run_backtest
        r = run_backtest("TEST", StrategyConfig(), ohlc_from(rising(50)), ohlc_from(rising(50)))
        self.assertIn("error", r)


if __name__ == "__main__":
    unittest.main()
