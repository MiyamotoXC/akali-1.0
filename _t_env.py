# -*- coding: utf-8 -*-
"""临时：只打印 .env 里的配额相关行（不显示任何密钥）。用后即删。"""
import io
import os

p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
lines = io.open(p, encoding='utf-8').read().splitlines()
hit = [l for l in lines if l.startswith('FX_QUOTA') or l.startswith('FX_CLOUD')]
print('[quota lines in .env]')
for l in hit:
    print('  ' + l)
print('[total lines]', len(lines))
