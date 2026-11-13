import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Bet } from "../lib/data";
import type { Freshness } from "../lib/freshness";
import type { BookTop } from "../lib/liveBook";
import { BetsTable } from "./BetsTable";

function bet(over: Partial<Bet> = {}): Bet {
  return {
    title: "Highest temperature in NYC",
    city: "NYC",
    venue: "polymarket",
    variable: "TMAX",
    slug: "nyc-high",
    bucket: "70-71",
    side: "YES",
    mu: 70.5,
    sigma: 2,
    nSources: 3,
    pWin: 0.75,
    ask: 0.6,
    edge: 0.15,
    shownEdge: 0.14,
    edgeBasis: "net",
    yesTokenId: "tok",
    station: "KNYC",
    ...over,
  };
}

const FRESH: Freshness = { ageLabel: "1h ago", stale: false };
const STALE: Freshness = { ageLabel: "5h ago", stale: true };

function render(
  bets: Bet[],
  tops: Record<string, BookTop | null>,
  fresh: Freshness | null = FRESH,
): string {
  return renderToStaticMarkup(
    createElement(BetsTable, { bets, tops: new Map(Object.entries(tops)), fresh }),
  );
}

describe("BetsTable", () => {
  it("renders the empty state", () => {
    expect(render([], {})).toContain("No bets pass the gates today.");
  });

  it("shows the live ask, its move from the stored ask, live net edge and status", () => {
    const html = render([bet()], { tok: { bestAsk: 0.58, bestBid: 0.5 } });
    expect(html).toContain("0.58");
    expect(html).toContain("-0.02");
    expect(html).toContain("clears the gate");
    expect(html).not.toContain("live price unavailable");
    expect(html).not.toContain("old forecast");
  });

  it("shows live price unavailable next to the stored ask and its age", () => {
    const html = render([bet()], { tok: null });
    expect(html).toContain("live price unavailable");
    expect(html).toContain("1h ago");
    expect(html).toContain("0.60");
    expect(html).not.toContain("clears the gate");
  });

  it("shows live price unavailable for a bet with no token", () => {
    const html = render([bet({ yesTokenId: null })], {});
    expect(html).toContain("live price unavailable");
  });

  it("shows a Kalshi row's stored price and age, with no status", () => {
    const html = render([bet({ venue: "kalshi", ask: 0.5, yesTokenId: null })], {});
    expect(html).toContain("0.50");
    expect(html).toContain("stored");
    expect(html).toContain("1h ago");
    expect(html).not.toContain("live price unavailable");
    expect(html).not.toContain("the gate");
  });

  it("marks a stale row that clears the gate as an old forecast", () => {
    const html = render([bet()], { tok: { bestAsk: 0.58, bestBid: 0.5 } }, STALE);
    expect(html).toContain("clears the gate");
    expect(html).toContain("old forecast");
  });

  it("does not show the positive colour on a stale row that clears", () => {
    const stale = render([bet()], { tok: { bestAsk: 0.58, bestBid: 0.5 } }, STALE);
    const at = stale.indexOf("clears the gate");
    const status = stale.slice(at - 120, at);
    expect(status).toContain("text-warn");
    expect(status).not.toContain("text-pos");
  });

  it("shows the positive colour on a fresh row that clears", () => {
    const fresh = render([bet()], { tok: { bestAsk: 0.58, bestBid: 0.5 } });
    const at = fresh.indexOf("clears the gate");
    expect(fresh.slice(at - 120, at)).toContain("text-pos");
  });

  it("treats an unknown run time as an old forecast", () => {
    const html = render([bet()], { tok: { bestAsk: 0.58, bestBid: 0.5 } }, null);
    expect(html).toContain("old forecast");
  });

  it.each([
    [0.8, "edge gone"],
    [0.68, "below the gate"],
    [0.4, "above the edge cap"],
    [0.58, "clears the gate"],
  ])("renders the status for a live ask of %s", (bestAsk, text) => {
    expect(render([bet()], { tok: { bestAsk, bestBid: 0.3 } })).toContain(text);
  });

  it("keeps the stored-edge order whatever the live prices do", () => {
    const first = bet({ title: "First market", yesTokenId: "a", shownEdge: 0.2 });
    const second = bet({ title: "Second market", yesTokenId: "b", shownEdge: 0.1 });
    // The second bet's live edge is far better, yet it stays second.
    const html = render([first, second], {
      a: { bestAsk: 0.74, bestBid: 0.7 },
      b: { bestAsk: 0.5, bestBid: 0.4 },
    });
    expect(html.indexOf("First market")).toBeLessThan(html.indexOf("Second market"));
  });
});
