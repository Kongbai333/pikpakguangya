import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def pytest_addoption(parser):
    parser.addoption(
        "--run-network",
        action="store_true",
        default=False,
        help="额外跑需要访问 mypikpak.com 的测试（默认跳过）",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "network: 需要真实网络的测试，用 --run-network 开启")
    # 某些 Windows 环境下系统临时目录不可写（WinError 5），把 basetemp 放到项目内
    if not config.option.basetemp:
        config.option.basetemp = str(ROOT / ".pytest-tmp")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-network"):
        return
    skip = pytest.mark.skip(reason="需要 --run-network 才会访问真实网络")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)
