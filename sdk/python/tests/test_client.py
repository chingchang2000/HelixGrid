import unittest

from helixgrid import HelixClient


class HelixClientValidationTests(unittest.TestCase):
    def test_rejects_invalid_base_url(self) -> None:
        with self.assertRaises(ValueError):
            HelixClient("localhost:8080")

    def test_rejects_nonpositive_timeout(self) -> None:
        with self.assertRaises(ValueError):
            HelixClient(timeout=0)

    def test_wait_rejects_invalid_intervals_before_network_use(self) -> None:
        client = HelixClient()
        with self.assertRaisesRegex(ValueError, "poll_interval"):
            client.wait("wf", poll_interval=0)
        with self.assertRaisesRegex(ValueError, "timeout"):
            client.wait("wf", timeout=-1)


if __name__ == "__main__":
    unittest.main()
