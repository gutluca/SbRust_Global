"""Regression checks: ordinary editing is allowed and changed assets are caught."""
from pathlib import Path
import tempfile
import unittest
from checksum_inventory import update_inventory,verify_inventory


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo=Path(self.temp.name)
        self.asset=self.repo/'data/spatial/example.csv'
        self.asset.parent.mkdir(parents=True)
        self.asset.write_bytes(b'cell,area\n1,100\n')
        update_inventory(self.repo)

    def test_readme_and_documentation_can_be_edited(self):
        (self.repo/'README.md').write_text('User-authored README\n')
        docs=self.repo/'docs';docs.mkdir()
        (docs/'methods.md').write_text('Revised wording\n')
        verify_inventory(self.repo)
        (self.repo/'README.md').write_text('Another normal edit\n')
        verify_inventory(self.repo)

    def test_asset_change_is_detected(self):
        self.asset.write_bytes(b'cell,area\n1,999\n')
        with self.assertRaisesRegex(AssertionError,'example.csv'):
            verify_inventory(self.repo)

    def test_model_binary_change_is_detected(self):
        binary=self.repo/'model/bin/dscsm048'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'original model')
        update_inventory(self.repo)
        binary.write_bytes(b'changed model')
        with self.assertRaisesRegex(AssertionError,'dscsm048'):
            verify_inventory(self.repo)

    def test_missing_or_unregistered_assets_are_detected(self):
        self.asset.unlink()
        with self.assertRaisesRegex(AssertionError,'missing='):
            verify_inventory(self.repo)
        self.asset.write_bytes(b'cell,area\n1,100\n')
        (self.asset.parent/'unexpected.csv').write_bytes(b'new data')
        with self.assertRaisesRegex(AssertionError,'unregistered='):
            verify_inventory(self.repo)

    def test_downloads_and_code_do_not_change_frozen_inventory(self):
        raw=self.repo/'data/raw/countries';raw.mkdir(parents=True)
        (raw/'download.csv.gz').write_bytes(b'downloaded archive')
        workflow=self.repo/'workflow';workflow.mkdir()
        (workflow/'analysis.py').write_text('pass\n')
        verify_inventory(self.repo)


if __name__=='__main__':unittest.main()
