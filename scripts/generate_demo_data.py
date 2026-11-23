"""生成 AuditPilot V2 高仿真审计演示数据（Excel 财务流水 + 2 份采购合同）。"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

ROOT_DIR = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT_DIR / "sample_data"

CONTRACT_1_MD = """# 年度原材料框架采购合同

合同编号：CT-2026-001
签订日期：2026年01月08日
签订地点：上海市浦东新区

[第1页]

## 第1条 合同主体与账户信息
甲方（采购方）：星辰智造科技有限公司
甲方纳税人识别号：91310115MA1K88990X
甲方联系手机：13812345678

乙方（供应方）：华兴供应链管理有限公司
乙方纳税人识别号：91320594MA2233445B
乙方联系手机：13987654321
乙方指定收款开户行：中国工商银行苏州工业园区支行
乙方指定收款账号：6222 0210 0112 3456 789

## 第2条 采购标的与合同总金额
2.1 乙方向甲方供应工业级铝材与标准紧固件，合同有效期为2026年1月1日至2026年12月31日。
2.2 本合同含税总金额上限为人民币 1,200,000.00 元（大写：壹佰贰拾万元整）。未经双方书面签署补充协议，累计付款金额不得超过合同总金额上限。

[第2页]

## 第3条 付款方式与审批要求
3.1 甲方按批次验收合格后 30 个工作日内通过银行对公转账支付至乙方指定收款账号（6222 0210 0112 3456 789）。
3.2 单笔付款金额达到或超过人民币 200,000.00 元的，须经甲方财务总监及总经理双重书面审批，严禁拆分付款规避审批权限。

## 第4条 价格调整与违约责任（高风险条款）
4.1 若大宗商品市场价格单月波动超过 3%，乙方有权单方面上调未交付批次单价，且无需甲方书面同意，甲方不得因此解除合同。
4.2 甲方逾期付款超过 5 日的，每逾期一日须按合同总金额的千分之五（0.5%/日）向乙方支付违约金，且违约金无上限。
4.3 质量验收争议以乙方出厂质检报告为最终唯一依据，甲方放弃第三方复检权利。

[第3页]

## 第5条 保密与争议解决
5.1 双方对本合同涉及的交易价格、银行账户信息及财务数据负有严格保密义务。
5.2 争议提交乙方所在地人民法院诉讼解决。
"""

CONTRACT_2_MD = """# 智能产线精密设备采购合同

合同编号：CT-2026-002
签订日期：2026年02月15日
签订地点：北京市海淀区

[第1页]

## 第1条 合同主体与指定账户
甲方（采购方）：星辰智造科技有限公司
甲方纳税人识别号：91310115MA1K88990X
甲方项目联系人手机：13600112233

乙方（供应方）：中科精密设备有限公司
乙方纳税人识别号：91110108MA0099887C
乙方项目联系人手机：13766554433
乙方唯一指定收款开户行：中国农业银行北京海淀支行
乙方唯一指定收款账号：6228 4800 8877 6655 432

## 第2条 设备清单与合同价款
2.1 乙方向甲方提供高精度五轴检测仪 2 套及配套控制软件。
2.2 合同总价款为人民币 800,000.00 元（大写：捌拾万元整），分三期支付：预付款 30%（240,000.00 元）、到货验收款 40%（320,000.00 元）、质保尾款 30%（240,000.00 元）。

[第2页]

## 第3条 交付、质保与资金安全
3.1 甲方所有款项仅可汇入乙方第1条载明的唯一指定收款账号（6228 4800 8877 6655 432）。任何口头或非盖章书面变更收款账户的通知均属无效，甲方擅自向第三方或非指定账户付款不视为履行付款义务。
3.2 设备质保期仅为 3 个月（显著低于行业常规 12 个月标准），且软件升级维护需另行按年支付合同总价 25% 的服务费。

## 第4条 知识产权与排他性限制（高风险条款）
4.1 甲方在使用设备过程中产生的所有工艺参数与检测数据归乙方独占所有，甲方未经乙方书面授权不得导出或用于其他产线。
4.2 若甲方提前终止合同，已支付的全部款项不予退还，且甲方须额外赔偿合同总价的 50% 作为惩罚性违约金。
"""

NORMAL_VENDORS = [
    ("安捷办公用品有限公司", "6217001210004567111", "91310104MA1100111A", "办公耗材", "王财务"),
    ("云图云计算服务有限公司", "6214830112349876222", "91440300MA5511222B", "云服务费", "李总监"),
    ("瑞峰电力设备有限公司", "6226090211223344333", "91320100MA3322333C", "设备维护", "王财务"),
    ("顺通快递物流有限公司", "6225880123456789444", "91310112MA4433444D", "运费杂费", "周明"),
    ("智汇人力资源有限公司", "6222081001112222555", "91110105MA6644555E", "咨询培训", "李总监"),
    ("蓝海包装材料有限公司", "6228480033445566666", "91330100MA7755666F", "包材采购", "王财务"),
]

REMARK_POOL = {
    "办公耗材": ["月度办公耗材采购", "打印耗材及文具结算", "常规行政物资采购"],
    "云服务费": ["云服务器月度账单", "对象存储与带宽续费", "企业邮箱及云安全费用"],
    "设备维护": ["产线常规保养费", "传感器备件更换", "年度设备校准服务费"],
    "运费杂费": ["常规样品寄送运费", "月结物流运输费", "配件调拨运费"],
    "咨询培训": ["财务合规内训服务费", "研发团队技术培训费", "法务常年顾问费"],
    "包材采购": ["成品防震纸箱采购", "周转托盘及标签采购", "常规包装材料结算"],
}


def generate_ledger_rows() -> list[dict]:
    rng = random.Random(20261123)
    rows: list[dict] = []

    # 1. 生成 58 条正常工作日付款流水
    start_dt = date(2026, 1, 5)
    workdays: list[date] = []
    for offset in range(175):
        d = start_dt + timedelta(days=offset)
        if d.weekday() < 5 and d not in {date(2026, 1, 1), date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)}:
            workdays.append(d)

    for idx in range(1, 59):
        d = rng.choice(workdays)
        vendor, acct, tax_id, cat, approver = rng.choice(NORMAL_VENDORS)
        hour = rng.randint(9, 17)
        minute = rng.randint(0, 59)
        second = rng.randint(0, 59)
        amount = round(rng.uniform(4500, 92000), 2)
        rows.append(
            {
                "voucher_no": f"PZ-{d.strftime('%Y%m%d')}-{idx:03d}",
                "payment_date": d.isoformat(),
                "payment_time": f"{hour:02d}:{minute:02d}:{second:02d}",
                "payee_name": vendor,
                "bank_account": acct,
                "tax_id": tax_id,
                "amount": amount,
                "category": cat,
                "contract_no": "",
                "approver": approver,
                "remark": rng.choice(REMARK_POOL[cat]),
            }
        )

    # 2. 精准埋入 5 类审计异常与合同关联流水
    planted = [
        # --- 异常 1：CT-2026-001 华兴供应链超合同总额付款（合同上限 120 万，实付 158 万，超付 38 万）---
        # 注意：流水账号不带空格，合同账号带空格，用于验证实体归一化零误报
        {
            "voucher_no": "PZ-20260120-901",
            "payment_date": "2026-01-20",
            "payment_time": "10:15:22",
            "payee_name": "华兴供应链管理有限公司",
            "bank_account": "6222021001123456789",
            "tax_id": "91320594MA2233445B",
            "amount": 400000.00,
            "category": "原材料采购",
            "contract_no": "CT-2026-001",
            "approver": "总经理-陈总",
            "remark": "CT-2026-001 第一批铝材采购款",
        },
        {
            "voucher_no": "PZ-20260305-902",
            "payment_date": "2026-03-05",
            "payment_time": "14:28:11",
            "payee_name": "华兴供应链管理有限公司",
            "bank_account": "6222021001123456789",
            "tax_id": "91320594MA2233445B",
            "amount": 400000.00,
            "category": "原材料采购",
            "contract_no": "CT-2026-001",
            "approver": "总经理-陈总",
            "remark": "CT-2026-001 第二批铝材采购款",
        },
        {
            "voucher_no": "PZ-20260422-903",
            "payment_date": "2026-04-22",
            "payment_time": "11:05:45",
            "payee_name": "华兴供应链管理有限公司",
            "bank_account": "6222021001123456789",
            "tax_id": "91320594MA2233445B",
            "amount": 400000.00,
            "category": "原材料采购",
            "contract_no": "CT-2026-001",
            "approver": "总经理-陈总",
            "remark": "CT-2026-001 第三批紧固件采购款",
        },
        {
            "voucher_no": "PZ-20260608-904",
            "payment_date": "2026-06-08",
            "payment_time": "16:40:09",
            "payee_name": "华兴供应链管理有限公司",
            "bank_account": "6222021001123456789",
            "tax_id": "91320594MA2233445B",
            "amount": 380000.00,
            "category": "原材料采购",
            "contract_no": "CT-2026-001",
            "approver": "财务总监-赵总",
            "remark": "CT-2026-001 追加批次原材料货款（无补充协议）",
        },
        # --- 异常 2：CT-2026-002 中科精密设备，1 笔打入非合同约定账号 ---
        {
            "voucher_no": "PZ-20260224-905",
            "payment_date": "2026-02-24",
            "payment_time": "09:50:00",
            "payee_name": "中科精密设备有限公司",
            "bank_account": "6228480088776655432",
            "tax_id": "91110108MA0099887C",
            "amount": 240000.00,
            "category": "设备采购",
            "contract_no": "CT-2026-002",
            "approver": "总经理-陈总",
            "remark": "CT-2026-002 预付款 30%",
        },
        {
            "voucher_no": "PZ-20260416-906",
            "payment_date": "2026-04-16",
            "payment_time": "15:12:30",
            "payee_name": "中科精密设备有限公司",
            "bank_account": "6228480088776655432",
            "tax_id": "91110108MA0099887C",
            "amount": 320000.00,
            "category": "设备采购",
            "contract_no": "CT-2026-002",
            "approver": "总经理-陈总",
            "remark": "CT-2026-002 到货验收款 40%",
        },
        {
            "voucher_no": "PZ-20260528-907",
            "payment_date": "2026-05-28",
            "payment_time": "17:20:18",
            "payee_name": "中科精密设备有限公司",
            "bank_account": "6225880199998888777",
            "tax_id": "91110108MA0099887C",
            "amount": 220000.00,
            "category": "设备采购",
            "contract_no": "CT-2026-002",
            "approver": "部门经理-周明",
            "remark": "CT-2026-002 尾款结算（口头通知变更收款账户）",
        },
        # --- 异常 3：盛达物流同日拆分 3 笔规避 20 万审批阈值（合计 545,000）---
        {
            "voucher_no": "PZ-20260318-908",
            "payment_date": "2026-03-18",
            "payment_time": "10:08:15",
            "payee_name": "盛达物流有限公司",
            "bank_account": "6217008800112233445",
            "tax_id": "91310115MA8899001G",
            "amount": 185000.00,
            "category": "运费杂费",
            "contract_no": "",
            "approver": "部门经理-周明",
            "remark": "Q1大宗货物运输费-批次A",
        },
        {
            "voucher_no": "PZ-20260318-909",
            "payment_date": "2026-03-18",
            "payment_time": "10:19:42",
            "payee_name": "盛达物流有限公司",
            "bank_account": "6217008800112233445",
            "tax_id": "91310115MA8899001G",
            "amount": 182000.00,
            "category": "运费杂费",
            "contract_no": "",
            "approver": "部门经理-周明",
            "remark": "Q1大宗货物运输费-批次B",
        },
        {
            "voucher_no": "PZ-20260318-910",
            "payment_date": "2026-03-18",
            "payment_time": "10:31:05",
            "payee_name": "盛达物流有限公司",
            "bank_account": "6217008800112233445",
            "tax_id": "91310115MA8899001G",
            "amount": 178000.00,
            "category": "运费杂费",
            "contract_no": "",
            "approver": "部门经理-周明",
            "remark": "Q1大宗货物运输费-批次C",
        },
        # --- 异常 4：恒信模具加工厂连续两日重复付款 86,500.00 ---
        {
            "voucher_no": "PZ-20260512-911",
            "payment_date": "2026-05-12",
            "payment_time": "14:05:33",
            "payee_name": "恒信模具加工厂",
            "bank_account": "6222023300445566778",
            "tax_id": "91320200MA9911223H",
            "amount": 86500.00,
            "category": "模具加工",
            "contract_no": "",
            "approver": "王财务",
            "remark": "精密模具加工尾款",
        },
        {
            "voucher_no": "PZ-20260513-912",
            "payment_date": "2026-05-13",
            "payment_time": "14:18:09",
            "payee_name": "恒信模具加工厂",
            "bank_account": "6222023300445566778",
            "tax_id": "91320200MA9911223H",
            "amount": 86500.00,
            "category": "模具加工",
            "contract_no": "",
            "approver": "王财务",
            "remark": "精密模具加工尾款",
        },
        # --- 异常 5：非工作时间 / 节假日 / 周末深夜大额付款 ---
        {
            "voucher_no": "PZ-20260501-913",
            "payment_date": "2026-05-01",
            "payment_time": "23:42:10",
            "payee_name": "远景咨询服务有限公司",
            "bank_account": "6226098877665544321",
            "tax_id": "91110101MA7766554J",
            "amount": 168000.00,
            "category": "咨询费",
            "contract_no": "",
            "approver": "部门经理-周明",
            "remark": "紧急市场调研加急咨询费",
        },
        {
            "voucher_no": "PZ-20260614-914",
            "payment_date": "2026-06-14",
            "payment_time": "02:15:30",
            "payee_name": "远景咨询服务有限公司",
            "bank_account": "6226098877665544321",
            "tax_id": "91110101MA7766554J",
            "amount": 195000.00,
            "category": "咨询费",
            "contract_no": "",
            "approver": "部门经理-周明",
            "remark": "海外渠道专项咨询服务费",
        },
    ]

    rows.extend(planted)
    rows.sort(key=lambda r: (r["payment_date"], r["payment_time"], r["voucher_no"]))
    return rows


def write_excel(rows: list[dict], output_path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "财务明细流水"

    headers = [
        "voucher_no",
        "payment_date",
        "payment_time",
        "payee_name",
        "bank_account",
        "tax_id",
        "amount",
        "category",
        "contract_no",
        "approver",
        "remark",
    ]
    ws.append(headers)

    header_fill = PatternFill(start_color="1E3A5F", end_color="1E3A5F", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")

    for row in rows:
        ws.append([row[h] for h in headers])

    wb.save(output_path)


def main() -> None:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    rows = generate_ledger_rows()
    excel_path = SAMPLE_DIR / "财务明细流水.xlsx"
    write_excel(rows, excel_path)

    c1_path = SAMPLE_DIR / "CT-2026-001_华兴供应链采购合同.md"
    c2_path = SAMPLE_DIR / "CT-2026-002_中科精密设备采购合同.md"
    c1_path.write_text(CONTRACT_1_MD.strip() + "\n", encoding="utf-8")
    c2_path.write_text(CONTRACT_2_MD.strip() + "\n", encoding="utf-8")

    # 保存合同结构化摘要元数据，供交叉核对与评测基线使用
    meta = {
        "total_rows": len(rows),
        "planted_anomalies": {
            "overpayment": {"contract_no": "CT-2026-001", "payee": "华兴供应链管理有限公司", "contract_limit": 1200000.0, "actual_paid": 1580000.0, "excess": 380000.0},
            "unauthorized_account": {"contract_no": "CT-2026-002", "voucher_no": "PZ-20260528-907", "expected_account": "6228480088776655432", "actual_account": "6225880199998888777", "amount": 220000.0},
            "split_payment": {"payee": "盛达物流有限公司", "date": "2026-03-18", "count": 3, "total_amount": 545000.0},
            "duplicate_payment": {"payee": "恒信模具加工厂", "vouchers": ["PZ-20260512-911", "PZ-20260513-912"], "amount": 86500.0},
            "off_hours_payment": {"payee": "远景咨询服务有限公司", "vouchers": ["PZ-20260501-913", "PZ-20260614-914"], "total_amount": 363000.0},
        },
    }
    (SAMPLE_DIR / "ground_truth.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Generated {len(rows)} ledger rows -> {excel_path}")
    print(f"Generated contracts -> {c1_path.name}, {c2_path.name}")


if __name__ == "__main__":
    main()
