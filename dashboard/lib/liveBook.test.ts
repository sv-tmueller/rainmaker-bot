import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { Bet } from "./data";
import { MAX_EDGE } from "./gate";
import { shownEdge } from "./fees";
import { fetchBookTops, livePrice, parseBookTop, type BookTop } from "./liveBook";

function fixture(name: string): Record<string, unknown> {
  return JSON.parse(readFileSync(new URL(`../../tests/fixtures/${name}`, import.meta.url), "utf8"));
}

const YES_BOOK = fixture("clob_book_nyc_7071_yes.json");
const EMPTY_BOOK = fixture("clob_book_empty.json");
const THIN_BOOK = fixture("clob_book_trade_thin.json");
const TOKEN = YES_BOOK.asset_id as string;

describe("parseBookTop", () => {
  it("takes the lowest ask and highest bid, not the level at either end", () => {
    // Levels arrive worst first for asks, and bids are ascending in this fixture.
    const top = parseBookTop(THIN_BOOK, "tok");
    expect(top).toBeNull(); // placeholder asset_id does not match
    const matched = parseBookTop({ ...THIN_BOOK, asset_id: "tok" }, "tok");
    expect(matched).toEqual({ bestAsk: 0.58, bestBid: 0.5 });
  });

  it("reads the saved YES book", () => {
    expect(parseBookTop(YES_BOOK, TOKEN)).toEqual({ bestAsk: 0.999, bestBid: 0.996 });
  });

  it("gives null on both sides for an empty book", () => {
    expect(parseBookTop(EMPTY_BOOK, TOKEN)).toEqual({ bestAsk: null, bestBid: null });
  });

  it("returns null when asset_id is not the requested token", () => {
    expect(parseBookTop(YES_BOOK, "some-other-token")).toBeNull();
  });

  it("drops prices that are not finite or outside 0 to 1", () => {
    const book = {
      asset_id: "t",
      asks: [{ price: "abc" }, { price: "1.5" }, { price: "-0.1" }, { price: "0.7" }],
      bids: [{ price: "NaN" }, { price: "2" }],
    };
    expect(parseBookTop(book, "t")).toEqual({ bestAsk: 0.7, bestBid: null });
  });

  it("returns null for a non-object body", () => {
    expect(parseBookTop(null, "t")).toBeNull();
    expect(parseBookTop("x", "t")).toBeNull();
    expect(parseBookTop({ asset_id: "t" }, "t")).toEqual({ bestAsk: null, bestBid: null });
  });
});

function ok(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200 });
}

describe("fetchBookTops", () => {
  it("fetches each token and returns its top", async () => {
    const fetchFn = async () => ok(YES_BOOK);
    const out = await fetchBookTops([TOKEN], { fetchFn });
    expect(out.get(TOKEN)).toEqual({ bestAsk: 0.999, bestBid: 0.996 });
  });

  it("makes only read-only GETs to the public book endpoint", async () => {
    const calls: { url: string; init: RequestInit | undefined }[] = [];
    const fetchFn = async (url: string | URL | Request, init?: RequestInit) => {
      calls.push({ url: String(url), init });
      return ok(YES_BOOK);
    };
    await fetchBookTops([TOKEN], { fetchFn });
    expect(calls).toHaveLength(1);
    const u = new URL(calls[0].url);
    expect(`${u.origin}${u.pathname}`).toBe("https://clob.polymarket.com/book");
    expect([...u.searchParams.keys()]).toEqual(["token_id"]);
    expect(u.searchParams.get("token_id")).toBe(TOKEN);
    expect(calls[0].init?.method ?? "GET").toBe("GET");
    expect(calls[0].init?.body).toBeUndefined();
    expect(calls[0].init?.headers).toBeUndefined();
    expect(calls[0].init?.cache).toBe("no-store");
  });

  it("fetches a duplicate id once", async () => {
    let n = 0;
    const fetchFn = async () => {
      n++;
      return ok(YES_BOOK);
    };
    await fetchBookTops([TOKEN, TOKEN, TOKEN], { fetchFn });
    expect(n).toBe(1);
  });

  it("gives null for a 404", async () => {
    const fetchFn = async () => new Response("{}", { status: 404 });
    expect((await fetchBookTops([TOKEN], { fetchFn })).get(TOKEN)).toBeNull();
  });

  it("gives null for a rejected fetch", async () => {
    const fetchFn = async () => {
      throw new Error("network down");
    };
    expect((await fetchBookTops([TOKEN], { fetchFn })).get(TOKEN)).toBeNull();
  });

  it("gives null for a synchronous throw", async () => {
    const fetchFn = (() => {
      throw new Error("sync boom");
    }) as unknown as typeof fetch;
    expect((await fetchBookTops([TOKEN], { fetchFn })).get(TOKEN)).toBeNull();
  });

  it("gives null for invalid JSON", async () => {
    const fetchFn = async () => new Response("<html>", { status: 200 });
    expect((await fetchBookTops([TOKEN], { fetchFn })).get(TOKEN)).toBeNull();
  });

  it("gives null for a token mismatch", async () => {
    const fetchFn = async () => ok(YES_BOOK);
    expect((await fetchBookTops(["other"], { fetchFn })).get("other")).toBeNull();
  });

  it("resolves at the deadline when a fetch never settles", async () => {
    const fetchFn = (() => new Promise<Response>(() => {})) as unknown as typeof fetch;
    const started = Date.now();
    const out = await fetchBookTops([TOKEN], { fetchFn, deadlineMs: 30 });
    expect(out.get(TOKEN)).toBeNull();
    expect(Date.now() - started).toBeLessThan(1000);
  });

  it("keeps results that finished before the deadline", async () => {
    const fetchFn = ((url: string) =>
      String(url).includes("slow")
        ? new Promise<Response>(() => {})
        : Promise.resolve(ok({ ...YES_BOOK, asset_id: "fast" }))) as unknown as typeof fetch;
    const out = await fetchBookTops(["fast", "slow"], { fetchFn, deadlineMs: 30 });
    expect(out.get("fast")).toEqual({ bestAsk: 0.999, bestBid: 0.996 });
    expect(out.get("slow")).toBeNull();
  });

  it("never runs more requests at once than the limit", async () => {
    let inFlight = 0;
    let peak = 0;
    const fetchFn = async (url: string | URL | Request) => {
      inFlight++;
      peak = Math.max(peak, inFlight);
      await new Promise((r) => setTimeout(r, 5));
      inFlight--;
      const id = new URL(String(url)).searchParams.get("token_id");
      return ok({ ...YES_BOOK, asset_id: id });
    };
    const ids = Array.from({ length: 15 }, (_, i) => `t${i}`);
    const out = await fetchBookTops(ids, { fetchFn, concurrency: 4 });
    expect(peak).toBe(4);
    expect(out.size).toBe(15);
    expect(out.get("t14")).toEqual({ bestAsk: 0.999, bestBid: 0.996 });
  });

  it("returns an empty map for no ids without fetching", async () => {
    let n = 0;
    const fetchFn = async () => {
      n++;
      return ok(YES_BOOK);
    };
    expect((await fetchBookTops([], { fetchFn })).size).toBe(0);
    expect(n).toBe(0);
  });
});

function bet(over: Partial<Bet> = {}): Bet {
  return {
    title: "Highest temperature in NYC",
    city: "NYC",
    venue: "polymarket",
    variable: "TMAX",
    slug: "s",
    bucket: "70-71",
    side: "YES",
    mu: 70,
    sigma: 2,
    nSources: 3,
    pWin: 0.75,
    ask: 0.6,
    edge: 0.15,
    shownEdge: 0.13,
    edgeBasis: "net",
    yesTokenId: "tok",
    station: "KNYC",
    ...over,
  };
}

const FRESH = { ageLabel: "1h ago", stale: false };
const STALE = { ageLabel: "5h ago", stale: true };

describe("livePrice", () => {
  it("prices a YES bet off the live best ask and computes net edge", () => {
    const top: BookTop = { bestAsk: 0.58, bestBid: 0.5 };
    const v = livePrice(bet(), top, FRESH);
    expect(v.source).toBe("live");
    expect(v.ask).toBe(0.58);
    expect(v.move).toBeCloseTo(-0.02, 10);
    expect(v.netEdge).toBeCloseTo(shownEdge(0.75 - 0.58, 0.58, "polymarket").value, 10);
    expect(v.status).toBe("clears the gate");
    expect(v.oldForecast).toBe(false);
  });

  it("prices a NO bet off the complement of the best bid and never uses 1 - pWin", () => {
    const top: BookTop = { bestAsk: 0.999, bestBid: 0.996 };
    const v = livePrice(bet({ side: "NO", pWin: 0.9, ask: 0.01 }), top, FRESH);
    expect(v.ask).toBeCloseTo(0.004, 10);
    const gross = 0.9 - 0.004;
    expect(v.netEdge).toBeCloseTo(shownEdge(gross, 0.004, "polymarket").value, 10);
  });

  it("flags edge gone when the live ask removes the edge", () => {
    const v = livePrice(bet(), { bestAsk: 0.8, bestBid: 0.7 }, FRESH);
    expect(v.status).toBe("edge gone");
  });

  it("flags below the gate when edge is positive but under the floor", () => {
    const v = livePrice(bet(), { bestAsk: 0.68, bestBid: 0.6 }, FRESH);
    expect(v.status).toBe("below the gate");
  });

  it("flags above the edge cap on gross edge over the cap", () => {
    const ask = 0.75 - MAX_EDGE - 0.05;
    const v = livePrice(bet(), { bestAsk: ask, bestBid: 0.2 }, FRESH);
    expect(v.status).toBe("above the edge cap");
  });

  it("applies the KSFO TMAX floor", () => {
    // gross 0.16: net is about 0.14, above 0.12 but under 0.17.
    const v = livePrice(bet({ station: "KSFO", pWin: 0.76 }), { bestAsk: 0.6, bestBid: 0.5 }, FRESH);
    expect(v.status).toBe("below the gate");
    const other = livePrice(bet({ station: "KNYC", pWin: 0.76 }), { bestAsk: 0.6, bestBid: 0.5 }, FRESH);
    expect(other.status).toBe("clears the gate");
  });

  it("marks an old forecast without changing the status", () => {
    const v = livePrice(bet(), { bestAsk: 0.58, bestBid: 0.5 }, STALE);
    expect(v.status).toBe("clears the gate");
    expect(v.oldForecast).toBe(true);
  });

  it("treats an unknown run time as an old forecast", () => {
    expect(livePrice(bet(), { bestAsk: 0.58, bestBid: 0.5 }, null).oldForecast).toBe(true);
  });

  it("falls back to the stored ask when there is no book", () => {
    const v = livePrice(bet(), null, FRESH);
    expect(v.source).toBe("unavailable");
    expect(v.ask).toBe(0.6);
    expect(v.status).toBeNull();
    expect(v.netEdge).toBeNull();
    expect(v.move).toBeNull();
    expect(v.ageLabel).toBe("1h ago");
  });

  it("falls back when the needed side of the book is empty", () => {
    const yes = livePrice(bet(), { bestAsk: null, bestBid: 0.5 }, FRESH);
    expect(yes.source).toBe("unavailable");
    const no = livePrice(bet({ side: "NO" }), { bestAsk: 0.6, bestBid: null }, FRESH);
    expect(no.source).toBe("unavailable");
  });

  it("falls back when the live ask is not strictly between 0 and 1", () => {
    expect(livePrice(bet(), { bestAsk: 1, bestBid: 0.9 }, FRESH).source).toBe("unavailable");
    expect(livePrice(bet({ side: "NO" }), { bestAsk: 0.6, bestBid: 1 }, FRESH).source).toBe(
      "unavailable",
    );
    expect(livePrice(bet({ side: "NO" }), { bestAsk: 0.6, bestBid: 0 }, FRESH).source).toBe(
      "unavailable",
    );
  });

  it("falls back for a Polymarket bet with no token even if a book is given", () => {
    const v = livePrice(bet({ yesTokenId: null }), { bestAsk: 0.5, bestBid: 0.4 }, FRESH);
    expect(v.source).toBe("unavailable");
  });

  it("has no move when there is no stored ask", () => {
    const v = livePrice(bet({ ask: null }), { bestAsk: 0.58, bestBid: 0.5 }, FRESH);
    expect(v.source).toBe("live");
    expect(v.move).toBeNull();
  });

  it("shows the stored ask and age for a Kalshi bet, with no status", () => {
    const v = livePrice(bet({ venue: "kalshi", yesTokenId: null }), null, FRESH);
    expect(v.source).toBe("stored");
    expect(v.ask).toBe(0.6);
    expect(v.status).toBeNull();
    expect(v.ageLabel).toBe("1h ago");
  });

  it("ignores a book for a Kalshi bet", () => {
    const v = livePrice(bet({ venue: "kalshi" }), { bestAsk: 0.1, bestBid: 0.05 }, FRESH);
    expect(v.source).toBe("stored");
    expect(v.ask).toBe(0.6);
  });

  it("uses age unknown when the run time is missing", () => {
    expect(livePrice(bet(), null, null).ageLabel).toBe("age unknown");
  });
});
