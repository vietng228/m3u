import { describe, expect, it } from "vitest";
import { findLatestBlock, normalizeGroup, normalizeName, parseBlocks, resourceUrl } from "../worker/index";

const source = `#EXTM3U
#EXTINF:-1 group-title="VTV CAB",ON SPORTS
#KODIPROP:inputstream.adaptive.license_key=https://old.test/key
https://old.test/stream
#EXTINF:-1 group-title="VTVcab", ON SPORTS
#KODIPROP:inputstream.adaptive.license_key=https://new.test/key?token=secret
https://new.test/stream?token=secret
#EXTINF:-1 group-title="Other",ON SPORTS
https://wrong.test/stream
#EXTINF:-1 group-title="VTVcab",ON SPORTS+
https://plus.test/stream`;

describe("M3U matching", () => {
  it("normalizes accents, spacing and group punctuation", () => {
    expect(normalizeGroup("  Địa-Phương ")).toBe("diaphuong");
    expect(normalizeName(" ON  SPORTS+ ")).toBe("on sports plus");
  });

  it("requires both group and name and takes the latest exact normalized block", () => {
    const blocks = parseBlocks(source);
    const match = findLatestBlock(blocks, "VTV CAB", "ON SPORTS");
    expect(resourceUrl(match!, "stream")).toContain("new.test");
    expect(findLatestBlock(blocks, "Missing", "ON SPORTS")).toBeUndefined();
    expect(resourceUrl(findLatestBlock(blocks, "VTVcab", "ON SPORTS+")!, "stream")).toContain("plus.test");
  });

  it("extracts the current license URL independently", () => {
    const match = findLatestBlock(parseBlocks(source), "VTVcab", "ON SPORTS");
    expect(resourceUrl(match!, "license")).toBe("https://new.test/key?token=secret");
  });

  it("extracts a logo without exposing it in debug metadata", () => {
    const block = parseBlocks('#EXTINF:-1 group-title="G" tvg-logo="https://img.test/logo.png",N\nhttps://stream.test/live')[0]!;
    expect(resourceUrl(block, "logo")).toBe("https://img.test/logo.png");
  });
});
