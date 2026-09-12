#!/opt/camrelay-venv/bin/python
"""
Headless-Chromium relay for the K2 Plus camera.

The camera is WebRTC-only and its signalling page offers nothing but a public
STUN server, so from outside the LAN the browser and the printer never agree on
a media path: signalling succeeds through the Cloudflare tunnel and the video
stays black.

A pure-Python WebRTC client was tried first and got close - after correcting the
printer's malformed SDP (it answers with a payload type that was never offered,
and emits two fmtp lines for one type) ICE completed with a working candidate
pair - but DTLS then stalled: the printer's minimal `pear` stack never replies
to aiortc's ClientHello.

Chromium negotiates with it fine, which is exactly what the dashboard already
does on the LAN. So run a real headless Chromium beside the printer, lift frames
off the <video> element, and re-serve them as MJPEG - plain HTTP that a tunnel
forwards without complaint.

Chromium is started on demand and stopped again once nobody is watching.
Decoding the printer's 1080p stream continuously took this host to 98 C with the
fans already at maximum, which is why the relay spent a week disabled. Frame rate
is not the lever - the cost is the decode, which runs whether we grab 4 frames a
second or none - so the only thing that helps is not having a browser open.

  /snapshot  one JPEG          (starts the browser, may serve a stale frame first)
  /stream    multipart/x-mixed-replace MJPEG
  /healthz   json
"""
import asyncio, base64, logging, os, time
from aiohttp import web
from playwright.async_api import async_playwright

PRINTER  = os.environ.get("PRINTER_HOST", "10.20.5.28")
CAM_PORT = int(os.environ.get("CAM_PORT", "8000"))
BIND     = os.environ.get("CAM_BIND", "127.0.0.1")
PORT     = int(os.environ.get("CAM_RELAY_PORT", "8771"))
SIGNAL   = f"http://{PRINTER}:{CAM_PORT}/call/webrtc_local"
# Chromium loads OUR page, not the printer's. Firmware CR0CN240110C10 (built
# 2025-12-11) answers every path on :8000 with 200 and Content-Length: 0 - there
# is no viewer page there to drive, and waiting for one is why every relay cycle
# timed out. :8000 is signalling only, so the page that negotiates against it is
# ours to serve.
CAM_URL  = os.environ.get("CAM_URL", f"http://127.0.0.1:{PORT}/_viewer")
FPS      = float(os.environ.get("CAM_FPS", "4"))
QUALITY  = float(os.environ.get("CAM_JPEG_QUALITY", "0.7"))
IDLE_S   = float(os.environ.get("CAM_IDLE_S", "60"))
# Longer than a browser launch plus the 45s negotiation window.
FIRST_FRAME_S = float(os.environ.get("CAM_FIRST_FRAME_S", "90"))

log = logging.getLogger("camrelay")
latest = {"jpeg": None, "ts": 0.0, "frames": 0, "size": None}

# Demand, not a schedule: any request sets it, and the pump clears it once the
# last viewer has been gone for IDLE_S. Nobody on the dashboard -> no browser.
demand = asyncio.Event()
watch = {"viewers": 0, "touched": 0.0}


def touch():
    watch["touched"] = time.time()
    demand.set()

# Drawn to a canvas rather than screenshotted: a page screenshot in headless can
# come back with the video region black, and this also skips the page chrome.
GRAB_JS = """(q) => {
  const v = document.getElementById('remoteVideos');
  if (!v || v.readyState < 2 || !v.videoWidth) return null;
  // Cached on window deliberately. A fresh canvas per frame is an 8 MB backing
  // store four times a second, and the collector did not keep up: 1.9 G peak
  // and an oom-kill about 25 minutes into a session.
  let c = window.__grab;
  if (!c || c.width !== v.videoWidth || c.height !== v.videoHeight) {
    c = window.__grab = document.createElement('canvas');
    c.width = v.videoWidth; c.height = v.videoHeight;
  }
  c.getContext('2d').drawImage(v, 0, 0);
  return c.toDataURL('image/jpeg', q);
}"""

LIVE_JS = ("() => { const v = document.getElementById('remoteVideos');"
           " return !!(v && v.readyState >= 2 && v.videoWidth > 0); }")


# The page Chromium runs. This is the dashboard's original in-browser client,
# recovered from k2plus-dashboard@0cf35b7^ - the handshake the printer actually
# accepts, rather than a fresh guess at it:
#   POST base64(JSON {type:"offer", sdp}) as text/plain, which is a CORS-simple
#   request so the printer's `Access-Control-Allow-Origin: *` is enough and no
#   preflight is needed, then read back base64(JSON {type:"answer", sdp}).
# Non-trickle: the offer is only sent once ICE gathering has finished, which is
# what `ev.candidate === null` means. The element keeps the id `remoteVideos`
# that GRAB_JS and LIVE_JS already look for.
VIEWER = """<!doctype html><meta charset="utf-8">
<style>html,body{margin:0;background:#000}
video{width:100vw;height:100vh;object-fit:contain}</style>
<video id="remoteVideos" autoplay muted playsinline></video>
<script>
const SIGNAL = "__SIGNAL__";
function say(s){ console.log("cam: " + s); }
// On window so the relay can hang up from page.evaluate before it kills the
// browser. Dropping a session without closing it leaves the printer holding a
// peer that will never answer: it retransmits the DTLS handshake at it on a
// widening backoff, and sessions opened afterwards get no media. One orphan is
// enough to take the camera out until webrtc_local is restarted - which is what
// an oom-kill left behind on 2026-09-11.
var pc = null;
addEventListener("pagehide", () => { try{ pc && pc.close(); }catch(e){} });
function connect(){
  if(pc){ try{ pc.close(); }catch(e){} }
  // No STUN. The dashboard needed it because the browser was yours, out on the
  // internet behind NAT. This browser runs beside the printer, so host
  // candidates already describe a working path - and waiting for a gather
  // against stun.l.google.com to time out burned 40 of the 45 seconds the
  // capture loop allows, so the video started just after we gave up on it.
  pc = new RTCPeerConnection({iceServers:[]});
  pc.ontrack = e => { document.getElementById("remoteVideos").srcObject = e.streams[0];
                      say("track"); };
  pc.oniceconnectionstatechange = () => say("ice " + pc.iceConnectionState);
  pc.onicecandidate = ev => {
    if(ev.candidate !== null){ say("candidate " + ev.candidate.candidate); return; }
    fetch(SIGNAL, {method:"POST", headers:{"Content-Type":"text/plain"},
                   body: btoa(JSON.stringify({type:"offer", sdp:pc.localDescription.sdp}))})
      .then(r => r.text())
      .then(t => {
        let a;
        try { a = JSON.parse(atob(t)); }
        catch(e){ say("unreadable answer: " + t.slice(0,120)); return; }
        if(a.type !== "answer"){ say("no answer in reply: " + t.slice(0,120)); return; }
        pc.setRemoteDescription(new RTCSessionDescription(a))
          .then(() => say("answer accepted"))
          .catch(e => say("setRemoteDescription failed: " + e.message));
      })
      .catch(e => say("signalling failed: " + e.message));
  };
  pc.addTransceiver("video", {direction:"sendrecv"});
  pc.createOffer().then(d => pc.setLocalDescription(d))
                  .catch(e => say("offer failed: " + e.message));
}
connect();
</script>"""


async def h_viewer(request):
    return web.Response(text=VIEWER.replace("__SIGNAL__", SIGNAL),
                        content_type="text/html")


async def pump():
    """Run a browser session while anyone is watching, and not a moment longer."""
    interval = 1.0 / FPS
    fails = 0
    async with async_playwright() as pw:
        while True:
            await demand.wait()
            browser = page = None
            failed = False
            try:
                browser = await pw.chromium.launch(args=[
                    "--no-sandbox",                        # unprivileged LXC
                    "--disable-dev-shm-usage",             # /dev/shm is tiny here
                    "--autoplay-policy=no-user-gesture-required",
                    # Chromium publishes host candidates as obfuscated mDNS
                    # names (a1b2....local) unless told not to. A browser peer
                    # resolves those; the printer's minimal stack cannot, so it
                    # has no address to send to - it sits in CONNECTING and
                    # retransmits the DTLS handshake into nowhere while our side
                    # cheerfully reports ice connected. Both ends are on the LAN
                    # and the relay is not a privacy boundary.
                    "--disable-features=WebRtcHideLocalIpsWithMdns",
                    # Chromium's caches churn hard while decoding a continuous
                    # 1080p stream. The files never grow - they are written and
                    # deleted - but on LVM-thin every write claims fresh blocks
                    # and the freed ones are not returned, so the volume crept
                    # from 10% to 52% allocated in ten minutes and wedged the
                    # container. Nothing here needs to survive a restart.
                    "--disk-cache-size=1",
                    "--media-cache-size=1",
                    "--disable-gpu-shader-disk-cache",
                    "--disable-application-cache",
                    "--disable-back-forward-cache",
                ])
                page = await browser.new_page(viewport={"width": 1280, "height": 720})
                page.on("console", lambda m: log.info("page: %s", m.text))
                await page.goto(CAM_URL, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_function(LIVE_JS, timeout=45000)
                fails = 0
                log.info("video is live, capturing at %.1f fps", FPS)
                stale = 0
                while True:
                    if not watch["viewers"] and time.time() - watch["touched"] > IDLE_S:
                        demand.clear()
                        log.info("nobody watching for %.0fs, closing the browser", IDLE_S)
                        break
                    url = await page.evaluate(GRAB_JS, QUALITY)
                    if url:
                        latest["jpeg"] = base64.b64decode(url.split(",", 1)[1])
                        latest["ts"] = time.time()
                        latest["frames"] += 1
                        stale = 0
                    else:
                        stale += 1
                        if stale > FPS * 10:               # ~10s with no pixels
                            raise RuntimeError("video went dead")
                    await asyncio.sleep(interval)
            except Exception as e:
                failed = True
                log.warning("relay cycle failed: %s", e)
            finally:
                if browser:
                    try:
                        # Hang up first, so the printer frees the slot instead of
                        # retransmitting DTLS at a peer that is already gone.
                        await page.evaluate("() => { try{ pc && pc.close(); }catch(e){} }")
                    except Exception:
                        pass
                    try:
                        await browser.close()
                    except Exception:
                        pass
            if failed:
                # A printer whose camera is off answers signalling with {} and
                # never produces video, so a viewer left on the page would spin
                # a browser launch every ten seconds all day - at full tilt on a
                # host that has no thermal headroom. Back off instead.
                fails += 1
                wait = min(10 * 2 ** (fails - 1), 300)
                log.info("cycle %d failed, retrying in %ds", fails, wait)
                await asyncio.sleep(wait)


async def h_snapshot(request):
    touch()
    if latest["jpeg"] is None:
        return web.json_response({"error": "no frame yet"}, status=503)
    return web.Response(body=latest["jpeg"], content_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


async def h_stream(request):
    touch()
    watch["viewers"] += 1
    resp = web.StreamResponse(headers={
        "Content-Type": "multipart/x-mixed-replace; boundary=frame",
        "Cache-Control": "no-store"})
    await resp.prepare(request)
    sent = -1
    opened = wrote = time.time()
    try:
        while True:
            # A dead client only surfaces as a failed write, and during a cold
            # start the only writes are 2-byte keepalives, which can succeed
            # into a socket nobody is reading for a long time. Ask directly, or
            # the viewer count never drops and the browser never stops.
            if request.transport is None or request.transport.is_closing():
                break
            if sent < 0 and time.time() - opened > FIRST_FRAME_S:
                log.info("no first frame in %.0fs, dropping the viewer", FIRST_FRAME_S)
                break
            if latest["jpeg"] is not None and latest["frames"] != sent:
                sent = latest["frames"]
                jpg = latest["jpeg"]
                await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                wrote = time.time()
            elif sent < 0 and time.time() - wrote > 5:
                # Cold start: launching the browser and negotiating WebRTC can
                # outlast the dashboard proxy's 15s read timeout, which would
                # drop the viewer just before the first frame. Anything before
                # the first boundary is multipart preamble and is ignored, so
                # this keeps the socket busy without showing anything.
                await resp.write(b"\r\n")
                wrote = time.time()
            await asyncio.sleep(0.05)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        watch["viewers"] -= 1
    return resp


async def h_health(request):
    return web.json_response({
        "printer": PRINTER,
        "viewers": watch["viewers"],
        "wanted": demand.is_set(),
        "frames": latest["frames"],
        "have_frame": latest["jpeg"] is not None,
        "bytes": len(latest["jpeg"]) if latest["jpeg"] else 0,
        "last_frame_age_s": round(time.time() - latest["ts"], 2) if latest["ts"] else None})


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = web.Application()
    # Both bare and /camera-prefixed paths: cloudflared routes by path but does
    # not rewrite it, so a /camera/ ingress rule arrives here still prefixed.
    app.router.add_get("/_viewer", h_viewer)
    for prefix in ("", "/camera"):
        app.router.add_get(prefix + "/snapshot", h_snapshot)
        app.router.add_get(prefix + "/stream", h_stream)
        app.router.add_get(prefix + "/healthz", h_health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, BIND, PORT).start()
    log.info("serving on http://%s:%s (camera %s)", BIND, PORT, CAM_URL)
    await pump()


if __name__ == "__main__":
    asyncio.run(main())
