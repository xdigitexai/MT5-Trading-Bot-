/**
 * Server-side access to the bot's HTTP API.
 *
 * Everything here runs inside a React Server Component: the bearer token is read from
 * `process.env.API_TOKEN` on the server and is never handed to the browser. The responses are
 * narrowed into small view models so a field the API does not report becomes `null` (rendered as
 * an explicit "not reported" state) instead of a fabricated number.
 *
 * Environment: `API_URL` (base URL, defaults to http://localhost:8000) and `API_TOKEN`
 * (bearer token for the protected routes). Without a token the protected routes are not called at
 * all, so the page reports "not configured" rather than a table full of zeros.
 */

export const API_BASE_URL = process.env.API_URL ?? "http://localhost:8000";
export const API_TOKEN_CONFIGURED = Boolean(process.env.API_TOKEN);

const ERROR_SNIPPET_LIMIT = 200;

export type ApiResult<T> = { ok: true; value: T } | { ok: false; error: string };

export const NOT_CONFIGURED = "API_TOKEN is not configured for the dashboard, so this protected endpoint was not called.";

type Json = Record<string, unknown>;

function isRecord(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asRecord(value: unknown): Json {
  return isRecord(value) ? value : {};
}

function asText(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed === "" ? null : trimmed;
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asBool(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

async function get(path: string, auth: boolean): Promise<ApiResult<unknown>> {
  const token = process.env.API_TOKEN;
  if (auth && !token) return { ok: false, error: NOT_CONFIGURED };

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      cache: "no-store",
      headers: auth && token ? { Authorization: `Bearer ${token}` } : undefined,
    });
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    return { ok: false, error: `API unreachable at ${API_BASE_URL}: ${detail}` };
  }

  if (!response.ok) {
    let snippet = "";
    try {
      snippet = (await response.text()).replace(/\s+/g, " ").trim().slice(0, ERROR_SNIPPET_LIMIT);
    } catch {
      snippet = "";
    }
    return { ok: false, error: `GET ${path} answered HTTP ${response.status}${snippet ? `: ${snippet}` : ""}` };
  }

  try {
    return { ok: true, value: await response.json() };
  } catch {
    return { ok: false, error: `GET ${path} answered HTTP ${response.status} with a body that is not JSON.` };
  }
}

/* ------------------------------------------------------------------ MT5 object strings */

const MAPPING_ENTRY = /([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(-?\d+(?:\.\d+)?|None|True|False|'[^']*'|"[^"]*")/g;

/**
 * The `/api/account`, `/api/positions` and `/api/orders` routes return MetaTrader 5 objects as
 * `str(...)`, so the fields are read back out of that rendering. A value the rendering does not
 * contain simply stays absent, and every consumer falls back to an explicit "not reported".
 */
export function parseMapping(text: string): Record<string, string> {
  const fields: Record<string, string> = {};
  for (const match of text.matchAll(MAPPING_ENTRY)) {
    const name = match[1];
    if (name in fields) continue;
    const value = match[2].replace(/^(['"])([\s\S]*)\1$/, "$2").trim();
    if (value === "None" || value === "") continue;
    fields[name] = value;
  }
  return fields;
}

export function mappingNumber(fields: Record<string, string>, name: string): number | null {
  const raw = fields[name];
  if (raw === undefined) return null;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

export function mappingText(fields: Record<string, string>, name: string): string | null {
  return asText(fields[name]);
}

export interface Mt5Object {
  raw: string;
  fields: Record<string, string>;
}

function mt5Object(value: unknown): Mt5Object | null {
  const raw = asText(value);
  if (raw === null) return null;
  return { raw, fields: parseMapping(raw) };
}

/* ------------------------------------------------------------------ view models */

export interface HealthPayload {
  status: string | null;
  mt5: string | null;
  mode: string | null;
  liveOrdersPermitted: boolean | null;
  botState: string | null;
}

export interface AccountPayload {
  /** The API reported a non-null account. False means MT5 is not connected. */
  reported: boolean;
  account: Mt5Object | null;
}

export interface Mt5ListPayload {
  objects: Mt5Object[];
  /** Entries the API sent that were not usable text. */
  unreadable: number;
}

export interface SignalRow {
  signalId: string | null;
  symbol: string | null;
  strategy: string | null;
  timeframe: string | null;
  direction: string | null;
  confidence: number | null;
  score: number | null;
  entryPrice: number | null;
  stopLoss: number | null;
  takeProfit: number | null;
  reason: string | null;
  createdAt: string | null;
  executed: boolean | null;
  orderTicket: string | null;
  status: string | null;
}

export interface SignalsPayload {
  signals: SignalRow[];
  count: number | null;
  total: number | null;
  limit: number | null;
  offset: number | null;
}

export interface TradeRow {
  tradeId: string | null;
  symbol: string | null;
  status: string | null;
}

export interface PerformancePayload {
  state: string | null;
  totalTrades: number | null;
  winningTrades: number | null;
  losingTrades: number | null;
  breakevenTrades: number | null;
  grossProfit: number | null;
  grossLoss: number | null;
  netRealizedPnl: number | null;
  averageWin: number | null;
  averageLoss: number | null;
  largestWin: number | null;
  largestLoss: number | null;
  profitFactor: number | null;
  profitFactorState: string | null;
  expectancy: number | null;
  currentDrawdown: number | null;
  maximumDrawdown: number | null;
  notes: string[];
}

export interface GroupSummary {
  trades: number | null;
  winningTrades: number | null;
  losingTrades: number | null;
  breakevenTrades: number | null;
  grossProfit: number | null;
  grossLoss: number | null;
  netPnl: number | null;
  winRate: number | null;
  profitFactor: number | null;
  profitFactorState: string | null;
}

export interface StatisticsPayload {
  state: string | null;
  totalClosedTrades: number | null;
  winRate: number | null;
  lossRate: number | null;
  profitFactor: number | null;
  profitFactorState: string | null;
  maximumDrawdown: number | null;
  averageRiskReward: number | null;
  todayTrades: number | null;
  todayRealizedPnl: number | null;
  byStrategy: Record<string, GroupSummary>;
  bySymbol: Record<string, GroupSummary>;
  notes: string[];
}

export interface StrategyDetail {
  name: string | null;
  kind: string | null;
  implemented: boolean | null;
  enabled: boolean | null;
  timeframe: string | null;
}

export interface StrategiesPayload {
  strategies: string[];
  details: StrategyDetail[];
  enabled: string[];
  ensembleEnabled: boolean | null;
}

export interface RiskPayload {
  riskPerTradePct: number | null;
  maxDailyLossPct: number | null;
  maxDrawdownPct: number | null;
  maxOpenPositions: number | null;
  emergencyLocked: boolean | null;
  day: string | null;
  realizedPnlToday: number | null;
  startingEquity: number | null;
  peakEquity: number | null;
  mode: string | null;
  liveOrdersPermitted: boolean | null;
}

export interface SchedulerPayload {
  running: boolean | null;
  threadAlive: boolean | null;
  cycles: number | null;
  skippedOverlaps: number | null;
  intervalSeconds: number | null;
  lastScanAt: string | null;
  lastSignalAt: string | null;
  lastExecutionAt: string | null;
  lastReconciliationAt: string | null;
  lastError: string | null;
  symbols: string[];
  lastCycle: Json | null;
}

export interface BotStatusPayload {
  state: string | null;
  detail: string | null;
  running: boolean | null;
  emergencyLocked: boolean | null;
  startedAt: string | null;
  mt5Connected: boolean | null;
  mt5Detail: string | null;
  scheduler: SchedulerPayload | null;
  mode: string | null;
  liveOrdersPermitted: boolean | null;
}

/* ------------------------------------------------------------------ endpoints */

export async function getHealth(): Promise<ApiResult<HealthPayload>> {
  const result = await get("/api/health", false);
  if (!result.ok) return result;
  const body = asRecord(result.value);
  return {
    ok: true,
    value: {
      status: asText(body.status),
      mt5: asText(body.mt5),
      mode: asText(body.mode),
      liveOrdersPermitted: asBool(body.live_orders_permitted),
      botState: asText(body.bot_state),
    },
  };
}

export async function getAccount(): Promise<ApiResult<AccountPayload>> {
  const result = await get("/api/account", true);
  if (!result.ok) return result;
  const body = asRecord(result.value);
  return { ok: true, value: { reported: body.account !== null && body.account !== undefined, account: mt5Object(body.account) } };
}

function mt5List(value: unknown, key: string): Mt5ListPayload {
  const entries = asList(asRecord(value)[key]);
  const objects: Mt5Object[] = [];
  let unreadable = 0;
  for (const entry of entries) {
    const parsed = mt5Object(entry);
    if (parsed === null) unreadable += 1;
    else objects.push(parsed);
  }
  return { objects, unreadable };
}

export async function getPositions(): Promise<ApiResult<Mt5ListPayload>> {
  const result = await get("/api/positions", true);
  if (!result.ok) return result;
  return { ok: true, value: mt5List(result.value, "positions") };
}

export async function getOrders(): Promise<ApiResult<Mt5ListPayload>> {
  const result = await get("/api/orders", true);
  if (!result.ok) return result;
  return { ok: true, value: mt5List(result.value, "orders") };
}

export async function getTrades(): Promise<ApiResult<TradeRow[]>> {
  const result = await get("/api/trades", true);
  if (!result.ok) return result;
  if (!Array.isArray(result.value)) return { ok: false, error: "GET /api/trades did not answer with a list." };
  return {
    ok: true,
    value: result.value.map((entry) => {
      const row = asRecord(entry);
      return { tradeId: asText(row.trade_id), symbol: asText(row.symbol), status: asText(row.status) };
    }),
  };
}

export async function getSignals(limit: number): Promise<ApiResult<SignalsPayload>> {
  const result = await get(`/api/signals?limit=${limit}`, true);
  if (!result.ok) return result;
  const body = asRecord(result.value);
  const signals = asList(body.signals).map((entry) => {
    const row = asRecord(entry);
    return {
      signalId: asText(row.signal_id),
      symbol: asText(row.symbol),
      strategy: asText(row.strategy),
      timeframe: asText(row.timeframe),
      direction: asText(row.direction),
      confidence: asNumber(row.confidence),
      score: asNumber(row.score),
      entryPrice: asNumber(row.entry_price),
      stopLoss: asNumber(row.stop_loss),
      takeProfit: asNumber(row.take_profit),
      reason: asText(row.reason),
      createdAt: asText(row.created_at),
      executed: asBool(row.executed),
      orderTicket: asText(row.order_ticket),
      status: asText(row.status),
    };
  });
  return {
    ok: true,
    value: {
      signals,
      count: asNumber(body.count),
      total: asNumber(body.total),
      limit: asNumber(body.limit),
      offset: asNumber(body.offset),
    },
  };
}

export async function getPerformance(): Promise<ApiResult<PerformancePayload>> {
  const result = await get("/api/performance", true);
  if (!result.ok) return result;
  const body = asRecord(result.value);
  return {
    ok: true,
    value: {
      state: asText(body.state),
      totalTrades: asNumber(body.total_trades),
      winningTrades: asNumber(body.winning_trades),
      losingTrades: asNumber(body.losing_trades),
      breakevenTrades: asNumber(body.breakeven_trades),
      grossProfit: asNumber(body.gross_profit),
      grossLoss: asNumber(body.gross_loss),
      netRealizedPnl: asNumber(body.net_realized_pnl),
      averageWin: asNumber(body.average_win),
      averageLoss: asNumber(body.average_loss),
      largestWin: asNumber(body.largest_win),
      largestLoss: asNumber(body.largest_loss),
      profitFactor: asNumber(body.profit_factor),
      profitFactorState: asText(body.profit_factor_state),
      expectancy: asNumber(body.expectancy),
      currentDrawdown: asNumber(body.current_drawdown),
      maximumDrawdown: asNumber(body.maximum_drawdown),
      notes: asList(body.notes).map(asText).filter((note): note is string => note !== null),
    },
  };
}

function groupSummaries(value: unknown): Record<string, GroupSummary> {
  const groups: Record<string, GroupSummary> = {};
  for (const [name, entry] of Object.entries(asRecord(value))) {
    const row = asRecord(entry);
    groups[name] = {
      trades: asNumber(row.trades),
      winningTrades: asNumber(row.winning_trades),
      losingTrades: asNumber(row.losing_trades),
      breakevenTrades: asNumber(row.breakeven_trades),
      grossProfit: asNumber(row.gross_profit),
      grossLoss: asNumber(row.gross_loss),
      netPnl: asNumber(row.net_pnl),
      winRate: asNumber(row.win_rate),
      profitFactor: asNumber(row.profit_factor),
      profitFactorState: asText(row.profit_factor_state),
    };
  }
  return groups;
}

export async function getStatistics(): Promise<ApiResult<StatisticsPayload>> {
  const result = await get("/api/statistics", true);
  if (!result.ok) return result;
  const body = asRecord(result.value);
  return {
    ok: true,
    value: {
      state: asText(body.state),
      totalClosedTrades: asNumber(body.total_closed_trades),
      winRate: asNumber(body.win_rate),
      lossRate: asNumber(body.loss_rate),
      profitFactor: asNumber(body.profit_factor),
      profitFactorState: asText(body.profit_factor_state),
      maximumDrawdown: asNumber(body.maximum_drawdown),
      averageRiskReward: asNumber(body.average_risk_reward),
      todayTrades: asNumber(body.today_trades),
      todayRealizedPnl: asNumber(body.today_realized_pnl),
      byStrategy: groupSummaries(body.by_strategy),
      bySymbol: groupSummaries(body.by_symbol),
      notes: asList(body.notes).map(asText).filter((note): note is string => note !== null),
    },
  };
}

export async function getStrategies(): Promise<ApiResult<StrategiesPayload>> {
  const result = await get("/api/strategies", true);
  if (!result.ok) return result;
  const body = asRecord(result.value);
  return {
    ok: true,
    value: {
      strategies: asList(body.strategies).map(asText).filter((name): name is string => name !== null),
      details: asList(body.details).map((entry) => {
        const row = asRecord(entry);
        return {
          name: asText(row.name),
          kind: asText(row.kind),
          implemented: asBool(row.implemented),
          enabled: asBool(row.enabled),
          timeframe: asText(row.timeframe),
        };
      }),
      enabled: asList(body.enabled).map(asText).filter((name): name is string => name !== null),
      ensembleEnabled: asBool(body.ensemble_enabled),
    },
  };
}

export async function getRisk(): Promise<ApiResult<RiskPayload>> {
  const result = await get("/api/risk", true);
  if (!result.ok) return result;
  const body = asRecord(result.value);
  return {
    ok: true,
    value: {
      riskPerTradePct: asNumber(body.risk_per_trade_pct),
      maxDailyLossPct: asNumber(body.max_daily_loss_pct),
      maxDrawdownPct: asNumber(body.max_drawdown_pct),
      maxOpenPositions: asNumber(body.max_open_positions),
      emergencyLocked: asBool(body.emergency_locked),
      day: asText(body.day),
      realizedPnlToday: asNumber(body.realized_pnl_today),
      startingEquity: asNumber(body.starting_equity),
      peakEquity: asNumber(body.peak_equity),
      mode: asText(body.mode),
      liveOrdersPermitted: asBool(body.live_orders_permitted),
    },
  };
}

export async function getBotStatus(): Promise<ApiResult<BotStatusPayload>> {
  const result = await get("/api/bot/status", true);
  if (!result.ok) return result;
  const body = asRecord(result.value);
  const mt5 = asRecord(body.mt5);
  const raw = isRecord(body.scheduler) ? body.scheduler : null;
  const scheduler: SchedulerPayload | null = raw === null ? null : {
    running: asBool(raw.running),
    threadAlive: asBool(raw.thread_alive),
    cycles: asNumber(raw.cycles),
    skippedOverlaps: asNumber(raw.skipped_overlaps),
    intervalSeconds: asNumber(raw.interval_seconds),
    lastScanAt: asText(raw.last_scan_at),
    lastSignalAt: asText(raw.last_signal_at),
    lastExecutionAt: asText(raw.last_execution_at),
    lastReconciliationAt: asText(raw.last_reconciliation_at),
    lastError: asText(raw.last_error),
    symbols: asList(raw.symbols).map(asText).filter((symbol): symbol is string => symbol !== null),
    lastCycle: isRecord(raw.last_cycle) ? raw.last_cycle : null,
  };
  return {
    ok: true,
    value: {
      state: asText(body.state),
      detail: asText(body.detail),
      running: asBool(body.running),
      emergencyLocked: asBool(body.emergency_locked),
      startedAt: asText(body.started_at),
      mt5Connected: asBool(mt5.connected),
      mt5Detail: asText(mt5.detail),
      scheduler,
      mode: asText(body.mode),
      liveOrdersPermitted: asBool(body.live_orders_permitted),
    },
  };
}
