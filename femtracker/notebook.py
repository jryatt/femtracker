"""Notebook rendering: Markdown/HTML -> sanitized HTML, used by the preview, the saved entry and the
export, so there is exactly one place that decides what a notebook entry is allowed to contain.

Reference syntax, resolved against the database and turned into links (or a visible "not found"
marker, never an error):
  [[job:5]]  [[attempt:5/2]]  [[result:123]]  [[metrics:job:5]]  [[git:a1b2c3d]]  [[attachment:<sha256>]]
[[git:...]] takes any prefix of a commit hash (as git log --oneline shows) and finds the attempt(s)
whose recorded git state used it. [[attachment:...]] links to a zipped folder attached to an entry.
Images: ![caption](asset:<sha256>) for a pasted/uploaded screen grab, ![caption](result:<id>) for an
image already stored as a result. Both are rewritten to a same-origin /api/notebook/... URL before
sanitizing, so the sanitizer never has to trust a custom URL scheme.
"""
import base64
import binascii
import html
import json
import re
import struct
from html.parser import HTMLParser

import nh3
from markdown_it import MarkdownIt

ALLOWED_TAGS = {
    "p", "br", "hr", "strong", "em", "s", "code", "pre", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
    "table", "thead", "tbody", "tr", "th", "td", "a", "img", "span", "div",
}
ALLOWED_ATTRS = {
    "a": {"href", "title", "target", "rel", "download", "class", "data-kind", "data-id", "data-job", "data-number"},
    "img": {"src", "alt", "title"},
    "code": {"class"},
    "pre": {"class"},
    "th": {"style"},
    "td": {"style"},
    "span": {"class", "title"},
    "div": {"class"},
}

def _img_src_filter(tag, attr, value):
    if tag == "img" and attr == "src":
        return value if value.startswith("/api/") else None
    return value


def sanitize_html(raw: str) -> str:
    return nh3.clean(raw, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, attribute_filter=_img_src_filter,
                     link_rel=None, filter_style_properties={"text-align"}, strip_comments=True)


# .ipynb rendering is the only place a data: URI is ever allowed, and only for an <img src> we built
# ourselves in _nb_image() after sniffing the real bytes - never for anything reached via user HTML/MD.
_DATA_IMG_RE = re.compile(r"^data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+$")


def _ipynb_img_filter(tag, attr, value):
    if tag == "img" and attr == "src":
        return value if _DATA_IMG_RE.match(value) else None
    if value.startswith("data:"):
        return None
    return value


def _sanitize_ipynb_html(raw: str) -> str:
    return nh3.clean(raw, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, attribute_filter=_ipynb_img_filter,
                     link_rel=None, filter_style_properties={"text-align"}, strip_comments=True,
                     url_schemes=nh3.ALLOWED_URL_SCHEMES | {"data"})


def _render_link_open(self, tokens, idx, options, env):
    token = tokens[idx]
    href = token.attrGet("href") or ""
    if href.startswith("http://") or href.startswith("https://"):
        token.attrSet("target", "_blank")
        token.attrSet("rel", "noopener noreferrer")
    return self.renderToken(tokens, idx, options, env)


MD = MarkdownIt("gfm-like").disable("linkify")
MD.add_render_rule("link_open", _render_link_open)

IMAGE_SRC_RE = re.compile(r'src="asset:([a-fA-F0-9]{64})"'), re.compile(r'src="result:(\d+)"')


def rewrite_image_srcs(raw: str) -> str:
    raw = IMAGE_SRC_RE[0].sub(r'src="/api/notebook/assets/\1"', raw)
    raw = IMAGE_SRC_RE[1].sub(r'src="/api/notebook/results/\1/image"', raw)
    return raw


REF_RE = re.compile(r"\[\[(job|attempt|result|metrics|git|attachment):([^\]\[\s]+)\]\]")
SKIP_TEXT_IN = {"code", "pre", "script", "style"}

# A job's current metrics: its latest attempt that has been populated (done/skipped), plus hand-added results.
JOB_REPORTED_ATTEMPT = ("(SELECT id FROM attempts WHERE job_id=? AND populate_status IN ('done','skipped') "
                        "ORDER BY number DESC LIMIT 1)")


def _ref_anchor(kind, data_id, label, title=None, **extra):
    attrs = f' data-kind="{html.escape(kind)}" data-id="{html.escape(str(data_id))}"'
    if title:
        attrs += f' title="{html.escape(title)}"'
    for k, v in extra.items():
        attrs += f' data-{k}="{html.escape(str(v))}"'
    return f'<a class="nbref" href="#"{attrs}>{html.escape(label)}</a>'


def _ref_missing(text):
    return f'<span class="nbref-missing" title="Not found in this database">[[{html.escape(text)}]] (not found)</span>'


def _fmt_bytes(n):
    for unit, size in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} B"


def find_attempts_by_commit(c, prefix):
    """Attempts whose recorded git commit starts with this hex prefix, newest first."""
    prefix = prefix.lower()
    out = []
    for r in c.execute("SELECT a.job_id, a.number, a.git, j.name AS job_name FROM attempts a "
                       "JOIN jobs j ON j.id=a.job_id WHERE a.git IS NOT NULL ORDER BY a.id DESC"):
        git = json.loads(r["git"] or "null")
        if git and (git.get("commit") or "").lower().startswith(prefix):
            out.append({"job_id": r["job_id"], "number": r["number"], "job_name": r["job_name"],
                       "commit": git["commit"]})
    return out


def resolve_ref(c, kind, arg):
    """kind/arg from a [[kind:arg]] token -> HTML (a link, a missing-marker, or a small metrics table)."""
    try:
        if kind == "job":
            jid = int(arg)
            row = c.execute("SELECT name FROM jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                return _ref_missing(f"job:{arg}")
            return _ref_anchor("job", jid, f"Job #{jid} {row['name']}")
        if kind == "attempt":
            jid_s, num_s = arg.split("/")
            jid, num = int(jid_s), int(num_s)
            row = c.execute("SELECT j.name FROM attempts a JOIN jobs j ON j.id=a.job_id "
                            "WHERE a.job_id=? AND a.number=?", (jid, num)).fetchone()
            if not row:
                return _ref_missing(f"attempt:{arg}")
            return _ref_anchor("attempt", f"{jid}/{num}", f"Job #{jid} {row['name']}, attempt {num}",
                               job=jid, number=num)
        if kind == "result":
            rid = int(arg)
            row = c.execute("SELECT job_id, name FROM results WHERE id=?", (rid,)).fetchone()
            if not row:
                return _ref_missing(f"result:{arg}")
            return _ref_anchor("result", rid, f"Result #{rid} {row['name']}", job=row["job_id"])
        if kind == "metrics":
            if not arg.startswith("job:"):
                return _ref_missing(f"metrics:{arg}")
            jid = int(arg[len("job:"):])
            job = c.execute("SELECT name FROM jobs WHERE id=?", (jid,)).fetchone()
            if not job:
                return _ref_missing(f"metrics:{arg}")
            merged = {}
            for r in c.execute(f"SELECT metrics FROM results WHERE job_id=? AND superseded_at IS NULL "
                               f"AND (attempt_id IS NULL OR attempt_id={JOB_REPORTED_ATTEMPT}) ORDER BY id",
                               (jid, jid)):
                merged.update(json.loads(r[0] or "{}"))
            if not merged:
                return f'<p><em>No metrics yet for job #{jid} {html.escape(job["name"])}.</em></p>'
            rows = "".join(f"<tr><td>{html.escape(k)}</td><td>{html.escape(str(v))}</td></tr>"
                          for k, v in merged.items())
            return (f'<table><thead><tr><th>Metric</th><th>Job #{jid} {html.escape(job["name"])}</th></tr>'
                   f'</thead><tbody>{rows}</tbody></table>')
        if kind == "git":
            if len(arg) < 4:
                return _ref_missing(f"git:{arg}")
            matches = find_attempts_by_commit(c, arg)
            if not matches:
                return _ref_missing(f"git:{arg}")
            primary = matches[0]
            label = f"{arg} — Job #{primary['job_id']} {primary['job_name']}, attempt {primary['number']}"
            title = None
            if len(matches) > 1:
                label += f" (+{len(matches) - 1} more)"
                title = "Also used by: " + ", ".join(
                    f"job #{m['job_id']} attempt {m['number']}" for m in matches[1:])
            return _ref_anchor("git", f"{primary['job_id']}/{primary['number']}", label, title=title,
                               job=primary["job_id"], number=primary["number"])
        if kind == "attachment":
            sha = arg.lower()
            row = c.execute("SELECT filename, size FROM notebook_attachments WHERE sha256=?", (sha,)).fetchone()
            if not row:
                return _ref_missing(f"attachment:{arg}")
            name = html.escape(row["filename"])
            return f'<a href="/api/notebook/attachments/{sha}" download="{name}">{name} ({_fmt_bytes(row["size"])})</a>'
    except (ValueError, IndexError):
        pass
    return _ref_missing(f"{kind}:{arg}")


class _RefWalker(HTMLParser):
    """Replaces [[...]] references in text nodes only, leaving code/pre/script/style untouched."""

    def __init__(self, c):
        super().__init__(convert_charrefs=False)
        self.c = c
        self.skip_depth = 0
        self.out = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TEXT_IN:
            self.skip_depth += 1
        self.out.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        self.out.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        self.out.append(f"</{tag}>")
        if tag in SKIP_TEXT_IN and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data):
        if self.skip_depth:
            self.out.append(data)
        else:
            self.out.append(REF_RE.sub(lambda m: resolve_ref(self.c, m.group(1), m.group(2)), data))

    def handle_entityref(self, name):
        self.out.append(f"&{name};")

    def handle_charref(self, name):
        self.out.append(f"&#{name};")

    def handle_comment(self, data):
        self.out.append(f"<!--{data}-->")

    def get_html(self):
        return "".join(self.out)


def resolve_bracket_refs(raw: str, c) -> str:
    if "[[" not in raw:
        return raw
    walker = _RefWalker(c)
    walker.feed(raw)
    walker.close()
    return walker.get_html()


def render_body(c, fmt: str, body: str) -> str:
    """The one path: Markdown or raw HTML -> reference resolution -> sanitized HTML."""
    raw = MD.render(body or "") if fmt == "markdown" else (body or "")
    raw = rewrite_image_srcs(raw)
    raw = resolve_bracket_refs(raw, c)
    return sanitize_html(raw)


# ---------- .ipynb viewing: read-only, no kernel, same sanitize pass as everything else ----------
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _nb_text(src) -> str:
    """A cell's 'source' or an output's text: either a string or a list of line strings."""
    return "".join(src) if isinstance(src, list) else (src or "")


def _nb_image(data: str):
    """Base64 output data -> a verified data: URI, or None if it doesn't decode to a real image
    (never trust the notebook's own mime label)."""
    try:
        raw = base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return None
    try:
        sniffed, _, _ = detect_image(raw)
    except BadImage:
        return None
    return f'<img src="data:{sniffed};base64,{base64.b64encode(raw).decode("ascii")}">'


def _nb_output(out: dict) -> str:
    otype = out.get("output_type")
    if otype == "stream":
        return f"<pre>{html.escape(_nb_text(out.get('text')))}</pre>"
    if otype == "error":
        trace = "\n".join(ANSI_RE.sub("", line) for line in out.get("traceback") or [])
        header = f"{out.get('ename', 'Error')}: {out.get('evalue', '')}"
        return f'<pre class="nb-error">{html.escape(trace or header)}</pre>'
    if otype in ("execute_result", "display_data"):
        data = out.get("data") or {}
        for mime in ("image/png", "image/jpeg", "image/gif"):
            if mime in data:
                img = _nb_image(_nb_text(data[mime]))
                if img:
                    return img
        if "text/html" in data:
            return _nb_text(data["text/html"])
        if "text/plain" in data:
            return f"<pre>{html.escape(_nb_text(data['text/plain']))}</pre>"
    return ""  # image/svg+xml and anything else: not shown, consistent with "no SVG, ever"


def _nb_cell(cell: dict) -> str:
    ctype = cell.get("cell_type")
    source = _nb_text(cell.get("source"))
    if ctype == "markdown":
        body = MD.render(source)
    elif ctype == "code":
        outputs = "".join(_nb_output(o) for o in cell.get("outputs") or [])
        body = f"<pre><code>{html.escape(source)}</code></pre>{outputs}"
    else:
        return ""
    return f'<div class="nb-cell nb-{html.escape(ctype)}">{body}</div>'


def render_ipynb(raw: str) -> str:
    """A .ipynb file's JSON text -> sanitized read-only HTML. No kernel, nothing executes."""
    try:
        nb = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Not a valid .ipynb file: {e}")
    cells = nb.get("cells") if isinstance(nb, dict) else None
    if not isinstance(cells, list):
        raise ValueError("Not a valid .ipynb file: no cells")
    return _sanitize_ipynb_html("".join(_nb_cell(c) for c in cells))


# ---------- Images: real-content sniffing (never trust the filename or declared content-type) ----------
class BadImage(ValueError):
    pass


def _png_size(data):
    return struct.unpack(">II", data[16:24])


def _gif_size(data):
    return struct.unpack("<HH", data[6:10])


def _jpeg_size(data):
    pos = 2
    n = len(data)
    while pos + 4 <= n:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if pos + 4 > n:
            break
        seg_len = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if pos + 9 > n:
                break
            height, width = struct.unpack(">HH", data[pos + 5:pos + 9])
            return width, height
        pos += 2 + seg_len
    raise BadImage("Could not find JPEG image dimensions")


def _webp_size(data):
    fourcc = data[12:16]
    if fourcc == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            raise BadImage("Unrecognised WebP (lossy) frame header")
        w, h = struct.unpack("<HH", data[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if fourcc == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if fourcc == b"VP8X":
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return w, h
    raise BadImage("Unrecognised WebP sub-format")


def detect_image(data: bytes):
    """Sniff the real file contents -> (mime, width, height), or raise BadImage. No SVG, ever."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            w, h = _png_size(data)
        except struct.error:
            raise BadImage("This doesn't look like a valid PNG file")
        return "image/png", w, h
    if data[:6] in (b"GIF87a", b"GIF89a"):
        try:
            w, h = _gif_size(data)
        except struct.error:
            raise BadImage("This doesn't look like a valid GIF file")
        return "image/gif", w, h
    if data[:3] == b"\xff\xd8\xff":
        w, h = _jpeg_size(data)
        return "image/jpeg", w, h
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        w, h = _webp_size(data)
        return "image/webp", w, h
    raise BadImage("Only PNG, JPEG, GIF and WebP images are accepted (not SVG, and not whatever this is)")
