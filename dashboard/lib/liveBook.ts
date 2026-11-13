import type { Bet } from "./data";
import { shownEdge } from "./fees";
import type { Freshness } from "./freshness";
import { gateStatus, type GateStatus } from "./gate";

// Public, read-only Polymarket order book. No credentials, no POST.
const BOOK_URL = "https://clob.polymarket.com/book";

export type BookTop = { bestAsk: number | null; bestBid: number | null };

type Level = { price?: unknown };

// Prices arrive as strings. Anything not finite or outside 0 to 1 is dropped.
function validPrices(levels: unknown): number[] {
  if (!Array.isArray(levels)) return [];
  const out: number[] = [];
  for (const l of levels as Level[]) {
    const p = typeof l?.price === "string" || typeof l?.price === "number" ? Number(l.price) : NaN;
    if (Number.isFinite(p) && p >= 0 && p <= 1) out.push(p);
  }
  return out;
}

// Levels arrive worst first, so best ask is the minimum and best bid the
// maximum, never a position. A book whose asset_id is not the requested token
// is rejected, as in the Python book client.
export function parseBookTop(json: unknown, tokenId: string): BookTop | null {
  if (typeof json !== "object" || json === null) return null;
  const book = json as { asset_id?: unknown; asks?: unknown; bids?: unknown };
  if (book.asset_id !== tokenId) return null;
  const asks = validPrices(book.asks);
  const bids = validPrices(book.bids);
  return {
    bestAsk: asks.length > 0 ? Math.min(...asks) : null,
    bestBid: bids.length > 0 ? Math.max(...bids) : null,
  };
}

export type FetchBookOptions = {
  fetchFn?: typeof fetch;
  concurrency?: number;
  deadlineMs?: number;
};

// One GET per distinct token, at most `concurrency` at once, all bounded by one
// deadline for the whole batch. Never rejects: a failure of any kind is null
// for that token. The deadline is a timer race as well as an abort, so it holds
// even if a fetch never settles.
export async function fetchBookTops(
  tokenIds: string[],
  { fetchFn = fetch, concurrency = 6, deadlineMs = 3000 }: FetchBookOptions = {},
): Promise<Map<string, BookTop | null>> {
  const ids = [...new Set(tokenIds)];
  const results = new Map<string, BookTop | null>(ids.map((id) => [id, null]));
  if (ids.length === 0) return results;

  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<void>((resolve) => {
    timer = setTimeout(() => {
      controller.abort();
      resolve();
    }, deadlineMs);
  });

  let next = 0;
  const worker = async () => {
    while (next < ids.length && !controller.signal.aborted) {
      const id = ids[next++];
      try {
        const res = await fetchFn(`${BOOK_URL}?token_id=${encodeURIComponent(id)}`, {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!res.ok) continue;
        results.set(id, parseBookTop(await res.json(), id));
      } catch {
        // timeout, network error or bad JSON: this token stays null
      }
    }
  };

  await Promise.race([
    Promise.all(Array.from({ length: Math.min(concurrency, ids.length) }, worker)),
    deadline,
  ]);
  clearTimeout(timer);
  // Copy so a fetch that settles after the deadline cannot change what was returned.
  return new Map(results);
}

export type LiveView = {
  // live: priced off the order book. stored: Kalshi, stored ask only.
  // unavailable: Polymarket with no usable live price, stored ask shown.
  source: "live" | "stored" | "unavailable";
  ask: number | null;
  // Live ask minus the stored ask; null with no stored ask or no live price.
  move: number | null;
  netEdge: number | null;
  status: GateStatus | null;
  // The forecast behind the row is older than the stale limit (or of unknown age).
  oldForecast: boolean;
  ageLabel: string;
};

// The ask we would pay now: YES at the best ask, NO at the complement of the
// best bid. Must be strictly between 0 and 1, like the Python no_ask.
function liveAskFor(bet: Bet, top: BookTop | null): number | null {
  if (top === null) return null;
  const ask = bet.side === "YES" ? top.bestAsk : top.bestBid === null ? null : 1 - top.bestBid;
  return ask !== null && ask > 0 && ask < 1 ? ask : null;
}

export function livePrice(bet: Bet, top: BookTop | null, fresh: Freshness | null): LiveView {
  const oldForecast = fresh?.stale ?? true;
  const ageLabel = fresh?.ageLabel ?? "age unknown";
  const stored = {
    ask: bet.ask,
    move: null,
    netEdge: null,
    status: null,
    oldForecast,
    ageLabel,
  };
  if (bet.venue !== "polymarket") return { source: "stored", ...stored };
  const liveAsk = bet.yesTokenId === null ? null : liveAskFor(bet, top);
  if (liveAsk === null) return { source: "unavailable", ...stored };

  // pWin is already the NO probability on a NO row.
  const gross = bet.pWin - liveAsk;
  const net = shownEdge(gross, liveAsk, "polymarket").value;
  return {
    source: "live",
    ask: liveAsk,
    move: bet.ask === null ? null : liveAsk - bet.ask,
    netEdge: net,
    status: gateStatus(gross, net, bet.station, bet.variable),
    oldForecast,
    ageLabel,
  };
}
