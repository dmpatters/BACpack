"""The PICS as a web page: the Markdown subset PICS.md uses, turned into HTML."""
from __future__ import annotations

from bacpack.pics_html import markdown_to_html, render_pics_html


def test_headings_paragraphs_and_lists():
    html = markdown_to_html("# Title\n\nSome text\nwrapped.\n\n## Part\n\n- one\n- two")
    assert html == (
        "<h1>Title</h1>\n<p>Some text\nwrapped.</p>\n<h2>Part</h2>\n"
        "<ul>\n<li>one</li>\n<li>two</li>\n</ul>"
    )


def test_a_heading_with_a_paragraph_in_the_same_block():
    html = markdown_to_html("### analog-input\nCan be created: no.")
    assert html == "<h3>analog-input</h3>\n<p>Can be created: no.</p>"


def test_tables_split_on_unescaped_pipes_only():
    md = "| A | B |\n|---|---|\n| x \\| y | z |\n| <i> |  |"
    assert markdown_to_html(md) == (
        "<table>\n<thead><tr><th>A</th><th>B</th></tr></thead>\n<tbody>\n"
        "<tr><td>x | y</td><td>z</td></tr>\n"
        "<tr><td>&lt;i&gt;</td><td></td></tr>\n"
        "</tbody>\n</table>"
    )


def test_text_is_escaped_and_inline_code_kept():
    assert markdown_to_html("a <b> & `c<d>`") == "<p>a &lt;b&gt; &amp; <code>c&lt;d&gt;</code></p>"


def test_the_page_is_the_pics_without_the_note_for_editors():
    md = (
        "# BACpack PICS\n\nProtocol Implementation Conformance Statement, laid out per ASHRAE 135"
        " Annex A (the PICS form). Generated from the code by `scripts/make_pics.py`; do not edit by"
        " hand.\n\n## 1. Product\n\n- Product name: BACpack\n"
    )
    page = render_pics_html(md)
    assert page.startswith("<!DOCTYPE html>")
    assert "<title>BACpack PICS</title>" in page
    assert "<h2>1. Product</h2>" in page
    assert "the PICS form).</p>" in page
    assert "make_pics" not in page
    assert 'href="/api/pics.md"' in page
