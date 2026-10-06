#!/usr/bin/env python3
"""open-jobs -- a public, CC0 index of roughly 2M postings crawled from about
65,000 company boards (github.com/elliottdehn/open-jobs). Disabled by default
(`sources.openjobs.enabled` in settings.yml); it is the only source in this
directory that sends anything off the Mac.

What leaves: one "ideal job" description, built entirely from the lines under
`sources.openjobs` in settings.yml -- title, must-haves, nice-to-haves, the pay
floor, and the fact that he is remote from Sri Lanka. Never the CV, never his
name, email or phone. It is embedded by the service's own `/embed` endpoint
(rate-limited 10/10min per IP) into the same space as their crawled postings,
and the embedding is cached locally so a day that has not changed the profile
never calls it again.

The rest of the protocol needs no key and no heavy dependency:

  * `/data/manifest.json` -- the leaf-group tree and where the centroid file is.
  * `/data/centroids.bin` -- one float16 vector per leaf, concatenated. Decoded
    with `struct` (format code `e`, IEEE-754 half precision, stdlib since 3.6)
    rather than numpy -- this project's rule is stdlib first, and a few
    thousand centroids at ~1500 dimensions is a sub-second pure-Python job.
  * `/data/<groups-prefix><leaf id>.json` -- one leaf's postings, as plain JSON
    (`{"jobs": [...]}`). The upstream CLI copies these into a local parquet
    file for its own querying; this module reads the JSON directly and skips
    parquet and duckdb entirely, because neither is installed here and neither
    is needed to land rows in `posting`.

Their own key format, `<ats>/<slug>#<id>`, is stored verbatim as our
`source_id` -- it is also exactly the key `/status` wants, which is what
`status_check.py` uses to ask whether a posting is still live.
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
import sys
import time
from pathlib import Path

from . import Posting, epoch, get

HERE = Path(__file__).parent
CACHE = HERE.parent / ".openjobs-cache"
IDEAL_CACHE = CACHE / "ideal.json"
NAME = "openjobs"
BASE = "https://backend.dehnbostele.workers.dev"


def _ideal_text(conf: dict) -> tuple[str, str, str]:
    """(text, title, location). Built only from settings.yml -- no resume, no
    identity. `conf` is `settings()["sources"]["openjobs"]`."""
    title = conf.get("title", "")
    musts = conf.get("must_haves") or []
    nices = conf.get("nice_to_haves") or []
    lines = [
        f"{title} — Remote",
        "",
        "About 1-2 years of professional experience, based in Sri Lanka and",
        "working remotely for a distributed or worldwide team. Comfortable",
        "with a partial-overlap working day rather than a fixed office shift.",
        "",
        "Must-haves:",
        *(f"- {m}" for m in musts),
    ]
    if nices:
        lines += ["", "Nice-to-haves:", *(f"- {n}" for n in nices)]
    lines += ["", "Open to worldwide-remote, region-open or EOR/contractor roles;",
              "on-site only if it is on-site in Sri Lanka."]
    return "\n".join(lines), title, "Remote, worldwide (based in Sri Lanka)"


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _load_cached_ideal(text_hash: str) -> dict | None:
    if not IDEAL_CACHE.exists():
        return None
    try:
        cached = json.loads(IDEAL_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return cached if cached.get("text_hash") == text_hash else None


def embed(cfg: dict) -> dict | None:
    """The cached ideal-JD vector, embedding it fresh only when the built text
    has changed. Returns None (and logs to stderr) on any failure -- a source
    module must never raise; ingest.py already isolates it, this is belt and
    suspenders for a stage that also gets called from status-free tooling."""
    conf = cfg.get("sources", {}).get(NAME, {})
    text, title, location = _ideal_text(conf)
    text_hash = _text_hash(text)
    cached = _load_cached_ideal(text_hash)
    if cached:
        return cached
    payload = get(f"{BASE}/embed", cfg=cfg,
                  json_body={"text": text, "title": title, "location": location})
    if not payload or "vector" not in payload:
        print(f"openjobs: /embed did not answer -- skipping this run", file=sys.stderr)
        return None
    out = {"vector": payload["vector"], "recipe": payload.get("recipe", ""),
          "text_hash": text_hash, "embedded_at": int(time.time())}
    CACHE.mkdir(parents=True, exist_ok=True)
    IDEAL_CACHE.write_text(json.dumps(out))
    return out


def _decode_half_floats(data: bytes) -> list[float]:
    """IEEE-754 half precision -> python float, without numpy. `struct`'s `e`
    format code has done this natively since 3.6; the only work here is
    unpacking the whole buffer in one call rather than one float at a time."""
    n = len(data) // 2
    return list(struct.unpack(f"<{n}e", data[: n * 2]))


def _norm(vec: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vec)) or 1.0


def nearest_leaves(manifest: dict, centroids: list[float], vector: list[float],
                    k: int) -> list[dict]:
    """The k nearest leaf nodes to `vector`, by cosine similarity. Pure Python:
    leaves * dims multiply-adds, which for this index's shape (order 10^3
    leaves, order 10^3 dims) is a fraction of a second -- the same trade this
    project already made in `freshness.py`'s percentile code over numpy."""
    dims = manifest["dims"]
    tree = manifest["tree"]
    inv_norm = 1.0 / _norm(vector)
    unit = [x * inv_norm for x in vector]
    scored = []
    for node in tree:
        if node.get("children"):
            continue                        # only leaves carry postings
        start = node["id"] * dims
        centroid = centroids[start:start + dims]
        if len(centroid) < dims:
            continue
        cnorm = _norm(centroid)
        sim = sum(a * b for a, b in zip(unit, centroid)) / cnorm
        scored.append((sim, node))
    scored.sort(key=lambda x: -x[0])
    return [node for _, node in scored[:k]]


def _group_jobs(cfg: dict, manifest: dict, leaf: dict) -> list[dict]:
    prefix = manifest.get("groups", "groups/")
    payload = get(f"{BASE}/data/{prefix}{leaf['id']}.json", cfg=cfg)
    if not isinstance(payload, dict):
        return []
    return payload.get("jobs") or []


def fetch(cfg: dict) -> list[Posting]:
    conf = cfg.get("sources", {}).get(NAME, {})
    ideal = embed(cfg)
    if not ideal:
        return []
    manifest = get(f"{BASE}/data/manifest.json", cfg=cfg)
    if not manifest:
        print("openjobs: manifest unreachable -- skipping this run", file=sys.stderr)
        return []
    raw = get(f"{BASE}/data/centroids.bin", cfg=cfg, binary=True)
    if not raw:
        print("openjobs: centroids unreachable -- skipping this run", file=sys.stderr)
        return []
    centroids = _decode_half_floats(raw)

    leaves = nearest_leaves(manifest, centroids, ideal["vector"],
                            int(conf.get("top_groups", 8)))
    out: list[Posting] = []
    for leaf in leaves:
        for j in _group_jobs(cfg, manifest, leaf):
            ats, slug, ident = j.get("ats"), j.get("slug"), j.get("id")
            if not (ats and slug and ident and j.get("title") and j.get("url")):
                continue
            out.append(Posting(
                source=NAME,
                # Their own key format -- also exactly what /status wants.
                source_id=f"{ats}/{slug}#{ident}",
                url=j["url"], title=j["title"], company=j.get("company", ""),
                description=j.get("jd") or "",
                posted_at=epoch(j.get("pub")) or epoch(j.get("seen")),
                location_raw=j.get("location") or "",
                apply_url=j["url"],
                salary_raw={}, raw={"ats": ats, "slug": slug},
            ))
    return out
