"""
桥梁设计文档审核 — 规范库与领域规则

每条规则返回的 Finding 都带：
  - severity     : 严重程度
  - category     : 类别
  - location     : 位置
  - original     : 原文片段
  - standard_ref : 规范依据
  - required     : 规范要求
  - actual       : 文档现状
  - suggestion   : 修改建议
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field


# ---------- 规范库（节选） ----------
# 状态：current=现行；superseded=已被替代；retired=废止
STANDARDS = [
    {"code": "JTG B01-2014", "name": "公路工程技术标准", "status": "current"},
    {"code": "JTG D60-2015", "name": "公路桥涵设计通用规范", "status": "current"},
    {"code": "JTG 3362-2018", "name": "公路钢筋混凝土及预应力混凝土桥涵设计规范", "status": "current"},
    {"code": "JTG/T 3650-2020", "name": "公路桥涵施工技术规范", "status": "current"},
    {"code": "JTG C10-2007", "name": "公路勘测规范", "status": "current"},
    {"code": "JTG/T C10-2007", "name": "公路勘测细则", "status": "current"},
    {"code": "JTG D81-2017", "name": "公路交通安全设施设计技术规范", "status": "current"},
    {"code": "JTG F71-2017", "name": "公路交通安全设施施工技术规范", "status": "current"},
    {"code": "JTG 3363-2019", "name": "公路桥涵地基与基础设计规范", "status": "current"},
    {"code": "JTG/T 3365-04-2020", "name": "公路圬工桥涵设计规范", "status": "current"},
    {"code": "JTG D63-2005", "name": "公路圬工桥涵设计规范",
     "status": "superseded", "replaced_by": "JTG/T 3365-04-2020",
     "note": "已被《公路圬工桥涵设计规范》(JTG/T 3365-04-2020) 替代"},
    {"code": "JTG D61-2005", "name": "公路圬工桥涵设计规范",
     "status": "superseded", "replaced_by": "JTG/T 3365-04-2020",
     "note": "原 JTG D61-2005 现为 JTG D63-2005，且已被 JTG/T 3365-04-2020 替代"},
    {"code": "JTG/T 3365-02-2020", "name": "公路涵洞设计规范", "status": "current"},
    {"code": "JTG D64-2015", "name": "公路钢结构桥梁设计规范", "status": "current"},
    {"code": "JTG/T D64-01-2015", "name": "公路钢混组合桥梁设计与施工规范", "status": "current"},
    {"code": "JTG B02-2013", "name": "公路工程抗震规范", "status": "current"},
    {"code": "JTG/T 2231-01-2020", "name": "公路桥梁抗震设计规范", "status": "current"},
    {"code": "JTG/T 3360-01-2018", "name": "公路桥梁抗风设计规范", "status": "current"},
    {"code": "JTG 2120-2020", "name": "公路工程结构可靠性设计统一标准", "status": "current"},
    {"code": "JTG/T 3310-2019", "name": "公路工程混凝土结构耐久性设计规范", "status": "current"},
    {"code": "JT/T 722-2023", "name": "公路桥梁钢结构防腐涂装技术条件", "status": "current"},
    {"code": "JT/T 722-2008", "name": "公路桥梁钢结构防腐涂装技术条件",
     "status": "superseded", "replaced_by": "JT/T 722-2023",
     "note": "已被 JT/T 722-2023 替代"},
    {"code": "JT/T 695-2007", "name": "混凝土桥梁结构表面涂层防腐技术条件", "status": "current"},
    {"code": "JT/T 329-2010", "name": "公路桥梁预应力钢绞线用锚具、夹具和连接器", "status": "current"},
    {"code": "JT/T 4-2019", "name": "公路桥梁板式橡胶支座", "status": "current"},
    {"code": "JT/T 327-2016", "name": "公路桥梁伸缩装置通用技术条件", "status": "current"},
    {"code": "GB/T 706-2016", "name": "热轧型钢", "status": "current"},
    {"code": "GB/T 1591-2018", "name": "低合金高强度结构钢", "status": "current"},
    {"code": "GB/T 700-2006", "name": "碳素结构钢", "status": "current"},
    {"code": "GB/T 714-2015", "name": "桥梁用结构钢", "status": "current"},
    {"code": "GB/T 50476-2019", "name": "混凝土结构耐久性设计标准", "status": "current"},
    {"code": "GB 18306-2015", "name": "中国地震动参数区划图", "status": "current"},
    {"code": "GB 50010-2010", "name": "混凝土结构设计规范", "status": "current"},
    {"code": "GB/T 5224-2014", "name": "预应力混凝土用钢绞线", "status": "current"},
    {"code": "JG/T 225-2020", "name": "预应力混凝土用金属波纹管", "status": "current"},
    {"code": "JT/T 529-2016", "name": "预应力混凝土桥梁用塑料波纹管", "status": "current"},
]


STATUS_LABEL = {"current": "现行", "superseded": "已被替代", "retired": "已废止"}


def standards_index() -> str:
    lines = ["【现行规范】"]
    for s in STANDARDS:
        if s["status"] == "current":
            lines.append(f"- 《{s['name']}》({s['code']})")
    lines.append("\n【已被替代或废止规范】")
    for s in STANDARDS:
        if s["status"] != "current":
            note = s.get("note", "")
            lines.append(f"- 《{s['name']}》({s['code']}) — {note}")
    return "\n".join(lines)


# ---------- Finding ----------

@dataclass
class Finding:
    severity: str
    category: str
    location: str
    original: str
    standard_ref: str = ""
    required: str = ""
    actual: str = ""
    suggestion: str = ""
    # 兼容：旧字段 problem，由 required/actual 自动合成
    problem: str = field(default="", init=False)

    def __post_init__(self):
        if not self.problem and (self.required or self.actual):
            parts = []
            if self.required:
                parts.append(f"规范要求：{self.required}")
            if self.actual:
                parts.append(f"文档现状：{self.actual}")
            self.problem = "；".join(parts)

    def to_dict(self):
        return asdict(self)


def _line_no(text: str, idx: int) -> int:
    return text[:idx].count("\n") + 1


# ---------- 规则 1：连续重复行 ----------

# 表格单元格典型特征：含单位、数字、范围；不是完整句子
_TABLE_CELL_PATTERN = re.compile(
    r"^[\s\S]{0,80}?(?:"
    r"[\d.×x*]+\s*(?:m|km|kg|t|%|MPa|kPa|℃|°C|km/h|m/s|cm|mm)"  # 数值+单位
    r"|^[\d.×x*+\-/\s（）()]+$"   # 纯数字/算式
    r"|^[A-Z\-\d]+$"               # 标号（如 BZZ-100、Q345）
    r"|^第?[一二三四五六七八九十\d]+[级类型档]$"  # 等级标记
    r")"
)

def _looks_like_table_cell(s: str) -> bool:
    """判断是否像表格单元格：短文本、含数值+单位、或纯标识符。
    表格里同一参数的「设计值」和「规范值」常常完全相同（如 2×0.75 / 2×0.75），
    不应被当作重复行报错。
    """
    if len(s) > 30:
        return False
    # 含数字 + 单位
    if re.search(r"\d[\d.×x*]*\s*(?:m|km|kg|t|%|MPa|kPa|℃|°C|km/h|m/s|cm|mm)", s):
        return True
    # 纯数字 / 算式（含括号注释）
    if re.fullmatch(r"[\d.×x*+\-/\s（）()，,；;：:一-鿿]{1,30}", s) and re.search(r"\d", s):
        # 必须含数字，但又允许少量汉字注释（如「3.0（包括右侧路缘带0.5）」）
        return True
    return False


def find_duplicate_lines(text: str) -> list[Finding]:
    findings = []
    lines = text.split("\n")
    prev = None
    for i, line in enumerate(lines, 1):
        s = line.strip()
        # 长度阈值提高到 15；同时跳过表格单元格
        if len(s) > 15 and s == prev and not _looks_like_table_cell(s):
            findings.append(Finding(
                severity="medium",
                category="重复条目",
                location=f"第 {i} 行",
                original=s[:120],
                actual="本行与上一行完全相同",
                suggestion="删除重复行",
            ))
        prev = s
    return findings


# ---------- 规则 2：规范引用版本核查 ----------

STANDARD_REF_PATTERN = re.compile(
    r"《([^》]+)》\s*[\(（]\s*([A-Z]+(?:/[A-Z]+)?\s*\d+(?:\.\d+)?(?:-\d+)?(?:[\s\-—]+\d{4})?)\s*[\)）]"
)


def check_standard_references(text: str) -> list[Finding]:
    findings = []
    code_to_std = {re.sub(r"\s+", "", s["code"]): s for s in STANDARDS}
    seen_codes: dict[str, list[int]] = {}

    for m in STANDARD_REF_PATTERN.finditer(text):
        name, code = m.group(1).strip(), m.group(2).strip()
        line_no = _line_no(text, m.start())
        norm_code = re.sub(r"\s+", "", code).replace("—", "-")
        seen_codes.setdefault(norm_code, []).append(line_no)

        std = code_to_std.get(norm_code)
        if std and std["status"] != "current":
            replaced = std.get("replaced_by", "")
            findings.append(Finding(
                severity="high",
                category="规范引用版本",
                location=f"第 {line_no} 行",
                original=f"《{name}》({code})",
                standard_ref=f"国家标准化管理委员会 / 交通运输部公告（{code} 已废止/替代）",
                required=f"应引用现行版本：{replaced or '请查最新规范目录'}",
                actual=f"文档引用了已被替代的 {code}",
                suggestion=std.get("note", f"替换为 {replaced}"),
            ))

    for code, line_nos in seen_codes.items():
        if len(line_nos) >= 2:
            close = [(line_nos[i], line_nos[i+1]) for i in range(len(line_nos)-1) if line_nos[i+1] - line_nos[i] <= 5]
            if close:
                a, b = close[0]
                findings.append(Finding(
                    severity="medium",
                    category="规范引用重复",
                    location=f"第 {a}、{b} 行",
                    original=code,
                    actual=f"规范 {code} 在邻近位置重复引用",
                    suggestion="检查规范清单是否有重复条目",
                ))
    return findings


# ---------- 规则 3：主体结构设计使用年限 ----------

DESIGN_LIFE_PATTERN = re.compile(r"主体结构设计使用年限[^\d]{0,8}(\d+)\s*年")


def check_design_life(text: str) -> list[Finding]:
    findings = []
    # 判断项目是否含大桥/特大桥
    has_big_bridge = bool(re.search(r"(特大桥|大桥)", text))
    for m in DESIGN_LIFE_PATTERN.finditer(text):
        years = int(m.group(1))
        line_no = _line_no(text, m.start())
        if has_big_bridge and years < 100:
            findings.append(Finding(
                severity="high",
                category="设计标准",
                location=f"第 {line_no} 行",
                original=m.group(0),
                standard_ref="《公路工程结构可靠性设计统一标准》(JTG 2120-2020) 表 3.3.1",
                required="特大桥、大桥主体结构设计使用年限应为 100 年；中、小桥及涵洞为 50 年。本项目含特大桥、大桥，应取 100 年。",
                actual=f"文档声明主体结构设计使用年限为 {years} 年",
                suggestion="将主体结构设计使用年限由 50 年改为 100 年",
            ))
    return findings


# ---------- 规则 4：环境类别 ----------

ENV_CLASS_PATTERN = re.compile(r"环境类别\s*[:：]\s*([ⅠⅡⅢⅣⅤIVA-Za-z0-9\-]+)\s*级?")
VALID_ENV_CLASSES = {"Ⅰ", "Ⅱ", "Ⅲ", "Ⅳ", "Ⅴ", "I", "II", "III", "IV", "V"}


def check_environment_class(text: str) -> list[Finding]:
    findings = []
    for m in ENV_CLASS_PATTERN.finditer(text):
        val = m.group(1).replace(" ", "")
        line_no = _line_no(text, m.start())
        if val not in VALID_ENV_CLASSES:
            findings.append(Finding(
                severity="high",
                category="取值合规",
                location=f"第 {line_no} 行",
                original=m.group(0),
                standard_ref="《公路工程混凝土结构耐久性设计规范》(JTG/T 3310-2019) 表 3.0.6",
                required="公路桥涵的环境类别仅设 Ⅰ、Ⅱ、Ⅲ、Ⅳ、Ⅴ 五类（不含 A/B 等细分级）。GB/T 50476 的细分级别仅适用于房屋建筑混凝土结构。",
                actual=f"文档声明环境类别为「{val}」",
                suggestion=f"改为 Ⅰ ~ Ⅴ 中的对应类别（如本项目一般环境一般取 Ⅰ 类）",
            ))
    return findings


# ---------- 规则 5：主梁钢材牌号 ----------

MAIN_STEEL_PATTERN = re.compile(r"钢梁采用\s*(Q\d{3}[A-Z]?)\s*钢材")


def check_main_steel_grade(text: str) -> list[Finding]:
    findings = []
    for m in MAIN_STEEL_PATTERN.finditer(text):
        grade = m.group(1)
        line_no = _line_no(text, m.start())
        if grade.startswith("Q23"):
            findings.append(Finding(
                severity="high",
                category="材料牌号",
                location=f"第 {line_no} 行",
                original=m.group(0),
                standard_ref="《公路钢结构桥梁设计规范》(JTG D64-2015) 3.0.1；《桥梁用结构钢》(GB/T 714-2015)",
                required="桥梁主梁结构用钢材应选用 Q345、Q370、Q420、Q460 等低合金高强度结构钢，或 GB/T 714 桥梁专用钢（Q345q、Q370q 等）。Q235 系列因强度偏低，一般不用于桥梁主梁。",
                actual=f"文档声明钢梁采用 {grade}",
                suggestion="改为 Q355D（或 Q345q、Q370q 等桥梁结构常用钢种），与本项目其它钢混组合梁主梁的 Q355D 保持一致",
            ))
    return findings


# ---------- 规则 6：桥面板混凝土等级 ----------

DECK_CONCRETE_PATTERN = re.compile(r"桥面板[^\n。]{0,40}?C(\d{2,3})")


def check_deck_concrete(text: str) -> list[Finding]:
    findings = []
    for m in DECK_CONCRETE_PATTERN.finditer(text):
        grade = int(m.group(1))
        line_no = _line_no(text, m.start())
        if grade < 50:
            findings.append(Finding(
                severity="high",
                category="材料等级",
                location=f"第 {line_no} 行",
                original=m.group(0),
                standard_ref="《公路钢混组合桥梁设计与施工规范》(JTG/T D64-01-2015) 4.1.1；本项目其它处声明桥面板采用 C50",
                required="钢混组合梁桥面板混凝土设计强度等级应不低于 C40；本项目设计基础与其它处均明确为 C50 微膨胀混凝土。",
                actual=f"该处声明桥面板采用 C{grade}",
                suggestion="改为 C50 微膨胀混凝土，与本项目其它钢混组合梁桥面板一致",
            ))
    return findings


# ---------- 规则 7：防腐涂层保护年限 ----------

ANTICORROSIVE_LIFE_PATTERN = re.compile(r"防(?:护|腐)涂(?:层|装)?[^\n。]{0,30}?(?:保护年限|使用年限)[^\d]{0,8}(?:不低于|≥|不小于|大于)?\s*(\d+)\s*年")


def check_anticorrosive_life(text: str) -> list[Finding]:
    findings = []
    for m in ANTICORROSIVE_LIFE_PATTERN.finditer(text):
        years = int(m.group(1))
        line_no = _line_no(text, m.start())
        if years < 20:
            findings.append(Finding(
                severity="high",
                category="耐久性指标",
                location=f"第 {line_no} 行",
                original=m.group(0),
                standard_ref="《公路桥梁钢结构防腐涂装技术条件》(JT/T 722-2023) 6.1",
                required="公路桥梁钢结构外露部位防腐涂层保护年限一般不应低于 20 年（重防腐体系不低于 25 年）。",
                actual=f"文档要求防护涂层保护年限不低于 {years} 年",
                suggestion="将保护年限要求改为不低于 20 年",
            ))
    return findings


# ---------- 规则 8：T 梁支座选型 ----------

T_BEAM_BEARING_PATTERN = re.compile(r"(T\s*梁|矮\s*T\s*梁|叠合\s*T\s*梁)[、，及和\s　]*[^\n。]{0,40}采用\s*(盆式支座|球型支座|球面支座)")


def check_t_beam_bearing(text: str) -> list[Finding]:
    findings = []
    for m in T_BEAM_BEARING_PATTERN.finditer(text):
        beam, bearing = m.group(1), m.group(2)
        line_no = _line_no(text, m.start())
        findings.append(Finding(
            severity="high",
            category="构造选型",
            location=f"第 {line_no} 行",
            original=m.group(0),
            standard_ref="《公路桥涵设计通用规范》(JTG D60-2015) 8.2.2；《公路桥梁板式橡胶支座》(JT/T 4-2019)",
            required=f"{beam}等中小跨径简支梁桥应采用板式橡胶支座；盆式支座、球型支座主要适用于大跨径连续梁、变截面箱梁等结构。",
            actual=f"文档声明 {beam} 采用 {bearing}",
            suggestion=f"将 {beam} 的支座改为板式橡胶支座（如 GYZ、GJZ 系列）",
        ))
    return findings


# ---------- 规则 9：用词规范（大于 vs 不小于） ----------

WORDING_PATTERN = re.compile(r"(压实度|强度|抗压|抗拉|配合比)[^\n。]{0,30}(大于|超过)\s*(\d+(?:\.\d+)?)\s*([%MPa]*)")


def check_wording(text: str) -> list[Finding]:
    findings = []
    for m in WORDING_PATTERN.finditer(text):
        param, word, val, unit = m.groups()
        line_no = _line_no(text, m.start())
        findings.append(Finding(
            severity="low",
            category="用词规范",
            location=f"第 {line_no} 行",
            original=m.group(0),
            standard_ref="《工程建设标准编写规定》（住建部 2008）；GB/T 1.1-2020 标准化工作导则",
            required=f"参数限值的表述应区分「不小于（≥）」与「大于（>）」：等于该值时是否合格，二者结论不同。规范限值通常应表达为「不小于 X」或「≥ X」。",
            actual=f"文档表述为「{param}{word} {val}{unit}」",
            suggestion=f"改为「{param}不小于 {val}{unit}」或「{param}≥ {val}{unit}」",
        ))
    return findings


# ---------- 规则 10：数字粘连（表格丢失分隔） ----------

# 抓 12 位以上连续数字。低于这个位数大多是正常字段（年份、桩号、统计编号等），噪声太大。
NUMBER_BLOB_PATTERN = re.compile(r"(?<![\d.])(\d{12,})(?![\d.])")

# 这些上下文里出现长数字，是字段值而不是表格粘连，跳过。
_BLOB_SKIP_CONTEXT = re.compile(
    r"电话|手机|联系方式|联系人|联系电话|邮箱|邮编|身份证|"
    r"信用代码|统一代码|许可证|证号|账号|订单|发票|"
    r"传真|fax|tel|mobile|qq|微信",
    re.IGNORECASE,
)


def _is_phone(s: str) -> bool:
    """11 位且 1[3-9] 开头 = 中国手机号"""
    return len(s) == 11 and s[0] == "1" and s[1] in "3456789"


def _is_uscc_digits(s: str) -> bool:
    """18 位统一社会信用代码（纯数字版本，9 开头是组织机构）"""
    return len(s) == 18


def check_number_concatenation(text: str) -> list[Finding]:
    findings = []
    for m in NUMBER_BLOB_PATTERN.finditer(text):
        s = m.group(1)
        # 跳过手机号、统一信用代码这类合法长数字
        if _is_phone(s) or _is_uscc_digits(s):
            continue
        # 检查前后 ±30 字符的上下文里有没有"电话/信用代码/订单号"等字段名
        start, end = m.start(), m.end()
        ctx = text[max(0, start - 30): end + 30]
        if _BLOB_SKIP_CONTEXT.search(ctx):
            continue

        line_no = _line_no(text, start)
        findings.append(Finding(
            severity="medium",
            category="格式问题",
            location=f"第 {line_no} 行",
            original=s,
            required="表格中并列数值应保留分隔符（空格、分号或换行），不应粘连成单一字符串。",
            actual=f"原文出现 {len(s)} 位连续数字「{s}」",
            suggestion="检查原表格是否丢失分隔符；如「33500 32500 31500」被串成单串，应拆分还原",
        ))
    return findings


# ---------- 规则 11：温度梯度（典型值参照） ----------

# 这一条主要靠 LLM 处理（涉及计算与规范条款 5.6.3），规则层只做提示
TEMP_GRADIENT_PATTERN = re.compile(
    r"(?:(\d+(?:\.\d+)?)\s*cm\s*[^\n。]{0,10}铺装)?[^\n。]{0,40}?T\s*2\s*[=＝]\s*(\d+(?:\.\d+)?)\s*[℃°]",
    re.S,
)


def check_temperature_gradient(text: str) -> list[Finding]:
    """对照 JTG D60-2015 表 4.3.10-2 的典型值核查 T2"""
    findings = []
    # 表 4.3.10-2 典型值（℃）
    STD = {"5": 5.5, "10": 4.0, "无": 6.7}  # 无铺装基准值供参考
    for m in TEMP_GRADIENT_PATTERN.finditer(text):
        thickness, val_str = m.group(1), m.group(2)
        val = float(val_str)
        line_no = _line_no(text, m.start())

        # 找最近一处「Xcm ... 铺装」
        ctx = text[max(0, m.start()-100):m.end()+10]
        thickness_m = re.search(r"(\d+)\s*cm.{0,15}?铺装", ctx)
        thickness = thickness_m.group(1) if thickness_m else thickness

        if not thickness:
            continue
        expected = STD.get(thickness)
        if expected is None:
            continue
        if abs(val - expected) > 0.5:
            findings.append(Finding(
                severity="high",
                category="参数取值",
                location=f"第 {line_no} 行",
                original=m.group(0).strip(),
                standard_ref="《公路桥涵设计通用规范》(JTG D60-2015) 表 4.3.10-2 「混凝土箱梁竖向温度梯度基数」",
                required=f"{thickness}cm 沥青混凝土铺装下，竖向温度梯度 T2 规范取值为 {expected}℃",
                actual=f"文档取 T2 = {val}℃（铺装厚度 {thickness}cm）",
                suggestion=f"将 T2 改为 {expected}℃",
            ))
    return findings


# ---------- 规则 12：桩长自洽性 ----------

PILE_LENGTH_PATTERN = re.compile(r"最小有效桩长[^\d]{0,15}(\d+(?:\.\d+)?)\s*m\s*及\s*(\d+)\s*D")


def check_pile_length_consistency(text: str) -> list[Finding]:
    """文档自身多处声明的「最小有效桩长」应一致"""
    hits = []
    for m in PILE_LENGTH_PATTERN.finditer(text):
        hits.append({
            "line_no": _line_no(text, m.start()),
            "m_value": float(m.group(1)),
            "d_value": int(m.group(2)),
            "original": m.group(0),
        })
    if len(hits) < 2:
        return []
    # 找出值不一致的
    findings = []
    values = {(h["m_value"], h["d_value"]) for h in hits}
    if len(values) > 1:
        # 任意取两个不一致的进行报告
        first = hits[0]
        for other in hits[1:]:
            if (other["m_value"], other["d_value"]) != (first["m_value"], first["d_value"]):
                findings.append(Finding(
                    severity="high",
                    category="自洽性",
                    location=f"第 {first['line_no']}、{other['line_no']} 行",
                    original=f"L{first['line_no']}：{first['original']} ／ L{other['line_no']}：{other['original']}",
                    standard_ref="设计文件自洽性要求；《公路桥涵地基及基础设计规范》(JTG 3363-2019) 第 6.3.8 条",
                    required="同一项目的最小有效桩长要求在不同章节应保持一致",
                    actual=f"L{first['line_no']} 声明「不小于 {first['m_value']}m 及 {first['d_value']}D」；L{other['line_no']} 声明「不小于 {other['m_value']}m 及 {other['d_value']}D」",
                    suggestion="统一全线桩长要求；按本项目专家组意见执行情况，应为「不小于 10m 及 6D」",
                ))
                break
    return findings


# ---------- 入口：所有规则集合 ----------

ALL_RULES = [
    find_duplicate_lines,
    check_standard_references,
    check_design_life,
    check_environment_class,
    check_main_steel_grade,
    check_deck_concrete,
    check_anticorrosive_life,
    check_t_beam_bearing,
    check_wording,
    check_number_concatenation,
    check_temperature_gradient,
    check_pile_length_consistency,
]


def quick_scan(text: str) -> list[dict]:
    out: list[Finding] = []
    for fn in ALL_RULES:
        try:
            out += fn(text)
        except Exception:
            continue
    return [f.to_dict() for f in out]
