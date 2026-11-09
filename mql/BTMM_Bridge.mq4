//+------------------------------------------------------------------+
//| BTMM_Bridge (MT4): executes signals written by btmm.py            |
//| Python: --export-signals common   File: Common\Files\btmm_signals |
//+------------------------------------------------------------------+
#property strict

extern string InpFile        = "btmm_signals.csv";
extern int    InpMagic       = 26110601;
extern int    InpSlippage    = 20;
extern int    InpPollSec     = 5;
extern int    InpMaxOpen     = 3;
extern double InpLotOverride = 0.0;   // 0 = use lots from file
extern string InpTimeframes  = "";    // e.g. "15m,60m"; empty = execute all timeframes
extern bool   InpTrade       = true;  // false = log only

bool TimeframeAllowed(string id)
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
   EventSetTimer(InpPollSec);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason) { EventKillTimer(); }
void OnTimer() { Process(); }

int CountOpen()
  {
   int n = 0;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
	  if(OrderSelect(i, SELECT_BY_POS, MODE_TRADES) && OrderMagicNumber() == InpMagic)
		 n++;
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
   if(MarketInfo(sym, MODE_DIGITS) == 0)
	 {
	  Print("BTMM: unknown symbol ", sym, " (set --mt-symbol to the broker name)");
	  return;
	 }
   if(CountOpen() >= InpMaxOpen)
	  return;

   int digits = (int)MarketInfo(sym, MODE_DIGITS);
   double step = MarketInfo(sym, MODE_LOTSTEP);
   double minv = MarketInfo(sym, MODE_MINLOT);
   double maxv = MarketInfo(sym, MODE_MAXLOT);
   if(InpLotOverride > 0) lots = InpLotOverride;
   lots = MathMin(maxv, MathMax(minv, MathFloor(lots / step) * step));
   sl = NormalizeDouble(sl, digits);
   tp = NormalizeDouble(tp, digits);

   bool isLong = (dir == "long");
   double ask = MarketInfo(sym, MODE_ASK), bid = MarketInfo(sym, MODE_BID);
   if(isLong ? !(sl < bid && tp > ask) : !(sl > ask && tp < bid))
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
   int ticket = OrderSend(sym, isLong ? OP_BUY : OP_SELL, lots, isLong ? ask : bid, InpSlippage, sl, tp, comment, InpMagic, 0, clrNONE);
   if(ticket > 0)
	  GlobalVariableSet(gv, 1);
   else
	  Print("BTMM: OrderSend failed, error ", GetLastError());
  }
