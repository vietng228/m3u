const SOURCE_CACHE_SECONDS = 15;
const SOURCE_TIMEOUT_MS = 8_000;
const STREAM_TIMEOUT_MS = 12_000;
const MAX_SOURCE_BYTES = 2 * 1024 * 1024;

export type ChannelBlock = {
  extinf: string;
  group: string;
  name: string;
  lines: string[];
};

type ResourceKind = "stream" | "license" | "logo";
type SecretEnv = Env & {
  UPSTREAM_PLAYLIST_URL: string;
  PRIVATE_ASSET_BASE_URL: string;
};

function json(data: unknown, status = 200, extraHeaders: HeadersInit = {}): Response {
  return Response.json(data, {
    status,
    headers: {
      "cache-control": "no-store",
      "access-control-allow-origin": "*",
      ...extraHeaders,
    },
  });
}

function stripMarks(value: string): string {
  return value.normalize("NFD").replace(/[\u0300-\u036f]/g, "");
}

export function normalizeText(value: string, preservePlus = false): string {
  let text = stripMarks(value.trim().toLowerCase().replaceAll("đ", "d"));
  text = text.replaceAll("&", "and");
  if (preservePlus) text = text.replaceAll("+", " plus ");
  return text.replace(/[^a-z0-9]+/g, " ").replace(/\s+/g, " ").trim();
}

export function normalizeGroup(value: string): string {
  // Group labels frequently vary only by spacing ("VTV CAB" vs "VTVcab").
  return normalizeText(value).replaceAll(" ", "");
}

export function normalizeName(value: string): string {
  return normalizeText(value, true);
}

export function parseBlocks(text: string): ChannelBlock[] {
  const blocks: ChannelBlock[] = [];
  let current: string[] | undefined;

  for (const rawLine of text.replaceAll("\r\n", "\n").replaceAll("\r", "\n").split("\n")) {
    const line = rawLine.trim();
    if (line.startsWith("#EXTINF")) {
      if (current) blocks.push(toBlock(current));
      current = [line];
    } else if (current && line) {
      current.push(line);
    }
  }
  if (current) blocks.push(toBlock(current));
  return blocks;
}

function toBlock(lines: string[]): ChannelBlock {
  const extinf = lines[0] ?? "";
  const group = /group-title\s*=\s*"([^"]*)"/i.exec(extinf)?.[1]?.trim() ?? "";
  const comma = extinf.lastIndexOf(",");
  const name = comma >= 0 ? extinf.slice(comma + 1).trim() : "";
  return { extinf, group, name, lines };
}

export function findLatestBlock(blocks: ChannelBlock[], group: string, name: string): ChannelBlock | undefined {
  const wantedGroup = normalizeGroup(group);
  const wantedName = normalizeName(name);
  let match: ChannelBlock | undefined;
  for (const block of blocks) {
    if (normalizeGroup(block.group) === wantedGroup && normalizeName(block.name) === wantedName) {
      match = block;
    }
  }
  return match;
}

function extractUrl(value: string): string | undefined {
  const match = /https?:\/\/[^\s|]+/i.exec(value);
  return match?.[0];
}

export function resourceUrl(block: ChannelBlock, kind: ResourceKind): string | undefined {
  if (kind === "logo") {
    return /tvg-logo\s*=\s*"(https?:\/\/[^"]+)"/i.exec(block.extinf)?.[1];
  }
  if (kind === "license") {
    for (let index = block.lines.length - 1; index >= 1; index -= 1) {
      const line = block.lines[index] ?? "";
      if (/^#KODIPROP:inputstream\.adaptive\.license_key=/i.test(line)) return extractUrl(line);
    }
    return undefined;
  }

  for (let index = block.lines.length - 1; index >= 1; index -= 1) {
    const line = block.lines[index] ?? "";
    if (!line.startsWith("#")) return extractUrl(line);
  }
  return undefined;
}

async function readLimitedText(response: Response): Promise<string> {
  const contentLength = Number(response.headers.get("content-length") ?? "0");
  if (contentLength > MAX_SOURCE_BYTES) throw new Error("source_too_large");
  if (!response.body) return "";

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_SOURCE_BYTES) throw new Error("source_too_large");
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const combined = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    combined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(combined);
}

async function fetchSource(requestUrl: URL, env: SecretEnv): Promise<{ text: string; cache: "HIT" | "MISS" }> {
  const cache = caches.default;
  const cacheKey = new Request(`${requestUrl.origin}/__source-cache-v1`, { method: "GET" });
  const cached = await cache.match(cacheKey);
  if (cached) return { text: await readLimitedText(cached), cache: "HIT" };

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), SOURCE_TIMEOUT_MS);
  let upstream: Response;
  try {
    if (!env.UPSTREAM_PLAYLIST_URL) throw new Error("upstream_not_configured");
    upstream = await fetch(env.UPSTREAM_PLAYLIST_URL, {
      headers: { accept: "application/x-mpegURL,text/plain;q=0.9,*/*;q=0.1", "user-agent": "VietMiTV-Worker/1.0" },
      redirect: "follow",
      signal: controller.signal,
    });
  } finally {
    clearTimeout(timeout);
  }
  if (!upstream.ok) throw new Error(`source_http_${upstream.status}`);
  const text = await readLimitedText(upstream);
  if (!text.includes("#EXTINF")) throw new Error("source_invalid_m3u");

  const cacheResponse = new Response(text, {
    headers: { "content-type": "application/x-mpegURL; charset=utf-8", "cache-control": `public, max-age=${SOURCE_CACHE_SECONDS}` },
  });
  await cache.put(cacheKey, cacheResponse);
  return { text, cache: "MISS" };
}

function safeTarget(rawUrl: string | URL, requestUrl: URL): URL {
  const target = new URL(rawUrl);
  if (target.protocol !== "http:" && target.protocol !== "https:") throw new Error("unsupported_target_protocol");
  if (target.hostname === requestUrl.hostname) throw new Error("redirect_loop");
  return target;
}

async function proxy(target: URL, request: Request): Promise<Response> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), STREAM_TIMEOUT_MS);
  let upstream: Response;
  try {
    const headers = new Headers();
    for (const name of [
      "accept",
      "accept-language",
      "authorization",
      "content-type",
      "range",
      "user-agent",
      "origin",
      "referer",
    ]) {
      const value = request.headers.get(name);
      if (value) headers.set(name, value);
    }
    upstream = await fetch(target, {
      method: request.method,
      headers,
      body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
      redirect: "follow",
      signal: controller.signal,
    });
  } finally {
    clearTimeout(timeout);
  }
  const headers = new Headers(upstream.headers);
  headers.set("access-control-allow-origin", "*");
  headers.set("cache-control", "no-store");
  headers.delete("set-cookie");
  return new Response(request.method === "HEAD" ? null : upstream.body, { status: upstream.status, headers });
}

function privateTarget(requestUrl: URL, env: SecretEnv): URL {
  const baseName = requestUrl.searchParams.get("base");
  const relativePath = requestUrl.searchParams.get("path") ?? "";
  if (!relativePath.startsWith("/") || relativePath.includes("..") || relativePath.includes("\\")) {
    throw new Error("invalid_private_path");
  }
  const base = baseName === "asset" ? env.PRIVATE_ASSET_BASE_URL : new URL(env.UPSTREAM_PLAYLIST_URL).origin;
  if (!base) throw new Error("upstream_not_configured");
  return new URL(relativePath, `${base.replace(/\/$/, "")}/`);
}

async function privateRelay(request: Request, requestUrl: URL, env: SecretEnv): Promise<Response> {
  const target = safeTarget(privateTarget(requestUrl, env), requestUrl);
  if (requestUrl.searchParams.get("mode") === "proxy") return proxy(target, request);
  return new Response(null, {
    status: 302,
    headers: { location: target.toString(), "cache-control": "no-store", "access-control-allow-origin": "*" },
  });
}

async function resolveChannel(request: Request, requestUrl: URL, env: SecretEnv): Promise<Response> {
  const group = requestUrl.searchParams.get("group")?.trim() ?? "";
  const name = requestUrl.searchParams.get("name")?.trim() ?? "";
  const requestedKind = requestUrl.searchParams.get("kind");
  const kind: ResourceKind = requestedKind === "license" || requestedKind === "logo" ? requestedKind : "stream";
  if (!group || !name) return json({ ok: false, error: "missing_group_or_name" }, 400);

  const source = await fetchSource(requestUrl, env);
  const block = findLatestBlock(parseBlocks(source.text), group, name);
  if (!block) return json({ ok: false, error: "channel_not_found", group, name }, 404, { "x-vietmitv-source-cache": source.cache });
  const rawTarget = resourceUrl(block, kind);
  if (!rawTarget) return json({ ok: false, error: `${kind}_not_found`, group: block.group, name: block.name }, 404);
  const target = safeTarget(rawTarget, requestUrl);

  // DRM clients send the license challenge as POST, so licenses must be
  // proxied. Streams keep redirect semantics because HLS/DASH manifests can
  // contain relative segment paths that would break when served at /channel.
  if (kind === "license" || requestUrl.searchParams.get("mode") === "proxy") {
    return proxy(target, request);
  }
  return new Response(null, {
    status: 302,
    headers: {
      location: target.toString(),
      "cache-control": "no-store",
      "access-control-allow-origin": "*",
      "x-vietmitv-source-cache": source.cache,
    },
  });
}

async function health(requestUrl: URL, env: SecretEnv): Promise<Response> {
  const started = Date.now();
  const source = await fetchSource(requestUrl, env);
  const blocks = parseBlocks(source.text);
  return json({ ok: true, source: "reachable", blocks: blocks.length, cache: source.cache, elapsedMs: Date.now() - started });
}

async function debug(requestUrl: URL, env: SecretEnv): Promise<Response> {
  const group = requestUrl.searchParams.get("group")?.trim() ?? "";
  const name = requestUrl.searchParams.get("name")?.trim() ?? "";
  if (!group || !name) return json({ ok: false, error: "missing_group_or_name" }, 400);
  const source = await fetchSource(requestUrl, env);
  const block = findLatestBlock(parseBlocks(source.text), group, name);
  const stream = block ? resourceUrl(block, "stream") : undefined;
  const license = block ? resourceUrl(block, "license") : undefined;
  return json({
    ok: Boolean(block),
    requested: { group, name, normalizedGroup: normalizeGroup(group), normalizedName: normalizeName(name) },
    matched: block ? { group: block.group, name: block.name } : null,
    resources: { stream: Boolean(stream), license: Boolean(license), logo: Boolean(block && resourceUrl(block, "logo")) },
    cache: source.cache,
  }, block ? 200 : 404);
}

export default {
  async fetch(request: Request, env: SecretEnv): Promise<Response> {
    const requestUrl = new URL(request.url);
    try {
      const isChannelPost = request.method === "POST" && requestUrl.pathname === "/channel" && requestUrl.searchParams.get("kind") === "license";
      if (request.method !== "GET" && request.method !== "HEAD" && !isChannelPost) {
        return json({ ok: false, error: "method_not_allowed" }, 405, { allow: "GET, HEAD, POST" });
      }
      if (requestUrl.pathname === "/" || requestUrl.pathname === "/health") return health(requestUrl, env);
      if (requestUrl.pathname === "/debug") return debug(requestUrl, env);
      if (requestUrl.pathname === "/channel") return resolveChannel(request, requestUrl, env);
      if (requestUrl.pathname === "/private") return privateRelay(request, requestUrl, env);
      return json({ ok: false, error: "not_found" }, 404);
    } catch (error) {
      const message = error instanceof Error ? error.message : "unknown_error";
      console.error(JSON.stringify({ event: "request_failed", path: requestUrl.pathname, error: message }));
      if (message === "redirect_loop" || message === "unsupported_target_protocol") return json({ ok: false, error: message }, 502);
      if (error instanceof DOMException && error.name === "AbortError") return json({ ok: false, error: "upstream_timeout" }, 504);
      return json({ ok: false, error: "upstream_unavailable", detail: message }, 502);
    }
  },
} satisfies ExportedHandler<SecretEnv>;
