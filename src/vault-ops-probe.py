import os, json, pathlib, sys
from datetime import datetime
V=pathlib.Path.home()/"Documents/Obsidian Vault/BOSS招聘/知识库/复盘"
f=V/"错过人才复盘.md"
res={"at":datetime.now().isoformat(timespec="seconds"),"pid":os.getpid()}
orig=f.read_bytes() if f.exists() else b""
res["read"]=bool(orig)
# 1 建新文件
t=V/f".probe-new-{os.getpid()}"
try: t.write_text("x"); res["create_new"]=True
except OSError as e: res["create_new"]=f"{type(e).__name__}:{e.errno}"
# 2 就地覆写既有文件
try:
    with open(f,"wb") as fh: fh.write(orig)
    res["inplace_write"]=True
except OSError as e: res["inplace_write"]=f"{type(e).__name__}:{e.errno}"
# 3 rename 覆盖既有文件
if t.exists():
    try:
        t.write_bytes(orig); os.replace(t,f); res["rename_over"]=True
    except OSError as e:
        res["rename_over"]=f"{type(e).__name__}:{e.errno}"
        t.unlink(missing_ok=True)
# 4 rename 到不存在的新名
t2=V/f".probe-ren-{os.getpid()}"
try:
    t.write_text("y") if not t.exists() else None
    os.replace(t,t2); res["rename_new"]=True; t2.unlink(missing_ok=True)
except OSError as e: res["rename_new"]=f"{type(e).__name__}:{e.errno}"
finally:
    t.unlink(missing_ok=True) if t.exists() else None
p=pathlib.Path.home()/".coremem/health/vault-ops-probe.json"
p.write_text(json.dumps(res,ensure_ascii=False,indent=1))
print(json.dumps(res,ensure_ascii=False))
