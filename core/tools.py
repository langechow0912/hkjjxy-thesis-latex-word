# -*- coding: utf-8 -*-
"""外部工具路径解析。

查找顺序：profile.yaml 的 tools 段 → 环境变量 THESIS_<NAME> → 常见安装位置。
工具路径属于「机器配置」而不是「学校格式」，默认不放进 profile.yaml。
"""
import os
import shutil

_CANDIDATES = {
    "xelatex": [
        r"C:/Users/周良/AppData/Local/Programs/MiKTeX/miktex/bin/x64/xelatex.exe",
        r"C:/Program Files/MiKTeX/miktex/bin/x64/xelatex.exe",
        r"C:/texlive/2024/bin/windows/xelatex.exe",
        "xelatex",
    ],
    "soffice": [
        r"C:/Program Files/LibreOffice/program/soffice.exe",
        "soffice",
    ],
}


def resolve(name, cfg=None):
    if cfg:
        v = (cfg.get("tools") or {}).get(name)
        if v:
            return v
    v = os.environ.get("THESIS_" + name.upper())
    if v:
        return v
    for c in _CANDIDATES[name]:
        if os.path.isfile(c) or shutil.which(c):
            return c
    raise SystemExit("找不到工具 %s。请在 profile.yaml 的 tools 段配置，"
                     "或设置环境变量 THESIS_%s。" % (name, name.upper()))
