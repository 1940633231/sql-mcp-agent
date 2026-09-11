"""初始化业务库 sales_demo：建库、建表、造测试数据。

用法（在项目根目录运行）：
    python scripts/db_init.py            # 建库（若已存在则提示，不重建）
    python scripts/db_init.py --reset    # 强制重建（删库建库）

数据模型：公司 sales 场景
    - company       公司主表
    - sale_records  销售流水（按日期记录，可按年/季/月聚合）
"""
import argparse
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import pymysql

# 以脚本方式运行时，把项目根目录加入 sys.path，保证能 import mcp_server 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_server import config  # noqa: E402


def connect(database=""):
    conn = config.get_connection()
    conn["database"] = database
    return pymysql.connect(**conn, autocommit=True, cursorclass=pymysql.cursors.DictCursor)


def ensure_database(conn, reset: bool):
    name = config.TARGET_DATABASE
    with conn.cursor() as cur:
        cur.execute("CREATE DATABASE IF NOT EXISTS `%s` DEFAULT CHARACTER SET utf8mb4" % name)
    if reset:
        with conn.cursor() as cur:
            cur.execute("DROP DATABASE `%s`" % name)
            cur.execute("CREATE DATABASE `%s` DEFAULT CHARACTER SET utf8mb4" % name)
            print("[db_init] 已重建数据库 %s" % name)


SCHEMA = """
CREATE TABLE IF NOT EXISTS company (
    company_id   INT AUTO_INCREMENT PRIMARY KEY,
    name         VARCHAR(100) NOT NULL,
    industry     VARCHAR(50)  NOT NULL,
    headquarters VARCHAR(50)  NOT NULL,
    founded_year INT          NOT NULL,
    employees    INT          NOT NULL
);

CREATE TABLE IF NOT EXISTS sale_records (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    company_id  INT          NOT NULL,
    amount      DECIMAL(14,2) NOT NULL,
    record_date DATE         NOT NULL,
    CONSTRAINT fk_company FOREIGN KEY (company_id) REFERENCES company(company_id)
);

CREATE INDEX idx_sr_company_date ON sale_records (company_id, record_date);
"""


def create_tables(db):
    with db.cursor() as cur:
        for stmt in filter(None, [s.strip() for s in SCHEMA.split(";")]):
            try:
                cur.execute(stmt)
            except pymysql.err.OperationalError as exc:
                # MySQL 不支持 CREATE INDEX IF NOT EXISTS；重复建库时保留既有索引。
                if exc.args and exc.args[0] == 1061:
                    continue
                raise
    print("[db_init] 建表完成（company / sale_records）")


def load_mock_data(db, num_companies=40, start_year=2023, end_year=2026):
    industries = [
        "零售", "快消", "电子", "汽车", "医药", "金融", "能源", "互联网",
        "地产", "餐饮", "服装", "家居",
    ]
    cities = ["北京", "上海", "深圳", "广州", "杭州", "成都", "武汉", "西安", "南京", "苏州"]
    companies = []
    for i in range(1, num_companies + 1):
        companies.append((
            "示例%s第%02d公司" % (random.choice(["科技", "集团", "控股", "实业"]), i),
            random.choice(industries),
            random.choice(cities),
            random.randint(1995, 2020),
            random.randint(100, 20000),
        ))

    with db.cursor() as cur:
        for c in companies:
            cur.execute(
                "INSERT INTO company (name, industry, headquarters, founded_year, employees) "
                "VALUES (%s, %s, %s, %s, %s)", c,
            )

    # 销售流水：每家公司在 2023~2026 年间不定期产生多笔订单
    today = date(end_year, 12, 31)
    start = date(start_year, 1, 1)
    rows = []
    company_ids = list(range(1, num_companies + 1))
    for company_id in company_ids:
        n = random.randint(80, 200)  # 每家公司交易笔数
        # 每家公司给一个基础规模，让排名有区分度
        base = random.uniform(2_000, 300_000)
        for _ in range(n):
            d = start + timedelta(days=random.randint(0, (today - start).days))
            amount = round(random.gauss(base, base * 0.4) + random.uniform(5, 5000), 2)
            rows.append((company_id, max(amount, 1.0), d.isoformat()))

    with db.cursor() as cur:
        cur.executemany(
            "INSERT INTO sale_records (company_id, amount, record_date) VALUES (%s, %s, %s)",
            rows,
        )
    print(f"[db_init] 造数完成：{num_companies} 家公司，{len(rows)} 条销售流水")


PERMISSION_COMPANIES = [
    (9001, "权限测试电子甲", "电子", "北京", 2010, 1200),
    (9002, "权限测试电子乙", "电子", "上海", 2014, 2400),
    (9003, "权限测试医药甲", "医药", "北京", 2012, 3600),
    (9004, "权限测试医药乙", "医药", "上海", 2016, 4800),
    (9005, "权限测试汽车甲", "汽车", "深圳", 2011, 6000),
    (9006, "权限测试汽车乙", "汽车", "广州", 2018, 7200),
]


def load_permission_fixtures(db) -> None:
    """写入可重复的 V0.3 多主体权限测试数据。"""
    with db.cursor() as cur:
        for company_id, name, industry, city, founded, employees in PERMISSION_COMPANIES:
            cur.execute(
                "INSERT INTO company (company_id, name, industry, headquarters, founded_year, employees) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE name=VALUES(name), industry=VALUES(industry), "
                "headquarters=VALUES(headquarters), founded_year=VALUES(founded_year), "
                "employees=VALUES(employees)",
                (company_id, name, industry, city, founded, employees),
            )

        company_ids = [row[0] for row in PERMISSION_COMPANIES]
        placeholders = ",".join("%s" for _ in company_ids)
        cur.execute(
            "DELETE FROM sale_records WHERE company_id IN (%s)" % placeholders,
            company_ids,
        )
        rows = []
        for offset, company_id in enumerate(company_ids, start=1):
            rows.extend([
                (company_id, offset * 1000 + 100, "2026-01-15"),
                (company_id, offset * 1000 + 200, "2026-02-20"),
                (company_id, offset * 1000 + 300, "2026-03-25"),
            ])
        cur.executemany(
            "INSERT INTO sale_records (company_id, amount, record_date) VALUES (%s, %s, %s)",
            rows,
        )
    print(
        "[db_init] V0.3 权限测试数据完成：%d 家固定公司，%d 条固定流水"
        % (len(PERMISSION_COMPANIES), len(rows))
    )


def main():
    parser = argparse.ArgumentParser(description="初始化 sales_demo 测试库")
    parser.add_argument("--reset", action="store_true", help="强制重建数据库")
    parser.add_argument(
        "--permission-fixtures",
        action="store_true",
        help="写入可重复的 V0.3 权限测试数据",
    )
    args = parser.parse_args()

    if not args.reset and not args.permission_fixtures:
        ans = input("将创建数据库 sales_demo，若已存在会保留。继续? [y/N] ")
        if ans.strip().lower() != "y":
            print("已取消")
            sys.exit(0)

    conn = connect()
    try:
        ensure_database(conn, args.reset)
        db = connect(config.TARGET_DATABASE)
        try:
            create_tables(db)
            with db.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM company")
                has_data = cur.fetchone()["n"] > 0 and not args.reset
            if has_data:
                print("[db_init] 已存在数据，跳过随机造数。使用 --reset 可重建。")
            else:
                load_mock_data(db)
            if args.permission_fixtures:
                load_permission_fixtures(db)
        finally:
            db.close()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
