import ocean


def test():
    assert ocean.oa_abstract({"sea": [0], "velocity": [2], "surface": [1]}) == "sea surface velocity"
    assert ocean.oa_abstract(None) == ""
    assert ocean.fts_query("SSV field inference from coastal data") == '"ssv" OR "field" OR "inference" OR "coastal" OR "data"'
    assert "sea surface velocity" in ocean.TRACK and "coral" in ocean.IGNORE
    ocean.DB = ":memory:"
    c = ocean.db()
    row = dict(id="x", source="t", title="Submesoscale SWOT velocity", abstract="HF radar validation", authors="a",
               venue="v", date="2026-01-01", url="u", cites=5)
    junk = dict(row, id="y", title="Coral reef bleaching", abstract="", cites=99)
    assert ocean.upsert(c, [row, row, junk]) == 2 and ocean.upsert(c, [row]) == 0
    assert ocean.search(c, "what does HF radar say about SWOT")[0]["id"] == "x"
    assert ocean.search(c, "plankton") == []
    assert [r["id"] for r in ocean.taste_rank([junk, row], 10)] == ["x"]
    brief = dict(date="2026-01-01", n_items=2, headline="h", items={"1": dict(title="T1", venue="V", date="2026", url="u", authors="a", source="openalex", cites=3)},
                 must_reads=[dict(ref=1, what_is_new="n", why_it_matters="w", preprint=True, prereqs=["adjoint methods"])],
                 for_you=[dict(ref=1, hook="hk")], themes=[dict(name="th", summary="s", refs=[1])], study=[dict(topic="4D-Var", why="y", level="advanced", refs=[1])])
    md = ocean.brief_md(brief)
    assert "**T1**" in md and "adjoint methods" in md and "## Study next" in md and "4D-Var" in md
    print("ok")


if __name__ == "__main__":
    test()
