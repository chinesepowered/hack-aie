"""Run a scan from the command line: python scripts/scan_cli.py path/to/photo_or_video"""
import asyncio, json, logging, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from capykondo import pipeline, store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

async def main(src: str):
    store.bus.bind(asyncio.get_running_loop())
    q = store.bus.subscribe()
    mode = "video" if Path(src).suffix.lower() in (".mp4", ".mov", ".webm", ".m4v") else "photo"
    sid = store.create_scan(mode, src)
    t = time.time()
    task = asyncio.create_task(pipeline.run_scan(sid, Path(src), mode))
    while True:
        evt = await q.get()
        k = evt["kind"]
        if k == "stage":
            print(f"[{time.time()-t:5.1f}s] stage {evt['stage']} {evt['status']} {evt.get('seconds','')} {evt.get('count','')}")
        elif k == "items_found":
            print(f"[{time.time()-t:5.1f}s] found", [i["sam_phrase"] for i in evt["items"]])
        elif k == "item_ready":
            it = evt["item"]; d = it["data"]
            print(f"[{time.time()-t:5.1f}s] READY {it['title']!r} ${it['price']} (floor {it['floor_price']}) {it['condition']} src={d.get('price_source')} comps={len(d.get('comps',[]))} t={d.get('timings')}")
        elif k in ("scan_done", "scan_failed"):
            print(f"[{time.time()-t:5.1f}s] {k}", {x: evt.get(x) for x in ("count", "total", "seconds", "error")})
            break
    await task
    print("scan", sid)

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
