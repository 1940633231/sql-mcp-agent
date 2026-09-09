"""初始化业务库 sales_demo：建库、建表、造测试数据。

用法：
    python db_init.py            # 建库（若已存在则提示，不重建）
    python db_init.py --reset    # 强制重建（删库建库）

数据模型：公司 sales 场景
    - company       公司主表
    - sale_records  销售流水（按日期记录，可按年/季/月聚合）
"""
import argparse
import random
import sys
from datetime import date, timedelta

import pymysql

import config


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
            cur.execute(stmt)
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


def main():
    parser = argparse.ArgumentParser(description="初始化 sales_demo 测试库")
    parser.add_argument("--reset", action="store_true", help="强制重建数据库")
    args = parser.parse_args()

    if not args.reset:
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
            # 未重置时若已有数据则跳过造数，避免重复
            with db.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM company")
                if cur.fetchone()["n"] > 0 and not args.reset:
                    print("[db_init] 已存在数据，跳过造数。使用 --reset 可重建。")
                    return
            load_mock_data(db)
        finally:
            db.close()
    finally:
        conn.close()


if __name__ == "__main__":
    main()