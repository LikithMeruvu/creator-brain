"""Live web view of the brain: incremental graph builder + tiny local HTTP server (stdlib only)."""

from __future__ import annotations

import json
import re
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_LINK_RE = re.compile(r"\[\[([^\]\n]+?)\]\]")
_SKIP_DIRS = {".brain", ".obsidian", ".git", ".trash"}
_TEMPLATE = Path(__file__).with_name("templates") / "livegraph.html"


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v.strip()


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML subset: scalars and `- item` lists. Returns (meta, body)."""
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not m:
        return {}, text
    meta: dict = {}
    key = None
    for line in m.group(1).splitlines():
        if re.match(r"^\s+-\s", line) or line.strip() == "-":
            if key is not None:
                if not isinstance(meta.get(key), list):
                    meta[key] = []
                meta[key].append(_unquote(line.strip()[1:]))
            continue
        km = re.match(r"^([A-Za-z0-9_\-]+):\s*(.*)$", line)
        if km:
            key, val = km.group(1), km.group(2).strip()
            if val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                meta[key] = [_unquote(x) for x in inner.split(",")] if inner else []
            elif val == "":
                meta[key] = []
            else:
                meta[key] = _unquote(val)
    return meta, text[m.end():]


def _to_int(v) -> int:
    try:
        return int(float(str(v)))
    except (TypeError, ValueError):
        return 0


def _classify(parts: tuple[str, ...], stem: str) -> tuple[str | None, str]:
    """(group, creator) from relative path parts; group None => excluded."""
    if parts[0] == "creators" and len(parts) >= 3:
        creator = parts[1]
        if len(parts) == 3:
            if stem.endswith(" - All Videos"):
                return None, creator
            return ("creator" if stem == creator else "home"), creator
        if parts[2] == "videos":
            return "video", creator
        return "update", creator
    if parts[0] == "topics":
        return "topic", ""
    if parts[0] == "outputs":
        return "output", ""
    return "home", ""


def _file_created(st) -> float:
    return getattr(st, "st_birthtime", None) or st.st_ctime or st.st_mtime


def _read_note(path: Path, rel: tuple[str, ...], st) -> dict | None:
    stem = path.stem
    group, creator = _classify(rel, stem)
    if group is None:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta, body = _parse_frontmatter(text)
    links = []
    for raw in _LINK_RE.findall(body):
        t = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if t:
            links.append(t)
    aliases = meta.get("aliases") if isinstance(meta.get("aliases"), list) else []
    label = stem
    if group == "video" and isinstance(meta.get("title"), str) and meta["title"]:
        label = meta["title"]
    return {
        "id": "/".join(rel)[:-3], "stem": stem, "label": label, "group": group, "creator": creator,
        "aliases": [a for a in aliases if a], "links": links,
        "claims": _to_int(meta.get("claims")), "published": str(meta.get("published", "") or ""),
        "views": _to_int(meta.get("views")), "url": str(meta.get("url", "") or ""),
        "created": _file_created(st),
    }


def build_graph(root: Path, cache: dict | None = None) -> dict:
    """Scan the vault. `cache` (mutated in place) maps rel path -> (mtime_ns, size, record)."""
    root = Path(root)
    if cache is None:
        cache = {}
    seen: set[str] = set()
    records: list[dict] = []
    for path in root.rglob("*.md"):
        rel = path.relative_to(root).parts
        if any(p in _SKIP_DIRS for p in rel[:-1]):
            continue
        key = "/".join(rel)
        try:
            st = path.stat()
        except OSError:
            continue
        seen.add(key)
        hit = cache.get(key)
        if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            rec = hit[2]
        else:
            rec = _read_note(path, rel, st)
            cache[key] = (st.st_mtime_ns, st.st_size, rec)
        if rec:
            records.append(rec)
    for k in [k for k in cache if k not in seen]:
        del cache[k]

    # name -> node id (stems first; aliases only fill gaps)
    names: dict[str, str] = {}
    for r in sorted(records, key=lambda r: r["id"]):
        names.setdefault(r["stem"].lower(), r["id"])
    for r in records:
        for a in r["aliases"]:
            names.setdefault(a.lower(), r["id"])

    edges: set[tuple[str, str]] = set()
    for r in records:
        for t in r["links"]:
            tid = names.get(t.lower())
            if tid and tid != r["id"]:
                edges.add((r["id"], tid) if r["id"] < tid else (tid, r["id"]))
    degree: dict[str, int] = {}
    for a, b in edges:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1

    nodes = []
    counts: dict[str, int] = {}
    for r in sorted(records, key=lambda r: r["created"]):
        deg = degree.get(r["id"], 0)
        size = 3 + min(deg, 60) ** 0.6 * 1.6 + min(r["claims"], 100) ** 0.5 * 0.8
        if r["group"] == "creator":
            size += 8
        elif r["group"] == "video":
            size = 2.5 + min(deg, 20) ** 0.5
        n = {"id": r["id"], "label": r["label"], "group": r["group"], "creator": r["creator"],
             "size": round(size, 2), "created": round(r["created"], 3), "degree": deg}
        if r["published"]:
            n["published"] = r["published"]
        if r["views"]:
            n["views"] = r["views"]
        if r["url"]:
            n["url"] = r["url"]
        nodes.append(n)
        counts[r["group"]] = counts.get(r["group"], 0) + 1
    return {
        "nodes": nodes,
        "links": [{"source": a, "target": b} for a, b in sorted(edges)],
        "stats": {"nodes": len(nodes), "links": len(edges), "groups": counts},
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _make_handler(root: Path):
    cache: dict = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            try:
                if path in ("/", "/index.html"):
                    self._send(200, "text/html; charset=utf-8", _TEMPLATE.read_bytes())
                elif path == "/graph.json":
                    with lock:
                        data = build_graph(root, cache)
                    self._send(200, "application/json", json.dumps(data).encode("utf-8"))
                else:
                    self._send(404, "text/plain", b"not found")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # keep the server alive
                self._send(500, "text/plain", str(e).encode("utf-8", "replace"))

        def log_message(self, *a) -> None:
            pass

    return Handler


def make_server(root: Path, port: int = 8787) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(Path(root)))
    srv.daemon_threads = True
    return srv


def serve(root: Path, port: int = 8787, open_browser: bool = True) -> None:
    srv = make_server(root, port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Creator Brain live graph: {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        print("stopped")
