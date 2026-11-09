# Mr Fin signal exporter

## Console

Start the interactive console in a terminal:

```powershell
.\run_btmm.bat --control
```

In the console, select up to three distinct instruments and start the exporter:

```text
start EURUSD,GBPUSD,USDJPY
status
stop
quit
```

Use `symbols` to list built-in names. `start` processes the selected instruments
in one exporter and writes their signals together. `stop` waits for the current
refresh to finish and clears the pending signal file; `quit` also stops the
exporter. The terminal must remain open. To change pairs, stop first, then start
again. By default, the exporter uses Yahoo data, a 60-minute timeframe, and
refreshes every five minutes.

Options are passed when starting the console, for example:

```powershell
.\run_btmm.bat --control --timeframe 15m,60m --watch 5 --mt-suffix .m
```

Use `--mt-map US30=DJ30.cash,XAUUSD=GOLD` for individual broker names. For MT5
broker data, use `--source mt5` with a running, logged-in MT5 terminal; the
strategy uses closed bars, not live ticks. For MT4, Yahoo or an exported CSV is
the data source; the control console supports Yahoo and MT5, not CSV.

The exporter and the MT4/MT5 bridge EA are separate. Attach the corresponding
EA and enable trading in the terminal if execution is intended. **Use a demo
account first.** Stopping the exporter does not disable the EA or close open
positions, and an EA may already be processing a signal when stop is requested.
To prevent further trading, also disable AutoTrading/Algo Trading or remove the
EA. Existing exporters started before the locking feature was added must be
stopped separately before using the console; otherwise they can overwrite the
same signal file.

## Settings page

```powershell
.\run_btmm.bat --ui --source mt5
```

Opens `http://localhost:8765/` (local only). On the page you can:

1. Pick up to 3 instruments (built-in list, or any broker symbol; "Load broker symbols" lists them from a running MT5).
2. Pick up to 3 timeframes.
3. Override the pip size (blank = per-instrument default).
4. Turn SL and TP on/off independently or together, and set them by strategy, fixed pips, or a ratio (1:1, 1:2, 1:3 or custom; TP = SL x ratio).
5. Set a schedule: 1-3 days from pressing Start, and optional trading hours (with a UTC offset) in which signals are exported.
6. Set amount limits: initial amount, ATT (amount to trade), ATL (amount to lose), ATP (amount to profit), plus a formula switch.

Formula: the running amount is `ATT + session profit/loss`. When it is at or below ATL, or at or above ATP, the exporter stops, clears pending signals and closes the EA's open positions. With the formula off, nothing is stopped by amount. The check runs once per refresh (default 5 minutes) and needs `--source mt5` to read profit/loss; with other sources it is not enforced. Closing positions uses the EA's magic number (`26110601`), so keep it unchanged in the EA. Settings are saved in `btmm_plan.json`.

The EAs treat an SL or TP of 0 in the signal file as "off"; re-compile them after updating.
