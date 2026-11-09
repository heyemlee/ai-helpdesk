import unittest

import numpy as np

from ai_sql_agent.retrieval.vector_index import VectorIndex


class VectorIndexTest(unittest.TestCase):
    def test_row_scores_and_tie_order(self):
        class Embeddings:
            def embed_texts(self, texts):
                if texts == ["query"]:
                    return np.array([[0.6, 0.8, 0]], dtype=np.float32)
                return np.array([[1, 0, 0], [0, 1, 0], [0, 1, 0], [0, 0, 0]], dtype=np.float32)

        index = VectorIndex(Embeddings())
        index.build(["a", "b", "c", "empty"])
        result = index.search("query", top_k=4)
        self.assertEqual([item[0] for item in result], [1, 2, 0, 3])
        np.testing.assert_allclose([item[1] for item in result], [0.8, 0.8, 0.6, 0])


if __name__ == "__main__":
    unittest.main()
