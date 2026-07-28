#!/usr/bin/env bash
# Publish the podcast feed over Tailscale Funnel, token-protected.
#
# Needed for podcast apps that crawl feeds from their own servers (Overcast,
# Pocket Casts, Spotify) and therefore cannot reach a tailnet address.
#
# The feed becomes reachable from the internet but requires ?token=. Episode
# audio stays on the tailnet, so only titles ever leave the network. If the app
# turns out to need the audio publicly too, set PUBLIC_AUDIO=1.
#
# Ordering matters here: the image is rebuilt and token enforcement is proven
# locally BEFORE the funnel is opened. Exposing first and checking afterwards
# risks publishing an unprotected feed, however briefly.
#
# Prerequisites (once):
#   1. Tailscale admin > DNS > enable HTTPS Certificates
#   2. Tailscale admin > Access Controls > allow funnel:
#        "nodeAttrs": [{ "target": ["autogroup:member"], "attr": ["funnel"] }]
#   3. sudo tailscale set --operator=$USER
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-3402}"
PUBLIC_AUDIO="${PUBLIC_AUDIO:-0}"

if [[ ! -f .env ]]; then
  echo "error: .env not found; it must hold VOCAST_FEED_TOKEN" >&2
  exit 1
fi

# Read the token without exporting anything: values sourced now would shadow
# the file after it is rewritten below, and compose would see stale URLs.
FEED_TOKEN="$(sed -n 's/^VOCAST_FEED_TOKEN=//p' .env | head -1)"
if [[ -z "$FEED_TOKEN" ]]; then
  echo "error: VOCAST_FEED_TOKEN is not set in .env." >&2
  echo "       generate one with: openssl rand -hex 32" >&2
  exit 1
fi

HOSTNAME_TS="$(tailscale status --json | python3 -c \
  'import json,sys; print((json.load(sys.stdin)["Self"]["DNSName"]).rstrip("."))')"
TAILNET_IP="$(tailscale ip -4)"
PUBLIC_URL="https://${HOSTNAME_TS}"

if [[ "$PUBLIC_AUDIO" == "1" ]]; then
  AUDIO_BASE=""   # enclosures fall back to the public host
  AUDIO_NOTE="PUBLIC (reachable by anyone holding the token)"
else
  AUDIO_BASE="http://${TAILNET_IP}:${PORT}"
  AUDIO_NOTE="tailnet only (${AUDIO_BASE})"
fi

echo "host:    ${HOSTNAME_TS}"
echo "tailnet: ${TAILNET_IP}"
echo "audio:   ${AUDIO_NOTE}"
echo

python3 - "$PUBLIC_URL" "$AUDIO_BASE" <<'PY'
import re
import sys
from pathlib import Path

public, audio = sys.argv[1], sys.argv[2]
path = Path(".env")
body = path.read_text()


def upsert(text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    if re.search(rf"^{key}=.*$", text, re.M):
        return re.sub(rf"^{key}=.*$", line, text, flags=re.M)
    return text.rstrip("\n") + f"\n{line}\n"


body = upsert(body, "VOCAST_PUBLIC_BASE_URL", public)
body = upsert(body, "VOCAST_AUDIO_BASE_URL", audio)
path.write_text(body)
PY

# --build matters: `up -d` alone recreates the container from the existing
# image, so new code would silently not be running.
echo "building and restarting..."
docker compose up -d --build

echo "waiting for the service..."
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:${PORT}/api/health"; then
    break
  fi
  sleep 2
done

# Prove the token is enforced locally before opening the funnel.
echo "verifying token enforcement..."
check() {
  curl -s -o /dev/null --max-time 10 -w '%{http_code}' "$1"
}
no_token="$(check "http://127.0.0.1:${PORT}/feeds/all.xml")"
bad_token="$(check "http://127.0.0.1:${PORT}/feeds/all.xml?token=definitely-wrong")"
good_token="$(check "http://127.0.0.1:${PORT}/feeds/all.xml?token=${FEED_TOKEN}")"

if [[ "$no_token" != "401" || "$bad_token" != "401" || "$good_token" != "200" ]]; then
  echo >&2
  echo "ABORTING: feed token is not being enforced." >&2
  echo "  no token=${no_token} (want 401)" >&2
  echo "  bad token=${bad_token} (want 401)" >&2
  echo "  good token=${good_token} (want 200)" >&2
  echo "The funnel was NOT opened, so nothing is exposed." >&2
  exit 1
fi
echo "  ok: 401 without a token, 200 with it"

# The library, the phone reader and the API are for the tailnet. Prove the
# service itself refuses them from the internet before opening anything, so a
# mistake in the funnel paths below is not the only thing standing in the way.
echo "verifying the private surface is closed to the internet..."
funnel_check() {
  curl -s -o /dev/null --max-time 10 -H 'Tailscale-Funnel-Request: ?1' \
    -w '%{http_code}' "$1"
}
private_library="$(check "http://127.0.0.1:${PORT}/library")"
public_library="$(funnel_check "http://127.0.0.1:${PORT}/library?token=${FEED_TOKEN}")"
public_mobile="$(funnel_check "http://127.0.0.1:${PORT}/m?token=${FEED_TOKEN}")"
public_api="$(funnel_check "http://127.0.0.1:${PORT}/api/health")"

if [[ "$private_library" != "200" || "$public_library" != "404" ||
      "$public_mobile" != "404" || "$public_api" != "404" ]]; then
  echo >&2
  echo "ABORTING: the private surface is reachable from the internet." >&2
  echo "  tailnet library=${private_library} (want 200)" >&2
  echo "  internet library=${public_library} (want 404, even with a token)" >&2
  echo "  internet phone reader=${public_mobile} (want 404)" >&2
  echo "  internet api=${public_api} (want 404)" >&2
  echo "The funnel was NOT changed." >&2
  exit 1
fi
echo "  ok: library open on the tailnet, absent from the internet"

# Publish the podcast and nothing else: the feed documents, the audio they
# enclose, and the cover art, which is everything a podcast client fetches. The
# library, the phone reader and the API stay on the tailnet, so the service root
# is never published either.
tailscale funnel --https=443 off >/dev/null 2>&1 || true
tailscale funnel --bg --https=443 --set-path=/feeds \
  "http://127.0.0.1:${PORT}/feeds" >/dev/null
tailscale funnel --bg --https=443 --set-path=/feed.xml \
  "http://127.0.0.1:${PORT}/feed.xml" >/dev/null
tailscale funnel --bg --https=443 --set-path=/cover.jpg \
  "http://127.0.0.1:${PORT}/cover.jpg" >/dev/null
if [[ "$PUBLIC_AUDIO" == "1" ]]; then
  tailscale funnel --bg --https=443 --set-path=/audio \
    "http://127.0.0.1:${PORT}/audio" >/dev/null
fi
echo

echo "Subscribe with this URL (treat it as a password):"
echo "  ${PUBLIC_URL}/feeds/all.xml?token=${FEED_TOKEN}"
echo
echo "Revoke:        change VOCAST_FEED_TOKEN in .env, then re-run this script."
echo "Take offline:  tailscale funnel --https=443 off"
