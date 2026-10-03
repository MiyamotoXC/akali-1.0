# -*- coding: utf-8 -*-
"""临时：确认配额默认值与周期口径。用后即删。"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '通用工具'))
from core import quota  # noqa: E402

print('free/paid/whitelist =', quota.quota_free(), quota.quota_paid(), quota.quota_whitelist())
p = quota._period()
print('period =', p, '| weekday =', date.fromisoformat(p).strftime('%A'))
