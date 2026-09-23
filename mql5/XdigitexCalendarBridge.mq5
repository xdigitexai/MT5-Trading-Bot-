//+------------------------------------------------------------------+
//|                                        XdigitexCalendarBridge.mq5|
//|                          Reads the native MetaTrader 5 calendar  |
//+------------------------------------------------------------------+
//  Bridges the MetaTrader 5 economic calendar (the MetaQuotes calendar the terminal already
//  downloads, i.e. no paid API) into a single JSON file shared with the Python engine through the
//  MT5 Common Files folder:  <Common>\Files\xdigitex_calendar.json
//
//  - Only the eight currencies this deployment trades are exported (USD EUR GBP JPY CHF AUD CAD
//    NZD); the terminal's calendar holds thousands of irrelevant rows and exporting them would
//    bloat the bridge for no reason.
//  - Importance comes from MQL5's own ENUM_CALENDAR_EVENT_IMPORTANCE (CALENDAR_IMPORTANCE_HIGH /
//    _MODERATE / _LOW / _NONE). No importance level is invented here.
//  - Calendar times are *trade server* time (documented for every Calendar* function). The bridge
//    therefore exports the raw server time, the server-vs-UTC offset it measured itself
//    (TimeTradeServer() - TimeGMT(), rounded to the minute) and the UTC instant derived from the
//    two, so the Python side can check the conversion instead of trusting it.
//  - The live file is never partially overwritten: every cycle writes a temp file, flushes and
//    closes it, then replaces the target with an atomic rename (FileMove + FILE_REWRITE).
//  - This program never trades: it opens no orders, reads no prices and needs no trade permission.
#property copyright "Xdigitex"
#property version   "1.00"
#property description "Exports the native MT5 economic calendar for the eight traded currencies to Common\\Files\\xdigitex_calendar.json, atomically, with a heartbeat."

#define BRIDGE_NAME           "XdigitexCalendarBridge"
#define BRIDGE_VERSION        "1.00"
#define BRIDGE_SOURCE         "MetaTrader 5 native economic calendar (MQL5 CalendarValueHistory)"
#define FILE_FINAL            "xdigitex_calendar.json"
#define FILE_TEMP             "xdigitex_calendar.json.tmp"
// MQL5 reports "this value is not published yet / was never published" as LONG_MIN.
#define CALENDAR_VALUE_UNSET  ((long)(-9223372036854775807 - 1))

input int  InpRefreshSeconds    = 60;    // heartbeat: rebuild the file at least this often
input int  InpEventCacheSeconds = 1800;  // how long event descriptions/countries are cached
input int  InpLookbackHours     = 48;    // how far back events are exported (server time)
input int  InpHorizonDays       = 21;    // how far ahead events are exported (server time)
input bool InpVerbose           = true;  // log every successful cycle

string g_currencies[8] = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"};

MqlCalendarEvent    g_events[];
MqlCalendarCountry  g_countries[];

datetime g_last_write       = 0;
datetime g_last_event_cache = 0;
ulong    g_change_id        = 0;
long     g_cycles           = 0;
long     g_failures         = 0;
string   g_last_error       = "";

//+------------------------------------------------------------------+
//| Text helpers                                                     |
//+------------------------------------------------------------------+
string JsonEscape(const string text)
  {
   string out = "";
   int    length = StringLen(text);
   for(int i = 0; i < length; i++)
     {
      ushort code = StringGetCharacter(text, i);
      if(code == '"')
         out += "\\\"";
      else if(code == '\\')
         out += "\\\\";
      else if(code == '\n')
         out += "\\n";
      else if(code == '\r')
         out += "\\r";
      else if(code == '\t')
         out += "\\t";
      else if(code < 0x20 || code > 0x7E)
         out += StringFormat("\\u%04x", code);   // keep the file pure ASCII: readable as UTF-8
      else
         out += ShortToString(code);
     }
   return out;
  }

string JsonString(const string text)
  {
   return "\"" + JsonEscape(text) + "\"";
  }

// "2026-10-02T12:30:00+00:00": a UTC instant, always with the explicit offset.
string IsoUtc(const datetime moment)
  {
   MqlDateTime parts;
   TimeToStruct(moment, parts);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02d+00:00",
                       parts.year, parts.mon, parts.day, parts.hour, parts.min, parts.sec);
  }

// "2026-10-02T15:30:00": an instant in trade-server time; the offset travels in its own field.
string IsoServer(const datetime moment)
  {
   MqlDateTime parts;
   TimeToStruct(moment, parts);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02d",
                       parts.year, parts.mon, parts.day, parts.hour, parts.min, parts.sec);
  }

string CurrenciesText()
  {
   string text = "";
   for(int i = 0; i < ArraySize(g_currencies); i++)
      text += (i == 0 ? "" : ", ") + g_currencies[i];
   return text;
  }

string CurrenciesJson()
  {
   string text = "";
   for(int i = 0; i < ArraySize(g_currencies); i++)
      text += (i == 0 ? "" : ", ") + JsonString(g_currencies[i]);
   return text;
  }

string ImportanceName(const ENUM_CALENDAR_EVENT_IMPORTANCE importance)
  {
   switch(importance)
     {
      case CALENDAR_IMPORTANCE_HIGH:
         return "high";
      case CALENDAR_IMPORTANCE_MODERATE:
         return "medium";
      case CALENDAR_IMPORTANCE_LOW:
         return "low";
      case CALENDAR_IMPORTANCE_NONE:
         return "none";
     }
   return "unknown";
  }

double MultiplierFactor(const ENUM_CALENDAR_EVENT_MULTIPLIER multiplier)
  {
   // The enum's members are spelled in the plural in this MQL5 build.
   switch(multiplier)
     {
      case CALENDAR_MULTIPLIER_THOUSANDS:
         return 1000.0;
      case CALENDAR_MULTIPLIER_MILLIONS:
         return 1000000.0;
      case CALENDAR_MULTIPLIER_BILLIONS:
         return 1000000000.0;
     }
   return 1.0;
  }

// MQL5 stores a calendar value as an integer scaled by 10^digits and by the event multiplier.
string ValueText(const long raw, const int digits, const double factor)
  {
   if(raw == CALENDAR_VALUE_UNSET)
      return "null";
   double value = (double)raw / MathPow(10.0, (double)digits) * factor;
   return JsonString(DoubleToString(value, digits));
  }

string ValueRaw(const long raw)
  {
   if(raw == CALENDAR_VALUE_UNSET)
      return "null";
   return IntegerToString(raw);
  }

//+------------------------------------------------------------------+
//| Calendar lookups                                                 |
//+------------------------------------------------------------------+
void RefreshEventCache()
  {
   ArrayResize(g_events, 0);
   for(int c = 0; c < ArraySize(g_currencies); c++)
     {
      MqlCalendarEvent events[];
      int count = CalendarEventByCurrency(g_currencies[c], events);
      if(count <= 0)
        {
         g_last_error = StringFormat("CalendarEventByCurrency(%s) returned %d (error %d)",
                                     g_currencies[c], count, GetLastError());
         PrintFormat("%s: %s", BRIDGE_NAME, g_last_error);
         continue;
        }
      int base = ArraySize(g_events);
      if(ArrayResize(g_events, base + count) != base + count)
        {
         g_last_error = "could not grow the event-description cache";
         continue;
        }
      for(int i = 0; i < count; i++)
         g_events[base + i] = events[i];
     }

   MqlCalendarCountry countries[];
   int country_count = CalendarCountries(countries);
   if(country_count > 0)
     {
      ArrayResize(g_countries, country_count);
      for(int i = 0; i < country_count; i++)
         g_countries[i] = countries[i];
     }

   g_last_event_cache = TimeTradeServer();
   PrintFormat("%s: cached %d event description(s) and %d country(ies) for %s",
               BRIDGE_NAME, ArraySize(g_events), ArraySize(g_countries), CurrenciesText());
  }

bool EventById(const ulong event_id, MqlCalendarEvent &event)
  {
   for(int i = 0; i < ArraySize(g_events); i++)
      if(g_events[i].id == event_id)
        {
         event = g_events[i];
         return true;
        }
   // A value whose description is not cached yet is fetched on demand and remembered.
   if(CalendarEventById(event_id, event))
     {
      int index = ArraySize(g_events);
      if(ArrayResize(g_events, index + 1) == index + 1)
         g_events[index] = event;
      return true;
     }
   return false;
  }

bool CountryById(const ulong country_id, MqlCalendarCountry &country)
  {
   for(int i = 0; i < ArraySize(g_countries); i++)
      if(g_countries[i].id == country_id)
        {
         country = g_countries[i];
         return true;
        }
   return CalendarCountryById(country_id, country);
  }

//+------------------------------------------------------------------+
//| Snapshot                                                         |
//+------------------------------------------------------------------+
string EventJson(const MqlCalendarValue &value, const MqlCalendarEvent &event,
                 const int offset_seconds, const string generated_at)
  {
   MqlCalendarCountry country;
   string country_name  = "";
   string country_code  = "";
   string currency_code = "";
   if(CountryById(event.country_id, country))
     {
      country_name = country.name;
      country_code = country.code;
      currency_code = country.currency;
     }
   if(StringLen(currency_code) == 0)
      currency_code = "unknown";

   double factor     = MultiplierFactor(event.multiplier);
   datetime server_time = value.time;
   // The calendar is reported in trade-server time; the UTC instant is the server instant minus
   // the offset this bridge measured itself, never a hard-coded +2/+3.
   datetime utc_time = server_time - (datetime)offset_seconds;

   string json = "  {\n";
   json += "    \"event_id\": " + IntegerToString((long)event.id) + ",\n";
   json += "    \"value_id\": " + IntegerToString((long)value.id) + ",\n";
   json += "    \"event_code\": " + JsonString(event.event_code) + ",\n";
   json += "    \"name\": " + JsonString(event.name) + ",\n";
   json += "    \"title\": " + JsonString(event.name) + ",\n";
   json += "    \"currency\": " + JsonString(currency_code) + ",\n";
   json += "    \"country\": " + JsonString(country_name) + ",\n";
   json += "    \"country_code\": " + JsonString(country_code) + ",\n";
   json += "    \"scheduled_time\": " + JsonString(IsoUtc(utc_time)) + ",\n";
   json += "    \"scheduled_time_server\": " + JsonString(IsoServer(server_time)) + ",\n";
   json += "    \"scheduled_time_utc\": " + JsonString(IsoUtc(utc_time)) + ",\n";
   json += "    \"importance\": " + JsonString(ImportanceName(event.importance)) + ",\n";
   json += "    \"importance_mql5\": " + JsonString(EnumToString(event.importance)) + ",\n";
   json += "    \"importance_code\": " + IntegerToString((int)event.importance) + ",\n";
   json += "    \"impact_type_code\": " + IntegerToString((int)value.impact_type) + ",\n";
   json += "    \"actual\": " + ValueText(value.actual_value, (int)event.digits, factor) + ",\n";
   json += "    \"actual_raw\": " + ValueRaw(value.actual_value) + ",\n";
   json += "    \"forecast\": " + ValueText(value.forecast_value, (int)event.digits, factor) + ",\n";
   json += "    \"forecast_raw\": " + ValueRaw(value.forecast_value) + ",\n";
   json += "    \"previous\": " + ValueText(value.prev_value, (int)event.digits, factor) + ",\n";
   json += "    \"previous_raw\": " + ValueRaw(value.prev_value) + ",\n";
   json += "    \"revision\": " + IntegerToString(value.revision) + ",\n";
   json += "    \"unit\": " + JsonString(EnumToString(event.unit)) + ",\n";
   json += "    \"multiplier\": " + JsonString(EnumToString(event.multiplier)) + ",\n";
   json += "    \"source\": " + JsonString(BRIDGE_SOURCE) + ",\n";
   json += "    \"source_url\": " + JsonString(event.source_url) + ",\n";
   json += "    \"last_updated\": " + JsonString(generated_at) + "\n";
   json += "  }";
   return json;
  }

string EventsJson(const int offset_seconds, const string generated_at, int &exported, int &high_impact)
  {
   datetime server_now = TimeTradeServer();
   datetime from       = server_now - (datetime)InpLookbackHours * 3600;
   datetime to         = server_now + (datetime)InpHorizonDays * 86400;
   string   json       = "";
   exported            = 0;
   high_impact         = 0;

   for(int c = 0; c < ArraySize(g_currencies); c++)
     {
      string currency = g_currencies[c];
      MqlCalendarValue values[];
      if(!CalendarValueHistory(values, from, to, NULL, currency))
        {
         int error = GetLastError();
         g_failures++;
         g_last_error = StringFormat("CalendarValueHistory(%s) failed with error %d", currency, error);
         PrintFormat("%s: %s", BRIDGE_NAME, g_last_error);
         continue;
        }

      for(int i = 0; i < ArraySize(values); i++)
        {
         MqlCalendarEvent event;
         if(!EventById(values[i].event_id, event))
           {
            g_last_error = StringFormat("no description for event_id=%I64u", values[i].event_id);
            continue;
           }
         if(exported > 0)
            json += ",\n";
         json += EventJson(values[i], event, offset_seconds, generated_at);
         exported++;
         if(event.importance == CALENDAR_IMPORTANCE_HIGH)
            high_impact++;
        }
     }
   if(exported > 0)
      json += "\n";
   return json;
  }

bool WriteSnapshot()
  {
   g_cycles++;
   datetime server_now = TimeTradeServer();
   datetime gmt_now    = TimeGMT();
   // The offset is measured, never assumed: earlier cycles keep the exported times correct even
   // when the broker changes its server clock (DST), because every cycle re-measures it.
   int offset_seconds = (int)MathRound((double)(server_now - gmt_now) / 60.0) * 60;
   string generated_at = IsoUtc(gmt_now);

   if(ArraySize(g_events) == 0 || server_now - g_last_event_cache >= InpEventCacheSeconds)
      RefreshEventCache();

   int exported = 0;
   int high_impact = 0;
   long failures_before = g_failures;
   string events = EventsJson(offset_seconds, generated_at, exported, high_impact);

   string status = "OK";
   if(g_failures > failures_before || ArraySize(g_events) == 0)
     {
      status = "DEGRADED";
      if(StringLen(g_last_error) == 0)
         g_last_error = "no calendar event descriptions could be read from the trade server";
     }

   string json = "";
   json += "{\n";
   json += "  \"bridge\": " + JsonString(BRIDGE_NAME) + ",\n";
   json += "  \"bridge_version\": " + JsonString(BRIDGE_VERSION) + ",\n";
   json += "  \"bridge_status\": " + JsonString(status) + ",\n";
   json += "  \"generated_at\": " + JsonString(generated_at) + ",\n";
   json += "  \"generated_at_source\": " + JsonString("TimeGMT()") + ",\n";
   json += "  \"terminal_server_time\": " + JsonString(IsoServer(server_now)) + ",\n";
   json += "  \"terminal_server_time_utc\": " + JsonString(IsoUtc(server_now - (datetime)offset_seconds)) + ",\n";
   json += "  \"server_utc_offset_seconds\": " + IntegerToString(offset_seconds) + ",\n";
   json += "  \"terminal_build\": " + IntegerToString((int)TerminalInfoInteger(TERMINAL_BUILD)) + ",\n";
   json += "  \"terminal_company\": " + JsonString(TerminalInfoString(TERMINAL_COMPANY)) + ",\n";
   json += "  \"account_server\": " + JsonString(AccountInfoString(ACCOUNT_SERVER)) + ",\n";
   json += "  \"account_login\": " + IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN)) + ",\n";
   json += "  \"connected\": " + (TerminalInfoInteger(TERMINAL_CONNECTED) != 0 ? "true" : "false") + ",\n";
   json += "  \"currencies\": [" + CurrenciesJson() + "],\n";
   json += "  \"window_from_server\": " + JsonString(IsoServer(server_now - (datetime)InpLookbackHours * 3600)) + ",\n";
   json += "  \"window_to_server\": " + JsonString(IsoServer(server_now + (datetime)InpHorizonDays * 86400)) + ",\n";
   json += "  \"refresh_seconds\": " + IntegerToString(InpRefreshSeconds) + ",\n";
   json += "  \"cycles\": " + IntegerToString(g_cycles) + ",\n";
   json += "  \"failures\": " + IntegerToString(g_failures) + ",\n";
   json += "  \"change_id\": " + IntegerToString((long)g_change_id) + ",\n";
   json += "  \"event_count\": " + IntegerToString(exported) + ",\n";
   json += "  \"high_impact_event_count\": " + IntegerToString(high_impact) + ",\n";
   json += "  \"last_error\": " + (StringLen(g_last_error) == 0 ? "null" : JsonString(g_last_error)) + ",\n";
   json += "  \"events\": [\n" + events + "  ]\n";
   json += "}\n";

   int handle = FileOpen(FILE_TEMP, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle == INVALID_HANDLE)
     {
      g_failures++;
      PrintFormat("%s: cannot open the temp bridge file (error %d)", BRIDGE_NAME, GetLastError());
      return false;
     }
   FileWriteString(handle, json);
   FileFlush(handle);
   FileClose(handle);

   // Atomic replace: Python can never read a half-written file.
   if(!FileMove(FILE_TEMP, FILE_COMMON, FILE_FINAL, FILE_COMMON | FILE_REWRITE))
     {
      g_failures++;
      PrintFormat("%s: cannot replace %s (error %d)", BRIDGE_NAME, FILE_FINAL, GetLastError());
      FileDelete(FILE_TEMP, FILE_COMMON);
      return false;
     }

   g_last_write = server_now;
   if(status == "OK")
      g_last_error = "";
   if(InpVerbose)
      PrintFormat("%s: wrote %d event(s) (%d high impact) status=%s server=%s offset=%ds",
                  BRIDGE_NAME, exported, high_impact, status, IsoServer(server_now), offset_seconds);
   return true;
  }

//+------------------------------------------------------------------+
//| Event handlers                                                   |
//+------------------------------------------------------------------+
int OnInit()
  {
   PrintFormat("%s %s: starting on %s %s, exporting %s to Common\\Files\\%s every %ds",
               BRIDGE_NAME, BRIDGE_VERSION, _Symbol, EnumToString((ENUM_TIMEFRAMES)_Period),
               CurrenciesText(), FILE_FINAL, InpRefreshSeconds);
   EventSetTimer(MathMax(5, InpRefreshSeconds));
   WriteSnapshot();
   return(INIT_SUCCEEDED);
  }

void OnTimer()
  {
   if(TimeTradeServer() - g_last_write >= InpRefreshSeconds)
      WriteSnapshot();
  }

void OnTick()
  {
   // A second trigger, so the bridge keeps beating even if the timer is starved.
   if(TimeTradeServer() - g_last_write >= InpRefreshSeconds)
      WriteSnapshot();
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   PrintFormat("%s: stopped (reason %d), %d cycle(s), %d failure(s)", BRIDGE_NAME, reason, g_cycles, g_failures);
  }
//+------------------------------------------------------------------+
