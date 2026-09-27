import argparse
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


def ask(url):
    payload = json.dumps({"question": "What are the library working hours?"}).encode()
    request = urllib.request.Request(
        f"{url.rstrip('/')}/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            response.read()
            return response.status, (time.perf_counter() - started) * 1000
    except Exception:
        return 0, (time.perf_counter() - started) * 1000


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(ask, args.url) for _ in range(args.requests)]
        values = [future.result() for future in as_completed(futures)]
    successes = [latency for status, latency in values if status == 200]
    print(f"successful={len(successes)}/{len(values)}")
    if successes:
        ordered = sorted(successes)
        print(f"median_ms={ordered[len(ordered)//2]:.1f}")
        print(f"max_ms={max(ordered):.1f}")
    raise SystemExit(0 if len(successes) == len(values) else 1)


if __name__ == "__main__":
    main()
