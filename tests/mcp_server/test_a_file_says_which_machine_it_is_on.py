"""An answer that names a file says which machine the file is on.

Measured 2026-10-24: this server ran on a Linux host and was reached over an
ssh tunnel from a Mac. browser_download answered `saved: /home/...`, nothing
said the path was on another machine, and the agent opened it locally. On
macOS /home is an automounted network volume, so the open raised a privacy
prompt nobody could see and the agent's whole process blocked in the kernel,
twice in one afternoon. With INVISIBLE_MCP_FILE_HOST set, every answer naming a
file or directory names the machine and the scp command that copies it.
"""
from __future__ import annotations

import json
import shlex

import pytest

from invisible_playwright_mcp.mcp import actions


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setenv(actions.FILE_HOST_ENV, "browser-host")
    monkeypatch.setattr(actions.socket, "gethostname", lambda: "linux-box.example.net")
    return "browser-host"


def test_unset_means_callers_share_this_disk(monkeypatch):
    monkeypatch.delenv(actions.FILE_HOST_ENV, raising=False)
    assert actions.remote_file("/home/u/x.csv") == {}
    assert actions.remote_dir_lines("/u", "/d") == ""


@pytest.mark.parametrize("path", [
    "/home/u/downloads/goods_export.csv",
    "/home/u/downloads/a statement (1).pdf",
    "/home/u/downloads/it's $HOME; rm -rf ~.pdf",
])
def test_the_fetch_command_copies_exactly_that_path(host, path):
    said = actions.remote_file(path)
    assert said["host"] == "linux-box"
    assert shlex.split(said["fetch"]) == ["scp", f"{host}:{path}", "<local-dir>"]
    assert "linux-box (ssh browser-host)" in said["where"]
    assert said["fetch"] in said["where"] and "do not open" in said["where"]


def test_a_hostname_equal_to_the_ssh_name_is_said_once(host, monkeypatch):
    monkeypatch.setattr(actions.socket, "gethostname", lambda: "browser-host")
    said = actions.remote_file("/x")
    assert said["host"] == "browser-host"
    assert "browser-host (ssh" not in said["where"]


def test_directory_lines_keep_the_path_lines_exact(host):
    lines = actions.remote_dir_lines("/up dir", "/down").splitlines()
    assert lines[0] == ""
    assert lines[1].startswith("files host: linux-box (ssh browser-host)")
    assert shlex.split(lines[2].removeprefix("upload with: ")) == \
        ["scp", "<local-file>", "browser-host:/up dir/"]
    assert shlex.split(lines[3].removeprefix("fetch with: ")) == \
        ["scp", "browser-host:/down/<file>", "<local-dir>"]
    assert all(not line.startswith(("upload dir:", "download dir:")) for line in lines)
    only_down = actions.remote_dir_lines(download_dir="/down")
    assert "upload with:" not in only_down and "fetch with:" in only_down


def test_a_network_file_says_where_it_is(host, tmp_path):
    root = tmp_path / "down"
    root.mkdir()
    out = actions.write_network_ndjson([{"id": 1}], save_to=None,
                                       env={actions.DOWNLOAD_DIRS_ENV: str(root)},
                                       max_bytes=1 << 20)
    assert shlex.split(out["fetch"])[1] == "browser-host:" + out["path"]
    assert json.loads((root / out["path"].rsplit("/", 1)[1]).read_text()) == {"id": 1}

