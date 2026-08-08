"""Static file server for the dashboard that refuses to be cached.

`python3 -m http.server` sends `Last-Modified` and nothing else -- no
`Cache-Control`, no `ETag`. Browsers respond to that by applying *heuristic*
caching: with no explicit freshness directive they're free to invent one
(commonly ~10% of the document's age), and they'll serve app.js from cache
without even sending a conditional request.

That bit during development: an app.js edited between two page loads kept
serving the old file, and because the stale copy threw inside a caught
block, the page half-worked -- the integrity panel sat on "Checking…"
forever while everything else looked fine. The same failure during a live
demo would be invisible and unfixable in the moment.

These files are tiny and served from localhost, so there is nothing to gain
from caching them and a whole class of confusing staleness to lose.
"""

import functools
import http.server
import os
import sys


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # Must be sent before end_headers() closes the header block.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def send_head(self):
        # SimpleHTTPRequestHandler answers If-Modified-Since with a 304 on
        # its own. A browser shouldn't send one given the headers above, but
        # a stale conditional request already in flight (or an intermediary)
        # would still get a 304 and reuse the old body -- so drop the header
        # before it's ever consulted.
        if "If-Modified-Since" in self.headers:
            del self.headers["If-Modified-Since"]
        return super().send_head()


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4023
    directory = os.path.dirname(os.path.abspath(__file__))
    handler = functools.partial(NoCacheHandler, directory=directory)
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"Dashboard (no-store) on http://127.0.0.1:{port}/index.html", flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
