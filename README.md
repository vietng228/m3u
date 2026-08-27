# VietMiTV dynamic stream Worker

The Worker keeps stable playlist URLs while resolving current upstream stream, DRM license, and logo URLs. The upstream playlist address is stored as a deployment secret and is not committed.

## Endpoints

- `/channel?group=VTVcab&name=ON%20SPORTS` resolves the current stream with a `302` redirect.
- `/channel?group=VTVcab&name=ON%20SPORTS&kind=license` resolves the current DRM license URL.
- `/channel?group=VTVcab&name=ON%20SPORTS&kind=logo` resolves the current logo URL.
- Add `&mode=proxy` to proxy the selected resource instead of redirecting.
- `/health` checks and parses the source playlist.
- `/debug?group=VTVcab&name=ON%20SPORTS` shows safe matching diagnostics without exposing signed tokens.
- `/private` relays a small set of unmatched legacy resources against secret, allowlisted base URLs; it never accepts an arbitrary host.

Matching always requires the normalized `group-title` and channel name. There is no name-only fallback. If duplicate normalized pairs exist, the last block in the source playlist wins.

## Deploy

```sh
pnpm install
pnpm test
pnpm check
pnpm deploy
```

The production deployment is `https://vietmitv-stream.viet-ng228.workers.dev`. Set `UPSTREAM_PLAYLIST_URL` in the environment before running `scripts/update.py`. It changes private upstream and signed channel resources to stable Worker URLs while preserving the playlist structure.
