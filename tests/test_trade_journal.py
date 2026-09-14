"""Tests del journal de cerradas. Todo en tmp (path explícito o fixture
autouse FINANZAS_POSITIONS/FINANZAS_JOURNAL); nunca el fichero real."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

import trade_journal as tj


def _pos(entry=100.0, stop=90.0, units=10.0, fecha="2026-09-01"):
    return {"fecha_entrada": fecha, "precio_entrada": entry,
            "maximo_desde_entrada": entry, "stop_inicial": stop,
            "stop_actual": stop, "unidades": units, "invertido": entry * units}


class TestCloseTrade(unittest.TestCase):
    def test_win(self):
        # (120-100)*10 = +200; riesgo 10*10=100 -> +2.0R; 10 días
        r = tj.close_trade("T", _pos(), 120.0, fecha_cierre="2026-09-11")
        self.assertEqual(r["resultado_usd"], 200.0)
        self.assertAlmostEqual(r["resultado_r"], 2.0)
        self.assertEqual(r["dias_en_mercado"], 10)
        self.assertEqual(r["riesgo_inicial"], 10.0)
        self.assertEqual(r["motivo_cierre"], "manual")

    def test_loss(self):
        # (70-80)*8 = -80; riesgo 10*8=80 -> -1.0R
        r = tj.close_trade("T", _pos(80.0, 70.0, 8.0), 70.0,
                           fecha_cierre="2026-09-08")
        self.assertEqual(r["resultado_usd"], -80.0)
        self.assertAlmostEqual(r["resultado_r"], -1.0)
        self.assertEqual(r["dias_en_mercado"], 7)

    def test_no_units_gives_r_zero(self):
        r = tj.close_trade("T", _pos(units=0.0), 150.0)
        self.assertEqual((r["resultado_usd"], r["resultado_r"]), (0.0, 0.0))


class TestStats(unittest.TestCase):
    def _trades(self):
        return [
            {"resultado_usd": 200.0, "resultado_r": 2.0, "dias_en_mercado": 10},
            {"resultado_usd": 75.0, "resultado_r": 1.5, "dias_en_mercado": 5},
            {"resultado_usd": 50.0, "resultado_r": 0.5, "dias_en_mercado": 3},
            {"resultado_usd": -80.0, "resultado_r": -1.0, "dias_en_mercado": 7},
            {"resultado_usd": -20.0, "resultado_r": -0.5, "dias_en_mercado": 2},
            {"resultado_usd": 0.0, "resultado_r": 0.0, "dias_en_mercado": 4},
        ]

    def test_aggregates(self):
        s = tj.stats(self._trades())
        self.assertEqual(s["n"], 6)
        self.assertAlmostEqual(s["win_rate"], 0.5)
        self.assertAlmostEqual(s["avg_r_winners"], 4.0 / 3)
        self.assertAlmostEqual(s["avg_r_losers"], -0.5)
        self.assertAlmostEqual(s["expectancy"], 0.5 * (4.0 / 3) - 0.5 * 0.5)
        self.assertAlmostEqual(s["avg_days"], 31.0 / 6)
        self.assertAlmostEqual(s["avg_days_winners"], 6.0)
        self.assertAlmostEqual(s["avg_days_losers"], 13.0 / 3)
        self.assertTrue(s["reliable"])

    def test_insufficient_sample(self):
        s = tj.stats(self._trades()[:4])
        self.assertFalse(s["reliable"])
        self.assertEqual(s["n"], 4)

    def test_empty(self):
        s = tj.stats([])
        self.assertEqual((s["n"], s["reliable"]), (0, False))


class TestPersistence(unittest.TestCase):
    def test_log_appends_with_all_fields(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            fp = os.path.join(d, "journal.json")
            self.assertEqual(tj.load_journal(fp), [])
            rec = tj.log_close("T", _pos(), 120.0, fecha_cierre="2026-09-11",
                               path=fp)
            self.assertAlmostEqual(rec["resultado_r"], 2.0)
            back = tj.load_journal(fp)
            self.assertEqual(len(back), 1)
            for k in ("ticker", "fecha_entrada", "fecha_cierre",
                      "dias_en_mercado", "precio_entrada", "precio_cierre",
                      "unidades", "riesgo_inicial", "resultado_usd",
                      "resultado_r", "motivo_cierre"):
                self.assertIn(k, back[0])

    def test_guard_blocks_real_write(self):
        real = tj.REAL_JOURNAL_FILE
        existed_before = real.exists()
        with self.assertRaises(RuntimeError):
            tj.log_close("SYNTH", _pos(), 1.0, path=real)
        self.assertEqual(real.exists(), existed_before)


if __name__ == "__main__":
    unittest.main()
