#!/usr/bin/env python3
"""Validate both thin adapters against the shared harness contract."""
import ast
import re

import yaml

from common import HERE, SOURCE, read_json


def check_gitlab_rule_operators(rules):
    for rule in rules:
        if "if" not in rule:
            continue
        expression = rule["if"]
        # Ignore string/regex literals. GitLab supports != and !~, but no
        # standalone ! operator as used in shell/GitHub conditions.
        operators = re.sub(r'"(?:\\.|[^"\\])*"|/(?:\\.|[^/\\])*/', "", expression)
        assert not re.search(r"!(?!=|~)", operators), "GitLab rules:if does not support unary !: " + expression


def lint():
    cases = read_json(HERE / "cases.json")["cases"]
    assert len(cases) == 7 and len({c["id"] for c in cases}) == 7
    assert {c["group"] for c in cases} == {"rtl", "linux"}
    for spec in read_json(HERE / "assets.lock.json")["assets"].values():
        assert re.fullmatch("[0-9a-f]{64}", spec["sha256"])
        assert spec["size"] > 0 and not spec["jf_path"].startswith("/")
    for path in HERE.glob("*.py"):
        ast.parse(path.read_text(), filename=str(path))
    gitlab = yaml.safe_load((SOURCE / ".gitlab-ci.yml").read_text())
    check_gitlab_rule_operators(gitlab["workflow"]["rules"])
    github = yaml.safe_load((SOURCE / ".github/workflows/iommu-ci.yml").read_text())
    assert gitlab["stages"] == ["check", "build", "test", "report"]
    assert gitlab["iommu:report"]["when"] == "always"
    assert github["jobs"]["test"]["strategy"]["matrix"]["group"] == ["rtl", "linux"]
    for path in (SOURCE / ".gitlab-ci.yml", SOURCE / ".github/workflows/iommu-ci.yml"):
        text = path.read_text()
        assert all("ci.py " + stage in text for stage in ("check", "doctor", "build", "run", "report"))
        assert "allow_failure" not in text
        assert "artifactory/" not in text  # Inputs belong to the common lock/config.
    print("OpenIOMMU CI adapters and manifests: PASS")


if __name__ == "__main__":
    lint()
