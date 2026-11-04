from __future__ import annotations

import importlib.util
import tempfile
import unittest
import sys
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "prepare-docs-pages.py"
spec = importlib.util.spec_from_file_location("prepare_docs_pages", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class PagesPortableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="keycloak-pages-tests-")
        self.root = Path(self.temp.name)
        (self.root / "docs/documentation/topics/templates").mkdir(parents=True)
        attrs = """\
:project_docs_path: nightly
:pages_base_url: https://example.invalid
:docs_repo_edit_base: https://github.com/example/keycloak/edit/main
:project_doc_base_url: {pages_base_url}/docs/{project_docs_path}
:project_doc_base_url_latest: {pages_base_url}/docs/nightly
:apidocs_link: {project_doc_base_url}/api_documentation/
:apidocs_javadocs_link: https://www.keycloak.org/docs-api/latest/javadocs/
:apidocs_adminrest_link: https://www.keycloak.org/docs-api/latest/rest-api/
"""
        (self.root / "docs/documentation/topics/templates/document-attributes.adoc").write_text(attrs)
        (self.root / "_site-parent-marker").write_text("x")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _guide_html(self, slug: str, repository: str = "alice/keycloak-fork", extra: str = "") -> str:
        source = f"docs/documentation/{slug}/topics/test.adoc"
        source_path = self.root / source
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text("= Source\n")
        return f'''<!doctype html><html><head><title>{slug}</title></head><body>
<a href="https://github.com/{repository}/edit/main/docs/documentation/{slug}/topics/test.adoc">Edit this section</a>
<a href="https://github.com/keycloak/keycloak/issues/new?template=bug.yml">Report an issue</a>
{extra}</body></html>'''

    def _zip_tree(self, root: Path, archive: Path, base: str | None = None) -> None:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in root.rglob("*"):
                if path.is_file():
                    relative = path.relative_to(root).as_posix()
                    name = f"{base}/{relative}" if base else relative
                    zf.write(path, name)

    def _prepare_args(self, selection: str, api_reference: str = "false"):
        return type("Args", (), {
            "repo_root": self.root,
            "output_dir": self.root / "_site",
            "repository": "alice/keycloak-fork",
            "branch": "main",
            "pages_base_url": None,
            "selection": selection,
            "api_reference": api_reference,
        })()

    def test_repository_and_pages_url_are_derived_for_project_and_user_sites(self) -> None:
        cfg = module.make_config("alice/keycloak-fork", "main")
        self.assertEqual(cfg.base_url, "https://alice.github.io/keycloak-fork")
        self.assertEqual(cfg.edit_prefix, "https://github.com/alice/keycloak-fork/edit/main/docs/documentation/")
        user_site = module.make_config("alice/alice.github.io", "main")
        self.assertEqual(user_site.base_url, "https://alice.github.io")
        custom = module.make_config("alice/keycloak-fork", "main", "https://docs.example.org/keycloak")
        self.assertEqual(custom.base_url, "https://docs.example.org/keycloak")
        for unsafe in (
            "http://docs.example.org", "https://user@docs.example.org",
            "https://docs.example.org/../other", "https://docs.example.org/path?query=1",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                module.make_config("alice/keycloak-fork", "main", unsafe)
        with self.assertRaises(ValueError):
            module.make_config("alice/keycloak-fork", "main", "main;echo bad")

    def test_selection_parser_accepts_all_and_one_or_more_guides(self) -> None:
        selected, all_guides = module.parse_selection("all")
        self.assertTrue(all_guides)
        self.assertEqual(selected, module.GUIDE_ORDER)
        selected, all_guides = module.parse_selection("server_admin,authorization_services")
        self.assertFalse(all_guides)
        self.assertEqual(selected, ("authorization_services", "server_admin"))

    def test_selection_parser_rejects_unknown_and_duplicate_ids(self) -> None:
        with self.assertRaises(ValueError):
            module.parse_selection("server_admin,not-a-guide")
        with self.assertRaises(ValueError):
            module.parse_selection("server_admin,server_admin")

    def test_configure_rewrites_pages_and_edit_urls_for_the_current_fork(self) -> None:
        args = type("Args", (), {
            "repository": "alice/keycloak-fork", "branch": "main", "selection": "server_admin",
            "api_reference": "false", "pages_base_url": None, "repo_root": self.root,
        })()
        module.configure(args)
        text = (self.root / "docs/documentation/topics/templates/document-attributes.adoc").read_text()
        self.assertIn(":pages_base_url: https://alice.github.io/keycloak-fork", text)
        self.assertIn(":docs_repo_edit_base: https://github.com/alice/keycloak-fork/edit/main", text)
        self.assertIn(":project_doc_base_url: https://alice.github.io/keycloak-fork/docs/nightly", text)
        self.assertIn(":apidocs_link: https://alice.github.io/keycloak-fork/docs/nightly/api_documentation/", text)
        self.assertIn(":apidocs_javadocs_link: https://www.keycloak.org/docs-api/latest/javadocs/", text)

    def test_selected_build_copies_only_selected_guides_and_redirects_others(self) -> None:
        for guide in ("server_admin", "authorization_services"):
            out = self.root / "docs/documentation" / guide / "target/generated-docs"
            out.mkdir(parents=True)
            (out / "index.html").write_text(self._guide_html(guide))
            (out / "guide.css").write_text("body { font-family: sans-serif; }")
        module.prepare(self._prepare_args("server_admin,authorization_services"))
        site = self.root / "_site"
        self.assertTrue((site / "docs/nightly/server_admin/index.html").is_file())
        self.assertTrue((site / "docs/nightly/authorization_services/index.html").is_file())
        redirect = (site / "docs/nightly/api_documentation/index.html").read_text()
        self.assertIn("https://www.keycloak.org/docs/latest/api_documentation/", redirect)
        landing = (site / "docs/nightly/index.html").read_text()
        self.assertIn("Server Administration", landing)
        self.assertIn("Authorization Services", landing)
        self.assertIn("noindex", redirect)
        self.assertTrue((site / ".nojekyll").is_file())

    def test_full_build_assembles_docs_and_api_archives(self) -> None:
        docs = self.root / "docs/documentation/dist/target"
        docs.mkdir(parents=True)
        archive_root = self.root / "full-docs"
        for guide in module.GUIDE_ORDER:
            target = archive_root / guide
            target.mkdir(parents=True)
            (target / "index.html").write_text(self._guide_html(guide))
        (archive_root / "index.html").write_text("<html><head></head><body>old index</body></html>")
        self._zip_tree(archive_root, docs / "keycloak-documentation-test.zip", "keycloak-documentation-test")

        api_dir = self.root / "distribution/api-docs-dist/target"
        api_dir.mkdir(parents=True)
        api_root = self.root / "api-docs"
        for name in ("javadocs", "rest-api"):
            (api_root / name).mkdir(parents=True)
            (api_root / name / "index.html").write_text(
                f'<html><head></head><body>{name}<a href="#generated-reference">Generated link</a></body></html>'
            )
        (api_root / "index.html").write_text("<html><head></head><body>API index</body></html>")
        self._zip_tree(api_root, api_dir / "keycloak-api-docs-test.zip", "keycloak-api-docs-test")

        module.prepare(self._prepare_args("all"))
        site = self.root / "_site"
        self.assertTrue((site / "docs/nightly/api_documentation/index.html").is_file())
        self.assertTrue((site / "docs-api/nightly/javadocs/index.html").is_file())
        self.assertTrue((site / "docs-api/nightly/rest-api/index.html").is_file())
        self.assertIn("alice/keycloak-fork", (site / "index.html").read_text())

    def test_broken_internal_reference_is_rejected(self) -> None:
        guide = self.root / "docs/documentation/server_admin/target/generated-docs"
        guide.mkdir(parents=True)
        (guide / "index.html").write_text(self._guide_html("server_admin", extra='<a href="missing.html">broken</a>'))
        with self.assertRaisesRegex(RuntimeError, "Broken local reference"):
            module.prepare(self._prepare_args("server_admin"))

    def test_broken_guide_fragment_is_rejected(self) -> None:
        guide = self.root / "docs/documentation/server_admin/target/generated-docs"
        guide.mkdir(parents=True)
        html = self._guide_html("server_admin", extra='<a href="#missing">broken fragment</a>')
        (guide / "index.html").write_text(html)
        with self.assertRaisesRegex(RuntimeError, "Missing fragment #missing"):
            module.prepare(self._prepare_args("server_admin"))

    def test_missing_edit_source_is_rejected(self) -> None:
        guide = self.root / "docs/documentation/server_admin/target/generated-docs"
        guide.mkdir(parents=True)
        html = self._guide_html("server_admin")
        (self.root / "docs/documentation/server_admin/topics/test.adoc").unlink()
        (guide / "index.html").write_text(html)
        with self.assertRaisesRegex(RuntimeError, "Edit target does not exist"):
            module.prepare(self._prepare_args("server_admin"))

    def test_edit_link_must_point_to_fork(self) -> None:
        guide = self.root / "docs/documentation/server_admin/target/generated-docs"
        guide.mkdir(parents=True)
        html = self._guide_html("server_admin", repository="wrong/repo")
        (guide / "index.html").write_text(html)
        with self.assertRaisesRegex(RuntimeError, "Edit link points to another repository"):
            module.prepare(self._prepare_args("server_admin"))

    def test_selected_guide_can_include_the_api_reference(self) -> None:
        guide = self.root / "docs/documentation/server_admin/target/generated-docs"
        guide.mkdir(parents=True)
        (guide / "index.html").write_text(self._guide_html("server_admin"))
        api_dir = self.root / "distribution/api-docs-dist/target"
        api_dir.mkdir(parents=True)
        api_root = self.root / "selected-api"
        for name in ("javadocs", "rest-api"):
            (api_root / name).mkdir(parents=True)
            (api_root / name / "index.html").write_text(f"<html><head></head><body>{name}</body></html>")
        (api_root / "index.html").write_text("<html><head></head><body>API</body></html>")
        self._zip_tree(api_root, api_dir / "keycloak-api-docs-selected.zip", "keycloak-api-docs-selected")
        module.prepare(self._prepare_args("server_admin", api_reference="true"))
        site = self.root / "_site"
        self.assertTrue((site / "docs-api/nightly/javadocs/index.html").is_file())
        self.assertTrue((site / "docs-api/nightly/rest-api/index.html").is_file())
        self.assertIn("/docs-api/nightly/", (site / "index.html").read_text())

    def test_guide_without_edit_sidebar_can_still_be_selected(self) -> None:
        guide = self.root / "docs/documentation/release_notes/target/generated-docs"
        guide.mkdir(parents=True)
        (guide / "index.html").write_text("<html><head></head><body><h1>Release notes</h1></body></html>")
        module.prepare(self._prepare_args("release_notes"))
        self.assertTrue((self.root / "_site/docs/nightly/release_notes/index.html").is_file())

    def test_pages_workflow_keeps_dotfiles_and_artifact_permissions(self) -> None:
        repository_root = Path(__file__).resolve().parents[3]
        workflow = (repository_root / ".github/workflows/documentation-pages.yml").read_text()
        self.assertIn("actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9", workflow)
        self.assertIn("include-hidden-files: true", workflow)
        self.assertIn("actions: read", workflow)
        self.assertIn("actions/deploy-pages@368f82528645a54fb793d4d04e342629a3f51346", workflow)

    def test_zip_path_traversal_is_rejected(self) -> None:
        archive = self.root / "unsafe.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../escape.txt", "bad")
        with self.assertRaisesRegex(ValueError, "Unsafe path"):
            module.extract_archive(archive, self.root / "unsafe")

    def test_zip_symlink_is_rejected(self) -> None:
        archive = self.root / "symlink.zip"
        info = zipfile.ZipInfo("root/link")
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(info, "target")
        with self.assertRaisesRegex(ValueError, "Symbolic link"):
            module.extract_archive(archive, self.root / "symlink")


if __name__ == "__main__":
    unittest.main()
