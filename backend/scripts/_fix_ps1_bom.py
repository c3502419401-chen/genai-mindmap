"""给 check-neo4j.ps1 补 UTF-8 BOM。

Windows PowerShell 5.1 会把无 BOM 的 UTF-8 当 GBK 读，中文里的「」/「——」会被解成
智能引号（PowerShell 认 U+2018 当字符串定界符），脚本当场括号失配。写一次就够，
但它必须能被重复执行——`edit` 工具会去掉 BOM，改完脚本跑这个。
"""
import io

PATH = r"D:\genai-mindmap\check-neo4j.ps1"

with io.open(PATH, encoding="utf-8-sig") as handle:
    body = handle.read()
with io.open(PATH, "w", encoding="utf-8-sig", newline="\n") as handle:
    handle.write(body)
print("BOM ok:", open(PATH, "rb").read(3))
