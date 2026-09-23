# -*- coding: utf-8 -*-
"""纯标准库 xlsx 读取器（openpyxl 对数值自动化表.xlsx 的样式解析会崩，故自实现）。

只支持读取单元格的值（不解析样式/公式结果以外的东西），够用即可。
"""
import zipfile
import re
import xml.etree.ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


class Xlsx:
    def __init__(self, path):
        self.z = zipfile.ZipFile(path)
        self._shared = self._load_shared()
        wbx = ET.fromstring(self.z.read("xl/workbook.xml"))
        rels = ET.fromstring(self.z.read("xl/_rels/workbook.xml.rels"))
        rid2t = {r.get("Id"): r.get("Target") for r in rels}
        self.sheets = {}
        for s in wbx.iter(NS + "sheet"):
            name = s.get("name")
            tgt = rid2t[s.get(RNS + "id")]
            if not tgt.startswith("xl/"):
                tgt = "xl/" + tgt.lstrip("/")
            self.sheets[name] = tgt

    def _load_shared(self):
        out = []
        try:
            data = self.z.read("xl/sharedStrings.xml")
        except KeyError:
            return out
        for si in ET.fromstring(data).iter(NS + "si"):
            out.append("".join(t.text or "" for t in si.iter(NS + "t")))
        return out

    @staticmethod
    def _col(ref):
        m = re.match(r"([A-Z]+)", ref or "A")
        n = 0
        for ch in m.group(1):
            n = n * 26 + (ord(ch) - 64)
        return n - 1

    def rows(self, sheet, max_rows=None):
        """按行产出 list[str|float|None]，列位对齐（空白列补 None）。"""
        tgt = self.sheets[sheet]
        yield from self._iter(tgt, max_rows)

    def _iter(self, tgt, max_rows):
        n = 0
        cur = {}
        maxc = -1
        with self.z.open(tgt) as f:
            for ev, el in ET.iterparse(f, events=("end",)):
                if el.tag != NS + "row":
                    continue
                cur = {}
                for c in el.iter(NS + "c"):
                    ref = c.get("r")
                    ci = self._col(ref)
                    t = c.get("t")
                    v = c.find(NS + "v")
                    isel = c.find(NS + "is")
                    if t == "s":
                        # 部分写入器对 index 0 省略 <v> 节点
                        idx = 0 if v is None else v.text
                        try:
                            val = self._shared[int(idx)]
                        except (ValueError, TypeError, IndexError):
                            val = None
                    elif t == "inlineStr" and isel is not None:
                        val = "".join(x.text or "" for x in isel.iter(NS + "t"))
                    elif v is not None:
                        txt = v.text
                        try:
                            val = float(txt)
                            if val == int(val):
                                val = int(val)
                        except (TypeError, ValueError):
                            val = txt
                    else:
                        val = None
                    if val is not None:
                        cur[ci] = val
                        maxc = max(maxc, ci)
                el.clear()
                n += 1
                yield [cur.get(i) for i in range(maxc + 1)] if cur else []
                if max_rows and n >= max_rows:
                    return


if __name__ == "__main__":
    import sys
    x = Xlsx(sys.argv[1])
    print("sheets:", list(x.sheets))
    sn = sys.argv[2] if len(sys.argv) > 2 else list(x.sheets)[0]
    nr = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    for i, r in enumerate(x.rows(sn, max_rows=nr)):
        print(f"r{i}:", [str(c)[:12] if c is not None else "" for c in r][:45])
