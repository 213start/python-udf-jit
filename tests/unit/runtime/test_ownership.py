from __future__ import annotations

import unittest

from python_udf_jit.runtime.ownership import (
    AtomicOutputPublication,
    PublicationError,
    PublicationRejectCode,
)


class OwnershipTest(unittest.TestCase):
    def test_output_publication_is_exactly_once_and_abort_is_terminal(self):
        publication = AtomicOutputPublication(int)
        publication.stage(7)
        self.assertEqual(publication.publish(), 7)
        with self.assertRaises(PublicationError) as raised:
            publication.publish()
        self.assertEqual(
            raised.exception.code,
            PublicationRejectCode.ALREADY_PUBLISHED,
        )

        aborted = AtomicOutputPublication(int)
        aborted.stage(9)
        aborted.abort()
        with self.assertRaises(PublicationError) as raised:
            aborted.publish()
        self.assertEqual(
            raised.exception.code,
            PublicationRejectCode.ABORTED,
        )


if __name__ == "__main__":
    unittest.main()
