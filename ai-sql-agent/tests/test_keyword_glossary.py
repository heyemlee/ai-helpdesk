"""口语说法 → Schema 术语的映射，离线锁定。

留出集召回率只有 66.7%（合理线 85%），差距几乎全在语义鸿沟上：
「料快没了」原样保留后，BM25 匹配不上 safety_stock，向量侧走的是本地哈希嵌入
（没有语义能力），两条路都召不回来。

修法刻意选了「把业务元数据喂给抽词模型」而不是「往短语表里加口语词」：
后者只对补进去的那几条有效，而且等于把留出集变成第二个训练集。
"""

import unittest

from ai_sql_agent.pipeline.local_clients import HybridKeywordExtractor, build_schema_glossary
from ai_sql_agent.retrieval.keyword_extractor_client import AliyunKeywordExtractor

METAS = {
    "wms_db": {
        "material": {
            "description": "物料主数据表。",
            "columns": {
                "material_name": {
                    "description": "物料名称。",
                    "aliases": ["物料", "材料名称"],
                },
                "safety_stock": {
                    "description": "安全库存量，低于此值需补货。",
                    "aliases": ["最低库存", "补货点"],
                },
            },
        }
    }
}


class GlossaryTest(unittest.TestCase):
    def test_contains_column_names_and_business_meaning(self):
        glossary = build_schema_glossary(METAS)
        self.assertIn("wms_db.material", glossary)
        self.assertIn("safety_stock", glossary)
        self.assertIn("安全库存量", glossary)

    def test_aliases_already_in_description_are_not_repeated(self):
        """术语表越紧凑模型越容易读，重复词只是浪费预算。"""
        glossary = build_schema_glossary(METAS)
        self.assertEqual(glossary.count("安全库存量"), 1)

    def test_respects_char_budget(self):
        self.assertLessEqual(len(build_schema_glossary(METAS, max_chars=40)), 40)

    def test_empty_metadata_is_safe(self):
        self.assertEqual(build_schema_glossary({}), "")


class ExtractorParsingTest(unittest.TestCase):
    def test_both_original_and_mapped_terms_are_kept(self):
        """
        映射是增量不是替换：标准业务说法本来就能召回，
        不能为了口语说法把它们搞坏。
        """
        keywords = AliyunKeywordExtractor._parse_keywords(
            None, "解析结果：料，快没了\n映射术语：安全库存，现存量"
        )
        self.assertEqual(keywords, ["料", "快没了", "安全库存", "现存量"])

    def test_plain_output_without_mapping_still_parses(self):
        self.assertEqual(
            AliyunKeywordExtractor._parse_keywords(None, "解析结果：准时率，供应商名称"),
            ["准时率", "供应商名称"],
        )

    def test_duplicates_across_two_lines_are_merged(self):
        self.assertEqual(
            AliyunKeywordExtractor._parse_keywords(
                None, "解析结果：安全库存\n映射术语：安全库存，物料名称"
            ),
            ["安全库存", "物料名称"],
        )


class GlossaryPassThroughTest(unittest.TestCase):
    class FakeExtractor:
        def __init__(self):
            self.seen = None

        def extract(self, query, glossary=""):
            self.seen = glossary
            return ["kw"]

    def test_glossary_reaches_the_model_extractor(self):
        fake = self.FakeExtractor()
        extractor = HybridKeywordExtractor(model_extractor=fake, glossary="G")
        extractor.extract("哪些料快没了")
        self.assertEqual(fake.seen, "G")

    def test_rule_path_still_works_without_model(self):
        """离线降级路不依赖术语表，模型挂掉不能把整条查询带下水。"""
        extractor = HybridKeywordExtractor(glossary="G")
        extractor._model_failed = True
        self.assertTrue(extractor.extract("查询准时率低于0.9的供应商名称"))


if __name__ == "__main__":
    unittest.main()
