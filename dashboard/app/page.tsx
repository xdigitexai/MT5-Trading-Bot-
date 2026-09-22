import type { ReactNode } from "react";
import {
  API_BASE_URL,
  API_TOKEN_CONFIGURED,
  NOT_CONFIGURED,
  getAccount,
  getBotStatus,
  getHealth,
  getOrders,
  getPerformance,
  getPositions,
  getRisk,
  getSignals,
  getStatistics,
  getStrategies,
  getTrades,
  mappingNumber,
  mappingText,
} from "../lib/api";
import type { ApiResult, GroupSummary, Mt5Object } from "../lib/api";
import { amount, booleanLabel, fractionPercent, integer, percentValue, pnlTone, rawNumber, utcStamp } from "../lib/format";
import { Badge, CodeList, Field, Fields, Notice, Panel, SourceNote, Stat, StatGrid, Table, Unavailable } from "../components/ui";
import styles from "./dashboard.module.css";

/* Dynamic + no-store: every render asks the live backend, nothing is baked in at build time. */
export const dynamic = "force-dynamic";

const SIGNAL_LIMIT = 20;
/** MT5 POSITION_TYPE_BUY / POSITION_TYPE_SELL, as the API renders position objects. */
const POSITION_SIDES: Record<string, string> = { "0": "BUY", "1": "SELL" };
const DASH = "—";
const dot = (items: string[]): ReactNode => items.join(" · ");

function pick<T, R>(result: ApiResult<T>, select: (value: T) => R | null): R | null {
  return result.ok ? select(result.value) : null;
}

function sectionError(error: string): string {
  return error === NOT_CONFIGURED
    ? "Not configured: the dashboard has no API_TOKEN, so this endpoint was not called (see the banner at the top of the page)."
    : error;
}

function cell(value: string | null): string {
  return value ?? DASH;
}

/** A nullable metric with the API's own explanation of why it is undefined. */
function ValueOrReason({ value, reason }: { value: string | null; reason: string | null }) {
  if (value !== null) return <>{value}</>;
  return <span style={{ color: "#7c8aa5", fontStyle: "italic" }}>{reason ?? DASH}</span>;
}

function groupRows(groups: Record<string, GroupSummary>, currency: string | null): ReactNode[][] {
  return Object.entries(groups).map(([name, summary]) => [
    name,
    cell(integer(summary.trades)),
    cell(integer(summary.winningTrades)),
    cell(integer(summary.losingTrades)),
    cell(integer(summary.breakevenTrades)),
    cell(fractionPercent(summary.winRate)),
    cell(amount(summary.netPnl, 2, currency)),
    <ValueOrReason key="pf" value={amount(summary.profitFactor, 2)} reason={summary.profitFactorState} />,
  ]);
}

function positionSide(position: Mt5Object): string | null {
  const raw = position.fields.type;
  if (raw === undefined) return null;
  const side = POSITION_SIDES[raw] ?? null;
  return side === null ? `type ${raw}` : `${side} (type ${raw})`;
}

export default async function Dashboard() {
  const [health, bot, account, positions, orders, risk, performance, statistics, strategies, signals, trades] = await Promise.all([
    getHealth(),
    getBotStatus(),
    getAccount(),
    getPositions(),
    getOrders(),
    getRisk(),
    getPerformance(),
    getStatistics(),
    getStrategies(),
    getSignals(SIGNAL_LIMIT),
    getTrades(),
  ]);

  const mode = pick(health, (value) => value.mode) ?? pick(bot, (value) => value.mode) ?? pick(risk, (value) => value.mode);
  const livePermitted =
    pick(health, (value) => value.liveOrdersPermitted) ??
    pick(bot, (value) => value.liveOrdersPermitted) ??
    pick(risk, (value) => value.liveOrdersPermitted);
  const mt5Connected = pick(bot, (value) => value.mt5Connected) ?? (health.ok ? health.value.status === "ok" : null);
  const mt5Detail = pick(bot, (value) => value.mt5Detail) ?? pick(health, (value) => value.mt5);
  const botState = pick(bot, (value) => value.state) ?? pick(health, (value) => value.botState);
  const emergencyLocked = pick(bot, (value) => value.emergencyLocked) ?? pick(risk, (value) => value.emergencyLocked);
  const normalizedMode = mode === null ? null : mode.toLowerCase();

  /* ---- account: the API renders the MT5 account object as a string, so the fields are parsed out of it ---- */
  const accountObject = account.ok ? account.value.account : null;
  const accountFields = accountObject === null ? null : accountObject.fields;
  const accountFallback =
    account.ok && !account.value.reported
      ? "MT5 is not connected, so the API reports no account object"
      : "the account string reported by the API does not carry this field";
  const field = (name: string): string | null => (accountFields === null ? null : mappingText(accountFields, name));
  const metric = (name: string): number | null => (accountFields === null ? null : mappingNumber(accountFields, name));
  const currency = field("currency");

  /* ---- open positions ---- */
  const positionObjects = positions.ok ? positions.value.objects : [];
  const unparsedPositions = positionObjects.filter((position) => Object.keys(position.fields).length === 0);
  const positionProfits = positionObjects.map((position) => mappingNumber(position.fields, "profit"));
  const knownProfits = positionProfits.filter((value): value is number => value !== null);
  const floatingPnl = positionObjects.length > 0 && knownProfits.length === positionObjects.length ? knownProfits.reduce((sum, value) => sum + value, 0) : null;
  const positionEmptyHint =
    mt5Connected === false
      ? "MT5 is not connected, and the API answers with an empty list in that case — this is not proof that the account is flat."
      : "MT5 is connected and reported no open positions for the terminal.";

  return (
    <main style={{ background: "#0b1220", color: "#e5e7eb", minHeight: "100vh", padding: "32px 20px 64px", fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif" }}>
      <div style={{ maxWidth: 1180, margin: "0 auto" }}>
        <header>
          <h1 style={{ margin: 0, fontSize: 26 }}>MT5 Forex Bot</h1>
          <p style={{ color: "#93a4c3", maxWidth: 780, fontSize: 14 }}>
            Read-only operator view of the running bot. Every figure below comes from the bot&apos;s HTTP API on each render
            (no caching, no stored snapshots), and a field the API does not report is shown as an explicit empty state rather
            than as a zero. {DASH} means the API returned nothing for that cell.
          </p>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center", margin: "12px 0" }}>
            {normalizedMode === "demo" ? <Badge label="Demo mode" tone="good" accent="#38bdf8" /> : null}
            {normalizedMode === "live" ? (
              <Badge label={livePermitted === true ? "Live mode — real money" : "Live mode"} tone="bad" pulse={livePermitted === true ? styles.livePulse : undefined} />
            ) : null}
            {normalizedMode === "paper" ? <Badge label="Paper mode" tone="plain" /> : null}
            {normalizedMode === "backtest" ? <Badge label="Backtest mode" tone="plain" /> : null}
            {normalizedMode === null ? <Badge label="Trading mode not reported" tone="warn" /> : null}
            {livePermitted === true ? <Badge label="Live orders permitted" tone="bad" pulse={styles.livePulse} /> : null}
            {emergencyLocked === true ? <Badge label="Emergency lock in force" tone="bad" /> : null}
            <Badge
              label={mt5Connected === true ? "MT5 connected" : mt5Connected === false ? "MT5 not connected" : "MT5 state unknown"}
              tone={mt5Connected === true ? "good" : mt5Connected === false ? "bad" : "warn"}
            />
          </div>
          <p style={{ fontSize: 12, color: "#64748b", margin: 0, fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace" }}>
            {dot([
              `API base URL: ${API_BASE_URL}`,
              `API_TOKEN ${API_TOKEN_CONFIGURED ? "configured (server-side only)" : "NOT configured"}`,
              `rendered ${utcStamp(new Date().toISOString()) ?? DASH}`,
            ])}
          </p>
        </header>

        {livePermitted === true ? (
          <Notice tone="bad" title="LIVE ORDERS PERMITTED">
            The API reports <code>live_orders_permitted = true</code>, which means this bot can place real-money orders on the
            configured MT5 account. This dashboard is read-only and offers no trading controls.
          </Notice>
        ) : null}

        {!API_TOKEN_CONFIGURED ? (
          <Notice tone="warn" title="API token not configured">
            Set <code>API_TOKEN</code> in the dashboard&apos;s server environment to show account, position, bot and analytics
            data. Until then the protected endpoints are not called at all, so their sections below say &quot;not configured&quot;
            instead of showing empty tables that would look like real zeros.
          </Notice>
        ) : null}

        {!health.ok ? (
          <Notice tone="bad" title="API unreachable">
            {health.error} The endpoint <code>GET /api/health</code> is unauthenticated, so this is a connectivity or
            configuration problem, not a permission one.
          </Notice>
        ) : null}

        <Panel title="Backend connection" endpoint="GET /api/health (unauthenticated)">
          <Fields>
            <Field label="Reported status" value={pick(health, (value) => value.status)} />
            <Field label="MT5 detail" value={mt5Detail} />
            <Field label="Trading mode" value={mode} />
            <Field label="Live orders permitted" value={booleanLabel(livePermitted, "yes", "no")} />
            <Field label="Bot state" value={botState} />
            <Field label="MT5 connected (from the authenticated bot status)" value={booleanLabel(mt5Connected, "yes", "no")} />
          </Fields>
          {!health.ok ? <Unavailable error={health.error} /> : null}
          <SourceNote>
            &quot;degraded&quot; means the gateway is up but MT5 is not usable; the detail column carries the gateway&apos;s own wording.
          </SourceNote>
        </Panel>

        <Panel title="Bot status" endpoint="GET /api/bot/status">
          {bot.ok ? (
            <>
              <StatGrid>
                <Stat label="State" value={bot.value.state} tone={bot.value.state === "RUNNING" ? "good" : bot.value.state === "DEGRADED" || bot.value.state === "ERROR" ? "bad" : "plain"} />
                <Stat label="Market loop running" value={booleanLabel(bot.value.running, "yes", "no")} />
                <Stat label="Emergency locked" value={booleanLabel(bot.value.emergencyLocked, "yes", "no")} tone={bot.value.emergencyLocked ? "bad" : "plain"} />
                <Stat label="Started at" value={utcStamp(bot.value.startedAt)} fallback="not started in this process" />
                <Stat label="Scheduler cycles" value={integer(bot.value.scheduler?.cycles ?? null)} />
                <Stat label="Scheduler interval" value={bot.value.scheduler?.intervalSeconds === null || bot.value.scheduler?.intervalSeconds === undefined ? null : `${bot.value.scheduler.intervalSeconds}s`} />
              </StatGrid>
              <div style={{ marginTop: 14 }}>
                <Fields>
                  <Field label="Detail" value={bot.value.detail} />
                  <Field label="MT5 health" value={bot.value.mt5Detail} />
                  <Field label="Last scan" value={utcStamp(bot.value.scheduler?.lastScanAt ?? null)} />
                  <Field label="Last signal" value={utcStamp(bot.value.scheduler?.lastSignalAt ?? null)} />
                  <Field label="Last execution" value={utcStamp(bot.value.scheduler?.lastExecutionAt ?? null)} />
                  <Field label="Last reconciliation" value={utcStamp(bot.value.scheduler?.lastReconciliationAt ?? null)} />
                  <Field label="Skipped overlapping cycles" value={integer(bot.value.scheduler?.skippedOverlaps ?? null)} />
                  <Field label="Configured symbols" value={bot.value.scheduler === null ? null : bot.value.scheduler.symbols.length === 0 ? "none reported" : bot.value.scheduler.symbols.join(", ")} />
                  <Field label="Last scheduler error" value={bot.value.scheduler?.lastError ?? null} fallback="none reported" />
                </Fields>
              </div>
              <SourceNote>
                The per-symbol detail of the last cycle is exposed by the API but is not rendered here, because it is a nested
                per-symbol object whose shape varies with the strategy and data layers.
              </SourceNote>
            </>
          ) : (
            <Unavailable error={sectionError(bot.error)} />
          )}
        </Panel>

        <Panel title="Account" endpoint="GET /api/account">
          {account.ok ? (
            accountObject === null ? (
              <Notice tone="warn" title="No account information">
                The API reports no MT5 account object, which it does whenever MT5 is not connected.
              </Notice>
            ) : (
              <>
                <StatGrid>
                  <Stat label="Balance" value={amount(metric("balance"), 2, currency)} fallback={accountFallback} />
                  <Stat label="Equity" value={amount(metric("equity"), 2, currency)} fallback={accountFallback} />
                  <Stat label="Margin used" value={amount(metric("margin"), 2, currency)} fallback={accountFallback} />
                  <Stat label="Free margin" value={amount(metric("margin_free"), 2, currency)} fallback={accountFallback} />
                  <Stat label="Margin level" value={percentValue(metric("margin_level"))} fallback={accountFallback} />
                  <Stat label="Floating result of open positions" value={amount(metric("profit"), 2, currency)} fallback={accountFallback} tone={pnlTone(metric("profit"))} />
                </StatGrid>
                <div style={{ marginTop: 14 }}>
                  <Fields>
                    <Field label="Login" value={field("login")} fallback={accountFallback} />
                    <Field label="Server" value={field("server")} fallback={accountFallback} />
                    <Field label="Currency" value={field("currency")} fallback={accountFallback} />
                    <Field label="Leverage" value={field("leverage")} fallback={accountFallback} />
                    <Field label="Trade mode" value={field("trade_mode")} fallback={accountFallback} />
                    <Field label="Trading allowed" value={field("trade_allowed")} fallback={accountFallback} />
                    <Field label="Credit" value={amount(metric("credit"), 2, currency)} fallback={accountFallback} />
                  </Fields>
                </div>
                <SourceNote>
                  <code>GET /api/account</code> returns the MT5 account object rendered as a string, so these fields are read out
                  of that rendering. A field the rendering does not contain stays empty above. Raw value as reported:
                  <div style={{ marginTop: 6 }}>
                    <CodeList items={[accountObject.raw]} empty="empty string" />
                  </div>
                </SourceNote>
              </>
            )
          ) : (
            <Unavailable error={sectionError(account.error)} />
          )}
        </Panel>

        <Panel title="Open positions" endpoint="GET /api/positions">
          {positions.ok ? (
            <>
              <StatGrid>
                <Stat label="Open positions reported" value={integer(positionObjects.length)} />
                <Stat
                  label="Floating result of these positions"
                  value={amount(floatingPnl, 2, currency)}
                  tone={pnlTone(floatingPnl)}
                  fallback="not derivable: at least one position did not report a profit field"
                  hint="Sum of the profit fields MT5 reports; it is not a field of the API itself."
                />
                {positions.value.unreadable > 0 ? <Stat label="Entries not usable" value={integer(positions.value.unreadable)} /> : null}
              </StatGrid>
              <div style={{ marginTop: 14 }}>
                <Table
                  head={["Ticket", "Symbol", "Side", "Volume", "Open price", "Current price", "Stop loss", "Take profit", "Profit", "Magic"]}
                  rows={positionObjects.map((position) => [
                    cell(mappingText(position.fields, "ticket")),
                    cell(mappingText(position.fields, "symbol")),
                    cell(positionSide(position)),
                    cell(rawNumber(mappingNumber(position.fields, "volume"))),
                    cell(rawNumber(mappingNumber(position.fields, "price_open"))),
                    cell(rawNumber(mappingNumber(position.fields, "price_current"))),
                    cell(rawNumber(mappingNumber(position.fields, "sl"))),
                    cell(rawNumber(mappingNumber(position.fields, "tp"))),
                    cell(amount(mappingNumber(position.fields, "profit"), 2, currency)),
                    cell(mappingText(position.fields, "magic")),
                  ])}
                  empty="No open positions were reported."
                  emptyHint={positionEmptyHint}
                />
              </div>
              {unparsedPositions.length > 0 ? (
                <SourceNote>
                  {unparsedPositions.length} of the reported positions carry no readable fields; shown raw:
                  <div style={{ marginTop: 6 }}>
                    <CodeList items={unparsedPositions.map((position) => position.raw)} empty="none" />
                  </div>
                </SourceNote>
              ) : null}
              <SourceNote>
                The API returns each MT5 position as a string, so rows above are parsed out of that rendering; the side is MT5&apos;s
                position type 0 = BUY, 1 = SELL.
              </SourceNote>
            </>
          ) : (
            <Unavailable error={sectionError(positions.error)} />
          )}
        </Panel>

        <Panel title="Pending orders" endpoint="GET /api/orders">
          {orders.ok ? (
            <>
              <StatGrid>
                <Stat label="Pending orders reported" value={integer(orders.value.objects.length)} />
                {orders.value.unreadable > 0 ? <Stat label="Entries not usable" value={integer(orders.value.unreadable)} /> : null}
              </StatGrid>
              <div style={{ marginTop: 14 }}>
                <Table
                  head={["Ticket", "Symbol", "Type (raw)", "Volume", "Open price", "Stop loss", "Take profit", "Magic"]}
                  rows={orders.value.objects.map((order) => [
                    cell(mappingText(order.fields, "ticket")),
                    cell(mappingText(order.fields, "symbol")),
                    cell(mappingText(order.fields, "type")),
                    cell(rawNumber(mappingNumber(order.fields, "volume_current"))),
                    cell(rawNumber(mappingNumber(order.fields, "price_open"))),
                    cell(rawNumber(mappingNumber(order.fields, "sl"))),
                    cell(rawNumber(mappingNumber(order.fields, "tp"))),
                    cell(mappingText(order.fields, "magic")),
                  ])}
                  empty="No pending orders were reported."
                  emptyHint={mt5Connected === false ? "MT5 is not connected, and the API answers with an empty list in that case." : "MT5 is connected and reported no pending orders for the terminal."}
                />
              </div>
              <SourceNote>
                The order type is printed as the raw MT5 code the API reports; the dashboard does not translate it, because the
                order-type codes differ from the position-type codes used above.
              </SourceNote>
            </>
          ) : (
            <Unavailable error={sectionError(orders.error)} />
          )}
        </Panel>

        <Panel title="Risk state and limits" endpoint="GET /api/risk">
          {risk.ok ? (
            <>
              <StatGrid>
                <Stat label="Emergency lock" value={booleanLabel(risk.value.emergencyLocked, "locked", "not locked")} tone={risk.value.emergencyLocked ? "bad" : "plain"} />
                <Stat label="Realized P/L today" value={amount(risk.value.realizedPnlToday, 2, currency)} tone={pnlTone(risk.value.realizedPnlToday)} />
                <Stat label="Starting equity" value={amount(risk.value.startingEquity, 2, currency)} fallback="not observed yet: no equity snapshot has been recorded" />
                <Stat label="Peak equity" value={amount(risk.value.peakEquity, 2, currency)} fallback="not observed yet: no equity snapshot has been recorded" />
                <Stat label="Risk per trade limit" value={percentValue(risk.value.riskPerTradePct)} />
                <Stat label="Maximum daily loss limit" value={percentValue(risk.value.maxDailyLossPct)} />
                <Stat label="Maximum drawdown limit" value={percentValue(risk.value.maxDrawdownPct)} />
                <Stat label="Maximum open positions" value={integer(risk.value.maxOpenPositions)} />
                <Stat label="Risk day (UTC)" value={risk.value.day} />
                <Stat label="Mode reported here" value={risk.value.mode} />
              </StatGrid>
            </>
          ) : (
            <Unavailable error={sectionError(risk.error)} />
          )}
        </Panel>

        <Panel title="Closed-trade performance" endpoint="GET /api/performance">
          {performance.ok ? (
            <>
              {performance.value.totalTrades === 0 ? (
                <Notice tone="warn" title={performance.value.state ?? "No reconciled closed trades yet"}>
                  No closed trade has been reconciled from MT5 yet, so every outcome metric below is undefined. The API reports
                  that explicitly instead of zeroes.
                </Notice>
              ) : null}
              <StatGrid>
                <Stat label="Closed trades counted" value={integer(performance.value.totalTrades)} />
                <Stat label="Net realized P/L" value={amount(performance.value.netRealizedPnl, 2, currency)} tone={pnlTone(performance.value.netRealizedPnl)} />
                <Stat label="Winning trades" value={integer(performance.value.winningTrades)} />
                <Stat label="Losing trades" value={integer(performance.value.losingTrades)} />
                <Stat label="Break-even trades" value={integer(performance.value.breakevenTrades)} />
                <Stat label="Gross profit" value={amount(performance.value.grossProfit, 2, currency)} tone={pnlTone(performance.value.grossProfit)} />
                <Stat label="Gross loss" value={amount(performance.value.grossLoss, 2, currency)} tone={performance.value.grossLoss === null ? "plain" : "bad"} />
                <Stat
                  label="Profit factor"
                  value={amount(performance.value.profitFactor, 2)}
                  fallback={performance.value.profitFactorState ?? performance.value.state}
                />
                <Stat label="Expectancy per trade" value={amount(performance.value.expectancy, 2, currency)} tone={pnlTone(performance.value.expectancy)} fallback={performance.value.state} />
                <Stat label="Average win" value={amount(performance.value.averageWin, 2, currency)} tone={pnlTone(performance.value.averageWin)} fallback="no winning trade in the sample" />
                <Stat label="Average loss" value={amount(performance.value.averageLoss, 2, currency)} tone={pnlTone(performance.value.averageLoss)} fallback="no losing trade in the sample" />
                <Stat label="Largest win" value={amount(performance.value.largestWin, 2, currency)} tone={pnlTone(performance.value.largestWin)} fallback="no winning trade in the sample" />
                <Stat label="Largest loss" value={amount(performance.value.largestLoss, 2, currency)} tone={pnlTone(performance.value.largestLoss)} fallback="no losing trade in the sample" />
                <Stat label="Current drawdown (closed-trade curve)" value={amount(performance.value.currentDrawdown, 2, currency)} fallback={performance.value.state} />
                <Stat label="Maximum drawdown (closed-trade curve)" value={amount(performance.value.maximumDrawdown, 2, currency)} fallback={performance.value.state} />
              </StatGrid>
              {performance.value.notes.map((note) => (
                <SourceNote key={note}>{note}</SourceNote>
              ))}
              <SourceNote>
                Only trades MT5 has confirmed as realized are counted, and drawdown is measured in account currency on the
                closed-trade P/L curve, not against the equity high-water mark.
              </SourceNote>
            </>
          ) : (
            <Unavailable error={sectionError(performance.error)} />
          )}
        </Panel>

        <Panel title="Closed-trade statistics" endpoint="GET /api/statistics">
          {statistics.ok ? (
            <>
              {statistics.value.totalClosedTrades === 0 ? (
                <Notice tone="warn" title={statistics.value.state ?? "No reconciled closed trades yet"}>
                  No closed trade has been reconciled from MT5 yet. Win rate, loss rate and profit factor are undefined in that
                  state, and the per-strategy and per-symbol tables below are empty because the API returns no groups.
                </Notice>
              ) : null}
              <StatGrid>
                <Stat label="Closed trades counted" value={integer(statistics.value.totalClosedTrades)} />
                <Stat label="Win rate" value={fractionPercent(statistics.value.winRate)} fallback={statistics.value.state} />
                <Stat label="Loss rate" value={fractionPercent(statistics.value.lossRate)} fallback={statistics.value.state} />
                <Stat label="Profit factor" value={amount(statistics.value.profitFactor, 2)} fallback={statistics.value.profitFactorState ?? statistics.value.state} />
                <Stat label="Maximum drawdown (closed-trade curve)" value={amount(statistics.value.maximumDrawdown, 2, currency)} fallback={statistics.value.state} />
                <Stat label="Average planned risk/reward" value={amount(statistics.value.averageRiskReward, 2)} fallback="the sample has no closed trade with a known stop, target and entry" />
                <Stat label="Trades closed today (UTC)" value={integer(statistics.value.todayTrades)} />
                <Stat label="Realized P/L today (UTC)" value={amount(statistics.value.todayRealizedPnl, 2, currency)} tone={pnlTone(statistics.value.todayRealizedPnl)} />
              </StatGrid>
              {statistics.value.notes.map((note) => (
                <SourceNote key={note}>{note}</SourceNote>
              ))}
            </>
          ) : (
            <Unavailable error={sectionError(statistics.error)} />
          )}
        </Panel>

        <Panel title="Per-strategy statistics" endpoint="GET /api/statistics → by_strategy">
          {statistics.ok ? (
            <Table
              head={["Strategy", "Trades", "Wins", "Losses", "Break-even", "Win rate", `Net P/L${currency === null ? "" : ` (${currency})`}`, "Profit factor"]}
              rows={groupRows(statistics.value.byStrategy, currency)}
              empty="No strategy group has a reconciled closed trade yet."
              emptyHint="Groups only exist for strategies that produced a reconciled closed trade; the API returns an empty object otherwise."
            />
          ) : (
            <Unavailable error={sectionError(statistics.error)} />
          )}
        </Panel>

        <Panel title="Per-symbol statistics" endpoint="GET /api/statistics → by_symbol">
          {statistics.ok ? (
            <Table
              head={["Symbol", "Trades", "Wins", "Losses", "Break-even", "Win rate", `Net P/L${currency === null ? "" : ` (${currency})`}`, "Profit factor"]}
              rows={groupRows(statistics.value.bySymbol, currency)}
              empty="No symbol group has a reconciled closed trade yet."
              emptyHint="Groups only exist for symbols with a reconciled closed trade; the API returns an empty object otherwise."
            />
          ) : (
            <Unavailable error={sectionError(statistics.error)} />
          )}
        </Panel>

        <Panel title={`Recent signals (newest ${SIGNAL_LIMIT})`} endpoint={`GET /api/signals?limit=${SIGNAL_LIMIT}`}>
          {signals.ok ? (
            <>
              <StatGrid>
                <Stat label="Signals returned" value={integer(signals.value.count)} />
                <Stat label="Signals persisted in total" value={integer(signals.value.total)} />
                <Stat label="Offset" value={integer(signals.value.offset)} />
              </StatGrid>
              <div style={{ marginTop: 14 }}>
                <Table
                  head={["Created (UTC)", "Symbol", "Strategy", "TF", "Direction", "Confidence", "Score", "Entry", "Stop loss", "Take profit", "Status", "Executed", "Order ticket"]}
                  rows={signals.value.signals.map((signal) => [
                    cell(utcStamp(signal.createdAt)),
                    cell(signal.symbol),
                    cell(signal.strategy),
                    cell(signal.timeframe),
                    cell(signal.direction),
                    cell(fractionPercent(signal.confidence, 1)),
                    cell(integer(signal.score)),
                    cell(rawNumber(signal.entryPrice)),
                    cell(rawNumber(signal.stopLoss)),
                    cell(rawNumber(signal.takeProfit)),
                    <span key="status" title={signal.reason ?? undefined}>
                      {cell(signal.status)}
                    </span>,
                    cell(booleanLabel(signal.executed, "yes", "no")),
                    cell(signal.orderTicket),
                  ])}
                  empty="No signal has been persisted yet."
                  emptyHint="The market loop writes a signal row only once a strategy produces a candidate, so an empty list means no candidate has been recorded."
                />
              </div>
              <SourceNote>
                Hovering a status cell shows the reason the API stored for that signal. Priorities and confidence are the values
                the strategies produced; confidence is reported here as a share of 1.
              </SourceNote>
            </>
          ) : (
            <Unavailable error={sectionError(signals.error)} />
          )}
        </Panel>

        <Panel title="Recent trades" endpoint="GET /api/trades">
          {trades.ok ? (
            <>
              <StatGrid>
                <Stat label="Trades returned" value={integer(trades.value.length)} />
              </StatGrid>
              <div style={{ marginTop: 14 }}>
                <Table
                  head={["Trade id", "Symbol", "Status"]}
                  rows={trades.value.map((trade) => [cell(trade.tradeId), cell(trade.symbol), cell(trade.status)])}
                  empty="No trade has been persisted yet."
                  emptyHint="Trade rows are written when a signal is executed; the API returns at most the 200 newest."
                />
              </div>
              <SourceNote>
                <code>GET /api/trades</code> exposes only <code>trade_id</code>, <code>symbol</code> and <code>status</code>, so
                per-trade prices and P/L are not shown: the API does not return them for this route. Realized results are
                aggregated on <code>/api/performance</code> and <code>/api/statistics</code> above.
              </SourceNote>
            </>
          ) : (
            <Unavailable error={sectionError(trades.error)} />
          )}
        </Panel>

        <Panel title="Strategy registry" endpoint="GET /api/strategies">
          {strategies.ok ? (
            <>
              <StatGrid>
                <Stat label="Strategy names known to the bot" value={integer(strategies.value.strategies.length)} />
                <Stat label="Enabled" value={strategies.value.enabled.length === 0 ? "none" : strategies.value.enabled.join(", ")} />
                <Stat label="Ensemble combiner enabled" value={booleanLabel(strategies.value.ensembleEnabled, "yes", "no")} />
              </StatGrid>
              <div style={{ marginTop: 14 }}>
                <Table
                  head={["Name", "Kind", "Enabled", "Implemented", "Timeframe"]}
                  rows={strategies.value.details.map((detail) => [
                    cell(detail.name),
                    cell(detail.kind),
                    cell(booleanLabel(detail.enabled, "yes", "no")),
                    cell(booleanLabel(detail.implemented, "yes", "no")),
                    cell(detail.timeframe),
                  ])}
                  empty="The API reported no strategy names."
                  emptyHint="The registry is derived from the backend's strategy table; an empty answer would mean it could not be read."
                />
              </div>
              <SourceNote>
                &quot;Implemented&quot; is false for a known name the backend cannot run, and such a name never produces statistics.
              </SourceNote>
            </>
          ) : (
            <Unavailable error={sectionError(strategies.error)} />
          )}
        </Panel>

        <SourceNote>
          Not exposed by the API, therefore not shown: per-trade prices and results, the equity curve, the exposure and margin
          per open position, broker deal history, and any broker credential. This page also never offers trading controls —
          starting, stopping and emergency-resetting the bot require an authenticated POST from an operator, not from the
          dashboard.
        </SourceNote>
      </div>
    </main>
  );
}
