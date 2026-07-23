# Jarvis Companion (iOS) — where this stands

Built 2026-07-22. A native iOS chat app that talks directly to your existing
`brain.respond()` on the Mac through `webhook.py` — same handling (tool calls,
empty-promise guard, budget caps) as anything typed into Telegram. Nothing
about the Telegram bot changed or was removed; this is additive.

**Status: builds clean, runs in the iOS Simulator, verified end-to-end against
the real running `jarvis` instance** (health check + a real `/capture` message
round-tripped through the actual Claude-backed brain — see chat log 2026-07-22
for the proof). Zero dollars spent so far.

## What's actually in this folder
- `project.yml` — plain-text spec (XcodeGen) that generates the real Xcode
  project. Safe to hand-edit and regenerate (`xcodegen generate`) rather than
  fighting Xcode's project file format directly.
- `JarvisCompanion/Sources/` — all the Swift/SwiftUI code: a chat screen, a
  Settings screen (server address + shared secret, stored in the iOS Keychain,
  never in plain text), and the networking layer.
- `JarvisCompanion.xcodeproj` — generated; regenerate anytime with
  `cd ios-app && xcodegen generate` after editing `project.yml`.

## Backend change made alongside this (already deployed)
- `webhook.py` gained a `/health` endpoint (same shared-secret check as
  `/capture`) so the app can show a live Connected/Not‑reachable indicator.
- **Real bug found and fixed during this session:** jarvis's webhook port 8765
  collided with an unrelated process (`umbrella`'s dashboard server, a
  different project on this Mac) — the wrong process was silently answering
  requests to `127.0.0.1:8765`. Moved jarvis's webhook to **port 8766** in
  `instances/jarvis/.env` (`WEBHOOK_PORT=8766`) and restarted both `jarvis`
  and `penny` to load the new code. Confirmed via `lsof` that 8766 is
  exclusively jarvis's now.

## Xcode update — done, with a real finding worth knowing (2026-07-22)

This Mac is a 2018 13" MacBook Pro (`MacBookPro15,2`, Intel), which Apple has
cut off from macOS Tahoe (26) entirely — confirmed via Apple's own supported-
device list. That looked like a hard wall at first: Apple now requires
**Xcode 26 + the iOS 26 SDK for any TestFlight/App Store submission**
(mandatory since 2026-04-28), and the *latest* Xcode (26.6 as of this
writing) requires macOS 26.2, which this Mac can never run.

**The actual fix: `Xcode-26.1.app` (an earlier 26.x point release) installs
and runs fine on macOS Sequoia** — no Tahoe required for that specific
version. Installed alongside the old `Xcode.app` (13.1) rather than
replacing it. Verified for real: `xcodebuild -version` reports Xcode 26.1
with the iOS 26.1 SDK, and the app **builds clean** under it
(`BUILD SUCCEEDED`, zero errors). That satisfies Apple's SDK mandate — no
new hardware, no cloud-Mac rental, no borrowed machine needed.

One caveat found while testing: the iOS Simulator itself is slow to boot a
modern iOS 26.1 runtime on this 2018 Intel chip (cache-generation grinds for
minutes). Not a correctness problem — the build itself is proven — just
expect the Simulator to be sluggish on this hardware. Testing on your actual
iPhone (next step) will likely be faster than the Simulator here anyway.

**Still needed, one-time, needs your password (I can't type it):**
```
sudo xcode-select -s /Applications/Xcode-26.1.app/Contents/Developer
```
Until you run that, the system default stays on the old 13.1 — I've been
building by pointing `DEVELOPER_DIR` at 26.1 directly, which works fine for
me, but you'll want the switch done for when you open Xcode's GUI yourself.
Worth deleting the old `Xcode.app` (13.1) afterward too, to free ~7GB and
avoid Spotlight confusion — ask before I do that, since it's your call.

## Do this now if you have a Siri Shortcut already set up
If you built the Siri Shortcut from an earlier session, it's aimed at port
**8765** — that port now belongs to a different process (umbrella's
dashboard), not jarvis. Open the Shortcut and change the URL to port
**8766**, or it will silently hit the wrong service. (The Companion app
below already defaults to 8766.)

## What's left before you can actually use this on your phone (all $0 still)

1. ~~Update Xcode~~ — **done** (see above).
2. **Install Tailscale (free) and sign in.** This replaces "only works on home
   Wi-Fi" with a private, encrypted tunnel between your phone and this Mac —
   reachable from anywhere, never exposed to the raw internet. Sign-in needs
   your own account approval in a browser, which I can't do for you.
   `brew install --cask tailscale` gets the app installed; the login is a
   manual step.
3. **Run it on your actual phone via Xcode, free.** Plug your iPhone in, open
   `JarvisCompanion.xcodeproj` (with Xcode 26.1), pick your phone as the run
   destination, sign in with your regular Apple ID as a "Personal Team"
   (Xcode → Settings → Accounts), hit Run. No $99 needed for this step —
   only for distributing via TestFlight to more than your own dev-connected
   device.
4. **When you're ready to pay the one unavoidable cost:** enroll in the Apple
   Developer Program ($99/year) at developer.apple.com, then in Xcode assign
   that team to the project and use Xcode's "Distribute App" → TestFlight
   flow. At that point you (and anyone else you invite, up to 100 people) can
   install it like a real app, no more USB cable, and it survives Xcode being
   closed.

## Deliberately NOT built yet (by design, cost-minimization)
- **Push notifications.** Needs an APNs key, which needs the paid Developer
  account — out of scope for the "$0 right now" goal. Telegram keeps doing
  all proactive nudging/reminders in the meantime; this app is a pull-based
  companion for now, not a replacement notification channel.
- **App icon / branding.** Using Xcode's default placeholder. Trivial to swap
  in later (drop a 1024×1024 image into the asset catalog) — not worth doing
  before the app is actually running on a real device.
- **A public/multi-user backend.** Out of scope entirely — this only ever
  talks to your own Mac, by design (see the cost/safety conversation that led
  here).
