"""秒传的原因文案拼接。

光鸭「空间不足」返回的 msg 常常就是「空间不足」，直接拼前缀会拼出
「光鸭空间不足：空间不足」这种话 —— 它会原样进任务说明、网页和 Bot 通知。
"""

from __future__ import annotations

import pytest

from app.transfer import join_reason


@pytest.mark.parametrize("prefix, detail, expected", [
    # 细节和前缀重复：只留一份
    ("光鸭空间不足", "空间不足", "光鸭空间不足"),
    ("光鸭空间不足", "光鸭空间不足", "光鸭空间不足"),
    # 细节更长、包含前缀：用细节（信息更多）
    ("光鸭空间不足", "空间不足，请清理回收站", "空间不足，请清理回收站"),
    # 互不重复：正常拼接
    ("光鸭空间不足", "容量上限 10GB", "光鸭空间不足：容量上限 10GB"),
    # 只有一边
    ("光鸭空间不足", "", "光鸭空间不足"),
    ("", "光鸭登录已失效", "光鸭登录已失效"),
    ("", "", ""),
    # 两边都带冒号空格，拼出来不该出现「：：」
    ("光鸭空间不足：", "容量不足", "光鸭空间不足：容量不足"),
])
def test_join_reason(prefix, detail, expected):
    assert join_reason(prefix, detail) == expected
