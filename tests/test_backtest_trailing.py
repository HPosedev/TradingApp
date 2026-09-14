"""Tests del motor trailing-stop: mismas funciones que el vivo, sin look-ahead.

Todo offline con series sintéticas congeladas (semilla fija). Ningún test
escribe ficheros reales (el motor es puro; save_results solo a tmp).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

import numpy as np
import pandas as pd

from backtest import (
    apply_costs,
    avg_cost,
    net_view,
    rs_diff_series,
    save_results,
    side_cost,
    simulate_ticker,
    weekly_state_series,
)
from strategy import (
    compute_indicators,
    decide_signal,
    relative_strength,
    spy_regime,
    weekly_trend_ok,
)
from strategy_config import StrategyConfig
from trade_journal import close_trade


def ohlc(close, volume=1_000_000.0, spread=0.6, start="2020-01-01", freq="B"):
    v = np.asarray(close, dtype=float)
    idx = pd.date_range(start, periods=len(v), freq=freq)
    return pd.DataFrame({
        "Open": v, "High": v + spread, "Low": v - spread,
        "Close": v, "Volume": [volume] * len(v),
    }, index=idx)


def synth_pair():
    """Serie sintética congelada con racha LONG 2021-06-02..08 (calibrada)."""
    n = 400
    rng = np.random.default_rng(7)
    trend = 100 + np.cumsum(0.25 + rng.normal(0, 0.5, n))
    for i in range(9, n, 10):
        trend[i] -= 2.0
    vol = np.full(n, 1_000_000.0)
    vol[-30:] = 1_500_000.0
    df = ohlc(trend)
    df["Volume"] = vol
    spy_c = 400 + np.cumsum(0.05 + rng.normal(0, 0.3, n))
    spy = ohlc(spy_c, volume=10_000_000.0, spread=1.0)
    return df, spy


class TestCutoffEquivalence(unittest.TestCase):
    def test_live_call_unchanged(self):
        df, _ = synth_pair()
        cfg = StrategyConfig()
        self.assertEqual(weekly_trend_ok(df), weekly_trend_ok(df, cutoff=None))
        self.assertEqual(weekly_trend_ok(df),
                         weekly_trend_ok(df, cutoff=df.index[-1]))

    def test_weekly_series_matches_cutoff(self):
        # Serie vectorizada == evaluación truncada en D (varios cortes,
        # incluyendo zona fail-open temprana y calendario con fines de semana).
        for freq in ("B", "D"):
            df, _ = synth_pair()
            if freq == "D":
                df = ohlc(np.asarray(df["Close"]), start="2020-01-01", freq="D")
            series = weekly_state_series(df)
            for ts in list(df.index[::37]) + [df.index[-1]]:
                self.assertEqual(bool(series.loc[ts]),
                                 weekly_trend_ok(df, cutoff=ts),
                                 (freq, ts))

    def test_rs_series_matches_cutoff(self):
        df, spy = synth_pair()
        series = rs_diff_series(df, spy)
        for ts in list(df.index[::53]) + [df.index[-1]]:
            live = relative_strength(df["Close"].loc[:ts], spy["Close"].loc[:ts])
            got = float(series.loc[ts])
            if pd.isna(live):
                self.assertTrue(pd.isna(got), ts)
            else:
                self.assertAlmostEqual(got, live, places=8, msg=ts)

    def test_rs_weekend_calendar_uses_live_function(self):
        # El motor llama a relative_strength (as-of) por día, NO a la
        # serie shift-based: en calendario con finde difieren. Se prueba
        # la propiedad que importa: cada entrada del motor reevalúa LONG
        # con relative_strength truncado (aquí en par sintético D/B).
        n = 400
        rng = np.random.default_rng(7)
        trend = 100 + np.cumsum(0.25 + rng.normal(0, 0.5, n))
        for i in range(9, n, 10):
            trend[i] -= 2.0
        vol = np.full(n, 1_000_000.0)
        vol[-30:] = 1_500_000.0
        df = ohlc(trend, start="2020-01-01", freq="D")
        df["Volume"] = vol
        spy_c = 400 + np.cumsum(0.05 + rng.normal(0, 0.3, n))
        # SPY solo laborable, como en la realidad.
        bdays = pd.date_range("2020-01-01", periods=n, freq="D")
        bdays = bdays[bdays.dayofweek < 5][: len(spy_c)]
        spy = pd.DataFrame({
            "Open": spy_c[: len(bdays)], "High": spy_c[: len(bdays)] + 1,
            "Low": spy_c[: len(bdays)] - 1, "Close": spy_c[: len(bdays)],
            "Volume": 10_000_000.0,
        }, index=bdays)
        cfg = StrategyConfig()
        r = simulate_ticker("WEEKEND", cfg, df, spy)
        self.assertNotIn("error", r)
        ind = compute_indicators(df, cfg.atr_sl_mult, cfg.atr_tp_mult)
        wk = weekly_state_series(df)
        s50 = spy["Close"].rolling(50).mean()
        s200 = spy["Close"].rolling(200).mean()
        self.assertGreaterEqual(len(r["trades"]), 1)
        for t in r["trades"]:
            ts = pd.Timestamp(t["fecha_entrada"])
            row = ind.loc[ts]
            rs_live = relative_strength(df["Close"].loc[:ts], spy["Close"].loc[:ts])
            flags = (bool(row["Close"] > row["SMA200"]),
                     bool(row["Close"] > row["EMA8"]),
                     bool(float(row["dist_ema8"]) <= 1.5),
                     bool(45.0 <= float(row["RSI"]) <= 65.0),
                     bool(float(row["vol_ratio"]) >= 1.2),
                     bool(wk.loc[ts]),
                     bool(float(row["ADX"]) >= 20.0),
                     bool(rs_live >= 0.0))
            reg = spy_regime(float(spy["Close"].loc[:ts].iloc[-1]),
                             float(s50.loc[:ts].iloc[-1]),
                             float(s200.loc[:ts].iloc[-1]))
            self.assertEqual(decide_signal(*flags, reg, True, True), "LONG", ts)


class TestEngineLoop(unittest.TestCase):
    def test_opens_first_long_and_never_pyramids(self):
        df, spy = synth_pair()
        cfg = StrategyConfig()
        # Oráculo independiente (expresiones a mano) del primer LONG.
        ind = compute_indicators(df, cfg.atr_sl_mult, cfg.atr_tp_mult)
        wk = weekly_state_series(df)
        rs = rs_diff_series(df, spy)
        s50 = spy["Close"].rolling(50).mean()
        s200 = spy["Close"].rolling(200).mean()
        first_long = None
        for ts in df.index:
            row = ind.loc[ts]
            if pd.isna(row["SMA200"]) or pd.isna(row["RSI"]) or pd.isna(row["ATR"]):
                continue
            flags = (bool(row["Close"] > row["SMA200"]),
                     bool(row["Close"] > row["EMA8"]),
                     bool(float(row["dist_ema8"]) <= 1.5),
                     bool(45.0 <= float(row["RSI"]) <= 65.0),
                     bool(float(row["vol_ratio"]) >= 1.2),
                     bool(wk.loc[ts]),
                     bool(float(row["ADX"]) >= 20.0),
                     bool(float(rs.loc[ts]) >= 0.0))
            reg = spy_regime(float(spy["Close"].loc[ts]),
                             float(s50.loc[ts]), float(s200.loc[ts]))
            if decide_signal(*flags, reg, True, True) == "LONG":
                first_long = ts.date().isoformat()
                break
        self.assertEqual(first_long, "2021-06-02")
        r = simulate_ticker("SYNTH", cfg, df, spy)
        self.assertNotIn("error", r)
        self.assertGreaterEqual(len(r["trades"]), 1)
        # Abre exactamente en la primera oportunidad...
        self.assertEqual(r["trades"][0]["fecha_entrada"], "2021-06-02")
        # ...y nunca piramida: entradas estrictamente crecientes y cada
        # salida posterior a su entrada.
        entries = [t["fecha_entrada"] for t in r["trades"]]
        self.assertEqual(entries, sorted(entries))
        self.assertEqual(len(set(entries)), len(entries))
        for t in r["trades"]:
            self.assertLess(t["fecha_entrada"], t["fecha_cierre"])

    def test_management_walk_matches_longhand(self):
        # Recomputa la gestión del primer trade con aritmética a mano
        # (sin stop_with_breakeven): mismo exit, mismo R, mismos días.
        df, spy = synth_pair()
        cfg = StrategyConfig()
        r = simulate_ticker("SYNTH", cfg, df, spy)
        tr = r["trades"][0]
        ind = compute_indicators(df, cfg.atr_sl_mult, cfg.atr_tp_mult)
        entry = tr["precio_entrada"]
        init = entry - 2.5 * float(ind.loc[tr["fecha_entrada"]]["ATR"])
        self.assertAlmostEqual(tr["riesgo_inicial"], entry - init, places=8)
        risk = entry - init
        max_px, stop = entry, init
        exit_day = None
        exit_motivo = "fin-historial"
        for ts in df.loc[tr["fecha_entrada"]:].index[1:]:
            c = float(df["Close"].loc[ts])
            a = float(ind.loc[ts]["ATR"])
            max_px = max(max_px, c)
            trail = max_px - 3.0 * a
            level = max(trail, init)
            if max_px >= entry + risk:
                level = max(level, entry)
            stop = max(stop, level)  # ratchet
            if c < stop:
                exit_day = ts.date().isoformat()
                exit_motivo = "stop"
                break
        if exit_day is None:  # nunca tocó stop: cierra al último cierre
            exit_day = df.index[-1].date().isoformat()
        self.assertEqual(tr["fecha_cierre"], exit_day)
        self.assertEqual(tr["motivo_cierre"], exit_motivo)
        self.assertAlmostEqual(tr["precio_cierre"],
                               float(df["Close"].loc[exit_day]), places=8)
        units = tr["unidades"]
        self.assertAlmostEqual(tr["resultado_usd"],
                               (tr["precio_cierre"] - entry) * units, places=6)
        self.assertAlmostEqual(tr["resultado_r"],
                               tr["resultado_usd"] / (risk * units), places=8)
        d_in = pd.Timestamp(tr["fecha_entrada"]).date()
        d_out = pd.Timestamp(tr["fecha_cierre"]).date()
        self.assertEqual(tr["dias_en_mercado"], (d_out - d_in).days)

    def test_only_long_opens(self):
        # Cada trade del motor nació en un día LONG reevaluado con las
        # funciones reales (los WEAK/WAIT nunca abren).
        df, spy = synth_pair()
        cfg = StrategyConfig()
        r = simulate_ticker("SYNTH", cfg, df, spy)
        ind = compute_indicators(df, cfg.atr_sl_mult, cfg.atr_tp_mult)
        wk = weekly_state_series(df)
        rs = rs_diff_series(df, spy)
        s50 = spy["Close"].rolling(50).mean()
        s200 = spy["Close"].rolling(200).mean()
        for t in r["trades"]:
            ts = pd.Timestamp(t["fecha_entrada"])
            row = ind.loc[ts]
            flags = (bool(row["Close"] > row["SMA200"]),
                     bool(row["Close"] > row["EMA8"]),
                     bool(float(row["dist_ema8"]) <= 1.5),
                     bool(45.0 <= float(row["RSI"]) <= 65.0),
                     bool(float(row["vol_ratio"]) >= 1.2),
                     bool(wk.loc[ts]),
                     bool(float(row["ADX"]) >= 20.0),
                     bool(float(rs.loc[ts]) >= 0.0))
            reg = spy_regime(float(spy["Close"].loc[ts]),
                             float(s50.loc[ts]), float(s200.loc[ts]))
            self.assertEqual(decide_signal(*flags, reg, True, True), "LONG", ts)

    def test_short_data_returns_error(self):
        cfg = StrategyConfig()
        df = ohlc(list(range(50)))
        spy = ohlc([400.0] * 50)
        r = simulate_ticker("SYNTH", cfg, df, spy)
        self.assertIn("error", r)
        self.assertEqual(r["trades"], [])

    def test_results_schema_matches_journal(self):
        import tempfile
        df, spy = synth_pair()
        cfg = StrategyConfig()
        r = simulate_ticker("SYNTH", cfg, df, spy)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "backtest_results.json")
            save_results(r["trades"], p)
            from trade_journal import load_journal
            back = load_journal(p)
            self.assertEqual(len(back), len(r["trades"]))
            ref_keys = set(close_trade("X", {"precio_entrada": 1.0,
                                             "stop_inicial": 0.9,
                                             "unidades": 1.0,
                                             "fecha_entrada": "2020-01-01"},
                                       1.1, "2020-01-02").keys())
            for t in back:
                self.assertEqual(set(t.keys()), ref_keys)


class TestIngCosts(unittest.TestCase):
    def test_side_cost_hand(self):
        cfg = StrategyConfig()
        # Apertura 20x100=2000: min(20, 3+2)=5 + FX 10 = 15.0
        self.assertAlmostEqual(side_cost(2000.0, cfg), 15.0)
        # Cierre 20x120=2400: min(20, 3+2.4)=5.4 + FX 12 = 17.4
        self.assertAlmostEqual(side_cost(2400.0, cfg), 17.4)

    def test_side_cost_cap(self):
        cfg = StrategyConfig()
        # 200000: min(20, 3+200)=20 + FX 1000 = 1020.0
        self.assertAlmostEqual(side_cost(200000.0, cfg), 1020.0)

    def test_apply_costs_hand(self):
        cfg = StrategyConfig()
        trade = {"ticker": "T", "precio_entrada": 100.0, "precio_cierre": 120.0,
                 "unidades": 20.0, "riesgo_inicial": 10.0,
                 "resultado_usd": 400.0, "resultado_r": 2.0,
                 "motivo_cierre": "stop"}
        net = apply_costs(trade, cfg)
        # Brutos intactos + netos: coste 32.4, usd 367.6, R 1.838
        self.assertEqual((net["resultado_usd"], net["resultado_r"]), (400.0, 2.0))
        self.assertAlmostEqual(net["coste_total"], 32.4)
        self.assertAlmostEqual(net["resultado_usd_neto"], 367.6)
        self.assertAlmostEqual(net["resultado_r_neto"], 367.6 / 200.0)

    def test_net_stats_and_avg_cost(self):
        from trade_journal import stats as journal_stats
        cfg = StrategyConfig()
        gross = [
            {"resultado_usd": 400.0, "resultado_r": 2.0, "dias_en_mercado": 10,
             "precio_entrada": 100.0, "precio_cierre": 120.0,
             "unidades": 20.0, "riesgo_inicial": 10.0},
            {"resultado_usd": -80.0, "resultado_r": -1.0, "dias_en_mercado": 5,
             "precio_entrada": 80.0, "precio_cierre": 70.0,
             "unidades": 8.0, "riesgo_inicial": 10.0},
        ]
        netted = [apply_costs(t, cfg) for t in gross]
        # Coste T2: open 8x80=640 -> min(20,3.64)+3.2=6.84;
        # close 8x70=560 -> min(20,3.56)+2.8=6.36; total 13.2
        self.assertAlmostEqual(netted[1]["coste_total"], 6.84 + 6.36)
        s = journal_stats(net_view(netted))
        self.assertAlmostEqual(s["win_rate"], 0.5)
        # Neto T1: 367.6/200=1.838; T2: (-80-13.2)/80=-1.165
        self.assertAlmostEqual(s["avg_r_winners"], 1.838)
        self.assertAlmostEqual(s["avg_r_losers"], -1.165)
        self.assertAlmostEqual(s["expectancy"], 0.5 * 1.838 - 0.5 * 1.165)
        c_usd, c_r = avg_cost(netted)
        self.assertAlmostEqual(c_usd, (32.4 + 13.2) / 2)
        self.assertAlmostEqual(c_r, (32.4 / 200.0 + 13.2 / 80.0) / 2)

    def test_config_defaults(self):
        cfg = StrategyConfig()
        self.assertEqual((cfg.broker_commission_fixed, cfg.broker_commission_pct,
                          cfg.broker_commission_cap, cfg.broker_fx_pct),
                         (3.0, 0.001, 20.0, 0.005))


class TestIbCosts(unittest.TestCase):
    def test_config_defaults_ib(self):
        cfg = StrategyConfig()
        self.assertEqual((cfg.ib_commission_per_share, cfg.ib_commission_min,
                          cfg.ib_fx_pct), (0.005, 1.0, 0.00002))

    def test_ib_many_cheap_shares(self):
        # AAPL 1999: 2316 acc -> max(1, 11.58)=11.58/lado, topado al 1% del
        # nocional: apertura min(11.58, 9.22165)=9.22165 + FX 0.01844;
        # cierre min(11.58, 11.99249)=11.58 (no topa) + FX 0.02398.
        cfg = StrategyConfig()
        self.assertAlmostEqual(side_cost(922.1652270555496, cfg, "ib", 2316.0, "AAPL"),
                               9.221652270555496 + 0.018443304541101, places=6)
        self.assertAlmostEqual(side_cost(1199.2494485378265, cfg, "ib", 2316.0, "AAPL"),
                               11.58 + 0.023984988970765, places=6)

    def test_ib_cap_one_percent(self):
        # Tope directo: 2316 acc a 0.10 (nocional 231.6) -> min(11.58,
        # 2.316)=2.316 + FX 0.004632.
        cfg = StrategyConfig()
        self.assertAlmostEqual(side_cost(231.6, cfg, "ib", 2316.0, "AAPL"),
                               2.316 + 0.004632, places=6)
        # Sin tope efectivo: 10 acc a 100 (nocional 1000) -> max(1,
        # 0.05)=1.0 + FX 0.02.
        self.assertAlmostEqual(side_cost(1000.0, cfg, "ib", 10.0, "AAPL"),
                               1.0 + 0.02, places=6)

    def test_ib_minimum_binds(self):
        # ING: 105 acc -> max(1, 0.525)=1.0/lado + FX 0.05688+0.05795
        cfg = StrategyConfig()
        self.assertAlmostEqual(side_cost(2844.0699005126953, cfg, "ib", 105.0, "ING"),
                               1.0 + 0.056881398010254, places=6)
        self.assertAlmostEqual(side_cost(2897.3521614074707, cfg, "ib", 105.0, "ING"),
                               1.0 + 0.057947043228149, places=6)

    def test_ib_crypto_no_per_share(self):
        # BTC-USD fraccional: comisión 0, solo FX.
        cfg = StrategyConfig()
        self.assertAlmostEqual(side_cost(1000.0, cfg, "ib", 0.05, "BTC-USD"), 0.02)
        self.assertAlmostEqual(side_cost(1000.0, cfg, "ib", 0.05, "BTC-USD"),
                               side_cost(1000.0, cfg, "ib", 5000.0, "BTC-USD"))

    def test_ib_apply_costs_hand(self):
        # Mismos 3 trades verificados con ING, ahora con IB.
        cfg = StrategyConfig()
        cases = [
            ({"ticker": "AAPL", "precio_entrada": 0.3981715142726898,
              "precio_cierre": 0.5178106427192688, "unidades": 2316.0,
              "riesgo_inicial": 277.0842214822769 / 2.771532582349964,
              "resultado_usd": 277.0842214822769,
              "resultado_r": 2.771532582349964, "motivo_cierre": "stop"},
             20.844080564067362),
            ({"ticker": "AAPL", "precio_entrada": 2.0102269649505615,
              "precio_cierre": 2.5563764572143555, "unidades": 531.0,
              "riesgo_inicial": 290.0053803920746 / 2.9039568797159747,
              "resultado_usd": 290.0053803920746,
              "resultado_r": 2.9039568797159747, "motivo_cierre": "stop"},
             5.358497327983421),
            ({"ticker": "ING", "precio_entrada": 27.086380004882812,
              "precio_cierre": 27.593830108642578, "unidades": 105.0,
              "riesgo_inicial": 53.28226089477539 / 0.5360799825821271,
              "resultado_usd": 53.28226089477539,
              "resultado_r": 0.5360799825821271, "motivo_cierre": "stop"},
             2.114828441238403),
        ]
        for trade, exp_cost in cases:
            net = apply_costs(trade, cfg, "ib")
            self.assertAlmostEqual(net["coste_total_ib"], exp_cost, places=3)
            exp_usd = trade["resultado_usd"] - exp_cost
            self.assertAlmostEqual(net["resultado_usd_neto_ib"], exp_usd, places=3)
            denom = trade["riesgo_inicial"] * trade["unidades"]
            self.assertAlmostEqual(net["resultado_r_neto_ib"], exp_usd / denom, places=3)
            # Claves ING intactas en el mismo dict si se aplican ambos.
            both = apply_costs(apply_costs(trade, cfg, "ing"), cfg, "ib")
            for k in ("coste_total", "resultado_usd_neto", "resultado_r_neto",
                      "coste_total_ib", "resultado_usd_neto_ib",
                      "resultado_r_neto_ib", "resultado_usd", "resultado_r"):
                self.assertIn(k, both)

    def test_ing_result_unchanged(self):
        # Parametrizar no cambia ING: mismos números que antes.
        cfg = StrategyConfig()
        self.assertAlmostEqual(side_cost(2000.0, cfg, "ing"), 15.0)
        self.assertAlmostEqual(side_cost(2000.0, cfg), 15.0)


if __name__ == "__main__":
    unittest.main()
