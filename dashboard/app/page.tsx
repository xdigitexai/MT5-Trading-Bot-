async function getHealth() {
  try { return await (await fetch(`${process.env.API_URL ?? "http://localhost:8000"}/api/health`, { cache: "no-store" })).json(); }
  catch { return { status: "offline", mt5: "API unavailable" }; }
}
export default async function Dashboard() {
  const health = await getHealth();
  return <main style={{fontFamily:"Arial",maxWidth:1000,margin:"48px auto",background:"#0b1220",color:"#e5e7eb",padding:32}}>
    <h1>MT5 Forex Bot</h1><p>Mode: <b>{health.mode ?? "unknown"}</b> · MT5: {health.mt5}</p>
    <p style={{color:"#fbbf24"}}>This dashboard never contains broker credentials. Trading controls require an authenticated backend request.</p>
    <section style={{display:"grid",gridTemplateColumns:"repeat(3,1fr)",gap:16}}>{["Account equity","Today’s P/L","Open trades","Drawdown","Risk exposure","Bot status"].map(x => <div key={x} style={{background:"#111c31",padding:20,borderRadius:8}}>{x}<br/><b>Awaiting backend data</b></div>)}</section>
  </main>
}
