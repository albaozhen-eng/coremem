import os, json, pathlib, sys
from datetime import datetime
H=pathlib.Path.home()
rel="Obsidian Vault/BOSS招聘/知识库/复盘/错过人才复盘.md"
paths={"Documents(TCC保护)": H/"Documents"/rel,
       "iCloudDrive(Library)": H/"Library/Mobile Documents/com~apple~CloudDocs/Documents"/rel}
res={"at":datetime.now().isoformat(timespec="seconds"),"pid":os.getpid()}
for label,p in paths.items():
    r={}
    try:
        data=p.read_bytes(); r["read"]=True; r["bytes"]=len(data)
    except OSError as e: r["read"]=f"{type(e).__name__}:{e.errno}"; data=None
    if data is not None:
        try:
            with open(p,"wb") as fh: fh.write(data)   # 原样写回
            r["write"]=True
        except OSError as e: r["write"]=f"{type(e).__name__}:{e.errno}"
    r["inode"]=p.stat().st_ino if p.exists() else None
    res[label]=r
out=H/".coremem/health/icloud-path-probe.json"
out.write_text(json.dumps(res,ensure_ascii=False,indent=1))
print(json.dumps(res,ensure_ascii=False))
