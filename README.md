# Mr Fin signal exporter

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
