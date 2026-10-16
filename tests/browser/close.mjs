// Finish CDP's WebSocket close handshake before Node tears down its native transport.
export async function closeSocket(ws) {
  if (ws.readyState === 3) return;
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("CDP WebSocket did not close within 3 seconds")), 3000);
    const done = () => { clearTimeout(timer); resolve(); };
    ws.addEventListener("close", done, { once: true }); ws.close();
    if (ws.readyState === 3) done();
  });
}
