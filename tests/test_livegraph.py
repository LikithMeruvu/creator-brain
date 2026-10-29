import json
import threading
import urllib.request

from cbrain import livegraph as lg


def _w(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def make_vault(root):
    _w(root, "Home.md", "# Home\n[[Alice]] [[Bench Press]]\n")
    _w(root, "creators/Alice/Alice.md", "---\ntype: creator\n---\n[[Alice - All Videos]]\n")
    _w(root, "creators/Alice/Alice - All Videos.md", "[[Vid One]] [[Vid Two]] [[Alice]]\n")
    _w(root, "creators/Alice/videos/Vid One.md",
       '---\ntype: video\ntitle: "Vid One Title"\nurl: "https://youtu.be/x"\nviews: 10\n---\n'
       "by [[Alice]]; see [[Bench Press|bench]] and [[Squat#form]] and [[bp]]\n")
    _w(root, "creators/Alice/videos/Vid Two.md", "---\ntype: video\n---\n[[Alice]] [[Nope]]\n")
    _w(root, "creators/Alice/updates/2026-01.md", "[[Alice]]\n")
    _w(root, "topics/Bench Press.md", '---\ntype: topic\naliases:\n  - "bp"\nclaims: 4\n---\n[[Alice]]\n')
    _w(root, "topics/Squat.md", "---\ntype: topic\n---\nx\n")
    _w(root, "outputs/Plan.md", "[[Squat]]\n")
    _w(root, ".brain/hidden.md", "[[Alice]]")
    _w(root, ".obsidian/x.md", "[[Alice]]")


def edge_set(g):
    return {frozenset((e["source"], e["target"])) for e in g["links"]}


def test_groups_links_and_exclusions(tmp_path):
    make_vault(tmp_path)
    g = lg.build_graph(tmp_path)
    by = {n["id"]: n for n in g["nodes"]}
    assert not any("All Videos" in i or ".brain" in i or ".obsidian" in i for i in by)
    assert by["Home"]["group"] == "home"
    assert by["creators/Alice/Alice"]["group"] == "creator"
    v1 = by["creators/Alice/videos/Vid One"]
    assert v1["group"] == "video" and v1["creator"] == "Alice" and v1["label"] == "Vid One Title"
    assert v1["url"] == "https://youtu.be/x"
    assert by["creators/Alice/updates/2026-01"]["group"] == "update"
    assert by["topics/Bench Press"]["group"] == "topic"
    assert by["outputs/Plan"]["group"] == "output"
    es = edge_set(g)
    vid1, bp, sq = v1["id"], "topics/Bench Press", "topics/Squat"
    assert frozenset((vid1, "creators/Alice/Alice")) in es
    assert frozenset((vid1, bp)) in es          # label form + alias form (same edge, deduped)
    assert frozenset((vid1, sq)) in es          # heading form
    assert frozenset(("Home", bp)) in es
    assert frozenset(("outputs/Plan", sq)) in es
    assert len(g["links"]) == len(es)           # no duplicate edges
    assert g["stats"]["groups"]["video"] == 2
    assert by[bp]["size"] > by[sq]["size"]


def test_incremental_cache(tmp_path, monkeypatch):
    make_vault(tmp_path)
    cache = {}
    g1 = lg.build_graph(tmp_path, cache)
    calls = []
    orig = lg._read_note
    monkeypatch.setattr(lg, "_read_note", lambda *a: (calls.append(a[0].name), orig(*a))[1])
    g2 = lg.build_graph(tmp_path, cache)
    assert calls == []
    assert g2["nodes"] == g1["nodes"] and g2["links"] == g1["links"]
    p = tmp_path / "topics/Squat.md"
    p.write_text("---\ntype: topic\n---\nnow links [[Alice]] and more text\n", encoding="utf-8")
    g3 = lg.build_graph(tmp_path, cache)
    assert calls == ["Squat.md"]
    assert frozenset(("topics/Squat", "creators/Alice/Alice")) in edge_set(g3)
    p.unlink()
    g4 = lg.build_graph(tmp_path, cache)
    assert "topics/Squat" not in {n["id"] for n in g4["nodes"]}


def test_server_routes(tmp_path):
    make_vault(tmp_path)
    srv = lg.make_server(tmp_path, 0)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        html = urllib.request.urlopen(base + "/", timeout=5).read().decode()
        assert "Creator Brain" in html and "d3" in html
        data = json.loads(urllib.request.urlopen(base + "/graph.json", timeout=5).read())
        assert data["stats"]["nodes"] == len(data["nodes"]) > 5
        try:
            urllib.request.urlopen(base + "/nope", timeout=5)
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)
