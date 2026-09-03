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

  /snapshot  one JPEG
  /stream    multipart/x-mixed-replace MJPEG
  /healthz   json
"""
import asyncio, base64, logging, os, time
from aiohttp import web
from playwright.async_api import async_playwright

PRINTER  = os.environ.get("PRINTER_HOST", "10.20.5.28")
CAM_PORT = int(os.environ.get("CAM_PORT", "8000"))
CAM_URL  = f"http://{PRINTER}:{CAM_PORT}/"
BIND     = os.environ.get("CAM_BIND", "127.0.0.1")
PORT     = int(os.environ.get("CAM_RELAY_PORT", "8771"))
FPS      = float(os.environ.get("CAM_FPS", "4"))
QUALITY  = float(os.environ.get("CAM_JPEG_QUALITY", "0.7"))

log = logging.getLogger("camrelay")
latest = {"jpeg": None, "ts": 0.0, "frames": 0, "size": None}

# Drawn to a canvas rather than screenshotted: a page screenshot in headless can
# come back with the video region black, and this also skips the page chrome.
GRAB_JS = """(q) => {
  const v = document.getElementById('remoteVideos');
  if (!v || v.readyState < 2 || !v.videoWidth) return null;
  const c = document.createElement('canvas');
  c.width = v.videoWidth; c.height = v.videoHeight;
  c.getContext('2d').drawImage(v, 0, 0);
  return c.toDataURL('image/jpeg', q);
}"""

LIVE_JS = ("() => { const v = document.getElementById('remoteVideos');"
           " return !!(v && v.readyState >= 2 && v.videoWidth > 0); }")


async def pump():
    """Keep one browser session alive, restarting it if the stream dies."""
    interval = 1.0 / FPS
    async with async_playwright() as pw:
        while True:
            browser = None
            try:
                browser = await pw.chromium.launch(args=[
                    "--no-sandbox",                        # unprivileged LXC
                    "--disable-dev-shm-usage",             # /dev/shm is tiny here
                    "--autoplay-policy=no-user-gesture-required",
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
                await page.goto(CAM_URL, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_function(LIVE_JS, timeout=45000)
                log.info("video is live, capturing at %.1f fps", FPS)
                stale = 0
                while True:
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
                log.warning("relay cycle failed: %s", e)
            finally:
                if browser:
                    try:
                        await browser.close()
                    except Exception:
                        pass
            log.info("restarting browser in 10s")
            await asyncio.sleep(10)


async def h_snapshot(request):
    if latest["jpeg"] is None:
        return web.json_response({"error": "no frame yet"}, status=503)
    return web.Response(body=latest["jpeg"], content_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


async def h_stream(request):
    resp = web.StreamResponse(headers={
        "Content-Type": "multipart/x-mixed-replace; boundary=frame",
        "Cache-Control": "no-store"})
    await resp.prepare(request)
    sent = -1
    try:
        while True:
            if latest["jpeg"] is not None and latest["frames"] != sent:
                sent = latest["frames"]
                jpg = latest["jpeg"]
                await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
            await asyncio.sleep(0.05)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return resp


async def h_health(request):
    return web.json_response({
        "printer": PRINTER,
        "frames": latest["frames"],
        "have_frame": latest["jpeg"] is not None,
        "bytes": len(latest["jpeg"]) if latest["jpeg"] else 0,
        "last_frame_age_s": round(time.time() - latest["ts"], 2) if latest["ts"] else None})


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = web.Application()
    # Both bare and /camera-prefixed paths: cloudflared routes by path but does
    # not rewrite it, so a /camera/ ingress rule arrives here still prefixed.
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
