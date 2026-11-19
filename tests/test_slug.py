"""Branch → slug/host/db/project mapping and git-ref validation."""

from __future__ import annotations

import pytest
from envd.tools.slug import (
    BranchError,
    compose_project,
    db_name,
    db_slug,
    env_url,
    host_label,
    is_protected,
    validate_branch,
)


class TestDbSlug:
    @pytest.mark.parametrize(
        ("branch", "slug"),
        [
            ("main", "main"),
            ("feature/new-login", "feature_new_login"),
            ("Feature/Mixed_CASE", "feature_mixed_case"),
            ("hotfix/urgent--fix!!", "hotfix_urgent_fix"),
            ("  spaces  ", "spaces"),
            ("a" * 60, "a" * 40),  # truncation at max_len
            ("123-numeric", "b123_numeric"),  # leading digit gets a letter prefix
        ],
    )
    def test_mapping(self, branch, slug):
        assert db_slug(branch, max_len=40) == slug

    def test_slug_alphabet_is_db_safe(self):
        for raw in ("weird/../branch", "tab\tname", "dot.branch", "über"):
            slug = db_slug(raw)
            assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in slug)

    @pytest.mark.parametrize("branch", ["----", "///", " ", "___"])
    def test_unmappable_raises(self, branch):
        with pytest.raises(BranchError):
            db_slug(branch)


class TestValidateBranch:
    @pytest.mark.parametrize(
        "branch",
        ["main", "feature/x", "release-20.0", "hotfix/under_score", "123-numeric"],
    )
    def test_valid(self, branch):
        assert validate_branch(branch) == branch

    @pytest.mark.parametrize(
        "branch",
        [
            "",
            "refs/heads/main",
            "-leading-dash",
            "with space",
            "with~tilde",
            "with^caret",
            "with:colon",
            "with?question",
            "with*star",
            "with[bracket",
            "with\\slash",
            "with..dots",
            "ends.lock",
            "ends/slash/",
            "double//slash",
            "x" * 101,
        ],
    )
    def test_rejected(self, branch):
        with pytest.raises(BranchError):
            validate_branch(branch)


class TestDerived:
    def test_host_and_db(self):
        assert host_label("feature_new") == "feature-new"
        assert db_name("feature_new") == "env_feature_new"
        assert compose_project("feature_new") == "env_feature_new"

    def test_url(self):
        assert env_url("feature_new", "odoo.example.com") == "https://feature-new.odoo.example.com"

    def test_is_protected(self):
        assert is_protected("main", ("main", "master"))
        assert not is_protected("dev", ("main", "master"))

    def test_slug_collision_between_branches(self):
        # Two distinct branches mapping to the same slug is the collision the
        # engine must detect via the store's UNIQUE constraints.
        assert db_slug("Feature X") == db_slug("feature_x")
