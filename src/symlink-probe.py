import os, json, pathlib
from datetime import datetime
H=pathlib.Path.home()
real=H/".boss-agent/reports/错过人才复盘.md"
link=H/"Documents/Obsidian Vault/BOSS招聘/知识库/复盘/错过人才复盘.md"
res={"at":datetime.now().isoformat(timespec="seconds"),"pid":os.getpid()}
# ① launchd 能否写非保护区真文件
try:
    real.parent.mkdir(parents=True,exist_ok=True)
    with open(real,"a") as fh: fh.write("")
    res["write_real"]=True
except OSError as e: res["write_real"]=f"{type(e).__name__}:{e.errno}"
# ② 真文件是否可读
try: res["read_real"]=len(real.read_bytes())>=0
except OSError as e: res["read_real"]=f"{type(e).__name__}:{e.errno}"
res["real_path"]=str(real).replace(str(H),"~")
res["vault_is_symlink"]=link.is_symlink()
(H/".coremem/health/symlink-probe.json").write_text(json.dumps(res,ensure_ascii=False,indent=1))
print(json.dumps(res,ensure_ascii=False))
