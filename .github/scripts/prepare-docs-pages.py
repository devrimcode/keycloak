#!/usr/bin/env python3
"""Configure, assemble, validate, and smoke-test portable Keycloak Pages docs.

Supported guide IDs:
  api_documentation, authorization_services, server_admin,
  server_development, release_notes, upgrading

A full build consumes Keycloak's documentation ZIP. A selected build consumes
only the selected Maven modules' target/generated-docs directories. Unselected
guide routes become noindex redirects to the official Keycloak nightly guide,
so cross-guide links do not become dead ends.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from stat import S_ISLNK
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

GUIDES = {
    "api_documentation": "API Documentation",
    "authorization_services": "Authorization Services",
    "server_admin": "Server Administration",
    "server_development": "Server Development",
    "release_notes": "Release Notes",
    "upgrading": "Upgrading",
}
GUIDE_ORDER = tuple(GUIDES)
DOCS_PATH_ATTRIBUTE = "project_docs_path"
ISSUE_PREFIX = "https://github.com/keycloak/keycloak/issues/new?"
OFFICIAL_DOCS_BASE = "https://www.keycloak.org/docs/latest"
OFFICIAL_API_BASE = "https://www.keycloak.org/docs-api/latest"
MAX_PUBLISHED_BYTES = 1_000_000_000


@dataclass(frozen=True)
class SiteConfig:
    repository: str
    branch: str
    base_url: str
    host: str
    path_prefix: str

    @property
    def edit_prefix(self) -> str:
        return f"https://github.com/{self.repository}/edit/{self.branch}/docs/documentation/"

    @property
    def repo_edit_base(self) -> str:
        return f"https://github.com/{self.repository}/edit/{self.branch}"


def parse_repository(value: str) -> str:
    parts = value.strip().split("/")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in parts):
        raise ValueError(f"Expected GitHub repository as owner/name, got {value!r}")
    return "/".join(parts)


def derive_pages_url(repository: str, override: str | None = None) -> str:
    owner, name = parse_repository(repository).split("/")
    if override and override.strip():
        candidate = override.strip().rstrip("/")
        if re.search(r"[\s<>\"'\\]", candidate):
            raise ValueError("PAGES_BASE_URL must not contain whitespace, quotes, or backslashes")
        parsed = urlsplit(candidate)
        path_segments = [unquote(part) for part in parsed.path.split("/")]
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("PAGES_BASE_URL contains an invalid port") from exc
        if (
            parsed.scheme.lower() != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or any(part in {".", ".."} for part in path_segments)
        ):
            raise ValueError(
                "PAGES_BASE_URL must be an HTTPS base URL without credentials, dot-paths, query, or fragment"
            )
        return candidate
    base = f"https://{owner}.github.io"
    if name.lower() != f"{owner}.github.io".lower():
        base += f"/{name}"
    return base


def make_config(repository: str, branch: str, base_url: str | None = None) -> SiteConfig:
    repository = parse_repository(repository)
    if (
        not branch or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch)
        or branch.startswith(("/", ".")) or branch.endswith(("/", "."))
        or ".." in branch or "//" in branch or "@{" in branch
    ):
        raise ValueError(f"Invalid documentation edit branch: {branch!r}")
    resolved = derive_pages_url(repository, base_url)
    parsed = urlsplit(resolved)
    prefix = parsed.path.rstrip("/")
    return SiteConfig(repository, branch, resolved, parsed.netloc.lower(), prefix)


def parse_selection(value: str) -> tuple[tuple[str, ...], bool]:
    raw = value.strip()
    if not raw or raw.lower() == "all":
        return GUIDE_ORDER, True
    items = [part.strip() for part in raw.split(",") if part.strip()]
    if not items:
        raise ValueError("Choose 'all' or at least one guide ID")
    unknown = sorted(set(items) - set(GUIDES))
    if unknown:
        raise ValueError(f"Unknown guide ID(s): {', '.join(unknown)}. Allowed: {', '.join(GUIDE_ORDER)}")
    if len(items) != len(set(items)):
        raise ValueError("Duplicate guide IDs are not allowed")
    selected = tuple(guide for guide in GUIDE_ORDER if guide in items)
    return selected, False


def bool_value(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def write_github_outputs(values: dict[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def configure(args: argparse.Namespace) -> None:
    repository = parse_repository(args.repository or os.environ.get("GITHUB_REPOSITORY", ""))
    branch = args.branch or os.environ.get("DOCS_BRANCH", "main")
    selected, all_guides = parse_selection(args.selection)
    include_api = all_guides or "api_documentation" in selected or bool_value(args.api_reference)
    config = make_config(repository, branch, args.pages_base_url or os.environ.get("PAGES_BASE_URL"))
    root = args.repo_root.resolve()
    attr_file = root / "docs/documentation/topics/templates/document-attributes.adoc"
    if not attr_file.is_file():
        raise FileNotFoundError(f"Documentation attributes file not found: {attr_file}")
    source = attr_file.read_text(encoding="utf-8")
    replacements = {
        "pages_base_url": config.base_url,
        "docs_repo_edit_base": config.repo_edit_base,
        "project_doc_base_url": f"{config.base_url}/docs/nightly",
        "project_doc_base_url_latest": f"{config.base_url}/docs/nightly",
        "apidocs_link": f"{config.base_url}/docs/nightly/api_documentation/",
        "apidocs_javadocs_link": (
            f"{config.base_url}/docs-api/nightly/javadocs/" if include_api
            else f"{OFFICIAL_API_BASE}/javadocs/"
        ),
        "apidocs_adminrest_link": (
            f"{config.base_url}/docs-api/nightly/rest-api/" if include_api
            else f"{OFFICIAL_API_BASE}/rest-api/"
        ),
    }
    for key, value in replacements.items():
        pattern = re.compile(rf"(?m)^:{re.escape(key)}:.*$")
        source, count = pattern.subn(f":{key}: {value}", source)
        if count != 1:
            raise ValueError(f"Expected exactly one :{key}: attribute in {attr_file}; found {count}")
    attr_file.write_text(source, encoding="utf-8")

    modules = ",".join(f"docs/documentation/{guide}" for guide in selected)
    selection_value = "all" if all_guides else ",".join(selected)
    outputs = {
        "selection": selection_value,
        "selected_guides": ",".join(selected),
        "maven_modules": modules,
        "include_api_reference": str(include_api).lower(),
        "pages_base_url": config.base_url,
        "docs_branch": branch,
    }
    write_github_outputs(outputs)
    for key, value in outputs.items():
        print(f"{key}={value}")


def read_docs_path(repository_root: Path) -> str:
    attr_file = repository_root / "docs/documentation/topics/templates/document-attributes.adoc"
    source = attr_file.read_text(encoding="utf-8")
    values = re.findall(rf"(?m)^:{re.escape(DOCS_PATH_ATTRIBUTE)}:\s*([^\s]+)\s*$", source)
    if len(values) != 1 or values[0] != "nightly":
        raise ValueError(f"Expected exactly one :{DOCS_PATH_ATTRIBUTE}: nightly attribute in {attr_file}; got {values}")
    return values[0]


def find_archive(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern)) if directory.is_dir() else []
    if len(matches) != 1:
        raise FileNotFoundError(f"Expected exactly one {pattern!r} in {directory}; found {[p.name for p in matches]}")
    return matches[0]


def extract_archive(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    expanded = 0
    with zipfile.ZipFile(archive) as zipped:
        for item in zipped.infolist():
            name = PurePosixPath(item.filename)
            if name.is_absolute() or ".." in name.parts:
                raise ValueError(f"Unsafe path in {archive.name}: {item.filename}")
            mode = item.external_attr >> 16
            if S_ISLNK(mode):
                raise ValueError(f"Symbolic link in {archive.name}: {item.filename}")
            expanded += item.file_size
        if expanded > MAX_PUBLISHED_BYTES:
            raise ValueError(f"Archive {archive.name} expands beyond the 1 GB Pages limit")
        zipped.extractall(destination)
    roots = [destination] if (destination / "index.html").is_file() else [
        p for p in destination.iterdir() if p.is_dir() and (p / "index.html").is_file()
    ]
    if len(roots) != 1:
        raise ValueError(f"Expected one archive root containing index.html in {archive.name}; found {len(roots)}")
    return roots[0]


def write_docs_index(docs_root: Path, selected: tuple[str, ...], include_api: bool, repository: str) -> None:
    rows = "\n".join(
        f'    <li><a href="{guide}/index.html">{GUIDES[guide]}</a>'
        + ("</li>" if guide in selected else " (opens official Keycloak nightly documentation)</li>")
        for guide in GUIDE_ORDER
    )
    api_row = (
        f'    <li><a href="{OFFICIAL_API_BASE}/">REST API and JavaDocs (official Keycloak site)</a></li>'
        if not include_api else
        '    <li><a href="../../docs-api/nightly/">REST API and JavaDocs</a></li>'
    )
    html = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex">
<title>Keycloak development documentation</title></head><body>
<main><h1>Keycloak development documentation</h1>
<p>Unofficial documentation build from <code>{escape_html(repository)}</code>.</p>
<p>Generated guide selection:</p><ul>{rows}</ul><ul>{api_row}</ul>
<p>Unselected guide links open the official Keycloak nightly documentation.</p></main>
</body></html>'''
    (docs_root / "index.html").write_text(html, encoding="utf-8")


def escape_html(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def write_redirect(path: Path, target: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    target = escape_html(target)
    html = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex">
<meta http-equiv="refresh" content="0; url={target}"><title>Redirecting to official Keycloak documentation</title>
<script>location.replace("{target}" + location.hash);</script></head><body>
<p>This guide was not selected for this build. <a href="{target}">Open the official Keycloak nightly guide</a>.</p>
</body></html>'''
    (path / "index.html").write_text(html, encoding="utf-8")


def add_noindex(root: Path) -> None:
    custom_header = re.compile(r"(?s)<!-- CUSTOM HEADER START -->.*?<!-- CUSTOM HEADER END -->\s*")
    robots = re.compile(r"<meta\b(?=[^>]*\bname\s*=\s*(['\"])robots\1)[^>]*>", re.I)
    for page in root.rglob("*.html"):
        source = page.read_text(encoding="utf-8")
        if source.count("<!-- CUSTOM HEADER START -->") != source.count("<!-- CUSTOM HEADER END -->"):
            raise ValueError(f"Unbalanced custom-header markers in {page}")
        source = custom_header.sub("", source)
        tags = list(robots.finditer(source))
        if len(tags) > 1:
            raise ValueError(f"More than one robots meta tag in {page}")
        tag = '<meta name="robots" content="noindex">'
        if tags:
            m = tags[0]
            source = source[:m.start()] + tag + source[m.end():]
        else:
            head = re.search(r"<head\b[^>]*>", source, re.I)
            if not head:
                raise ValueError(f"No <head> element in {page}")
            source = source[:head.end()] + "\n" + tag + source[head.end():]
        page.write_text(source, encoding="utf-8")


def write_root_index(site_root: Path, config: SiteConfig, include_api: bool) -> None:
    api_line = (
        f'<p><a href="{config.base_url}/docs-api/nightly/">REST API and JavaDocs</a></p>'
        if include_api else ""
    )
    html = f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="robots" content="noindex"><meta http-equiv="refresh" content="0; url={config.base_url}/docs/nightly/">
<title>Keycloak documentation — {escape_html(config.repository)}</title></head><body>
<main><h1>Keycloak development documentation</h1>
<p>Unofficial documentation build from <code>{escape_html(config.repository)}</code>.</p>
<p><a href="{config.base_url}/docs/nightly/">Open documentation</a></p>{api_line}
<p><a href="https://github.com/{config.repository}">View source repository</a></p>
<p>This site is not the official Keycloak website.</p></main></body></html>'''
    (site_root / "index.html").write_text(html, encoding="utf-8")


class PageParser(HTMLParser):
    RESOURCE_ATTRIBUTES = {
        ("a", "href"), ("area", "href"), ("img", "src"), ("script", "src"),
        ("link", "href"), ("iframe", "src"), ("source", "src"),
        ("video", "poster"), ("audio", "src"), ("object", "data"),
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[tuple[str, str]] = []
        self.ids: set[str] = set()
        self.edit_links: list[str] = []
        self.issue_links: list[str] = []
        self.robots_values: list[str] = []
        self.anchor_href: str | None = None
        self.anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        for key in ("id", "name"):
            if values.get(key):
                self.ids.add(values[key] or "")
        for element, attribute in self.RESOURCE_ATTRIBUTES:
            if tag == element and values.get(attribute):
                self.references.append((attribute, values[attribute] or ""))
        if tag == "meta" and (values.get("name") or "").lower() == "robots":
            self.robots_values.append((values.get("content") or "").lower())
        if values.get("srcset"):
            for candidate in (values["srcset"] or "").split(","):
                parts = candidate.strip().split()
                if parts:
                    self.references.append(("srcset", parts[0]))
        if tag == "a" and self.anchor_href is None:
            self.anchor_href = values.get("href")
            self.anchor_text = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.anchor_href is not None:
            self.anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self.anchor_href is None:
            return
        label = " ".join("".join(self.anchor_text).split()).casefold()
        if self.anchor_href:
            if label == "edit this section":
                self.edit_links.append(self.anchor_href)
            elif label == "report an issue":
                self.issue_links.append(self.anchor_href)
        self.anchor_href = None
        self.anchor_text = []


def parse_page(path: Path) -> PageParser:
    parser = PageParser()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    return parser


def resolve_local_reference(
    site_root: Path, page: Path, reference: str, config: SiteConfig
) -> tuple[Path | None, str | None, str | None]:
    parsed = urlsplit(reference)
    root = site_root.resolve()
    if parsed.netloc:
        if parsed.netloc.lower() != config.host:
            return None, None, None
        if config.path_prefix and parsed.path == config.path_prefix:
            target = root
        elif config.path_prefix and parsed.path.startswith(config.path_prefix + "/"):
            target = root / unquote(parsed.path[len(config.path_prefix) + 1:])
        elif not config.path_prefix:
            target = root / unquote(parsed.path.lstrip("/"))
        else:
            return None, None, f"same-host URL is outside Pages base path: {reference}"
    elif parsed.scheme:
        return None, None, None
    elif parsed.path.startswith("/"):
        if config.path_prefix and parsed.path.startswith(config.path_prefix + "/"):
            target = root / unquote(parsed.path[len(config.path_prefix) + 1:])
        elif not config.path_prefix:
            target = root / unquote(parsed.path.lstrip("/"))
        else:
            return None, None, f"root-relative URL escapes Pages path {config.path_prefix}: {reference}"
    elif parsed.path:
        target = page.parent / unquote(parsed.path)
    else:
        target = page
    target = target.resolve()
    if target != root and root not in target.parents:
        return None, None, f"local reference escapes Pages root: {reference}"
    if target.is_dir() or parsed.path.endswith("/"):
        target = target / "index.html"
    elif not target.exists() and not target.suffix:
        candidates = (target.with_suffix(".html"), target / "index.html")
        target = next((p for p in candidates if p.is_file()), target)
    return target, unquote(parsed.fragment) if parsed.fragment else None, None


def validate_css(site_root: Path, config: SiteConfig, errors: list[str]) -> None:
    url_pattern = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I)
    import_pattern = re.compile(r"@import\s+(?:url\(\s*)?(['\"])(.*?)\1\s*\)?", re.I)
    for css in site_root.rglob("*.css"):
        try:
            source = css.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"Cannot read CSS {css.relative_to(site_root)}: {exc}")
            continue
        refs = [(m.group(2).strip(), "url()") for m in url_pattern.finditer(source)]
        refs += [(m.group(2).strip(), "@import") for m in import_pattern.finditer(source)]
        for ref, kind in refs:
            if not ref or ref.startswith("#"):
                continue
            target, _, problem = resolve_local_reference(site_root, css, ref, config)
            if problem:
                errors.append(f"Invalid CSS {kind} in {css.relative_to(site_root)}: {problem}")
            elif target is not None and not target.is_file():
                errors.append(f"Broken CSS {kind} in {css.relative_to(site_root)}: {ref}")


def validate_site(
    site_root: Path, repository_root: Path, config: SiteConfig,
    selected: tuple[str, ...], include_api: bool,
) -> None:
    errors: list[str] = []
    required = ["index.html", "docs/nightly/index.html", ".nojekyll"]
    required.extend(f"docs/nightly/{guide}/index.html" for guide in GUIDE_ORDER)
    if include_api:
        required.extend([
            "docs-api/nightly/index.html", "docs-api/nightly/javadocs/index.html",
            "docs-api/nightly/rest-api/index.html",
        ])
    for rel in required:
        if not (site_root / rel).is_file():
            errors.append(f"Required file missing: {rel}")

    total_bytes = 0
    for path in [site_root, *site_root.rglob("*")]:
        if path.is_symlink():
            errors.append(f"Symlink in Pages artifact: {path.relative_to(site_root)}")
        elif path.is_dir():
            continue
        elif path.is_file():
            st = path.stat()
            total_bytes += st.st_size
            if st.st_nlink != 1:
                errors.append(f"Hard link in Pages artifact: {path.relative_to(site_root)}")
        else:
            errors.append(f"Unsupported filesystem entry: {path.relative_to(site_root)}")
    if total_bytes >= MAX_PUBLISHED_BYTES:
        errors.append(f"Pages payload exceeds 1 GB: {total_bytes:,} bytes")

    html_files = sorted(site_root.rglob("*.html"))
    if not html_files:
        errors.append("No HTML files were produced")
    fragment_cache: OrderedDict[Path, set[str]] = OrderedDict()
    for page in html_files:
        rel = page.relative_to(site_root).as_posix()
        content = page.read_text(encoding="utf-8")
        parser = parse_page(page)
        if rel.startswith(("docs/nightly/", "docs-api/nightly/")) and not any(
            "noindex" in value for value in parser.robots_values
        ):
            errors.append(f"Nightly page missing robots noindex: {rel}")
        if "https://github.com/keycloak/keycloak/tree/main/docs/documentation/" in content:
            errors.append(f"Old upstream Edit URL found in {rel}")
        for href in parser.edit_links:
            if not href.startswith(config.edit_prefix):
                errors.append(f"Edit link points to another repository in {rel}: {href}")
                continue
            relative = unquote(href[len(config.edit_prefix):]).split("#", 1)[0].split("?", 1)[0]
            candidate = PurePosixPath(relative)
            if candidate.is_absolute() or ".." in candidate.parts or not relative.endswith(".adoc"):
                errors.append(f"Invalid Edit source in {rel}: {href}")
            elif not (repository_root / "docs/documentation" / Path(*candidate.parts)).is_file():
                errors.append(f"Edit target does not exist in this fork: {href}")
        for href in parser.issue_links:
            if not href.startswith(ISSUE_PREFIX):
                errors.append(f"Report an issue does not use official issue tracker in {rel}: {href}")
        for _, ref in parser.references:
            target, fragment, problem = resolve_local_reference(site_root, page, ref, config)
            if problem:
                errors.append(f"Invalid local reference in {rel}: {problem}")
                continue
            if target is None:
                continue
            if not target.is_file():
                errors.append(f"Broken local reference in {rel}: {ref}")
            elif fragment and target.suffix.lower() in {".html", ".htm"}:
                key = target.resolve()
                ids = parser.ids if key == page.resolve() else fragment_cache.get(key)
                if ids is None:
                    try:
                        ids = parse_page(target).ids
                        fragment_cache[key] = ids
                        if len(fragment_cache) > 128:
                            fragment_cache.popitem(last=False)
                    except (OSError, UnicodeError) as exc:
                        errors.append(f"Cannot inspect fragment target {target}: {exc}")
                        continue
                if fragment not in ids:
                    errors.append(f"Missing fragment #{fragment} in {target.relative_to(site_root)} (from {rel})")
    validate_css(site_root, config, errors)

    selected_html = [site_root / "docs/nightly" / guide / "index.html" for guide in selected]
    edit_count = sum(len(parse_page(p).edit_links) for p in selected_html if p.is_file())
    issue_count = sum(len(parse_page(p).issue_links) for p in selected_html if p.is_file())

    if errors:
        rendered = "\n".join(f" - {e}" for e in errors[:100])
        extra = f"\n... and {len(errors)-100} more errors" if len(errors) > 100 else ""
        raise RuntimeError(f"Pages validation failed ({len(errors)} errors):\n{rendered}{extra}")
    print(f"Validated {len(html_files)} HTML files, {edit_count} Edit links, {issue_count} issue links")
    print(f"Validated local HTML/CSS references; payload size {total_bytes:,} bytes")


def prepare(args: argparse.Namespace) -> None:
    root = args.repo_root.resolve()
    output = args.output_dir.resolve()
    expected = (root / "_site").resolve()
    if output != expected:
        raise ValueError(f"Output path is restricted to {expected}; refusing {output}")
    docs_path = read_docs_path(root)
    selected, all_guides = parse_selection(args.selection)
    include_api = all_guides or "api_documentation" in selected or bool_value(args.api_reference)
    config = make_config(args.repository, args.branch, args.pages_base_url)

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    docs_dest = output / "docs" / docs_path
    docs_dest.mkdir(parents=True)

    if all_guides:
        archive = find_archive(root / "docs/documentation/dist/target", "keycloak-documentation-*.zip")
        with tempfile.TemporaryDirectory(prefix="keycloak-docs-") as temp:
            archive_root = extract_archive(archive, Path(temp) / "docs")
            for guide in GUIDE_ORDER:
                source = archive_root / guide
                if not (source / "index.html").is_file():
                    raise ValueError(f"Full documentation ZIP is missing {guide}/index.html")
                shutil.copytree(source, docs_dest / guide)
    else:
        for guide in selected:
            source = root / "docs/documentation" / guide / "target/generated-docs"
            if not (source / "index.html").is_file():
                raise FileNotFoundError(
                    f"Selected guide output missing: {source}/index.html. Build Maven module docs/documentation/{guide} first."
                )
            shutil.copytree(source, docs_dest / guide)

    for guide in GUIDE_ORDER:
        if guide not in selected:
            write_redirect(docs_dest / guide, f"{OFFICIAL_DOCS_BASE}/{guide}/")
    write_docs_index(docs_dest, selected, include_api, config.repository)

    if include_api:
        api_archive = find_archive(root / "distribution/api-docs-dist/target", "keycloak-api-docs-*.zip")
        with tempfile.TemporaryDirectory(prefix="keycloak-api-") as temp:
            api_root = extract_archive(api_archive, Path(temp) / "api")
            for required in ("javadocs/index.html", "rest-api/index.html"):
                if not (api_root / required).is_file():
                    raise ValueError(f"API archive is missing {required}")
            shutil.copytree(api_root, output / "docs-api" / docs_path)

    add_noindex(docs_dest)
    if include_api:
        add_noindex(output / "docs-api" / docs_path)
    write_root_index(output, config, include_api)
    (output / ".nojekyll").touch()
    validate_site(output, root, config, selected, include_api)
    print(f"Prepared {config.base_url} for {config.repository}; guides: {', '.join(selected)}; API reference: {include_api}")


def fetch_url(url: str, attempts: int = 5) -> bytes:
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(url, headers={"User-Agent": "Keycloak-docs-pages-smoke-test/2.0"})
            with urlopen(request, timeout=20) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"Unexpected HTTP {response.status} for {url}")
                return response.read()
        except HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code not in (404, 429):
                raise RuntimeError(f"HTTP {exc.code} for {url}") from exc
            last = exc
        except (URLError, TimeoutError, RuntimeError) as exc:
            last = exc
        if attempt + 1 < attempts:
            time.sleep(min(2 * (attempt + 1), 8))
    raise RuntimeError(f"Could not fetch {url} after {attempts} attempts: {last}")


def smoke(args: argparse.Namespace) -> None:
    selected, all_guides = parse_selection(args.selection)
    include_api = all_guides or "api_documentation" in selected or bool_value(args.api_reference)
    config = make_config(args.repository, args.branch, args.base_url)
    base = config.base_url
    paths = ["/", "/docs/nightly/", *[f"/docs/nightly/{guide}/index.html" for guide in GUIDE_ORDER]]
    if include_api:
        paths += ["/docs-api/nightly/", "/docs-api/nightly/javadocs/index.html", "/docs-api/nightly/rest-api/index.html"]
    for path in paths:
        fetch_url(base + path)
        print(f"HTTP OK {path}")
    for guide in selected:
        page_url = base + f"/docs/nightly/{guide}/index.html"
        html = fetch_url(page_url).decode("utf-8", errors="replace")
        parser = PageParser()
        parser.feed(html)
        parser.close()
        if not all(link.startswith(config.edit_prefix) for link in parser.edit_links):
            raise RuntimeError(f"Published {guide} page contains an Edit link to another repository")
        if not all(link.startswith(ISSUE_PREFIX) for link in parser.issue_links):
            raise RuntimeError(f"Published {guide} page contains a Report an issue link outside the official tracker")
        if parser.edit_links:
            fetch_url(parser.edit_links[0])
            print(f"HTTP OK Edit link for {guide}: {parser.edit_links[0]}")
    print("Published Pages smoke test passed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("configure", help="derive this fork's URLs and Maven selection")
    p.add_argument("--repo-root", type=Path, default=Path.cwd())
    p.add_argument("--repository", default=None)
    p.add_argument("--branch", default=None)
    p.add_argument("--pages-base-url", default=None)
    p.add_argument("--selection", default="all")
    p.add_argument("--api-reference", default="false")
    p.set_defaults(function=configure)

    p = commands.add_parser("prepare", help="assemble and validate selected documentation")
    p.add_argument("--repo-root", type=Path, default=Path.cwd())
    p.add_argument("--output-dir", type=Path, default=Path("_site"))
    p.add_argument("--repository", required=True)
    p.add_argument("--branch", default="main")
    p.add_argument("--pages-base-url", default=None)
    p.add_argument("--selection", default="all")
    p.add_argument("--api-reference", default="false")
    p.set_defaults(function=prepare)

    p = commands.add_parser("smoke", help="check the live Pages deployment")
    p.add_argument("--base-url", required=True)
    p.add_argument("--repository", required=True)
    p.add_argument("--branch", default="main")
    p.add_argument("--selection", default="all")
    p.add_argument("--api-reference", default="false")
    p.set_defaults(function=smoke)

    args = parser.parse_args()
    try:
        args.function(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
