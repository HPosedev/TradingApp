from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Header, Footer, Static, DataTable
from textual import work
import random
from datetime import datetime
import yfinance as yf
import pandas as pd

class MetricCard(Static):
    """Tarjeta simple de resumen financiero."""
    pass

class FinanceApp(App):
    CSS = """
    Screen { background: #121212; }
    #sidebar { width: 30%; border-right: vkey #333333; padding: 1; }
    #main { width: 70%; padding: 1; }
    #metrics { height: 7; margin-bottom: 1; }
    MetricCard { background: #1e1e1e; border: round #444444; padding: 1; margin: 0 1; }
    DataTable { height: 1fr; border: round #444444; }
    """

    BINDINGS = [
        ("q", "quit", "Salir"),
        ("r", "refresh_data", "Refrescar"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Static("[bold cyan]Watchlist[/bold cyan]\n")
                yield Static(id="watchlist-content")
            with Vertical(id="main"):
                with Horizontal(id="metrics"):
                    yield MetricCard("[bold]Balance Total[/bold]\n$45,230.50", classes="metric")
                    yield MetricCard("[bold green]P&L Diario[/bold green]\n+$1,120.00 (+2.5%)", classes="metric")
                    yield MetricCard("[bold green]P&L Total[/bold green]\n+$8,450.00 (+23.0%)", classes="metric")
                yield DataTable(id="orders-table")
        yield Footer()

    def on_mount(self) -> None:
        # Configurar tabla
        table = self.query_one(DataTable)
        table.add_columns("Ticker", "Precio", "Dist EMA8", "RSI 14", "Vol / Vol20", "SL / TP", "Señal")
        self.update_scan()
        self.set_interval(10.0, self.update_scan)

    def action_refresh_data(self) -> None:
        """Handler para la tecla de refresco."""
        self.update_scan()

    @work(thread=True)
    def update_scan(self) -> None:
        # Lista de tickers a vigilar
        tickers = ['AAPL', 'MSFT', 'NVDA', 'AMZN', 'GOOGL', 'TSLA', 'BTC-USD']
        
        # Crear una lista para almacenar los resultados
        results = []
        
        for ticker in tickers:
            try:
                # Descargar datos históricos usando Ticker en lugar de download
                # Esto evita problemas con MultiIndex y garante estructura limpia
                ticker_obj = yf.Ticker(ticker)
                data = ticker_obj.history(period='1y', interval='1d')
                
                if len(data) >= 200:
                    # Calcular indicadores técnicos
                    data['SMA200'] = data['Close'].rolling(window=200).mean()
                    data['EMA8'] = data['Close'].ewm(span=8, adjust=False).mean()
                    data['Vol_SMA20'] = data['Volume'].rolling(window=20).mean()
                    
                    # Calcular RSI de 14 periodos manualmente
                    close_series = data['Close']
                    delta = close_series.diff()
                    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
                    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
                    rs = gain / loss
                    rsi = float((100 - (100 / (1 + rs))).iloc[-1])
                    
                    # Calcular True Range y ATR de 14 periodos
                    high_low = data['High'] - data['Low']
                    high_close = (data['High'] - data['Close'].shift(1)).abs()
                    low_close = (data['Low'] - data['Close'].shift(1)).abs()
                    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
                    atr = float(tr.rolling(window=14).mean().iloc[-1])
                    
                    # Obtener los últimos valores como escalares
                    last_price = float(data['Close'].iloc[-1])
                    sma200 = float(data['SMA200'].iloc[-1])
                    ema8 = float(data['EMA8'].iloc[-1])
                    volume = float(data['Volume'].iloc[-1])
                    vol_sma20 = float(data['Vol_SMA20'].iloc[-1])
                    
                    # Calcular la distancia porcentual a la EMA 8
                    dist_ema8 = float(((last_price - ema8) / ema8) * 100)
                    
                    # Calcular el ratio de volumen
                    vol_ratio = volume / vol_sma20 if vol_sma20 > 0 else 0
                    
                    # Calcular niveles de Stop Loss y Take Profit
                    stop_loss = last_price - (1.5 * atr)
                    take_profit = last_price + (3.0 * atr)
                    
                    # Determinar la señal
                    if (last_price > sma200) and (last_price > ema8) and (dist_ema8 <= 2.0) and (volume > vol_sma20) and (50 <= rsi <= 65):
                        signal = "[bold green]🟢 ENTRADA LARGO[/bold green]"
                    else:
                        signal = "[dim white]⚪ ESPERAR[/dim white]"
                    
                    # Agregar a resultados
                    results.append((
                        ticker,
                        f"${last_price:,.2f}",
                        f"{dist_ema8:+.2f}%",
                        f"{rsi:.1f}",
                        f"{vol_ratio:.2f}x",
                        f"${stop_loss:.2f} / ${take_profit:.2f}",
                        signal
                    ))
                else:
                    # Si no hay suficientes datos, mostrar valores por defecto
                    results.append((ticker, "N/A", "N/A", "N/A", "N/A", "N/A", "⚠️ DATOS INSUFICIENTES"))
                    
            except Exception as e:
                # En caso de error, mostrar mensaje de error
                results.append((ticker, "ERROR", "ERROR", "ERROR", "ERROR", f"⚠️ ERROR: {str(e)}"))
        
        # Actualizar la tabla con los resultados
        table = self.query_one(DataTable)
        table.clear()
        table.add_rows(results)
        
        # Rellenar el contenido de watchlist
        watchlist_content = self.query_one("#watchlist-content")
        watchlist_content.update("[bold cyan]Watchlist[/bold cyan]\n" + "\n".join([f"{ticker}: {result[-1]}" for ticker, *result in results]))

if __name__ == "__main__":
    FinanceApp().run()