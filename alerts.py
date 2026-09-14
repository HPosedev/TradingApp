"""Alertas activas (punto 4): Telegram / Discord / escritorio.

Todo best-effort y síncrono con timeouts cortos: se llama desde el hilo
worker del scan, nunca desde el hilo UI. Sin dependencias (urllib +
shutil de la stdlib). Cada canal se configura por entorno o config.json;
los vacíos simplemente se omiten.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request

TIMEOUT = 10


def send_telegram(bot_token: str, chat_id: str, text: str, timeout: int = TIMEOUT) -> bool:
    if not bot_token or not chat_id:
        return False
    try:
        payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def send_discord(webhook_url: str, text: str, timeout: int = TIMEOUT) -> bool:
    if not webhook_url:
        return False
    try:
        payload = json.dumps({"content": text}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def desktop_notify(title: str, text: str) -> bool:
    """Notificación de escritorio si hay notify-send disponible."""
    if shutil.which("notify-send") is None:
        return False
    try:
        subprocess.run(
            ["notify-send", title, text], check=False, timeout=TIMEOUT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def format_signal(kind: str, ticker: str, price: float, sl: float, tp: float,
                  units: str, risk: float, rr_label: str, spy_regime: str) -> str:
    tag = "ENTRADA LARGO" if kind == "LONG" else "ENTRADA DÉBIL (SPY Pullback)"
    return (
        f"[{tag}] {ticker} @ ${price:,.2f} | SL ${sl:,.2f} | TP ${tp:,.2f} | "
        f"VOL {units} | RIESGO ${risk:,.0f} | R:R {rr_label} | SPY {spy_regime}"
    )


def notify_signals(signals: list[dict], cfg) -> dict:
    """Envía cada señal nueva por los canales configurados.

    signals: dicts con keys kind, ticker, price, sl, tp, units, risk,
    rr_label, spy_regime. cfg: StrategyConfig. Retorna {canal: ok}.
    """
    if not signals:
        return {}
    lines = [format_signal(**s) for s in signals]
    text = "\n".join(lines)
    results: dict[str, bool] = {}
    if getattr(cfg, "telegram_bot_token", "") and getattr(cfg, "telegram_chat_id", ""):
        results["telegram"] = send_telegram(
            cfg.telegram_bot_token, cfg.telegram_chat_id, f"Scanner:\n{text}"
        )
    if getattr(cfg, "discord_webhook", ""):
        results["discord"] = send_discord(cfg.discord_webhook, f"**Scanner:**\n{text}")
    if shutil.which("notify-send") is not None:
        results["desktop"] = all(
            desktop_notify(f"Scanner: {s['ticker']}", format_signal(**s)) for s in signals
        )
    return results
