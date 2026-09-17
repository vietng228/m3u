#!/usr/bin/env python3
# Placeholder bootstrap for SPORT STREAM resolver.
# Full resolver will be replaced after workflow smoke test.
import pathlib
p=pathlib.Path('sport.m3u')
if not p.exists(): p.write_text('#EXTM3U\n',encoding='utf-8')
print('sport.m3u ready')
