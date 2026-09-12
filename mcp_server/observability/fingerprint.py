"""生成规范化 SQL 指纹，并对字面量做统一替换。"""
import hashlib

from sqlglot import exp, parse_one

from ..security import parser


def raw_sql_hash(sql: str) -> str:
    return hashlib.sha256((sql or "").encode("utf-8")).hexdigest()


def canonical_sql(sql: str) -> str:
    try:
        tree = parse_one(sql or "", read=parser.DIALECT)
    except Exception:
        return (sql or "").strip()
    for literal in list(tree.find_all(exp.Literal)):
        literal.replace(exp.Var(this="?"))
    for boolean in list(tree.find_all(exp.Boolean)):
        boolean.replace(exp.Var(this="?"))
    for null in list(tree.find_all(exp.Null)):
        null.replace(exp.Var(this="?"))
    return tree.sql(dialect=parser.DIALECT)


def sql_fingerprint(sql: str) -> str:
    canonical = canonical_sql(sql)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return "qfp_%s" % digest
