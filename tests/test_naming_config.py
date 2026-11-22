import pytest

from previewctl.config import ConfigError, PreviewConfig, from_dict, load
from previewctl.naming import env_name, host_for, is_dns_label, validate_sha


@pytest.mark.parametrize(
    ("pr", "branch", "expected"),
    [
        (42, "feature/Add_Login!!", "pr-42-feature-add-login"),
        (7, "", "pr-7"),
        (7, "///", "pr-7"),
        (0, "main", "pr-0-main"),
        (3, "fix--double---dash", "pr-3-fix-double-dash"),
    ],
)
def test_env_name(pr, branch, expected):
    assert env_name(pr, branch) == expected


def test_long_branches_are_truncated_unique_and_valid():
    a = env_name(12345, "feature/" + "a" * 100)
    b = env_name(12345, "feature/" + "a" * 99 + "b")
    assert a != b
    for name in (a, b):
        assert len(name) <= 63 and is_dns_label(name) and name.startswith("pr-12345-")
    assert env_name(12345, "feature/" + "a" * 100) == a  # stable


@pytest.mark.parametrize("pr", [-1, True, "3", 1.5])
def test_env_name_rejects_bad_pr(pr):
    with pytest.raises(ValueError):
        env_name(pr, "x")


def test_host_and_sha():
    assert host_for("pr-1", "Preview.LocalTest.me.") == "pr-1.preview.localtest.me"
    with pytest.raises(ValueError):
        host_for("pr-1", "bad_domain")
    assert validate_sha(" ABCDEF1 ") == "abcdef1"
    for bad in ("abc", "zzzzzzz", "a" * 41):
        with pytest.raises(ValueError):
            validate_sha(bad)


def test_default_config_is_valid():
    cfg = PreviewConfig()
    assert cfg.ttl_hours == 72


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        ({"ttl_hour": 3}, "unknown config keys: ttl_hour"),
        ({"image": "app:latest"}, "without a tag"),
        ({"app": "Web_App"}, "DNS-1123"),
        ({"cpu_request": "500m", "cpu_limit": "250m"}, "cpu_request"),
        ({"memory_limit": "1Gi"}, "quota_memory"),
        ({"replicas": 3, "cpu_limit": "500m"}, "quota_cpu"),
        ({"memory_request": "64MB"}, "memory quantity"),
        ({"env": {"DB_PASSWORD": "x"}}, "looks like a secret"),
        ({"env": {"GIT_SHA": "x"}}, "reserved"),
        ({"env": {"lower": "x"}}, "UPPER_SNAKE_CASE"),
        ({"port": "eighty"}, "not supported between"),
        ({"env": ["A"]}, "mapping"),
    ],
)
def test_config_rejects(data, fragment):
    with pytest.raises(ConfigError, match=fragment):
        from_dict(data)


def test_load_yaml(tmp_path):
    p = tmp_path / "preview.yaml"
    p.write_text("app: api\nttl_hours: 24\nenv:\n  LOG_LEVEL: debug\n", encoding="utf-8")
    cfg = load(p)
    assert (cfg.app, cfg.ttl_hours, cfg.env) == ("api", 24, {"LOG_LEVEL": "debug"})
    p.write_text("- a\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load(p)
    p.write_text("", encoding="utf-8")
    assert load(p) == PreviewConfig()
