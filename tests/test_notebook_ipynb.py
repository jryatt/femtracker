"""render_ipynb: a .ipynb's JSON -> sanitized read-only HTML, plus the /api/results/{id}/notebook route."""
import base64
import json

from conftest import api, job, make_inputs, study, TMP
from femtracker import notebook
from test_notebook import make_gif, make_png


def notebook_json(cells):
    return json.dumps({"cells": cells})


def test_markdown_and_code_cells():
    html = notebook.render_ipynb(notebook_json([
        {"cell_type": "markdown", "source": ["# Title\n", "some *text*\n"]},
        {"cell_type": "code", "source": "print('hi')", "outputs": []},
    ]))
    assert "<h1>Title</h1>" in html
    assert "<em>text</em>" in html
    assert "print(&#39;hi&#39;)" in html or "print('hi')" in html  # escaped, not executed


def test_stream_and_error_outputs():
    html = notebook.render_ipynb(notebook_json([
        {"cell_type": "code", "source": "1/0", "outputs": [
            {"output_type": "stream", "name": "stdout", "text": ["line1\n", "line2\n"]},
            {"output_type": "error", "ename": "ZeroDivisionError", "evalue": "division by zero",
             "traceback": ["\x1b[0;31mTraceback\x1b[0m", "ZeroDivisionError: division by zero"]},
        ]},
    ]))
    assert "line1" in html and "line2" in html
    assert "nb-error" in html
    assert "ZeroDivisionError" in html
    assert "\x1b" not in html  # ANSI codes stripped


def test_real_image_output_is_embedded():
    png = make_png()
    html = notebook.render_ipynb(notebook_json([
        {"cell_type": "code", "source": "", "outputs": [
            {"output_type": "execute_result", "data": {"image/png": base64.b64encode(png).decode()}},
        ]},
    ]))
    assert "<img src=\"data:image/png;base64," in html


def test_fake_image_is_not_embedded():
    """A mime label claiming an image, backed by bytes that aren't one, must not become an <img>."""
    html = notebook.render_ipynb(notebook_json([
        {"cell_type": "code", "source": "", "outputs": [
            {"output_type": "execute_result",
             "data": {"image/png": base64.b64encode(b"not actually a png").decode()}},
        ]},
    ]))
    assert "<img" not in html


def test_gif_output_also_embedded():
    html = notebook.render_ipynb(notebook_json([
        {"cell_type": "code", "source": "", "outputs": [
            {"output_type": "display_data", "data": {"image/gif": base64.b64encode(make_gif()).decode()}},
        ]},
    ]))
    assert "<img src=\"data:image/gif;base64," in html


def test_html_output_table_survives_but_script_is_stripped():
    html = notebook.render_ipynb(notebook_json([
        {"cell_type": "code", "source": "", "outputs": [
            {"output_type": "execute_result",
             "data": {"text/html": "<table><tr><td>42</td></tr></table><script>alert(1)</script>"}},
        ]},
    ]))
    assert "<table>" in html and "<td>42</td>" in html
    assert "<script>" not in html and "alert(1)" not in html


def test_svg_output_is_never_shown():
    html = notebook.render_ipynb(notebook_json([
        {"cell_type": "code", "source": "", "outputs": [
            {"output_type": "display_data", "data": {"image/svg+xml": "<svg onload=alert(1)></svg>"}},
        ]},
    ]))
    assert "<svg" not in html and "<img" not in html


def test_plain_text_output_is_escaped():
    html = notebook.render_ipynb(notebook_json([
        {"cell_type": "code", "source": "", "outputs": [
            {"output_type": "execute_result", "data": {"text/plain": "<b>not bold</b>"}},
        ]},
    ]))
    assert "<b>" not in html
    assert "&lt;b&gt;not bold&lt;/b&gt;" in html


def test_invalid_json_raises():
    try:
        notebook.render_ipynb("not json")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_missing_cells_raises():
    try:
        notebook.render_ipynb(json.dumps({"not": "a notebook"}))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_upload_and_view_ipynb_result():
    """A .ipynb uploaded as a manual result (no special-casing needed, any file upload works
    already) renders through /api/results/{id}/notebook."""
    s = study("ipynb study", make_inputs(TMP / "ipynb_inputs"))
    j = job(s["id"], "j1")
    nb = json.dumps({"cells": [{"cell_type": "markdown", "source": ["# Analysis\n"]},
                               {"cell_type": "code", "source": "2+2",
                                "outputs": [{"output_type": "stream", "text": ["4\n"]}]}]}).encode()
    r = api("POST", f"/api/jobs/{j['id']}/results", data={"name": "nb", "description": "", "metrics": "{}"},
           files={"file": ("analysis.ipynb", nb, "application/json")})
    out = api("GET", f"/api/results/{r['id']}/notebook")
    assert "<h1>Analysis</h1>" in out["html"]
    assert "4" in out["html"]


def test_non_ipynb_result_rejected():
    s = study("not ipynb study", make_inputs(TMP / "notipynb_inputs"))
    j = job(s["id"], "j1")
    r = api("POST", f"/api/jobs/{j['id']}/results", data={"name": "nb", "description": "", "metrics": "{}"},
           files={"file": ("notes.txt", b"hello", "text/plain")})
    resp = api("GET", f"/api/results/{r['id']}/notebook", expect=400)
