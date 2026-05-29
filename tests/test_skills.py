import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from token_dashboard import skills as _skills_mod
from token_dashboard.skills import (
    scan_catalog,
    _slugs_for,
    _project_skill_roots_from_cwds,
    cached_catalog,
    _cache,
)
from token_dashboard.db import connect, init_db


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_user_skill(self):
        _write(self.tmp / "skills" / "frontend-design" / "SKILL.md", "x" * 400)
        cat = scan_catalog([self.tmp / "skills"])
        self.assertIn("frontend-design", cat)
        self.assertEqual(cat["frontend-design"]["chars"], 400)
        self.assertEqual(cat["frontend-design"]["tokens"], 100)

    def test_plugin_skill_registers_both_slugs(self):
        p = self.tmp / "plugins" / "marketplaces" / "official" / "plugins" / "superpowers" / "skills" / "brainstorming" / "SKILL.md"
        _write(p, "y" * 800)
        cat = scan_catalog([self.tmp / "plugins"])
        self.assertIn("brainstorming", cat)
        self.assertIn("superpowers:brainstorming", cat)
        self.assertEqual(cat["brainstorming"]["tokens"], 200)
        self.assertEqual(cat["superpowers:brainstorming"]["tokens"], 200)

    def test_scheduled_task_skill(self):
        _write(self.tmp / "scheduled-tasks" / "morning-coffee" / "SKILL.md", "z" * 100)
        cat = scan_catalog([self.tmp / "scheduled-tasks"])
        self.assertIn("morning-coffee", cat)

    def test_nested_skills_skills_dedup_prefers_shallow(self):
        shallow = self.tmp / "skills" / "foo" / "SKILL.md"
        deep    = self.tmp / "skills" / "skills" / "foo" / "SKILL.md"
        _write(shallow, "s" * 100)
        _write(deep, "d" * 999)
        cat = scan_catalog([self.tmp / "skills"])
        # Shallow wins
        self.assertEqual(cat["foo"]["chars"], 100)

    def test_slugs_for_plugin_path(self):
        p = Path("plugins/marketplaces/x/plugins/superpowers/skills/brainstorming/SKILL.md")
        slugs = set(_slugs_for(p))
        # Exactly two slugs: bare + plugin-qualified. No marketplace alias.
        self.assertEqual(slugs, {"brainstorming", "superpowers:brainstorming"})

    def test_slugs_for_marketplace_is_not_a_plugin_prefix(self):
        # The marketplace directory name (`x` below) must NEVER be registered
        # as a plugin prefix — Claude Code only accepts <plugin>:<skill>.
        p = Path("plugins/marketplaces/x/plugins/superpowers/skills/brainstorming/SKILL.md")
        slugs = set(_slugs_for(p))
        self.assertNotIn("x:brainstorming", slugs)
        self.assertNotIn("plugins:brainstorming", slugs)
        self.assertNotIn("marketplaces:brainstorming", slugs)

    def test_slugs_for_cache_versioned_path(self):
        p = Path("plugins/cache/claude-plugins-official/superpowers/5.0.7/skills/brainstorming/SKILL.md")
        slugs = set(_slugs_for(p))
        self.assertEqual(slugs, {"brainstorming", "superpowers:brainstorming"})
        # No version segment, no marketplace name leakage.
        self.assertNotIn("5.0.7:brainstorming", slugs)
        self.assertNotIn("claude-plugins-official:brainstorming", slugs)

    def test_slugs_for_cache_unversioned_path(self):
        # Defensive: some cache layouts skip the version directory.
        p = Path("plugins/cache/claude-plugins-official/superpowers/skills/brainstorming/SKILL.md")
        slugs = set(_slugs_for(p))
        self.assertEqual(slugs, {"brainstorming", "superpowers:brainstorming"})

    def test_slugs_for_cache_temp_git_has_no_plugin_prefix(self):
        # temp_git_* checkouts never expose a plugin name; only bare is valid.
        p = Path("plugins/cache/temp_git_abc123/skills/brainstorming/SKILL.md")
        self.assertEqual(_slugs_for(p), ["brainstorming"])

    def test_slugs_for_windows_path_no_home_pollution(self):
        # Regression: on Windows the absolute path includes the home segments
        # ("Users", "<username>"). They must NOT become plugin prefixes.
        # Using a constructed parts-tuple to exercise the algorithm without
        # depending on which OS the test runs on.
        from token_dashboard.skills import _plugin_name_from_path
        windows_parts = (
            "C:\\", "Users", "marcu", ".claude", "plugins", "marketplaces",
            "buzzwoo-claude-plugins", "plugins", "buzzwoo-ecom-shopware",
            "skills", "shopware-app-system", "SKILL.md",
        )
        self.assertEqual(_plugin_name_from_path(windows_parts), "buzzwoo-ecom-shopware")

    def test_slugs_for_user_skill(self):
        p = Path(".claude/skills/frontend-design/SKILL.md")
        self.assertEqual(_slugs_for(p), ["frontend-design"])

    def test_slugs_for_plugin_root_layout(self):
        # Some plugins put SKILL.md at the version root (no skills/ subdirectory).
        # The slug must be the plugin name, never the version string.
        p = Path("plugins/cache/visual-explainer-marketplace/visual-explainer/0.6.3/SKILL.md")
        slugs = set(_slugs_for(p))
        self.assertIn("visual-explainer", slugs)
        self.assertNotIn("0.6.3", slugs)

    def test_missing_skill_not_in_catalog(self):
        # No file written; lookup should return None (server surfaces as tokens_per_call: None)
        cat = scan_catalog([self.tmp / "skills"])
        self.assertNotIn("never-installed", cat)

    def test_project_local_skill_slug(self):
        _write(self.tmp / "proj" / ".claude" / "skills" / "my-skill" / "SKILL.md", "p" * 400)
        cat = scan_catalog([self.tmp / "proj" / ".claude" / "skills"])
        self.assertIn("my-skill", cat)
        self.assertEqual(cat["my-skill"]["tokens"], 100)

    def test_project_skill_roots_innermost_wins(self):
        outer = self.tmp / "outer" / ".claude" / "skills"
        inner = self.tmp / "outer" / "inner" / ".claude" / "skills"
        outer.mkdir(parents=True)
        inner.mkdir(parents=True)
        roots = _project_skill_roots_from_cwds([str(self.tmp / "outer" / "inner" / "src")])
        # Each root now carries scope + project_path so downstream tips can
        # reason about which sessions actually load it.
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]["root"], inner)
        self.assertEqual(roots[0]["scope"], "project-local")
        self.assertEqual(roots[0]["project_path"], str(self.tmp / "outer" / "inner"))

    def test_project_skill_roots_dedupes_across_cwds(self):
        root = self.tmp / "repo" / ".claude" / "skills"
        root.mkdir(parents=True)
        cwds = [
            str(self.tmp / "repo" / "src"),
            str(self.tmp / "repo" / "tests" / "unit"),
        ]
        roots = _project_skill_roots_from_cwds(cwds)
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]["root"], root)
        self.assertEqual(roots[0]["project_path"], str(self.tmp / "repo"))

    def test_cached_catalog_includes_project_local_from_db(self):
        # Reset the module-level cache so this test doesn't inherit neighbour state.
        _cache["at"] = 0.0
        _cache["data"] = {}
        _cache["key"] = None

        project = self.tmp / "myrepo"
        _write(project / ".claude" / "skills" / "repo-skill" / "SKILL.md", "x" * 400)

        db_path = self.tmp / "t.db"
        init_db(db_path)
        with connect(db_path) as c:
            c.execute(
                "INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, cwd)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("u1", "s1", "myrepo", "user", "2026-04-23T00:00:00Z",
                 str(project / "src")),
            )
            c.commit()

        # Neutralise the dev machine's real installed_plugins.json so we only
        # observe the project-local skill discovery path under test.
        with mock.patch.object(_skills_mod, "_default_roots", lambda: []):
            cat = cached_catalog(db_path)
        self.assertIn("repo-skill", cat)
        self.assertEqual(cat["repo-skill"]["tokens"], 100)
        # The project-local skill must be tagged with its project_path so
        # downstream tips can reason about scope.
        self.assertEqual(cat["repo-skill"]["scope"], "project-local")
        self.assertEqual(cat["repo-skill"]["project_path"], str(project))


class ManifestParsingTests(unittest.TestCase):
    """`_read_installed_plugin_entries` -- the Phase 1 source of truth."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = self.tmp / "settings.json"
        self.settings.write_text("{}", encoding="utf-8")

    def _write_manifest(self, payload: str) -> Path:
        m = self.tmp / "installed_plugins.json"
        m.write_text(payload, encoding="utf-8")
        return m

    def test_parses_user_scope_entry(self):
        from token_dashboard.skills import _read_installed_plugin_entries
        m = self._write_manifest('''
        {"version": 2, "plugins": {
          "foo@m": [{"installPath": "/some/path", "scope": "user",
                     "projectPath": "/home/u"}]
        }}''')
        entries = _read_installed_plugin_entries(m, self.settings)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["scope"], "user")
        self.assertEqual(entries[0]["project_path"], "/home/u")

    def test_parses_multiple_entries_per_plugin(self):
        # Same plugin can be installed twice: once scope=user, once scope=project.
        from token_dashboard.skills import _read_installed_plugin_entries
        m = self._write_manifest('''
        {"version": 2, "plugins": {
          "foo@m": [
            {"installPath": "/path/a", "scope": "user"},
            {"installPath": "/path/b", "scope": "project",
             "projectPath": "/repo/x"}
          ]
        }}''')
        entries = _read_installed_plugin_entries(m, self.settings)
        self.assertEqual(len(entries), 2)
        scopes = {e["scope"] for e in entries}
        self.assertEqual(scopes, {"user", "project"})

    def test_missing_file_returns_none(self):
        # None signals "unreadable / broken" → caller falls back to legacy scan.
        # Distinct from a valid-but-empty manifest (which returns []).
        from token_dashboard.skills import _read_installed_plugin_entries
        self.assertIsNone(_read_installed_plugin_entries(self.tmp / "absent.json", self.settings))

    def test_malformed_json_returns_none(self):
        from token_dashboard.skills import _read_installed_plugin_entries
        m = self._write_manifest("{not valid json")
        self.assertIsNone(_read_installed_plugin_entries(m, self.settings))

    def test_corrupt_utf8_returns_none(self):
        # UnicodeDecodeError is a ValueError subclass; must be caught.
        from token_dashboard.skills import _read_installed_plugin_entries
        m = self.tmp / "installed_plugins.json"
        m.write_bytes(b"\xff\xfe\x00invalid utf8 sequence")
        self.assertIsNone(_read_installed_plugin_entries(m, self.settings))

    def test_valid_empty_manifest_returns_empty_list(self):
        # A user with zero installed plugins is a legitimate state, NOT a
        # fallback signal. _default_roots must NOT add the legacy blanket
        # root for this case — otherwise marketplace clones get re-included.
        from token_dashboard.skills import _read_installed_plugin_entries
        m = self._write_manifest('{"version": 2, "plugins": {}}')
        self.assertEqual(_read_installed_plugin_entries(m, self.settings), [])

    def test_entry_without_install_path_skipped(self):
        from token_dashboard.skills import _read_installed_plugin_entries
        m = self._write_manifest('''
        {"version": 2, "plugins": {
          "x@m": [{"scope": "user"}]
        }}''')
        self.assertEqual(_read_installed_plugin_entries(m, self.settings), [])

    def test_project_scope_without_project_path_degrades_to_user(self):
        # Without a projectPath, is_active_in_cwd would short-circuit to False
        # for every cwd — silently making the skill invisible to budget /
        # dead-skills tips. Degrade to scope=user so it's at least counted.
        from token_dashboard.skills import _read_installed_plugin_entries
        m = self._write_manifest('''
        {"version": 2, "plugins": {
          "x@m": [{"installPath": "/some/path", "scope": "project"}]
        }}''')
        entries = _read_installed_plugin_entries(m, self.settings)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["scope"], "user")

    def test_disabled_plugin_excluded_when_enabled_plugins_set(self):
        from token_dashboard.skills import _read_installed_plugin_entries
        manifest = self._write_manifest(json.dumps({
            "version": 2,
            "plugins": {
                "active@m": [{"installPath": "/x/active", "scope": "user"}],
                "inactive@m": [{"installPath": "/x/inactive", "scope": "user"}],
            }
        }))
        settings = self.tmp / "settings.json"
        settings.write_text(json.dumps({
            "enabledPlugins": {"active@m": True, "inactive@m": False}
        }), encoding="utf-8")
        entries = _read_installed_plugin_entries(manifest, settings)
        install_paths = [e["install_path"] for e in entries]
        self.assertIn(Path("/x/active"), install_paths)
        self.assertNotIn(Path("/x/inactive"), install_paths)

    def test_absent_enabled_plugins_key_includes_all(self):
        from token_dashboard.skills import _read_installed_plugin_entries
        manifest = self._write_manifest(json.dumps({
            "version": 2,
            "plugins": {
                "a@m": [{"installPath": "/x/a", "scope": "user"}],
                "b@m": [{"installPath": "/x/b", "scope": "user"}],
            }
        }))
        settings = self.tmp / "settings.json"
        settings.write_text("{}", encoding="utf-8")
        entries = _read_installed_plugin_entries(manifest, settings)
        install_paths = [e["install_path"] for e in entries]
        self.assertIn(Path("/x/a"), install_paths)
        self.assertIn(Path("/x/b"), install_paths)


class DefaultRootsResolutionTests(unittest.TestCase):
    """`_default_roots` -- ties manifest entries to scoped roots."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _manifest(self, payload: str) -> Path:
        m = self.tmp / "installed_plugins.json"
        m.write_text(payload, encoding="utf-8")
        return m

    def _neutral_settings(self) -> Path:
        """Write a settings.json with no enabledPlugins key (= no filter)."""
        s = self.tmp / "settings.json"
        s.write_text("{}", encoding="utf-8")
        return s

    def test_manifest_drives_active_roots(self):
        from token_dashboard.skills import _default_roots
        m = self._manifest('''
        {"version": 2, "plugins": {
          "foo@m": [{"installPath": "/x/foo", "scope": "user"}],
          "bar@m": [{"installPath": "/x/bar", "scope": "project",
                     "projectPath": "/repo/y"}]
        }}''')
        roots = _default_roots(m, self._neutral_settings())
        # Two user-skills/scheduled-tasks roots + 2 plugin roots = 4 entries.
        plugin_roots = [r for r in roots if "/x/" in str(r["root"]).replace("\\", "/")]
        self.assertEqual(len(plugin_roots), 2)
        foo = next(r for r in plugin_roots if "foo" in str(r["root"]))
        bar = next(r for r in plugin_roots if "bar" in str(r["root"]))
        self.assertEqual(foo["scope"], "user-global")
        self.assertEqual(bar["scope"], "project-global")
        self.assertEqual(bar["project_path"], "/repo/y")

    def test_missing_manifest_falls_back_to_legacy_blanket_scan(self):
        from token_dashboard.skills import _default_roots, _LEGACY_PLUGINS_ROOT
        roots = _default_roots(self.tmp / "absent.json", self._neutral_settings())
        # Legacy fallback adds the blanket plugins root tagged 'unknown'.
        legacy = [r for r in roots if r["root"] == _LEGACY_PLUGINS_ROOT]
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0]["scope"], "unknown")

    def test_valid_empty_manifest_does_NOT_trigger_legacy_fallback(self):
        # Regression guard for the Phase 1 promise: user has uninstalled all
        # plugins. Marketplace clones still exist on disk under ~/.claude/
        # plugins/ but must stay excluded — otherwise the catalog re-pollutes.
        from token_dashboard.skills import _default_roots, _LEGACY_PLUGINS_ROOT
        m = self.tmp / "installed_plugins.json"
        m.write_text('{"version": 2, "plugins": {}}', encoding="utf-8")
        roots = _default_roots(m, self._neutral_settings())
        legacy = [r for r in roots if r["root"] == _LEGACY_PLUGINS_ROOT]
        self.assertEqual(legacy, [],
                         "Empty manifest must NOT trigger legacy blanket scan")


class CatalogFilteringTests(unittest.TestCase):
    """End-to-end: a SKILL.md outside any installed plugin must NOT be cataloged."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _neutral_settings(self) -> Path:
        s = self.tmp / "settings.json"
        s.write_text("{}", encoding="utf-8")
        return s

    def test_marketplace_clone_outside_install_paths_excluded(self):
        # Two SKILL.md files: one under an installed plugin, one orphaned in
        # the marketplaces clone area (not in installed_plugins.json).
        installed = self.tmp / "cache" / "m" / "installed-plugin" / "1.0" / "skills" / "in" / "SKILL.md"
        installed.parent.mkdir(parents=True)
        installed.write_text("y" * 200, encoding="utf-8")

        clone = self.tmp / "marketplaces" / "m" / "plugins" / "uninstalled" / "skills" / "out" / "SKILL.md"
        clone.parent.mkdir(parents=True)
        clone.write_text("z" * 200, encoding="utf-8")

        manifest = self.tmp / "installed_plugins.json"
        manifest.write_text(f'''
        {{"version": 2, "plugins": {{
          "installed-plugin@m": [{{"installPath": "{installed.parent.parent.parent.as_posix()}",
                                    "scope": "user"}}]
        }}}}''', encoding="utf-8")

        from token_dashboard.skills import scan_catalog, _default_roots
        roots = _default_roots(manifest, self._neutral_settings())
        cat = scan_catalog(roots)
        self.assertIn("in", cat)
        self.assertNotIn("out", cat)
        self.assertEqual(cat["in"]["scope"], "user-global")

    def test_project_scope_carries_project_path(self):
        installed = self.tmp / "cache" / "m" / "p" / "1.0" / "skills" / "s" / "SKILL.md"
        installed.parent.mkdir(parents=True)
        installed.write_text("x" * 100, encoding="utf-8")

        manifest = self.tmp / "installed_plugins.json"
        manifest.write_text(f'''
        {{"version": 2, "plugins": {{
          "p@m": [{{"installPath": "{installed.parent.parent.parent.as_posix()}",
                    "scope": "project", "projectPath": "/repo/r"}}]
        }}}}''', encoding="utf-8")

        from token_dashboard.skills import scan_catalog, _default_roots
        cat = scan_catalog(_default_roots(manifest, self._neutral_settings()))
        self.assertEqual(cat["s"]["scope"], "project-global")
        self.assertEqual(cat["s"]["project_path"], "/repo/r")


class SafeScanRootTests(unittest.TestCase):
    """`_safe_scan_root` -- bounds-check to prevent rglob DoS via tampered
    installPath."""

    def test_normal_path_is_accepted(self):
        from token_dashboard.skills import _safe_scan_root
        inside = Path.home() / ".claude" / "skills"
        result = _safe_scan_root(inside)
        self.assertIsNotNone(result)

    def test_filesystem_root_is_rejected(self):
        # A tampered manifest with installPath="/" or "C:\\" must not start
        # an rglob walk of the entire filesystem — that's a self-DoS.
        from token_dashboard.skills import _safe_scan_root
        roots = [Path("/"), Path("C:\\")] if os.name == "nt" else [Path("/")]
        for r in roots:
            self.assertIsNone(_safe_scan_root(r),
                              f"{r} should be rejected as a scan root")

    def test_paths_outside_home_are_accepted(self):
        # Users may legitimately keep projects outside their home (e.g.
        # /opt/proj on POSIX, D:\repos on Windows). Don't reject those.
        from token_dashboard.skills import _safe_scan_root
        candidate = Path("/opt/proj") if os.name != "nt" else Path("D:\\repos")
        # We don't require these to exist on disk; _safe_scan_root only
        # validates structure. is_dir() is checked separately by scan_catalog.
        self.assertIsNotNone(_safe_scan_root(candidate))


class ActiveInCwdTests(unittest.TestCase):
    def test_user_global_always_active(self):
        from token_dashboard.skills import is_active_in_cwd
        self.assertTrue(is_active_in_cwd("user-global", None, None))
        self.assertTrue(is_active_in_cwd("user-global", None, "/anything"))

    def test_unknown_treated_as_active(self):
        from token_dashboard.skills import is_active_in_cwd
        self.assertTrue(is_active_in_cwd("unknown", None, None))

    def test_project_scope_active_under_project_path(self):
        from token_dashboard.skills import is_active_in_cwd
        tmp = Path(tempfile.mkdtemp())
        sub = tmp / "src" / "deep"
        sub.mkdir(parents=True)
        self.assertTrue(is_active_in_cwd("project-global", str(tmp), str(sub)))
        self.assertTrue(is_active_in_cwd("project-local", str(tmp), str(tmp)))

    def test_project_scope_inactive_outside_project_path(self):
        from token_dashboard.skills import is_active_in_cwd
        tmp = Path(tempfile.mkdtemp())
        other = Path(tempfile.mkdtemp())
        self.assertFalse(is_active_in_cwd("project-global", str(tmp), str(other)))

    def test_project_scope_without_cwd_is_inactive(self):
        from token_dashboard.skills import is_active_in_cwd
        self.assertFalse(is_active_in_cwd("project-global", "/foo", None))
        self.assertFalse(is_active_in_cwd("project-global", None, "/foo"))


if __name__ == "__main__":
    unittest.main()
