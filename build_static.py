"""
Static snapshot generator for the Labor Market Monitor.

Produces ONE self-contained HTML file with the current data baked in, so it can
be hosted for free on GitHub Pages / Netlify with no server. It reuses the exact
page and data logic from dashboard.py, so the snapshot looks identical to the
live version - it's just frozen at the moment you run this.

Usage:
    python build_static.py
Then upload the generated index.html (see the printed instructions).
"""

import json
import dashboard   # reuse the page template and data builders

OUT = "index.html"


def main():
    # 1. Get the exact same data payload the live dashboard serves.
    payload = dashboard.load_series()
    data_json = json.dumps(payload)

    # 2. Take the dashboard's page template and replace the network fetch with
    #    the data embedded inline. The live page does:
    #        const res = await fetch("/data");
    #        const payload = await res.json();
    #    We swap those two lines for a baked-in constant so no server is needed.
    page = dashboard.PAGE

    # The live page fetches data over the network. Find that exact block and
    # replace it with the data embedded inline. We use a plain string replace
    # (NOT regex) for the replacement, because the JSON contains backslash
    # escapes (e.g. \u2192) that regex would misinterpret as escape sequences.
    fetch_block_variants = [
        '  const res = await fetch("/data");\n'
        '  const payload = await res.json();',
        'const res = await fetch("/data");\n'
        '  const payload = await res.json();',
    ]
    replacement = f"const payload = {data_json};"

    new_page = None
    for variant in fetch_block_variants:
        if variant in page:
            new_page = page.replace(variant, replacement, 1)
            break
    if new_page is None:
        # Fall back to a tolerant line-by-line splice so whitespace changes in
        # dashboard.py don't silently break the export.
        lines = page.split("\n")
        out, spliced = [], False
        i = 0
        while i < len(lines):
            if 'await fetch("/data")' in lines[i] and i + 1 < len(lines) \
                    and "await res.json()" in lines[i + 1]:
                indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
                out.append(indent + replacement)
                i += 2
                spliced = True
                continue
            out.append(lines[i])
            i += 1
        if not spliced:
            raise RuntimeError(
                "Could not find the fetch('/data') block in dashboard.py to "
                "replace. Its code may have changed - update build_static.py."
            )
        new_page = "\n".join(out)

    # 3. Add a small 'snapshot' note so viewers know it's a frozen export.
    as_of = payload.get("as_of", "")
    note = (
        '<div style="max-width:1040px;margin:0 auto;padding:6px 32px;'
        'font-size:11px;color:var(--muted);font-family:ui-monospace,Menlo,monospace">'
        f'Static snapshot &middot; data as of {as_of[:7] if as_of else "latest"} '
        '&middot; the live version updates monthly.</div>'
    )
    new_page = new_page.replace('<div class="guide">', note + '\n<div class="guide">', 1)

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(new_page)

    size_kb = len(new_page.encode("utf-8")) / 1024
    print(f"Wrote {OUT}  ({size_kb:.0f} KB, fully self-contained)")
    print("\nThis single file needs no server. To put it online free:")
    print("  1. Create a GitHub repo (e.g. 'labor-market-monitor').")
    print("  2. Add this index.html to it.")
    print("  3. Repo Settings -> Pages -> Source: main branch, root.")
    print("  4. Your URL will be https://<username>.github.io/<repo>/")


if __name__ == "__main__":
    main()