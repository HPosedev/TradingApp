from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Footer, Static, DataTable, Input, Sparkline
from textual.screen import ModalScreen
from textual import work
from rich.text import Text
import math
import random
from datetime import datetime
import yfinance as yf
import pandas as pd
import json
from pathlib import Path


def num_cell(text: str) -> Text:
    """Celda numérica alineada a la derecha (DataTable.add_column no acepta justify)."""
    return Text(text, justify="right")


# Score de filtros de entrada (8 condiciones de la ficha del ticker;
# el régimen SPY es filtro de mercado aparte y NO cuenta en la barra).
FILTER_KEYS = ('c_sma', 'c_ema', 'c_dist', 'c_rsi',
               'c_vol', 'c_weekly', 'c_adx', 'c_rs')


def filter_score(d: dict) -> int:
    """Nº de filtros cumplidos (0-8). Única fórmula del score."""
    try:
        return sum(1 for k in FILTER_KEYS if d.get(k, False))
    except Exception:
        return 0


def score_color(score: int) -> str:
    """Color por tramo: 7-8 verde, 5-6 ámbar, 0-4 gris neutro."""
    try:
        s = int(score)
    except Exception:
        return TN_DIM
    if s >= 7:
        return TN_GREEN
    if s >= 5:
        return TN_YELLOW
    return TN_DIM


def score_bar(score: int) -> str:
    """Texto de la barra: '6/8 ▰▰▰▰▰▰▱▱' (N llenos + resto vacíos)."""
    try:
        s = max(0, min(8, int(score)))
    except Exception:
        s = 0
    return f"{s}/8 {'▰' * s}{'▱' * (8 - s)}"


def score_cell(score: int) -> Text:
    """Celda Score coloreada por tramo, alineada a la derecha."""
    try:
        s = max(0, min(8, int(score)))
    except Exception:
        s = 0
    return Text.from_markup(f"[bold {score_color(s)}]{score_bar(s)}[/]",
                            justify="right")


from strategy_config import StrategyConfig
from strategy import (
    compute_indicators,
    decide_signal,
    relative_strength,
    spy_regime as spy_regime_of,
    trailing_stop as chandelier_trailing,
    weekly_trend_ok,
)
import journal
import alerts
import positions as positions_store
import trade_journal as trades_journal

TICKERS_FILE = Path("tickers.json")
DEFAULT_TICKERS = ['AAPL', 'MSFT', 'NVDA', 'AMZN', 'GOOGL', 'TSLA', 'BTC-USD']
# Config externa (config.json + entorno): los números viven fuera del código.
CONFIG = StrategyConfig.load()
DEFAULT_RISK_PER_TRADE = CONFIG.risk_per_trade
# Fuente única de verdad, derivada de CONFIG (compat legacy con turnos previos).
STRATEGY_RULES = CONFIG.to_rules_dict()
RR_LABEL = CONFIG.rr_label
# Paleta Tokyo Night — constantes semánticas centralizadas
TN_GREEN = "#9ece6a"
TN_RED = "#f7768e"
TN_YELLOW = "#e0af68"
TN_BLUE = "#7aa2f7"
TN_CYAN = "#7dcfff"
TN_DIM = "#565f89"
POSITIVO = TN_GREEN
NEGATIVO = TN_RED
NEUTRO = TN_BLUE
# Señales con símbolos finos y consistentes (●/○/◐) en vez de emoji.
# WEAK = setup válido pero SPY en Pullback (punto 3: filtro integrado,
# no solo informativo). Contiene "ENTRADA" para que orden y filtro la traten
# como accionable junto a LONG.
SIGNAL_LONG = f"[bold {TN_GREEN}]● ENTRADA LARGO[/]"
SIGNAL_WEAK = f"[bold {TN_YELLOW}]◐ ENTRADA DÉBIL (SPY Pullback)[/]"
SIGNAL_WAIT = "[dim white]○ ESPERAR[/]"
SIGNAL_SPY_WAIT = f"[bold {TN_YELLOW}]● ESPERAR (SPY Bajista)[/]"
SIGNAL_STRINGS = {
    "LONG": SIGNAL_LONG,
    "WEAK": SIGNAL_WEAK,
    "WAIT": SIGNAL_WAIT,
    "WAIT_SPY": SIGNAL_SPY_WAIT,
}
import json
from pathlib import Path


def generar_sparkline(precios: list) -> str:
    if not precios:
        return ""
    # Escala completa de bloques desde 1/8 hasta 8/8 (U+2581 a U+2588)
    caracteres = [' ', '▂', '▃', '▄', '▅', '▆', '▇', '█']
    min_p, max_p = min(precios), max(precios)
    if max_p == min_p:
        return caracteres[3] * len(precios)
    return "".join(caracteres[min(len(caracteres) - 1, int((p - min_p) / (max_p - min_p) * (len(caracteres) - 1)))] for p in precios)

class InspectionModal(ModalScreen):
    BINDINGS = [
        ("escape", "dismiss", "Cerrar"),
        ("enter", "dismiss", "Cerrar"),
        ("q", "dismiss", "Cerrar"),
    ]

    def __init__(self, ticker: str, data: dict, risk_per_trade: float,
                 position: dict | None = None) -> None:
        super().__init__()
        self.ticker = ticker
        self.d = data
        self.risk = risk_per_trade
        # Registro de la posición abierta (None = solo señal, sin compra).
        self.pos = dict(position) if isinstance(position, dict) else None

    def compose(self):
        chk = lambda c: f"[bold {POSITIVO}]✔ OK[/]" if c else f"[bold {NEGATIVO}]✖ NO[/]"
        risk_unit = self.d['price'] - self.d['sl']
        if risk_unit > 0:
            units_val, invested = positions_store.sizing_units(
                self.ticker, self.d['price'], self.d['sl'], self.risk
            )
            if "USD" in self.ticker:
                units_str = f"{units_val:.4f}"
            else:
                units_val = int(units_val)
                units_str = f"{units_val} acc" if units_val >= 1 else f"[dim {NEGATIVO}]0 acc (Riesgo insuficiente)[/dim]"
        else:
            units_str = "N/A"
            invested = 0.0

        prices = self.d.get('prices', [])
        p_min = min(prices) if prices else self.d['price']
        p_max = max(prices) if prices else self.d['price']
        score = self.d.get('score', filter_score(self.d))
        try:
            score = max(0, min(8, int(score)))
        except Exception:
            score = 0
        score_c = score_color(score)
        rs_val = self.d.get('rs_diff', float('nan'))
        rs_str = f"{rs_val:+.1f} pp" if math.isfinite(rs_val) else "N/A"
        adx_val = self.d.get('adx', float('nan'))
        adx_str = f"{adx_val:.1f}" if math.isfinite(adx_val) else "N/A"
        dist_color = POSITIVO if self.d['dist_ema8'] >= 0 else NEGATIVO
        # Salidas semanas/meses: sin objetivo fijo. Con posición abierta se
        # muestra el trailing stop; sin ella, solo el SL inicial (2.5x ATR).
        trail = self.d.get('trailing_stop')
        stop_eff = self.d.get('stop_actual')
        if self.pos is not None:
            if stop_eff is None:
                stop_eff = self.pos.get('stop_actual', self.d['sl'])
            if trail is None:
                trail = self.pos.get('trailing_stop', stop_eff)
            entry_px = float(self.pos.get('precio_entrada', self.d['price']))
            max_px = float(self.pos.get('maximo_desde_entrada', entry_px))
            be_on = bool(self.pos.get('breakeven', self.d.get('breakeven', False)))
            init_stop = float(self.pos.get('stop_inicial', self.d['sl']))
            try:
                be_target = entry_px + (entry_px - init_stop)
            except Exception:
                be_target = float('nan')
            import math as _m2
            be_str = (
                f"[bold {POSITIVO}]ACTIVO[/] (stop en entrada)"
                if be_on else
                (f"pendiente (+1R: ${be_target:,.2f})" if _m2.isfinite(be_target) else "pendiente")
            )
            trail_str = f"${float(trail):,.2f}" if (trail is not None and _m2.isfinite(float(trail))) else "—"
            stop_eff_str = f"${float(stop_eff):,.2f}" if (stop_eff is not None and _m2.isfinite(float(stop_eff))) else f"${init_stop:,.2f}"
            plan_lines = (
                f"[bold]Plan de Trading ($ {self.risk:,.0f} riesgo) · POSICIÓN ABIERTA:[/bold]\n"
                f"  • Entrada:          [bold white]${entry_px:,.2f}[/bold white] [dim]({self.pos.get('fecha_entrada', '?')})[/dim]\n"
                f"  • SL inicial (2.5x): [{NEGATIVO}]${init_stop:,.2f}[/]\n"
                f"  • Trailing Stop:    [bold {TN_CYAN}]{trail_str}[/] [dim](Chandelier 3.0x ATR · máx ${max_px:,.2f})[/dim]\n"
                f"  • Stop efectivo:    [bold {NEGATIVO}]{stop_eff_str}[/] [dim]· Breakeven {be_str}[/dim]\n"
                f"  • Sin objetivo fijo: [dim]la salida se mueve con el precio (solo sube)[/dim]\n"
                f"  • Ejecución sugerida: [bold white]{units_str}[/bold white] (~${invested:,.2f})\n"
                f"  • Score señal: [bold white]{score}/8[/bold white] · Señal: {self.d.get('signal', '')}"
            )
        else:
            plan_lines = (
                f"[bold]Plan de Trading ($ {self.risk:,.0f} riesgo) · SEÑAL (sin posición):[/bold]\n"
                f"  • Stop Loss inicial (2.5x ATR): [bold {NEGATIVO}]${self.d['sl']:,.2f}[/bold {NEGATIVO}]\n"
                f"  • Trailing Stop: [dim]— (aparece al abrir con 'o'; sin objetivo fijo)[/dim]\n"
                f"  • Ejecución sugerida: [bold white]{units_str}[/bold white] (~${invested:,.2f})\n"
                f"  • Score señal: [bold white]{score}/8[/bold white] · Señal: {self.d.get('signal', '')}"
            )

        with Vertical(id="modal-dialog"):
            yield Static(f"[bold {TN_BLUE}]{self.ticker}[/]  [dim]· FICHA TÉCNICA[/dim]", id="modal-title")
            yield Static(
                f"Último Cierre: [bold white]${self.d['price']:,.2f}[/bold white]  "
                f"ATR: [{TN_YELLOW}]${self.d['atr']:,.2f}[/]  "
                f"Dist EMA8: [bold {dist_color}]{self.d['dist_ema8']:+.2f}%[/]",
                id="modal-price",
                classes="modal-card",
            )
            yield Static(
                f"[bold]Tendencia ({len(prices) if prices else 20}d)[/bold]  "
                f"[dim]min ${p_min:,.2f} · max ${p_max:,.2f}[/dim]",
                id="modal-spark-label",
            )
            yield Sparkline(prices if prices else [], id="modal-spark")
            yield Static(
                f"[bold]Filtros Cuantitativos [{score_c}]{score_bar(score)}[/]:[/bold]\n"
                f"  {chk(self.d['c_sma'])} Tendencia Macro (Precio > SMA 200: ${self.d['sma200']:,.2f})\n"
                f"  {chk(self.d['c_ema'])} Momentum Corto (Precio > EMA 8: ${self.d['ema8']:,.2f})\n"
                f"  {chk(self.d['c_dist'])} Rango Entrada (Distancia EMA 8 <= {STRATEGY_RULES['MAX_DIST_EMA8']}% | Actual: {self.d['dist_ema8']:+.2f}%)\n"
                f"  {chk(self.d['c_rsi'])} Zona RSI ({STRATEGY_RULES['MIN_RSI']:.0f} - {STRATEGY_RULES['MAX_RSI']:.0f} | Actual: {self.d['rsi']:.1f})\n"
                f"  {chk(self.d['c_vol'])} Volumen Institucional (Volumen >= {STRATEGY_RULES['MIN_VOL_RATIO']}x | Actual: {self.d['vol_ratio']:.2f}x)\n"
                f"  {chk(self.d.get('c_weekly', True))} Tendencia Semanal (filtro multi-timeframe)\n"
                f"  {chk(self.d.get('c_adx', True))} Fuerza Tendencia (ADX >= {CONFIG.adx_min:.0f} | Actual: {adx_str})\n"
                f"  {chk(self.d.get('c_rs', True))} Fuerza Relativa (RS vs SPY 20d >= {CONFIG.rs_min:+.0f} pp | Actual: {rs_str})",
                id="modal-filters",
                classes="modal-card",
            )
            # Bloque exclusivo del modal (no repetido en sidebar): contexto de riesgo
            yield Static(
                plan_lines,
                id="modal-plan",
                classes="modal-card",
            )
            yield Static(
                "[dim]Pulsa [bold white]Enter[/bold white], [bold white]Esc[/bold white] o [bold white]q[/bold white] para volver[/dim]",
                id="modal-hint",
            )


class JournalModal(ModalScreen):
    """Estadísticas agregadas del journal de cerradas (tecla 'j')."""

    BINDINGS = [
        ("escape", "dismiss", "Cerrar"),
        ("enter", "dismiss", "Cerrar"),
        ("q", "dismiss", "Cerrar"),
    ]

    def __init__(self, trades: list) -> None:
        super().__init__()
        self.trades = list(trades) if isinstance(trades, list) else []

    def compose(self):
        st = trades_journal.stats(self.trades)
        n = st.get("n", 0)
        # Bloque 1 (Ledger): historial individual, siempre visible aunque
        # la muestra no dé para métricas fiables.
        if not self.trades:
            ledger = "[dim]Sin operaciones cerradas todavía. Cierra posiciones con 'x'.[/dim]"
        else:
            lines = []
            for t in reversed(self.trades[-5:]):
                try:
                    r = float(t.get("resultado_r", 0.0) or 0.0)
                    usd = float(t.get("resultado_usd", 0.0) or 0.0)
                    days = int(t.get("dias_en_mercado", 0) or 0)
                except Exception:
                    r, usd, days = 0.0, 0.0, 0
                r_c = POSITIVO if r > 0 else NEGATIVO
                lines.append(
                    f"[bold white]{str(t.get('ticker', '?')):<8}[/] "
                    f"[dim]{days}d[/] "
                    f"[bold {r_c}]{r:+.2f}R[/] "
                    f"[dim](${usd:+,.2f})[/dim]"
                )
            ledger = "[bold]Últimas operaciones[/bold]\n" + "\n".join(lines)
        # Bloque 2 (Métricas): con muestra preliminar se avisa pero se
        # muestran igual (ya no bloquean); consolidada si es fiable.
        wr = st["win_rate"] * 100.0
        wr_c = POSITIVO if wr >= 50 else NEGATIVO
        exp_c = POSITIVO if st["expectancy"] >= 0 else NEGATIVO
        metrics = (
            f"  • Win rate: [{wr_c}]{wr:.1f}%[/]\n"
            f"  • R prom ganadoras: [bold {POSITIVO}]+{st['avg_r_winners']:.2f}R[/]  "
            f"· perdedoras: [bold {NEGATIVO}]{st['avg_r_losers']:.2f}R[/]\n"
            f"  • Expectancy: [bold {exp_c}]{st['expectancy']:+.2f}R[/] por operación\n"
            f"  • Días en mercado: [bold white]{st['avg_days']:.1f}[/] "
            f"[dim](ganadoras {st['avg_days_winners']:.1f} · perdedoras {st['avg_days_losers']:.1f})[/]"
        )
        if st.get("reliable"):
            header = f"[bold]Journal de cerradas ({n} ops)[/bold]\n"
        else:
            header = (
                f"[bold {TN_YELLOW}]⚠ Muestra preliminar ({n}/5 ops):[/] "
                f"[dim]orientativo hasta {trades_journal.MIN_SAMPLE} cierres.[/dim]\n"
            )
        with Vertical(id="modal-dialog"):
            yield Static("[bold cyan]Rendimiento real[/bold cyan]", id="modal-title")
            yield Static(ledger, id="modal-ledger", classes="modal-card")
            yield Static(header + metrics, id="modal-plan", classes="modal-card")
            yield Static(
                "[dim]Pulsa [bold white]Enter[/bold white], [bold white]Esc[/bold white] o [bold white]q[/bold white] para volver[/dim]",
                id="modal-hint",
            )


class FinanceApp(App):
    CSS = """
    Screen {
        background: #16161e;
        color: #c0caf5;
    }

    #sidebar {
        width: 42;
        dock: left;
        background: #1a1b26;
        border-right: vkey #292e42;
        padding: 1 1;
    }

    #sidebar-detail {
        background: #1a1b26;
        padding: 0 1;
        height: 1fr;
        content-align: left top;
    }

    .side-card {
        background: #1f2335;
        border: round #3b4261;
        padding: 0 1;
        margin-bottom: 1;
        content-align: left top;
    }

    #side-spark {
        height: 3;
        color: #7dcfff;
    }

    #main {
        padding: 1 2;
        background: #16161e;
    }

    #top-bar {
        height: 3;
        margin-bottom: 1;
        layout: horizontal;
    }

    .metric-box {
        width: 1fr;
        background: #1f2335;
        border: round #3b4261;
        content-align: center middle;
        text-style: bold;
    }

    #market-card {
        width: 1.6fr;
        border: round #7aa2f7;
        background: #24283b;
    }

    #status-card-loading {
        border: round #e0af68;
    }

    #ticker-input {
        margin-bottom: 1;
        height: 3;
        border: round #3b4261;
        background: #1a1b26;
        color: #7aa2f7;
    }

    #ticker-input:focus {
        border: round #7aa2f7;
        background: #283457;
    }

    DataTable {
        height: 1fr;
        background: #1a1b26;
        border: round #3b4261;
    }

    DataTable > .datatable--header {
        background: #24283b;
        color: #7dcfff;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #283457;
        color: #c0caf5;
        text-style: bold;
    }

    DataTable > .datatable--even-row {
        background: #1a1b26;
    }

    DataTable > .datatable--odd-row {
        background: #1e2233;
    }

    Footer {
        background: #1a1b26;
        color: #c0caf5;
    }

    Footer > .footer--key {
        background: #3b4261;
        color: #7dcfff;
        text-style: bold;
    }

    Footer > .footer--description {
        color: #9aa5ce;
    }

    InspectionModal {
        align: center middle;
        background: rgba(16, 18, 27, 0.8);
    }

    #modal-dialog {
        width: 66;
        height: auto;
        max-height: 90%;
        background: #1f2335;
        border: round #7aa2f7;
        padding: 1 2;
    }

    #modal-title {
        text-style: bold;
        color: #7aa2f7;
        border-bottom: solid #3b4261;
        padding-bottom: 1;
        margin-bottom: 1;
    }

    .modal-card {
        background: #24283b;
        border: round #3b4261;
        padding: 0 1;
        margin-bottom: 1;
    }

    #modal-ledger {
        max-height: 10;
        overflow-y: auto;
    }

    #modal-spark {
        height: 5;
        color: #7dcfff;
        margin-bottom: 1;
    }

    #modal-spark-label {
        margin-top: 1;
    }

    #modal-hint {
        content-align: center middle;
    }
    """

    BINDINGS = [
        ("q", "quit", "Salir"),
        ("r", "refresh_data", "Refrescar"),
        ("f", "toggle_filter", "Filtrar Señales"),
        ("d", "delete_ticker", "Borrar Ticker"),
        ("+", "increase_risk", "+$25 Riesgo"),
        ("-", "decrease_risk", "-$25 Riesgo"),
        ("c", "copy_order", "Copiar Orden"),
        ("o", "open_position", "Abrir Posición"),
        ("x", "close_position", "Cerrar Posición"),
        ("j", "show_journal", "Journal"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.ticker_data: dict = {}
        self.filter_signals_only = False
        self.cached_results = []
        self.tickers = self.load_tickers()
        self.risk_per_trade = DEFAULT_RISK_PER_TRADE
        # Paso 1: registro de posiciones abiertas (dict persistido en
        # positions.json). Solo existe si el usuario la abre con "o".
        try:
            self.positions = positions_store.load_positions()
        except Exception:
            self.positions = {}
        # Exposición: backfill de sizing en registros legacy (sin
        # unidades/invertido) con el riesgo/op actual. Best-effort.
        try:
            for tk in list(self.positions):
                positions_store.ensure_sizing(
                    self.positions, tk, self.risk_per_trade
                )
        except Exception:
            pass

    def _positions_exposure(self) -> float:
        """Exposición total real: suma del invertido en self.positions.

        Único origen de verdad del contador (nunca una variable separada).
        """
        try:
            return positions_store.total_exposure(
                getattr(self, "positions", {})
            )
        except Exception:
            return 0.0

    def _count_card_text(self) -> str:
        """Texto del contador: activos + exposición real vs tope."""
        try:
            exp = self._positions_exposure()
            top = CONFIG.exposure_cap
        except Exception:
            exp = 0.0
            try:
                top = CONFIG.exposure_cap
            except Exception:
                return f"[{POSITIVO}]Activos:[/] {len(self.tickers)}"
        return (
            f"[{POSITIVO}]Activos:[/] {len(self.tickers)} "
            f"[dim]· Exp ${exp:,.0f}/${top:,.0f}[/dim]"
        )

    def _update_exposure_card(self) -> None:
        """Re-renderiza el contador desde positions.json (origen único)."""
        try:
            if self.query("#count-card"):
                self.query_one("#count-card", Static).update(
                    self._count_card_text()
                )
        except Exception:
            pass

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Static("[bold cyan]Auditoría Técnica[/bold cyan]\n")
                with Vertical(id="sidebar-detail"):
                    # Widgets persistentes: se actualizan con .update()/.data,
                    # nunca se destruyen (remove+mount en cada highlight
                    # produce condiciones de carrera: cards perdidas/duplicadas).
                    yield Static("[dim]Selecciona una fila para auditar...[/dim]", id="side-price", classes="side-card")
                    yield Sparkline([], id="side-spark", classes="side-card")
                    yield Static("", id="side-filters", classes="side-card")
                    yield Static("", id="side-plan", classes="side-card")
            with Vertical(id="main"):
                with Horizontal(id="top-bar"):
                    yield Static("[cyan]SCANNER[/cyan] [dim]|[/dim] [white]Textual TUI[/white]", classes="metric-box", id="title-card")
                    yield Static(self._count_card_text(), classes="metric-box", id="count-card")
                    yield Static("[dim]○ SPY: Evaluando...[/dim]", classes="metric-box", id="market-card")
                    yield Static("[cyan]⏱ Refresco:[/cyan] 05:00", classes="metric-box", id="timer-card")
                    yield Static(f"[bold {POSITIVO}]● Estado: Normal[/]", classes="metric-box", id="status-card")
                yield Input(placeholder="Escribe un ticker y pulsa Enter (ej. AMD, PLTR)...", id="ticker-input")
                yield DataTable(id="orders-table")
        yield Footer()

    def on_mount(self) -> None:
        # Configurar tabla
        table = self.query_one(DataTable)
        table.zebra_stripes = True
        # DataTable.add_column no acepta justify: se alinea con Text(justify="right")
        # en cabeceras y celdas numéricas (ver helper num_cell).
        table.add_column("Ticker", width=10)
        table.add_column(Text("Precio", justify="right"))
        table.add_column(Text("Dist EMA8", justify="right"))
        table.add_column(Text("RSI 14", justify="right"))
        table.add_column(Text("Vol / Vol20", justify="right"))
        table.add_column(Text("SL / Trail", justify="right"))
        table.add_column(Text("Score", justify="right"))
        table.add_column("Señal")
        table.cursor_type = "row"
        # Foco inicial en la tabla (no en el input): al arrancar se audita
        # el primer valor por defecto en vez del campo de añadir tickers.
        self.set_focus(table)
        # Ocultar cards vacías hasta la primera selección
        self.query_one("#side-spark", Sparkline).display = False
        self.query_one("#side-filters", Static).display = False
        self.query_one("#side-plan", Static).display = False
        # El contador ya refleja positions.json antes del primer refresco
        # (incluye posiciones de sesiones anteriores).
        self._update_exposure_card()
        self.request_scan()
        
        # Inicializar temporizador de auto-refresco
        self.refresh_interval = 300
        self.time_left = self.refresh_interval
        self.set_interval(1.0, self._tick_timer)

    def request_scan(self) -> None:
        """Marca estado cargando de forma segura y lanza el worker en background."""
        try:
            self.query_one("#status-card", Static).update(f"[bold {TN_YELLOW}]◌ Cargando...[/]")
        except Exception:
            pass
        self.update_scan()

    def _show_loading(self) -> None:
        try:
            self.query_one("#status-card", Static).update(f"[bold {TN_YELLOW}]◌ Cargando...[/]")
        except Exception:
            pass

    def _tick_timer(self) -> None:
        if not hasattr(self, 'time_left'):
            return
        self.time_left -= 1
        if self.time_left <= 0:
            self.time_left = self.refresh_interval
            self.request_scan()
        
        mins, secs = divmod(self.time_left, 60)
        if self.query("#timer-card"):
            self.query_one("#timer-card", Static).update(f"[cyan]⏱ Refresco:[/cyan] {mins:02d}:{secs:02d}")

    def _refresh_active_sidebar(self) -> None:
        table = self.query_one(DataTable)
        if table.cursor_row is not None and table.row_count > 0:
            try:
                row = table.get_row_at(table.cursor_row)
                self.update_sidebar_for_ticker(str(row[0]))
            except Exception:
                pass

    def action_increase_risk(self) -> None:
        self.risk_per_trade += 25.0
        self._refresh_active_sidebar()
        self.query_one("#status-card", Static).update(f"[{TN_CYAN}]Riesgo:[/] ${self.risk_per_trade:,.0f}")

    def action_decrease_risk(self) -> None:
        if self.risk_per_trade > 25.0:
            self.risk_per_trade -= 25.0
            self._refresh_active_sidebar()
            self.query_one("#status-card", Static).update(f"[{TN_CYAN}]Riesgo:[/] ${self.risk_per_trade:,.0f}")

    def update_sidebar_for_ticker(self, ticker: str) -> None:
        """Actualiza el sidebar con 3 cards separadas + Sparkline nativo"""
        if not hasattr(self, 'ticker_data') or ticker not in self.ticker_data:
            try:
                self.query_one("#side-price", Static).update(
                    f"[bold white]{ticker}[/bold white]\n[dim {TN_YELLOW}]Sin datos técnicos disponibles[/]"
                )
                self.query_one("#side-spark", Sparkline).display = False
                self.query_one("#side-filters", Static).display = False
                self.query_one("#side-plan", Static).display = False
            except Exception:
                pass
            return
        d = self.ticker_data[ticker]
        chk = lambda c: f"[bold {POSITIVO}]✔[/]" if c else f"[bold {NEGATIVO}]✖[/]"

        # Tamaño con el sizing único (misma fórmula que al abrir con 'o')
        risk_per_unit = d['price'] - d['sl']

        if risk_per_unit > 0:
            units_val, total_invested = positions_store.sizing_units(
                ticker, d['price'], d['sl'], self.risk_per_trade
            )
            if "USD" in ticker:
                units_str = f"{units_val:.4f}"
            else:
                units_val = int(units_val)
                units_str = f"{units_val} acc" if units_val >= 1 else f"[dim {NEGATIVO}]0 acc (Riesgo insuficiente)[/dim]"

            total_loss = units_val * risk_per_unit

            pos = getattr(self, 'positions', {}).get(ticker)
            if pos is not None:
                trail = d.get('trailing_stop', pos.get('trailing_stop'))
                stop_eff = d.get('stop_actual', pos.get('stop_actual', d['sl']))
                try:
                    trail_f = float(trail)
                    trail_str = f"[bold {TN_CYAN}]${trail_f:,.2f}[/]"
                except Exception:
                    trail_str = "[dim]—[/dim]"
                try:
                    seff_f = float(stop_eff)
                    seff_str = f"[bold {NEGATIVO}]${seff_f:,.2f}[/]"
                except Exception:
                    seff_str = f"[{NEGATIVO}]${d['sl']:,.2f}[/]"
                be_tag = " [bold green]BE[/]" if (pos.get('breakeven') or d.get('breakeven')) else ""
                plan_text = (
                    f"[bold]Plan ${self.risk_per_trade:,.0f} ● ABIERTA[/bold]\n"
                    f"• Tamaño: [bold white]{units_str}[/bold white] ([dim]${total_invested:,.2f}[/dim])\n"
                    f"• SL inicial (2.5x): [{NEGATIVO}]${d['sl']:,.2f}[/] ([dim]{(risk_per_unit/d['price'])*100:+.2f}%[/dim])\n"
                    f"• Trailing Stop: {trail_str} [dim](Chandelier 3.0x, sin TP fijo)[/dim]\n"
                    f"• Stop efectivo: {seff_str}{be_tag}\n"
                    f"• Riesgo: [{NEGATIVO}]-${total_loss:,.2f}[/] [dim](salida móvil, solo sube)[/dim]"
                )
            else:
                plan_text = (
                    f"[bold]Plan ${self.risk_per_trade:,.0f} ○ SEÑAL[/bold]\n"
                    f"• Tamaño: [bold white]{units_str}[/bold white] ([dim]${total_invested:,.2f}[/dim])\n"
                    f"• SL inicial (2.5x): [{NEGATIVO}]${d['sl']:,.2f}[/] ([dim]{(risk_per_unit/d['price'])*100:+.2f}%[/dim])\n"
                    f"• Trailing: [dim]— (abre con 'o'; sin objetivo fijo)[/dim]\n"
                    f"• Riesgo: [{NEGATIVO}]-${total_loss:,.2f}[/]"
                )
        else:
            plan_text = "• Tamaño sugerido: [dim]N/A[/dim]"

        prices = d.get('prices', [])
        p_min = min(prices) if prices else d['price']
        p_max = max(prices) if prices else d['price']
        dist_color = POSITIVO if d['dist_ema8'] >= 0 else NEGATIVO
        price_text = (
            f"[bold white]{ticker}[/bold white]\n"
            f"Último Cierre: [bold white]${d['price']:,.2f}[/bold white]\n"
            f"[dim]min ${p_min:,.2f} · max ${p_max:,.2f}[/dim]"
        )
        rs_val = d.get('rs_diff', float('nan'))
        rs_str = f"{rs_val:+.1f} pp" if math.isfinite(rs_val) else "N/A"
        side_score = d.get('score', filter_score(d))
        side_color = score_color(side_score)
        filters_text = (
            f"[bold]Filtros [{side_color}]{score_bar(side_score)}[/][/bold]\n"
            f"{chk(d['c_sma'])} SMA 200 [dim]${d['sma200']:,.2f}[/dim]\n"
            f"{chk(d['c_ema'])} EMA 8 [dim]${d['ema8']:,.2f}[/dim]\n"
            f"{chk(d['c_dist'])} Distancia EMA 8 <= {STRATEGY_RULES['MAX_DIST_EMA8']}%: [bold {dist_color}]{d['dist_ema8']:+.2f}%[/]\n"
            f"{chk(d['c_rsi'])} Zona RSI ({STRATEGY_RULES['MIN_RSI']:.0f} - {STRATEGY_RULES['MAX_RSI']:.0f}): [dim]{d['rsi']:.1f}[/dim]\n"
            f"{chk(d['c_vol'])} Volumen >= {STRATEGY_RULES['MIN_VOL_RATIO']}x: [dim]{d['vol_ratio']:.2f}x[/dim]\n"
            f"{chk(d.get('c_weekly', True))} Semanal (multi-TF)\n"
            f"{chk(d.get('c_adx', True))} ADX >= {CONFIG.adx_min:.0f}: [dim]{d.get('adx', float('nan')):.1f}[/dim]\n"
            f"{chk(d.get('c_rs', True))} RS vs SPY >= {CONFIG.rs_min:+.0f} pp: [dim]{rs_str}[/dim]"
        )
        try:
            # Actualización in-place: sin remove/mount para evitar carreras
            self.query_one("#side-price", Static).update(price_text)
            spark = self.query_one("#side-spark", Sparkline)
            spark.data = list(prices) if prices else []
            self.query_one("#side-filters", Static).update(filters_text)
            self.query_one("#side-plan", Static).update(plan_text)
            self.query_one("#side-price", Static).display = True
            spark.display = True
            self.query_one("#side-filters", Static).display = True
            self.query_one("#side-plan", Static).display = True
        except Exception:
            pass

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        ticker = str(event.row_key.value)
        self.update_sidebar_for_ticker(ticker)

    def _apply_table_rows(self) -> None:
        """Aplica las filas a la tabla según el filtro activo"""
        table = self.query_one(DataTable)
        # Guarda el índice actual antes de borrar
        saved_row = table.cursor_row if table.cursor_row is not None else None
        table.clear()
        
        rows = self.cached_results
        if self.filter_signals_only:
            # Mostrar solo las entradas activas (LONG + DÉBIL: ambas tienen "ENTRADA")
            rows = [row for row in self.cached_results if "ENTRADA" in str(row[-1])]
            
        # Inserta usando el ticker como key única para que funcione correctamente con los eventos de fila
        for row in rows:
            table.add_row(*row, key=row[0])
        
        # Restaura la posición del cursor si sigue existiendo;
        # si no había ninguna (primer llenado), selecciona la primera fila
        if saved_row is not None and saved_row < table.row_count:
            table.move_cursor(row=saved_row)
        elif table.row_count > 0:
            table.move_cursor(row=0)

    @work(thread=True)
    def update_scan(self) -> None:
        # Indicador de cargando thread-safe (por si se llamó sin request_scan)
        try:
            self.call_from_thread(self._show_loading)
        except Exception:
            pass

        # Crear una lista para almacenar los resultados
        results = []

        # Inicializar el diccionario de datos técnicos
        self.ticker_data = {}

        # Análisis del benchmark SPY (se reutiliza abajo para el filtro RS,
        # sin descargas adicionales por ticker)
        market_text = "[dim]SPY: N/D[/dim]"
        spy_regime = "UNKNOWN"
        spy_close = None
        try:
            spy_df = yf.Ticker("SPY").history(period="1y")
            if not spy_df.empty and len(spy_df) >= 200:
                spy_close = spy_df['Close']
                s_price = float(spy_close.iloc[-1])
                s_sma50 = float(spy_close.rolling(window=50).mean().iloc[-1])
                s_sma200 = float(spy_close.rolling(window=200).mean().iloc[-1])
                spy_regime = spy_regime_of(s_price, s_sma50, s_sma200)

                if spy_regime == "BULLISH":
                    market_text = f"[bold {POSITIVO}]● SPY Alcista[/bold {POSITIVO}] (${s_price:,.1f})"
                elif spy_regime == "PULLBACK":
                    market_text = f"[bold {TN_YELLOW}]● SPY Pullback[/bold {TN_YELLOW}] (${s_price:,.1f})"
                else:
                    market_text = f"[bold {NEGATIVO}]● SPY Bajista[/bold {NEGATIVO}] (${s_price:,.1f})"
        except Exception:
            market_text = f"[dim {TN_YELLOW}]SPY: Error red[/]"
            spy_regime = "UNKNOWN"

        # Journal persistente de señales (punto 2)
        try:
            journal_con = journal.connect(CONFIG.journal_path)
        except Exception:
            journal_con = None
        new_signals: list[dict] = []

        for ticker in self.tickers:
            try:
                # Descargar datos históricos usando Ticker en lugar de download
                # Esto evita problemas con MultiIndex y garante estructura limpia.
                # 5y en vez de 1y: el filtro semanal necesita ~4 años de histórico.
                ticker_obj = yf.Ticker(ticker)
                data = ticker_obj.history(period='5y', interval='1d')
                if isinstance(data.columns, pd.MultiIndex):
                    data.columns = [c[0] for c in data.columns]

                if len(data) >= 200:
                    # Indicadores vía strategy.py (misma matemática, testeable)
                    ind = compute_indicators(data, CONFIG.atr_sl_mult, CONFIG.atr_tp_mult)
                    last = ind.iloc[-1]
                    close_series = data['Close']

                    last_price = float(last['Close'])
                    sma200 = float(last['SMA200'])
                    ema8 = float(last['EMA8'])
                    rsi = float(last['RSI'])
                    atr = float(last['ATR'])
                    adx = float(last['ADX'])
                    vol_ratio = float(last['vol_ratio'])
                    if not math.isfinite(vol_ratio):
                        vol_ratio = 0.0
                    if not math.isfinite(rsi):
                        rsi = float('nan')
                    stop_loss = float(last['SL'])
                    take_profit = float(last['TP'])
                    dist_ema8 = float(last['dist_ema8'])

                    # Historial para Sparkline nativo (60 puntos modal, 20 sidebar) + fallback texto
                    clean_close = close_series.dropna()
                    ultimos_60 = [float(p) for p in clean_close.iloc[-60:].tolist()]
                    ultimos_20 = ultimos_60[-20:]
                    sparkline_str = generar_sparkline(ultimos_20)

                    # Señal unificada: setup + filtro SPY integrado + semanal
                    c_sma = last_price > sma200
                    c_ema = last_price > ema8
                    c_dist = dist_ema8 <= STRATEGY_RULES["MAX_DIST_EMA8"]
                    c_rsi = STRATEGY_RULES["MIN_RSI"] <= rsi <= STRATEGY_RULES["MAX_RSI"]
                    c_vol = vol_ratio >= STRATEGY_RULES["MIN_VOL_RATIO"]
                    c_weekly = weekly_trend_ok(data)
                    # ADX con guard NaN: sin valor calculable no supera el filtro
                    c_adx = bool(math.isfinite(adx) and adx >= CONFIG.adx_min)
                    # RS vs SPY con el histórico ya descargado (sin llamadas extra);
                    # NaN (p. ej. sin SPY) no supera el filtro, igual que ADX
                    rs_diff = relative_strength(close_series, spy_close)
                    c_rs = bool(math.isfinite(rs_diff) and rs_diff >= CONFIG.rs_min)
                    sig_key = decide_signal(
                        c_sma, c_ema, c_dist, c_rsi, c_vol, c_weekly, c_adx, c_rs,
                        spy_regime,
                        STRATEGY_RULES["REQUIRE_SPY_BULLISH"],
                        CONFIG.require_weekly_trend,
                    )
                    signal = SIGNAL_STRINGS[sig_key]
                    # Score 0-8 con la fórmula única (los 8 filtros de la
                    # ficha; el régimen SPY no cuenta). Se reutiliza para
                    # la columna Score y para el ordenamiento.
                    score = filter_score({
                        'c_sma': c_sma, 'c_ema': c_ema,
                        'c_dist': c_dist, 'c_rsi': c_rsi,
                        'c_vol': c_vol, 'c_weekly': c_weekly,
                        'c_adx': c_adx, 'c_rs': c_rs,
                    })

                    # Almacena los datos técnicos completos
                    self.ticker_data[ticker] = {
                        'price': last_price,
                        'sma200': sma200,
                        'ema8': ema8,
                        'dist_ema8': dist_ema8,
                        'rsi': rsi,
                        'vol_ratio': vol_ratio,
                        'atr': atr,
                        'sl': stop_loss,
                        'tp': take_profit,
                        'c_sma': c_sma,
                        'c_ema': c_ema,
                        'c_dist': c_dist,
                        'c_rsi': c_rsi,
                        'c_vol': c_vol,
                        'c_weekly': c_weekly,
                        'adx': adx,
                        'c_adx': c_adx,
                        'rs_diff': rs_diff,
                        'c_rs': c_rs,
                        'score': score,
                        'signal': signal,
                        'sparkline': sparkline_str,
                        'prices': ultimos_60,
                    }

                    # Journal: resolver pendientes con el histórico fresco
                    if journal_con is not None:
                        try:
                            journal.resolve_pending_for_ticker(
                                journal_con, ticker, data, CONFIG.max_hold_days
                            )
                        except Exception:
                            pass

                    # Registrar la señal de hoy (solo alerta si es nueva)
                    if journal_con is not None and sig_key in ("LONG", "WEAK"):
                        try:
                            bar_date = data.index[-1].date().isoformat()
                            is_new = journal.log_signal(
                                journal_con, ticker, bar_date, last_price,
                                stop_loss, take_profit, spy_regime, sig_key,
                            )
                        except Exception:
                            is_new = False
                        if is_new:
                            risk_unit = last_price - stop_loss
                            if risk_unit > 0:
                                raw_u = self.risk_per_trade / risk_unit
                                if "USD" in ticker:
                                    units_str = f"{raw_u:.4f}"
                                else:
                                    iu = int(raw_u)
                                    units_str = f"{iu} acc" if iu >= 1 else "0 acc (Riesgo insuficiente)"
                            else:
                                units_str = "N/A"
                            new_signals.append({
                                "kind": sig_key, "ticker": ticker,
                                "price": last_price, "sl": stop_loss,
                                "tp": take_profit, "units": units_str,
                                "risk": self.risk_per_trade,
                                "rr_label": RR_LABEL, "spy_regime": spy_regime,
                            })

                    # Dist EMA8 coloreada condicionalmente (verde + / rojo -)
                    dist_color = POSITIVO if dist_ema8 >= 0 else NEGATIVO
                    dist_str = f"[bold {dist_color}]{dist_ema8:+.2f}%[/]"

                    # Columna SL / Salida: con posición abierta muestra el
                    # trailing (Chandelier, sin TP fijo); sin ella, SL 2.5x.
                    try:
                        _pos = getattr(self, 'positions', {}).get(ticker)
                    except Exception:
                        _pos = None
                    if _pos is not None:
                        try:
                            _max = max(float(_pos.get('maximo_desde_entrada', last_price)), last_price)
                            _trail = _max - CONFIG.atr_trail_mult * float(atr)
                            _seff = _pos.get('stop_actual', stop_loss)
                            sl_cell = f"${float(_seff):.2f} / T${_trail:.2f}"
                        except Exception:
                            sl_cell = f"${stop_loss:.2f} / T—"
                    else:
                        sl_cell = f"${stop_loss:.2f} / —"
                    # Agregar a resultados (numéricas alineadas a la derecha)
                    results.append((
                        ticker,
                        num_cell(f"${last_price:,.2f}"),
                        Text.from_markup(dist_str, justify="right"),
                        num_cell(f"{rsi:.1f}"),
                        num_cell(f"{vol_ratio:.2f}x"),
                        num_cell(sl_cell),
                        score_cell(score),
                        signal
                    ))
                else:
                    # Si no hay suficientes datos, mostrar valores por defecto
                    results.append((ticker, "N/A", "N/A", "N/A", "N/A", "N/A", "N/A", "⚠️ DATOS INSUFICIENTES"))

            except Exception as e:
                # En caso de error, mostrar mensaje de error
                results.append((ticker, "ERROR", "ERROR", "ERROR", "ERROR", "N/A", "N/A", f"⚠ ERROR: {str(e)[:20]}"))

        # Alertas (punto 4): solo las señales nuevas del journal, best-effort,
        # desde el hilo worker (nunca en el hilo UI)
        if new_signals:
            try:
                alerts.notify_signals(new_signals, CONFIG)
            except Exception:
                pass
        if journal_con is not None:
            try:
                journal_con.close()
            except Exception:
                pass

        # Ordenar los resultados por prioridad: (1) señal ENTRADA,
        # (2) score numérico de ticker_data (nunca parseando la barra),
        # (3) desempate por ratio de volumen.
        def sort_priority(row):
            # row: (ticker, price, dist_ema8, rsi, vol_str, sl_tp, score, signal_str)
            import re
            has_signal = 1 if "ENTRADA" in str(row[-1]) else 0
            try:
                score_val = int(self.ticker_data.get(row[0], {}).get('score', 0))
            except (ValueError, IndexError, AttributeError):
                score_val = 0
            try:
                # Extrae el primer número aunque la celda lleve markup Rich
                m = re.search(r"-?\d+(?:\.\d+)?", str(row[4]))
                vol_val = float(m.group(0)) if m else 0.0
            except (ValueError, IndexError):
                vol_val = 0.0
            return (has_signal, score_val, vol_val)

        results.sort(key=sort_priority, reverse=True)

        self.call_from_thread(self._finish_scan, results, self.ticker_data, market_text)

    def _finish_scan(self, results, ticker_data, market_text) -> None:
        self.cached_results = results
        self.ticker_data = ticker_data

        # Paso 1+2: actualizar máximo y recalcular trailing stop
        # (Chandelier: max_desde_entrada - trail_mult * ATR fresco).
        # El trailing solo sube porque el máximo nunca baja; el stop
        # efectivo (con breakeven del paso 3) se guarda en la posición.
        # Best-effort, igual que el resto de la app.
        try:
            for tk, dd in (ticker_data or {}).items():
                if tk in getattr(self, "positions", {}):
                    try:
                        pos = positions_store.update_maximum(
                            self.positions, tk, float(dd.get("price", 0.0))
                        )
                    except Exception:
                        pos = self.positions.get(tk)
                    try:
                        atr_now = float(dd.get("atr", float("nan")))
                        max_px = float(pos.get("maximo_desde_entrada", dd.get("price", 0.0)))
                        import math as _m
                        if _m.isfinite(atr_now) and atr_now > 0:
                            dd["trailing_stop"] = chandelier_trailing(
                                max_px, atr_now, CONFIG.atr_trail_mult
                            )
                            # Paso 3: breakeven + ratchet (stop solo sube).
                            updated = positions_store.update_stop(
                                self.positions, tk, atr_now,
                                CONFIG.atr_trail_mult, CONFIG.atr_sl_mult,
                            )
                            if updated is not None:
                                dd["stop_actual"] = updated.get("stop_actual")
                                dd["breakeven"] = bool(updated.get("breakeven", False))
                                dd["stop_inicial"] = updated.get("stop_inicial")
                                # Sincronizar la celda SL / Trail de la tabla
                                stop_val = float(updated.get("stop_actual", dd.get("sl", 0.0)))
                                trail_val = dd["trailing_stop"]
                                cell_txt = f"${stop_val:.2f} / T${trail_val:.2f}"
                                for idx, r in enumerate(self.cached_results):
                                    if r[0] == tk:
                                        rl = list(r)
                                        rl[5] = num_cell(cell_txt)
                                        self.cached_results[idx] = tuple(rl)
                                        break
                    except Exception:
                        pass
        except Exception:
            pass

        # Exposición real: capital invertido en positions.json (origen
        # único). Criterio: invertido al ABRIR, fijo; el precio de mercado
        # no lo altera (el refresco solo re-renderiza + backfill legacy).
        try:
            for tk in list(getattr(self, "positions", {})):
                positions_store.ensure_sizing(
                    self.positions, tk, self.risk_per_trade
                )
        except Exception:
            pass
        exposure = self._positions_exposure()
        breached = bool(exposure > CONFIG.exposure_cap)

        # Actualizar el widget count-card si existe
        self._update_exposure_card()

        # Actualizar la tabla con los resultados
        self._apply_table_rows()

        # Actualizar el widget market-card si existe
        if self.query("#market-card"):
            self.query_one("#market-card", Static).update(market_text)

        # Estado: Normal, o aviso si se supera la exposición máxima
        if self.query("#status-card"):
            if breached:
                self.query_one("#status-card", Static).update(
                    f"[bold {TN_YELLOW}]⚠ Exposición ${exposure:,.0f} > tope ${CONFIG.exposure_cap:,.0f}[/]"
                )
            else:
                self.query_one("#status-card", Static).update(f"[bold {POSITIVO}]● Estado: Normal[/]")

        self._refresh_active_sidebar()

    def load_tickers(self) -> list:
        if TICKERS_FILE.exists():
            try:
                with open(TICKERS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list) and data:
                        return data
            except Exception:
                pass
        self.save_tickers(DEFAULT_TICKERS)
        return list(DEFAULT_TICKERS)

    def save_tickers(self, tickers: list = None) -> None:
        try:
            target = tickers if tickers is not None else self.tickers
            with open(TICKERS_FILE, "w", encoding="utf-8") as f:
                json.dump(target, f, indent=2)
        except Exception:
            pass

    def action_delete_ticker(self) -> None:
        table = self.query_one(DataTable)
        if table.cursor_row is None or table.row_count == 0:
            return

        try:
            # Obtener el ticker de la fila actual
            row = table.get_row_at(table.cursor_row)
            ticker = str(row[0])
        except Exception:
            return

        # 1. Eliminar de la lista de tickers y persistir en JSON
        if ticker in self.tickers:
            self.tickers.remove(ticker)
            self.save_tickers()

        # 2. Limpiar de las cachés en memoria
        self.cached_results = [r for r in self.cached_results if r[0] != ticker]
        if hasattr(self, 'ticker_data') and ticker in self.ticker_data:
            del self.ticker_data[ticker]

        # 3. Refrescar la tabla en pantalla de forma inmediata
        self._apply_table_rows()

        # 4. Actualizar contadores y tarjetas de estado
        self._update_exposure_card()

        self.query_one("#status-card", Static).update(f"[bold {NEGATIVO}]Eliminado:[/] {ticker}")

        # 5. Si la tabla quedó vacía, limpiar el sidebar; si no, refrescar la fila activa
        if table.row_count == 0:
            try:
                self.query_one("#side-price", Static).update("[dim]Lista vacía. Añade tickers para comenzar...[/dim]")
                self.query_one("#side-spark", Sparkline).display = False
                self.query_one("#side-filters", Static).display = False
                self.query_one("#side-plan", Static).display = False
            except Exception:
                pass
        else:
            self._refresh_active_sidebar()

    def action_refresh_data(self) -> None:
        self.time_left = self.refresh_interval
        self.request_scan()

    def action_toggle_filter(self) -> None:
        self.filter_signals_only = not self.filter_signals_only
        self._apply_table_rows()
        state = "ON" if self.filter_signals_only else "OFF"
        self.query_one("#status-card", Static).update(f"[{TN_CYAN}]Filtro señales:[/] {state}")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        nuevo_ticker = event.value.strip().upper()
        if nuevo_ticker:
            if nuevo_ticker not in self.tickers:
                self.tickers.append(nuevo_ticker)
                self.save_tickers()
                event.input.value = ""
                self.query_one("#status-card", Static).update(f"[bold {TN_YELLOW}]Añadido:[/] {nuevo_ticker}")
                # Actualizar el widget count-card si existe
                self._update_exposure_card()
                self.request_scan()
            else:
                event.input.value = ""
                self.query_one("#status-card", Static).update(f"[dim]Ya en lista: {nuevo_ticker}[/dim]")

    def _highlighted_ticker(self) -> str | None:
        """Ticker de la fila bajo el cursor, o None (mismo manejo de errores)."""
        try:
            table = self.query_one(DataTable)
            if table.cursor_row is None or table.row_count == 0:
                return None
            return str(table.get_row_at(table.cursor_row)[0])
        except Exception:
            return None

    def action_open_position(self) -> None:
        """Abrir posición manual (tecla 'o') al precio actual + SL 2.5x.

        Guarda unidades/invertido con el sizing único y bloquea si el
        total superaría el tope (aviso en status-card, sin abrir).
        """
        ticker = self._highlighted_ticker()
        if ticker is None:
            return
        if not hasattr(self, 'ticker_data') or ticker not in self.ticker_data:
            return
        try:
            price = float(self.ticker_data[ticker]['price'])
            sl_now = float(self.ticker_data[ticker]['sl'])
            atr_now = float(self.ticker_data[ticker].get('atr', float('nan')))
        except Exception:
            return
        try:
            import math as _m
            stop_init = None
            if _m.isfinite(atr_now) and atr_now > 0:
                stop_init = price - CONFIG.atr_sl_mult * atr_now
            if stop_init is None:
                stop_init = sl_now
            units, invested = positions_store.sizing_units(
                ticker, price, sl_now, self.risk_per_trade
            )
            if units <= 0:
                msg = f"Sizing insuficiente: riesgo de ${self.risk_per_trade:,.0f} insuficiente para comprar {ticker}."
                self.notify(msg, title="Sizing insuficiente", severity="warning", timeout=4)
                if self.query("#status-card"):
                    self.query_one("#status-card", Static).update(f"[bold {TN_YELLOW}]Sizing insuficiente:[/] {ticker} (0 acc)")
                return
            # Re-apertura del mismo ticker: su exposición previa se
            # sustituye, no se suma (se descuenta antes de comparar).
            try:
                old_exp = positions_store.position_exposure(
                    self.positions.get(ticker)
                )
            except Exception:
                old_exp = 0.0
            prospective = self._positions_exposure() - old_exp + invested
            if prospective > CONFIG.exposure_cap:
                msg = (
                    f"[bold {TN_YELLOW}]Límite de exposición alcanzado:[/] "
                    f"${prospective:,.0f} > tope ${CONFIG.exposure_cap:,.0f} "
                    f"({ticker} ${invested:,.0f})"
                )
                self.notify(
                    f"Límite de exposición alcanzado: abrir {ticker} "
                    f"(${invested:,.0f}) superaría el tope "
                    f"de ${CONFIG.exposure_cap:,.0f}.",
                    title="Exposición", severity="warning", timeout=5,
                )
                if self.query("#status-card"):
                    self.query_one("#status-card", Static).update(msg)
                return
            pos = positions_store.open_position(
                self.positions, ticker, price, stop_inicial=stop_init,
                unidades=units, invertido=invested,
            )
            # Actualizar celda SL / Trail de la tabla en vivo
            stop_eff = pos.get('stop_actual', stop_init)
            if _m.isfinite(atr_now) and atr_now > 0:
                _trail = price - CONFIG.atr_trail_mult * atr_now
                new_sl_cell = f"${float(stop_eff):.2f} / T${_trail:.2f}"
            else:
                new_sl_cell = f"${float(stop_eff):.2f} / T—"
            for idx, r in enumerate(self.cached_results):
                if r[0] == ticker:
                    rl = list(r)
                    rl[5] = num_cell(new_sl_cell)
                    self.cached_results[idx] = tuple(rl)
                    break
            self._apply_table_rows()
            self._update_exposure_card()
            self._refresh_active_sidebar()
            self.notify(
                f"{ticker} abierta @ ${pos['precio_entrada']:,.2f} "
                f"({pos['fecha_entrada']}) · Exp ${self._positions_exposure():,.0f}",
                title="Posición abierta", severity="information", timeout=4,
            )
            if self.query("#status-card"):
                self.query_one("#status-card", Static).update(
                    f"[bold {POSITIVO}]● Posición:[/] {ticker} @ ${pos['precio_entrada']:,.2f}"
                )
        except Exception:
            pass

    def action_close_position(self) -> None:
        """Cerrar posición manual (tecla 'x'): registra en journal y borra."""
        ticker = self._highlighted_ticker()
        if ticker is None:
            return
        try:
            pos = self.positions.get(ticker)
            result_str = ""
            if pos is not None:
                # Precio de cierre = último disponible; fallback a la entrada.
                try:
                    exit_px = float(self.ticker_data[ticker]["price"])
                except Exception:
                    exit_px = float(pos.get("precio_entrada", 0.0))
                try:
                    rec = trades_journal.log_close(ticker, pos, exit_px)
                    result_str = (
                        f" ({rec['resultado_r']:+.2f}R, "
                        f"${rec['resultado_usd']:+,.2f})"
                    )
                except Exception:
                    pass  # el cierre sigue aunque falle el journal
            existed = positions_store.close_position(self.positions, ticker)
            msg = f"Cerrada: {ticker}" if existed else f"Sin posición: {ticker}"
            if existed:
                # Actualizar celda SL / Trail de la tabla a estado sin posición
                sl_base = self.ticker_data.get(ticker, {}).get('sl', 0.0)
                new_sl_cell = f"${float(sl_base):.2f} / —" if sl_base else "N/A"
                for idx, r in enumerate(self.cached_results):
                    if r[0] == ticker:
                        rl = list(r)
                        rl[5] = num_cell(new_sl_cell)
                        self.cached_results[idx] = tuple(rl)
                        break
                self._apply_table_rows()
            self._update_exposure_card()
            self._refresh_active_sidebar()
            self.notify(msg + result_str, title="Posiciones", severity="information", timeout=3)
            if self.query("#status-card"):
                self.query_one("#status-card", Static).update(f"[bold {TN_YELLOW}]{msg}[/]")
        except Exception:
            pass

    def action_show_journal(self) -> None:
        """Vista de estadísticas agregadas del journal (tecla 'j')."""
        try:
            trades = trades_journal.load_journal()
        except Exception:
            trades = []
        try:
            self.push_screen(JournalModal(trades))
        except Exception:
            pass

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        ticker = str(event.row_key.value)
        if hasattr(self, 'ticker_data') and ticker in self.ticker_data:
            try:
                _pos = getattr(self, 'positions', {}).get(ticker)
            except Exception:
                _pos = None
            self.push_screen(InspectionModal(
                ticker, self.ticker_data[ticker], self.risk_per_trade,
                position=_pos,
            ))

    def action_copy_order(self) -> None:
        table = self.query_one(DataTable)
        if table.cursor_row is None or table.row_count == 0:
            return

        try:
            row = table.get_row_at(table.cursor_row)
            ticker = str(row[0])
        except Exception:
            return

        if not hasattr(self, 'ticker_data') or ticker not in self.ticker_data:
            self.notify(f"No hay datos técnicos disponibles para {ticker}.", title="Sin datos", severity="warning", timeout=3)
            return

        d = self.ticker_data[ticker]
        risk_unit = d['price'] - d['sl']

        if risk_unit > 0:
            units_val, _invested = positions_store.sizing_units(
                ticker, d['price'], d['sl'], self.risk_per_trade
            )
            if "USD" in ticker:
                units_str = f"{units_val:.4f}"
            else:
                units_str = f"{int(units_val)} acc" if units_val >= 1 else "0 acc (Riesgo insuficiente)"
        else:
            units_str = "N/A"

        try:
            _pos = getattr(self, 'positions', {}).get(ticker)
        except Exception:
            _pos = None
        if _pos is not None:
            _trail = d.get('trailing_stop', _pos.get('trailing_stop'))
            _seff = d.get('stop_actual', _pos.get('stop_actual', d['sl']))
            try:
                if _trail is not None and math.isfinite(float(_trail)):
                    exit_str = f"TRAIL: ${float(_trail):,.2f} | STOP_EF: ${float(_seff):,.2f}"
                else:
                    exit_str = f"STOP_EF: ${float(_seff):,.2f}"
            except Exception:
                exit_str = f"STOP_EF: ${d['sl']:,.2f}"
            order_text = (
                f"{ticker} | ENTRADA: ${float(_pos.get('precio_entrada', d['price'])):,.2f} | "
                f"{exit_str} | VOL: {units_str} | RIESGO: ${self.risk_per_trade:,.0f} | SIN TP FIJO"
            )
        else:
            order_text = (
                f"{ticker} | ENTRADA: ${d['price']:,.2f} | SL: ${d['sl']:,.2f} | "
                f"SIN TP FIJO (trailing tras abrir con 'o') | VOL: {units_str} | "
                f"RIESGO: ${self.risk_per_trade:,.0f}"
            )

        # Copiar nativamente al portapapeles del sistema
        self.copy_to_clipboard(order_text)

        # Feedback visual flotante y en tarjeta de estado
        self.notify(f"Orden copiada:\n{order_text}", title="Portapapeles", severity="information", timeout=4)
        if self.query("#status-card"):
            self.query_one("#status-card", Static).update(f"[bold {POSITIVO}]● Copiado:[/] {ticker}")

if __name__ == "__main__":
    app = FinanceApp()
    app.run()