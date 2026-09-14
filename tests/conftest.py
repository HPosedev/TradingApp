"""Aislamiento global de la suite: ningún test toca ficheros reales.

Fuerza FINANZAS_POSITIONS a un tmp_path único por test (autouse: cada
test lo recibe sin activarlo). Así, aunque un test futuro se olvide del
aislamiento, sus escrituras van a un temporal. Segunda capa: el guard
en positions.save_positions rechaza escrituras a la ruta real bajo
cualquier runner (pytest por variable, unittest por pila) con excepción.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolated_storage_files(tmp_path, monkeypatch):
    fp = tmp_path / "positions.json"
    fj = tmp_path / "journal.json"
    monkeypatch.setenv("FINANZAS_POSITIONS", str(fp))
    monkeypatch.setenv("FINANZAS_JOURNAL", str(fj))
    return fp
