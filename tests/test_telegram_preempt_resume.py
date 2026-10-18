import asyncio
import os
import tempfile
import threading
import time
import unittest

from blueprints.telegram_channels import scraper

CHUNK = scraper.TELEGRAM_RESUME_CHUNK


class TelegramPreemptionTest(unittest.TestCase):
    def test_higher_priority_waiting_download_interrupts_the_active_one(self):
        queue = scraper._TelegramQueue()
        queue.update(1, priority=0, paused=False)
        queue.acquire(1)  # 1 télécharge
        waiter = threading.Thread(target=lambda: (queue.acquire(2), queue.release()))
        queue.update(2, priority=0, paused=False)
        waiter.start()
        time.sleep(0.05)
        queue.check_interrupt(1)  # même priorité: pas d'interruption
        queue.update(2, priority=5)  # « télécharger en premier »
        with self.assertRaises(scraper.TelegramDownloadPreempted):
            queue.check_interrupt(1)
        queue.check_interrupt(1)  # signal consommé une seule fois
        queue.release()
        waiter.join(timeout=2)
        self.assertFalse(waiter.is_alive())

    def test_pause_interrupts_the_active_download(self):
        queue = scraper._TelegramQueue()
        queue.update(1, priority=0, paused=False)
        queue.acquire(1)
        queue.update(1, paused=True)
        with self.assertRaises(scraper.TelegramDownloadPaused):
            queue.check_interrupt(1)
        queue.release()


class FakeClient:
    def __init__(self, data):
        self.data = data
        self.offsets = []

    async def iter_download(self, media, offset=0, request_size=CHUNK, file_size=None):
        self.offsets.append(offset)
        for start in range(offset, len(self.data), request_size):
            yield self.data[start:start + request_size]


class FakeMessage:
    document = object()
    media = None


class ResumableDownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'album.cbz.part')
        self.data = os.urandom(3 * CHUNK + 1234)

    def tearDown(self):
        self.tmp.cleanup()

    def _download(self, client, progress=None):
        asyncio.run(scraper._download_resumable(client, FakeMessage(), self.path, len(self.data), progress))

    def test_resumes_from_last_full_chunk_of_the_part_file(self):
        with open(self.path, 'wb') as handle:
            handle.write(self.data[:CHUNK + 100])  # coupé au milieu du 2e morceau
        client = FakeClient(self.data)
        self._download(client)
        self.assertEqual(client.offsets, [CHUNK])
        with open(self.path, 'rb') as handle:
            self.assertEqual(handle.read(), self.data)

    def test_interruption_keeps_the_part_file(self):
        client = FakeClient(self.data)
        calls = []

        def progress(received, total):
            calls.append(received)
            if received >= 2 * CHUNK:
                raise scraper.TelegramDownloadPreempted()

        with self.assertRaises(scraper.TelegramDownloadPreempted):
            self._download(client, progress)
        self.assertEqual(os.path.getsize(self.path), 2 * CHUNK)
        self._download(FakeClient(self.data))
        with open(self.path, 'rb') as handle:
            self.assertEqual(handle.read(), self.data)


if __name__ == '__main__':
    unittest.main()
