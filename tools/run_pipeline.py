"""Оркестратор: симуляция рынка → LT Parsing → LT Matching → кросс-аналитика.

Запуск одного прогона:   python -m tools.run_pipeline
Бэкфилл истории:         python -m tools.run_pipeline --backfill-days 21 --runs-per-day 4
"""
import argparse
import functools
import http.server
import os
import socketserver
import threading
from datetime import datetime, timedelta, timezone

from tools.common.util import ROOT
from tools.market_sim.generate import generate_market
from tools.parsing import crawler
from tools.matching import matcher
from tools.analytics.aggregate import aggregate



class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def start_server():
    handler = functools.partial(_Quiet, directory=ROOT)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)  # свободный порт
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, httpd.server_address[1]


def one_run(port, asof=None, verbose=True):
    asof = asof or datetime.now(timezone.utc)
    market_stats = generate_market(asof)
    pm = crawler.run(f"http://127.0.0.1:{port}", asof)
    mm = matcher.run(asof)
    summary = aggregate(asof, market_stats, pm, mm)
    if verbose:
        print(f'[{asof:%Y-%m-%d %H:%M}] offers={pm["coverage"]["offers_parsed"]} '
              f'success={pm["reliability"]["crawl_success_rate"]:.3f} '
              f'auto={mm["funnel"]["auto"]} P={mm["quality"]["precision_auto"]:.3f} '
              f'R={mm["quality"]["recall"]:.3f} queue={mm["review"]["queue_size"]} '
              f'alerts={len(summary["alerts_open"])}')
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof")
    ap.add_argument("--backfill-days", type=int, default=0)
    ap.add_argument("--runs-per-day", type=int, default=4)
    args = ap.parse_args()

    httpd, port = start_server()
    try:
        if args.backfill_days:
            now = datetime.now(timezone.utc).replace(minute=17, second=0, microsecond=0)
            start = now - timedelta(days=args.backfill_days)
            step = timedelta(hours=24 // args.runs_per_day)
            asof = start
            while asof <= now:
                one_run(port, asof)
                asof += step
        else:
            asof = (datetime.fromisoformat(args.asof).replace(tzinfo=timezone.utc)
                    if args.asof else None)
            one_run(port, asof)
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
