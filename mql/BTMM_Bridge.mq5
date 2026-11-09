//+------------------------------------------------------------------+
//| BTMM_Bridge (MT5): executes signals written by btmm.py            |
//| Python: --export-signals common   File: Common\Files\btmm_signals |
//+------------------------------------------------------------------+
#property strict
#include <Trade\Trade.mqh>

input string InpFile        = "btmm_signals.csv";
input long   InpMagic       = 26110601;
input int    InpSlippage    = 20;
input int    InpPollSec     = 5;
input int    InpMaxOpen     = 3;
input double InpLotOverride = 0.0;   // 0 = use lots from file
input string InpTimeframes  = "";    // e.g. "15m,60m"; empty = execute all timeframes
input bool   InpTrade       = true;  // false = log only

CTrade trade;

bool TimeframeAllowed(const string id)
  {
   if(InpTimeframes == "")
	  return true;
   string tfs[];
   int n = StringSplit(InpTimeframes, ',', tfs);
   for(int i = 0; i < n; i++)
	 {
	  string tf = tfs[i];
	  StringTrimLeft(tf);
	  StringTrimRight(tf);
	  if(tf != "" && StringFind(id, "-" + tf + "-") >= 0)
		 return true;
	 }
   return false;
  }

int OnInit()
  {
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints(InpSlippage);
   EventSetTimer(InpPollSec);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason) { EventKillTimer(); }
void OnTimer() { Process(); }

int CountOpen()
  {
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
	 {
	  ulong t = PositionGetTicket(i);
	  if(t > 0 && PositionGetInteger(POSITION_MAGIC) == InpMagic)
		 n++;
	 }
   return n;
  }

void Process()
  {
   int h = FileOpen(InpFile, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON | FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(h == INVALID_HANDLE)
	  return;
   bool header = true;
   while(!FileIsEnding(h))
	 {
	  string line = FileReadString(h);
	  if(header) { header = false; continue; }
	  StringTrimLeft(line);
	  StringTrimRight(line);
	  string p[];
	  if(line == "" || StringSplit(line, ',', p) < 9)
		 continue;
	  Execute(p);
	 }
   FileClose(h);
  }

void Execute(string &p[])
  {
   string id = p[0], sym = p[1], dir = p[2];
   double sl = StringToDouble(p[4]), tp = StringToDouble(p[5]), lots = StringToDouble(p[6]);
   datetime expires = (datetime)StringToInteger(p[7]);
   string gv = "BTMM_" + id;

   if(GlobalVariableCheck(gv) || TimeGMT() > expires || !TimeframeAllowed(id))
	  return;
   if(!SymbolSelect(sym, true))
	 {
	  Print("BTMM: unknown symbol ", sym, " (set --mt-symbol to the broker name)");
	  return;
	 }
   if(CountOpen() >= InpMaxOpen)
	  return;

   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double step = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   double minv = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double maxv = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   if(InpLotOverride > 0) lots = InpLotOverride;
   lots = MathMin(maxv, MathMax(minv, MathFloor(lots / step) * step));
   sl = NormalizeDouble(sl, digits);
   tp = NormalizeDouble(tp, digits);

   bool isLong = (dir == "long");
   double ask = SymbolInfoDouble(sym, SYMBOL_ASK), bid = SymbolInfoDouble(sym, SYMBOL_BID);
   bool slOk = (sl == 0) || (isLong ? sl < bid : sl > ask);   // 0 = SL off
   bool tpOk = (tp == 0) || (isLong ? tp > ask : tp < bid);   // 0 = TP off
   if(!(slOk && tpOk))
	 {
	  Print("BTMM: ", id, " skipped, price already beyond SL/TP");
	  return;
	 }
   string comment = StringSubstr("BTMM " + id, 0, 31);
   if(!InpTrade)
	 {
	  Print("BTMM (log only): ", dir, " ", sym, " ", lots, " sl=", sl, " tp=", tp);
	  GlobalVariableSet(gv, 1);
	  return;
	 }
   bool ok = isLong ? trade.Buy(lots, sym, 0, sl, tp, comment) : trade.Sell(lots, sym, 0, sl, tp, comment);
   if(ok)
	  GlobalVariableSet(gv, 1);
   else
	  Print("BTMM: order failed ", trade.ResultRetcode(), " ", trade.ResultRetcodeDescription());
  }
