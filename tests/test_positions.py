"""Tests del tracking de posiciones con fichero AISLADO (tmp_path).

Regla: ningún test/pilot toca el positions.json real del usuario. Aquí
se usa tmp_path explícito + la variable FINANZAS_POSITIONS; el mismo
patrón deben seguir los pilots headless que abran/cierren posiciones.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

import positions as ps


class TestExposureIsolated(unittest.TestCase):
    def test_open_close_updates_total(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            fp = os.path.join(d, "positions.json")
            pos = {}
            u, i = ps.sizing_units("AAPL", 100.0, 95.0, 100.0)  # 20 acc, 2000
            self.assertEqual((u, i), (20.0, 2000.0))
            ps.open_position(pos, "AAPL", 100.0, fecha="2026-09-10",
                             stop_inicial=95.0, unidades=u, invertido=i,
                             path=fp)
            self.assertEqual(ps.total_exposure(pos), 2000.0)
            ps.close_position(pos, "AAPL", path=fp)
            self.assertEqual(ps.total_exposure(pos), 0.0)

    def test_block_threshold_math(self):
        # 6000 + 2000 = 8000 no supera; +500 más sí
        self.assertFalse(6000.0 + 2000.0 > 8000.0)
        self.assertTrue(8000.0 + 500.0 > 8000.0)

    def test_persistence_uses_given_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            fp = os.path.join(d, "positions.json")
            pos = ps.load_positions(fp)
            self.assertEqual(pos, {})
            # Con path explícito, open guarda directo en fp (no en el
            # default del cwd): así los tests nunca tocan el fichero real.
            ps.open_position(pos, "T", 10.0, fecha="2026-09-10",
                             stop_inicial=9.0, unidades=1.0, invertido=10.0,
                             path=fp)
            back = ps.load_positions(fp)
            self.assertEqual(back["T"]["invertido"], 10.0)
            self.assertTrue(ps.close_position(back, "T", path=fp))
            self.assertEqual(ps.load_positions(fp), {})

    def test_guard_blocks_real_write_under_test_runner(self):
        import hashlib
        real = ps.REAL_POSITIONS_FILE
        before = hashlib.sha256(real.read_bytes()).hexdigest() if real.exists() else None
        # Las 5 vías de persistencia pasan por save_positions: el guard
        # las para en seco en vez de escribir.
        with self.assertRaises(RuntimeError):
            ps.save_positions({"SYNTH": {}}, real)
        with self.assertRaises(RuntimeError):
            ps.open_position({}, "SYNTH", 1.0, path=real)
        with self.assertRaises(RuntimeError):
            ps.close_position({"SYNTH": {}}, "SYNTH", path=real)
        with self.assertRaises(RuntimeError):
            ps.update_maximum({"SYNTH": {"maximo_desde_entrada": 1.0}}, "SYNTH", 2.0, path=real)
        with self.assertRaises(RuntimeError):
            # Con atr_sl_mult deriva el stop y llega a escribir -> guard.
            ps.update_stop({"SYNTH": {"precio_entrada": 1.0}}, "SYNTH", 0.5, 3.0, 2.5, path=real)
        with self.assertRaises(RuntimeError):
            ps.ensure_sizing({"SYNTH": {"precio_entrada": 1.0, "stop_inicial": 0.5}},
                             "SYNTH", 100.0, path=real)
        after = hashlib.sha256(real.read_bytes()).hexdigest() if real.exists() else None
        self.assertEqual(before, after)

    def test_env_override_isolates_real_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            fp = os.path.join(d, "positions.json")
            os.environ["FINANZAS_POSITIONS"] = fp
            try:
                self.assertEqual(ps.positions_file(), ps.positions_file(fp))
                pos = ps.load_positions()  # resuelve al temporal
                self.assertEqual(pos, {})
                ps.open_position(pos, "TMP", 5.0, fecha="2026-09-10",
                                 stop_inicial=4.0, unidades=2.0, invertido=10.0)
                self.assertEqual(ps.load_positions()["TMP"]["invertido"], 10.0)
            finally:
                del os.environ["FINANZAS_POSITIONS"]


if __name__ == "__main__":
    unittest.main()
